"""
Voice Chat - signalizacijos serveris.

Sis serveris NEMATO ir NEPERDUODA garso. Garsas keliauja tiesiogiai tarp
naudotoju narsykliu per WebRTC (P2P, mesh topologija) ir yra sifruojamas
DTLS-SRTP. Serveris tik:
  1. Aptarnauja index.html
  2. Padeda dalyviams "susirasti" vienas kita (signaling per WebSocket)
  3. Saugo aktyviu kambariu kodus
  4. Isduoda laikinus (ephemeral) TURN kredencialus narsyklems (/api/ice)

Konfiguracija per aplinkos kintamuosius (zr. .env.example):
  TURN_HOST    - TURN/STUN serverio domenas (pvz. turn.tavo-domenas.lt)
  TURN_SECRET  - bendras slaptas raktas, sutampantis su coturn static-auth-secret
  TURN_REALM   - coturn realm (numatytas = TURN_HOST)
  TURN_TTL     - kredencialu galiojimas sekundemis (numatytas 3600)

Jei TURN_SECRET nera nustatytas, naudojami tik viesi STUN serveriai
(tinka lokaliam bandymui, bet ne komerciniam diegimui).

Paleidimas lokaliai:
    pip install -r requirements.txt
    python server.py
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Voice Chat")

BASE_DIR = Path(__file__).parent

TURN_HOST = os.environ.get("TURN_HOST", "").strip()
TURN_SECRET = os.environ.get("TURN_SECRET", "").strip()
TURN_REALM = os.environ.get("TURN_REALM", TURN_HOST).strip()
TURN_TTL = int(os.environ.get("TURN_TTL", "3600"))


@dataclass
class Room:
    code: str
    host_id: str | None = None
    peers: dict[str, WebSocket] = field(default_factory=dict)


rooms: dict[str, Room] = {}


def _new_code(length: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _ice_servers() -> list[dict]:
    """
    Grazina ICE serveriu sarasa narsyklei.

    Jei sukonfiguruotas TURN, sugeneruoja LAIKINUS kredencialus pagal coturn
    "use-auth-secret" mechanizma (HMAC-SHA1). Slaptas raktas niekada
    nepasiekia narsykles - tik trumpalaikis vartotojo vardas ir parasas.
    """
    servers: list[dict] = [{"urls": "stun:stun.l.google.com:19302"}]

    if TURN_SECRET and TURN_HOST:
        expiry = int(time.time()) + TURN_TTL
        username = f"{expiry}:webrtc"
        digest = hmac.new(TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
        credential = base64.b64encode(digest).decode()
        servers = [
            {"urls": f"stun:{TURN_HOST}:3478"},
            {
                "urls": [
                    f"turn:{TURN_HOST}:3478?transport=udp",
                    f"turn:{TURN_HOST}:3478?transport=tcp",
                ],
                "username": username,
                "credential": credential,
            },
        ]
    return servers


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/ice")
async def ice_config() -> JSONResponse:
    return JSONResponse({"iceServers": _ice_servers()})


@app.post("/api/rooms")
async def create_room() -> JSONResponse:
    code = _new_code()
    while code in rooms:
        code = _new_code()
    rooms[code] = Room(code=code)
    return JSONResponse({"code": code})


@app.get("/api/rooms/{code}")
async def room_exists(code: str) -> JSONResponse:
    code = code.upper()
    return JSONResponse({"exists": code in rooms})


async def _broadcast(room: Room, message: dict, exclude: str | None = None) -> None:
    for pid, ws in list(room.peers.items()):
        if pid == exclude:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            pass


@app.websocket("/ws/{code}")
async def signaling(ws: WebSocket, code: str) -> None:
    code = code.upper()
    await ws.accept()

    room = rooms.get(code)
    if room is None:
        await ws.send_json({"type": "error", "reason": "room-not-found"})
        await ws.close()
        return

    peer_id = secrets.token_hex(8)
    is_host = room.host_id is None
    if is_host:
        room.host_id = peer_id

    existing = list(room.peers.keys())
    room.peers[peer_id] = ws

    await ws.send_json({
        "type": "welcome",
        "peerId": peer_id,
        "isHost": is_host,
        "peers": existing,
    })
    await _broadcast(room, {"type": "peer-joined", "peerId": peer_id}, exclude=peer_id)

    try:
        while True:
            data = await ws.receive_json()
            target = data.get("to")
            data["from"] = peer_id
            if target and target in room.peers:
                try:
                    await room.peers[target].send_json(data)
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        room.peers.pop(peer_id, None)
        await _broadcast(room, {"type": "peer-left", "peerId": peer_id})
        if not room.peers:
            rooms.pop(code, None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
