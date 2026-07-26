import json
import logging
from typing import Dict, List
from fastapi import WebSocket

logger = logging.getLogger("ws_manager")

class ConnectionManager:
    def __init__(self):
        # active_rooms: { room_id: { player_id: { "ws": WebSocket, "username": str, "map": str } } }
        self.active_rooms: Dict[str, Dict[str, dict]] = {}
        # global_chats: List of connected global WebSockets
        self.global_sockets: List[WebSocket] = []

    async def connect_global(self, websocket: WebSocket):
        await websocket.accept()
        self.global_sockets.append(websocket)

    def disconnect_global(self, websocket: WebSocket):
        if websocket in self.global_sockets:
            self.global_sockets.remove(websocket)

    async def broadcast_global(self, message: dict):
        dead_sockets = []
        payload = json.dumps(message)
        for ws in self.global_sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead_sockets.append(ws)
        for ws in dead_sockets:
            self.disconnect_global(ws)

    async def join_room(self, room_id: str, player_id: str, username: str, selected_map: str, websocket: WebSocket):
        await websocket.accept()
        if room_id not in self.active_rooms:
            self.active_rooms[room_id] = {}
        
        self.active_rooms[room_id][player_id] = {
            "ws": websocket,
            "username": username,
            "map": selected_map
        }

        # Notify room members about new player
        await self.broadcast_to_room(room_id, {
            "type": "player_joined",
            "playerId": player_id,
            "username": username,
            "map": selected_map,
            "playerCount": len(self.active_rooms[room_id])
        }, exclude_player_id=None)

    def leave_room(self, room_id: str, player_id: str):
        if room_id in self.active_rooms and player_id in self.active_rooms[room_id]:
            del self.active_rooms[room_id][player_id]
            if not self.active_rooms[room_id]:
                del self.active_rooms[room_id]

    async def broadcast_to_room(self, room_id: str, message: dict, exclude_player_id: str = None):
        if room_id not in self.active_rooms:
            return
        
        dead_players = []
        payload = json.dumps(message)
        for p_id, p_info in self.active_rooms[room_id].items():
            if exclude_player_id and p_id == exclude_player_id:
                continue
            try:
                await p_info["ws"].send_text(payload)
            except Exception:
                dead_players.append(p_id)

        for p_id in dead_players:
            self.leave_room(room_id, p_id)

    def get_room_players(self, room_id: str) -> List[dict]:
        if room_id not in self.active_rooms:
            return []
        return [
            {"playerId": pid, "username": info["username"]}
            for pid, info in self.active_rooms[room_id].items()
        ]

    def get_active_rooms_summary(self) -> List[dict]:
        summary = []
        for rid, players in self.active_rooms.items():
            if players:
                first_player = next(iter(players.values()))
                summary.append({
                    "roomId": rid,
                    "map": first_player.get("map", "fortress"),
                    "host": first_player.get("username", "Unknown"),
                    "playerCount": len(players)
                })
        return summary

manager = ConnectionManager()
