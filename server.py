"""
Voice Chat - signalizacijos serveris su centriniu Authentik OIDC prisijungimu.

Sis serveris NEMATO ir NEPERDUODA garso (WebRTC P2P, DTLS-SRTP). Jis:
  1. Aptarnauja index.html
  2. Reikalauja prisijungimo per Authentik OIDC (kondev SSO)
  3. Padeda dalyviams susirasti (signaling per WebSocket)
  4. Isduoda laikinus TURN kredencialus (/api/ice)
  5. Leidzia ADMINUI ismesti narius is kambario (kick, be bano)

Konfiguracija per aplinkos kintamuosius (.env): TURN_*, OIDC_*.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

app = FastAPI(title="Voice Chat")
BASE_DIR = Path(__file__).parent

TURN_HOST = os.environ.get("TURN_HOST", "").strip()
TURN_SECRET = os.environ.get("TURN_SECRET", "").strip()
TURN_REALM = os.environ.get("TURN_REALM", TURN_HOST).strip()
TURN_TTL = int(os.environ.get("TURN_TTL", "3600"))

# --- OIDC (Authentik) ---
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
OIDC_AUTHORIZE_URL = os.environ.get("OIDC_AUTHORIZE_URL", "").strip()
OIDC_TOKEN_URL = os.environ.get("OIDC_TOKEN_URL", "").strip()
OIDC_USERINFO_URL = os.environ.get("OIDC_USERINFO_URL", "").strip()
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "").strip()
OIDC_ADMIN_GROUP = os.environ.get("OIDC_ADMIN_GROUP", "authentik Admins").strip()

# Vidinis service-to-service tokenas (dashboard -> voice). Naudojamas /internal/*
# (pvz. vartotojo deaktyvavimo propagavimui iš centrinės admin panelės).
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()

SESSION_COOKIE = "voice_session"
SESSION_TTL_DAYS = 7
# token -> {"user": str, "username": str, "is_admin": bool, "expires": datetime}
SESSIONS: dict[str, dict] = {}


def _new_session(user: str, is_admin: bool, username: str = "") -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "user": user,           # rodomas vardas
        "username": username,   # Authentik preferred_username (stabilus identifikatorius)
        "is_admin": is_admin,
        "expires": datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS),
    }
    return token


def _session_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    info = SESSIONS.get(token)
    if not info:
        return None
    if info["expires"] < datetime.now(timezone.utc):
        SESSIONS.pop(token, None)
        return None
    return info


def require_session(voice_session: str | None = Cookie(default=None)) -> dict:
    info = _session_from_token(voice_session)
    if not info:
        raise HTTPException(status_code=401, detail="prisijungimas reikalingas")
    return info


@dataclass
class Room:
    code: str
    host_id: str | None = None
    peers: dict[str, WebSocket] = field(default_factory=dict)
    meta: dict[str, dict] = field(default_factory=dict)  # peer_id -> {user, is_admin}


rooms: dict[str, Room] = {}


def _new_code(length: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _ice_servers() -> list[dict]:
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


# ============================================
# OIDC (Authentik) prisijungimas
# ============================================
@app.get("/auth/login")
def auth_login():
    if not OIDC_CLIENT_ID or not OIDC_AUTHORIZE_URL:
        raise HTTPException(status_code=500, detail="OIDC nesukonfigūruotas")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
    }
    url = OIDC_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie("voice_oidc_state", state, httponly=True, secure=True,
                    samesite="lax", max_age=600, path="/")
    return resp


@app.get("/auth/callback")
def auth_callback(code: str = "", state: str = "",
                  voice_oidc_state: str | None = Cookie(default=None)):
    if not code or not state or state != voice_oidc_state:
        raise HTTPException(status_code=400, detail="OIDC state/code klaida")
    try:
        with httpx.Client(timeout=15) as client:
            tok = client.post(OIDC_TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": OIDC_REDIRECT_URI,
                "client_id": OIDC_CLIENT_ID, "client_secret": OIDC_CLIENT_SECRET,
            })
            if tok.status_code != 200:
                raise HTTPException(status_code=502, detail="OIDC token mainai nepavyko")
            access_token = tok.json().get("access_token")
            ui = client.get(OIDC_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
            if ui.status_code != 200:
                raise HTTPException(status_code=502, detail="OIDC userinfo nepavyko")
            info = ui.json()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="OIDC serveris nepasiekiamas")

    user = info.get("name") or info.get("preferred_username") or info.get("email") or "user"
    username = info.get("preferred_username") or info.get("email") or user
    groups = info.get("groups", []) or []
    is_admin = OIDC_ADMIN_GROUP in groups
    token = _new_session(user, is_admin, username)
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True,
                    samesite="lax", max_age=SESSION_TTL_DAYS * 24 * 3600, path="/")
    resp.delete_cookie("voice_oidc_state", path="/")
    return resp


@app.get("/auth/logout")
def auth_logout(voice_session: str | None = Cookie(default=None)):
    if voice_session:
        SESSIONS.pop(voice_session, None)
    resp = RedirectResponse(url="https://kondev.app/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def api_me(voice_session: str | None = Cookie(default=None)):
    info = _session_from_token(voice_session)
    if not info:
        return {"authenticated": False, "user": None, "is_admin": False}
    return {"authenticated": True, "user": info["user"], "is_admin": info["is_admin"]}


# ============================================
# Vidinis API (service-to-service, NE vartotojams)
# ============================================
# Apsauga: bendras X-Internal-Token (ne vartotojo sesija). Pasiekiama tik per
# vidinį "web" docker tinklą (Caddy /internal/* viešai neatveria).
@app.post("/internal/set-active")
async def internal_set_active(
    payload: dict = Body(...),
    x_internal_token: str | None = Header(default=None),
):
    """Centrinė admin panelė kviečia, kai vartotojas deaktyvuojamas/aktyvuojamas.
    Voice neturi nuolatinės DB - tad deaktyvuojant tiesiog išmetame jo sesijas ir
    atjungiame aktyvius pokalbius (naują prisijungimą vis tiek blokuoja Authentik)."""
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="vidiniai endpoint'ai išjungti")
    if x_internal_token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="neteisingas vidinis tokenas")

    username = (payload.get("username") or "").strip()
    is_active = bool(payload.get("is_active"))
    if is_active or not username:
        # Aktyvuojant nieko daryti nereikia (naujas login leidžiamas per Authentik).
        return {"ok": True, "dropped_sessions": 0, "closed_peers": 0}

    # 1) Išmetam visas to vartotojo sesijas (pagal username)
    dropped = [t for t, s in SESSIONS.items() if s.get("username") == username]
    for t in dropped:
        SESSIONS.pop(t, None)

    # 2) Atjungiam aktyvius WS pokalbius (jei tuo metu kalbasi)
    closed = 0
    for room in list(rooms.values()):
        for pid, meta in list(room.meta.items()):
            if meta.get("username") == username:
                ws = room.peers.get(pid)
                if ws is not None:
                    try:
                        await ws.send_json({"type": "kicked", "by": "sistema"})
                        await ws.close()
                    except Exception:
                        pass
                    closed += 1
    return {"ok": True, "dropped_sessions": len(dropped), "closed_peers": closed}


# ============================================
# Puslapis + API (reikalauja prisijungimo)
# ============================================
@app.get("/")
async def index() -> FileResponse:
    # index.html pats patikrina /api/me ir nukreipia į /auth/login jei neprisijungęs
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/ice")
async def ice_config(_s: dict = Depends(require_session)) -> JSONResponse:
    return JSONResponse({"iceServers": _ice_servers()})


@app.post("/api/rooms")
async def create_room(_s: dict = Depends(require_session)) -> JSONResponse:
    code = _new_code()
    while code in rooms:
        code = _new_code()
    rooms[code] = Room(code=code)
    return JSONResponse({"code": code})


@app.get("/api/rooms/{code}")
async def room_exists(code: str, _s: dict = Depends(require_session)) -> JSONResponse:
    return JSONResponse({"exists": code.upper() in rooms})


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

    # Auth: WS reikalauja galiojančios sesijos (cookie)
    sess = _session_from_token(ws.cookies.get(SESSION_COOKIE))
    if not sess:
        await ws.send_json({"type": "error", "reason": "unauthorized"})
        await ws.close()
        return

    room = rooms.get(code)
    if room is None:
        await ws.send_json({"type": "error", "reason": "room-not-found"})
        await ws.close()
        return

    peer_id = secrets.token_hex(8)
    is_host = room.host_id is None
    if is_host:
        room.host_id = peer_id

    existing = list(room.peers.keys())   # ID tekstų sąrašas (frontend createPeer tikisi string)
    room.peers[peer_id] = ws
    room.meta[peer_id] = {"user": sess["user"], "username": sess.get("username", ""),
                          "is_admin": sess["is_admin"]}

    await ws.send_json({
        "type": "welcome",
        "peerId": peer_id,
        "isHost": is_host,
        "isAdmin": sess["is_admin"],
        "user": sess["user"],
        "peers": existing,
    })
    await _broadcast(room, {"type": "peer-joined", "peerId": peer_id, "user": sess["user"]}, exclude=peer_id)

    try:
        while True:
            data = await ws.receive_json()
            mtype = data.get("type")

            # ADMIN kick: tik adminas gali išmesti narį (be bano - gali grįžti)
            if mtype == "kick":
                if not sess["is_admin"]:
                    continue
                target = data.get("to")
                tws = room.peers.get(target)
                if tws is not None and target != peer_id:
                    try:
                        await tws.send_json({"type": "kicked", "by": sess["user"]})
                        await tws.close()
                    except Exception:
                        pass
                continue

            # Įprastas signaling perdavimas (offer/answer/ice)
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
        room.meta.pop(peer_id, None)
        await _broadcast(room, {"type": "peer-left", "peerId": peer_id})
        if not room.peers:
            rooms.pop(code, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
