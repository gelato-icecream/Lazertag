import json
import logging
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import engine, Base, get_db
from models import User, Friendship, ChatMessage, UserStats
from auth import get_password_hash, verify_password, create_access_token, get_current_user
from ws_manager import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI(title="CYBER-TAG 3D Backend", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized successfully.")

# --- Pydantic Schemas ---
class UserAuthSchema(BaseModel):
    username: str
    password: str

class TokenSchema(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str

class FriendAddSchema(BaseModel):
    friend_username: str

class ChatSendSchema(BaseModel):
    content: str
    room_id: Optional[str] = "global"

# --- REST Endpoints ---
@app.get("/")
async def root():
    return {"status": "ok", "app": "CYBER-TAG 3D Server", "version": "1.0.0"}

@app.post("/api/auth/register", response_model=TokenSchema)
async def register(data: UserAuthSchema, db: AsyncSession = Depends(get_db)):
    username = data.username.strip()
    if len(username) < 3 or len(username) > 20:
        raise HTTPException(status_code=400, detail="Username must be between 3 and 20 characters.")
    
    result = await db.execute(select(User).filter(User.username == username))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Username is already taken.")
    
    hashed_pwd = get_password_hash(data.password)
    new_user = User(username=username, password_hash=hashed_pwd, is_online=True)
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    # Initialize User Stats
    new_stats = UserStats(user_id=new_user.id, total_score=0, total_tags=0, games_played=0)
    db.add(new_stats)
    await db.commit()

    token = create_access_token({"sub": username})
    return {"access_token": token, "token_type": "bearer", "username": username}

@app.post("/api/auth/login", response_model=TokenSchema)
async def login(data: UserAuthSchema, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).filter(User.username == data.username.strip()))
    user = result.scalars().first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    
    user.is_online = True
    await db.commit()

    token = create_access_token({"sub": user.username})
    return {"access_token": token, "token_type": "bearer", "username": user.username}

@app.get("/api/auth/me")
async def get_me(user: Optional[User] = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    
    result = await db.execute(select(UserStats).filter(UserStats.user_id == user.id))
    stats = result.scalars().first()
    
    return {
        "id": user.id,
        "username": user.username,
        "stats": {
            "score": stats.total_score if stats else 0,
            "tags": stats.total_tags if stats else 0,
            "games": stats.games_played if stats else 0
        }
    }

@app.get("/api/rooms")
async def list_rooms():
    return {"rooms": manager.get_active_rooms_summary()}

@app.get("/api/friends")
async def list_friends(user: Optional[User] = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not user:
        return {"friends": []}
    
    result = await db.execute(select(Friendship).filter(Friendship.user_id == user.id))
    friendships = result.scalars().all()
    
    friends_data = []
    for f in friendships:
        f_user_res = await db.execute(select(User).filter(User.id == f.friend_id))
        f_user = f_user_res.scalars().first()
        if f_user:
            friends_data.append({
                "id": f_user.id,
                "username": f_user.username,
                "is_online": f_user.is_online,
                "status": f.status
            })
    return {"friends": friends_data}

@app.post("/api/friends/add")
async def add_friend(data: FriendAddSchema, user: Optional[User] = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    
    if data.friend_username.strip() == user.username:
        raise HTTPException(status_code=400, detail="You cannot add yourself as a friend.")
    
    result = await db.execute(select(User).filter(User.username == data.friend_username.strip()))
    target_user = result.scalars().first()
    if not target_user:
        raise HTTPException(status_code=4404, detail="User not found.")
    
    # Check existing friendship
    existing = await db.execute(
        select(Friendship).filter(Friendship.user_id == user.id, Friendship.friend_id == target_user.id)
    )
    if existing.scalars().first():
        return {"message": "Friendship already exists."}
    
    f1 = Friendship(user_id=user.id, friend_id=target_user.id, status="accepted")
    f2 = Friendship(user_id=target_user.id, friend_id=user.id, status="accepted")
    db.add_all([f1, f2])
    await db.commit()
    return {"message": f"Added {target_user.username} as friend!"}

@app.get("/api/chat/history")
async def chat_history(room_id: str = "global", db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ChatMessage).filter(ChatMessage.room_id == room_id).order_by(ChatMessage.timestamp.desc()).limit(30)
    )
    messages = result.scalars().all()
    messages.reverse()
    return {
        "messages": [
            {"username": m.username, "content": m.content, "timestamp": m.timestamp.isoformat()}
            for m in messages
        ]
    }

# --- WebSocket Endpoints ---
@app.websocket("/ws/global")
async def websocket_global(websocket: WebSocket):
    await manager.connect_global(websocket)
    try:
        while True:
            data_text = await websocket.receive_text()
            data = json.loads(data_text)
            
            # Global Chat routing
            if data.get("type") == "chat":
                username = data.get("username", "Guest")
                content = data.get("content", "").strip()
                if content:
                    await manager.broadcast_global({
                        "type": "chat",
                        "username": username,
                        "content": content,
                        "channel": "global"
                    })
    except WebSocketDisconnect:
        manager.disconnect_global(websocket)
    except Exception as e:
        logger.error(f"Global WS Error: {e}")
        manager.disconnect_global(websocket)

@app.websocket("/ws/room/{room_id}/{player_id}/{username}/{selected_map}")
async def websocket_room(
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    username: str,
    selected_map: str,
    mode: str = "2team",
    team_id: int = 0,
    colors: str = "#00f0ff,#ff00aa",
    capacity: int = 4
):
    team_colors = [c.strip() for c in colors.split(",") if c.strip()]
    await manager.join_room(room_id, player_id, username, selected_map, mode, team_id, team_colors, capacity, websocket)
    
    room_meta = manager.active_rooms.get(room_id, {}).get("meta", {})
    existing_players = manager.get_room_players(room_id)
    await websocket.send_text(json.dumps({
        "type": "room_state",
        "map": selected_map,
        "meta": room_meta,
        "players": existing_players
    }))

    try:
        while True:
            data_text = await websocket.receive_text()
            data = json.loads(data_text)
            msg_type = data.get("type")

            # Position / State Update (30Hz low latency broadcast)
            if msg_type == "pos":
                data["playerId"] = player_id
                data["username"] = username
                await manager.broadcast_to_room(room_id, data, exclude_player_id=player_id)
            
            # Laser Shoot Event
            elif msg_type == "shoot":
                data["playerId"] = player_id
                await manager.broadcast_to_room(room_id, data, exclude_player_id=player_id)
            
            # Hit Marker / Tag Event
            elif msg_type == "hit":
                await manager.broadcast_to_room(room_id, data, exclude_player_id=None)
            
            # In-Game Room Chat Message
            elif msg_type == "chat":
                content = data.get("content", "").strip()
                if content:
                    await manager.broadcast_to_room(room_id, {
                        "type": "chat",
                        "username": username,
                        "content": content,
                        "channel": "room"
                    }, exclude_player_id=None)

    except WebSocketDisconnect:
        manager.leave_room(room_id, player_id)
        await manager.broadcast_to_room(room_id, {
            "type": "player_left",
            "playerId": player_id,
            "username": username
        })
    except Exception as e:
        logger.error(f"Room WS Error: {e}")
        manager.leave_room(room_id, player_id)
        await manager.broadcast_to_room(room_id, {
            "type": "player_left",
            "playerId": player_id,
            "username": username
        })
