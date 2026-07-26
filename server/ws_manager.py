import json
import logging
from typing import Dict, List, Optional
from fastapi import WebSocket

logger = logging.getLogger("ws_manager")

class ConnectionManager:
    def __init__(self):
        # active_rooms: { room_id: { "meta": dict, "players": { player_id: dict } } }
        self.active_rooms: Dict[str, dict] = {}
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

    async def join_room(
        self,
        room_id: str,
        player_id: str,
        username: str,
        selected_map: str,
        mode: str,
        team_id: int,
        team_colors: List[str],
        team_capacity: int,
        websocket: WebSocket
    ):
        await websocket.accept()
        
        if room_id not in self.active_rooms:
            self.active_rooms[room_id] = {
                "meta": {
                    "mode": mode or "2team",
                    "map": selected_map or "fortress",
                    "teamColors": team_colors or ["#00f0ff", "#ff00aa", "#ffe600", "#00ff88"],
                    "teamCapacity": team_capacity or 4,
                    "host": username
                },
                "players": {}
            }
        
        room = self.active_rooms[room_id]
        
        # Enforce team capacity if not FFA
        if room["meta"]["mode"] != "ffa":
            current_team_count = len([p for p in room["players"].values() if p.get("team_id") == team_id])
            if current_team_count >= room["meta"]["teamCapacity"]:
                # Auto-reassign to team with least players
                team_counts = {t: 0 for t in range(len(room["meta"]["teamColors"]))}
                for p in room["players"].values():
                    t_idx = p.get("team_id", 0)
                    if t_idx in team_counts:
                        team_counts[t_idx] += 1
                team_id = min(team_counts, key=team_counts.get)

        room["players"][player_id] = {
            "ws": websocket,
            "username": username,
            "map": selected_map,
            "team_id": team_id
        }

        # Notify room members about new player and room mode state
        await self.broadcast_to_room(room_id, {
            "type": "player_joined",
            "playerId": player_id,
            "username": username,
            "team_id": team_id,
            "meta": room["meta"],
            "playerCount": len(room["players"])
        }, exclude_player_id=None)

    def leave_room(self, room_id: str, player_id: str):
        if room_id in self.active_rooms and player_id in self.active_rooms[room_id]["players"]:
            del self.active_rooms[room_id]["players"][player_id]
            if not self.active_rooms[room_id]["players"]:
                del self.active_rooms[room_id]

    async def broadcast_to_room(self, room_id: str, message: dict, exclude_player_id: str = None):
        if room_id not in self.active_rooms:
            return
        
        dead_players = []
        payload = json.dumps(message)
        players = self.active_rooms[room_id]["players"]
        for p_id, p_info in players.items():
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
            {
                "playerId": pid,
                "username": info["username"],
                "team_id": info.get("team_id", 0)
            }
            for pid, info in self.active_rooms[room_id]["players"].items()
        ]

    def get_active_rooms_summary(self) -> List[dict]:
        summary = []
        for rid, room_data in self.active_rooms.items():
            players = room_data["players"]
            meta = room_data["meta"]
            if players:
                summary.append({
                    "roomId": rid,
                    "map": meta.get("map", "fortress"),
                    "mode": meta.get("mode", "2team"),
                    "host": meta.get("host", "Unknown"),
                    "teamColors": meta.get("teamColors", []),
                    "teamCapacity": meta.get("teamCapacity", 4),
                    "playerCount": len(players)
                })
        return summary

manager = ConnectionManager()
