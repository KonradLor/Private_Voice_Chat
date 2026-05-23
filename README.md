# Voice Chat

*Read this in other languages: [Lietuviškai](README.lt.md)*

A private voice chat app (P2P WebRTC). Audio travels **directly between
participants** and is encrypted (DTLS-SRTP) — the server never sees or stores
it. Designed for rooms of 5–8 people (mesh topology).

## Components

| Service | Role |
|---------|------|
| `app` (FastAPI) | Serves the UI, handles signaling over WebSocket, issues short-lived TURN credentials |
| `coturn` | TURN/STUN server — enables connections behind strict NAT |
| Caddy (on host) | HTTPS reverse proxy (automatic certificates) |

## Features

- **Voice chat** — P2P mesh, encrypted (DTLS-SRTP); the server never sees audio.
- **Optional video** — each participant decides whether to turn their camera on;
  video is added/removed live via WebRTC renegotiation (perfect negotiation).
  Best for small groups (2–4) due to mesh bandwidth; works on mobile (front camera).
- **Per-participant volume** — each participant locally adjusts how loudly they
  hear others (0–200%, mute or boost). Only affects their own listening.
- **Text chat** — sent over a WebRTC DataChannel (P2P, encrypted) — the server
  never sees the messages, just like the audio.
- **Connection quality indicator** — a colored dot (green/yellow/red) on each
  participant, derived from WebRTC stats (packet loss + RTT).
- **Join/leave sounds and notifications** — an audible cue and a toast when
  someone connects or disconnects.
- **Speaking indicator** — the active speaker's tile is highlighted with a green glow.
- **Audio unlock** — a banner appears if the browser blocks autoplay (mobile).
- **Invites** — room code + link (`?room=CODE`), with native share on mobile.

---

## Local testing

```bash
pip install -r requirements.txt
python server.py
# http://localhost:8000
```

No TURN is needed locally — without `.env`, only public STUN servers are used.
The microphone works in the browser over `localhost` or `https://` (not over
`http://` with a bare IP).

---

## Deploying on an Oracle server (Docker)

### 1. Prepare `.env`

```bash
cp .env.example .env
# Generate a strong secret:
openssl rand -hex 32   # -> put it in TURN_SECRET
# Fill in TURN_HOST, TURN_REALM, PUBLIC_IP
# Fill in the OIDC_* values (see step 2b) — login won't work without them.
```

### 2. Point DNS

- `voice.your-domain.com`  -> server public IP (the app)
- `turn.your-domain.com`   -> same IP (coturn)

### 2b. Set up the Authentik OIDC client (required for login)

Login is handled by Authentik (OpenID Connect). In Authentik, create an
**OAuth2/OpenID Provider** + Application, then:

- Set the provider **Redirect URI** to `https://voice.your-domain.com/auth/callback`.
- Copy the **Client ID** and **Client Secret** into `.env`
  (`OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`).
- Put your Authentik endpoints into `OIDC_AUTHORIZE_URL`, `OIDC_TOKEN_URL`,
  `OIDC_USERINFO_URL` (see `.../.well-known/openid-configuration`).
- `OIDC_REDIRECT_URI` must match the Redirect URI above.
- `OIDC_ADMIN_GROUP` — the Authentik group whose members can kick participants.

`INTERNAL_API_TOKEN` is optional (only for the central admin panel's
`/internal/set-active` call); leave it empty to disable `/internal/*`.

### 3. Open ports

**Oracle Security List** (VCN -> Subnet -> Security List) **and** the server firewall:

| Port | Protocol | Purpose |
|------|----------|---------|
| 443 | TCP | HTTPS (Caddy) |
| 3478 | TCP + UDP | TURN/STUN |
| 49160–49200 | UDP | TURN relay range |

```bash
# Ubuntu/Oracle Linux example (iptables):
sudo iptables -I INPUT -p udp --dport 3478 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 3478 -j ACCEPT
sudo iptables -I INPUT -p udp --dport 49160:49200 -j ACCEPT
sudo netfilter-persistent save
```

> An Oracle instance has both an internal firewall (firewalld/iptables) and a
> VCN Security List. You must open **both**.

### 4. Caddy

Add the contents of `Caddyfile.example` to your Caddyfile and reload Caddy.

### 5. Run

```bash
docker compose up -d --build
docker compose logs -f
```

Open `https://voice.your-domain.com`.

---

## Verification

- **Is TURN working?** Open https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/ ,
  enter `turn:turn.your-domain.com:3478`, plus a username and password from the
  `GET /api/ice` response. A `relay` candidate should appear.
- **Is signaling working?** `docker compose logs app` — you'll see WebSocket connections.

---

## Deploying without Nginx / without a proxy on the host

If the server has no Caddy or Nginx, the stack itself can handle HTTPS.
Pick one of two options.

### Option A — Caddy in a container (recommended)

Caddy obtains a Let's Encrypt certificate itself. Add to `docker-compose.yml`:

```yaml
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    depends_on:
      - app

volumes:
  caddy_data:
```

Create a `Caddyfile` (without `.example`):

```
voice.your-domain.com {
    reverse_proxy app:8000
}
```

> Note: change the `app` port publishing to `expose: ["8000"]` (instead of
> `ports: 127.0.0.1:8000`) so the app is reachable only on the internal network.
> Run the same way: `docker compose up -d --build`.

### Option B — uvicorn directly with TLS (no proxy at all)

Good for a quick deploy. Get a certificate (e.g. `certbot certonly --standalone`)
and run the app directly on 443:

```bash
uvicorn server:app --host 0.0.0.0 --port 443 \
  --ssl-keyfile /etc/letsencrypt/live/DOMAIN/privkey.pem \
  --ssl-certfile /etc/letsencrypt/live/DOMAIN/fullchain.pem
```

> No automatic certificate renewal — you need a `certbot renew` cron job.
> Run coturn separately (see below) or via the same docker-compose.

### coturn without Docker (systemd)

```bash
sudo apt install coturn
sudo nano /etc/turnserver.conf   # enable: use-auth-secret, static-auth-secret=..., realm=..., external-ip=...
echo "TURNSERVER_ENABLED=1" | sudo tee /etc/default/coturn
sudo systemctl enable --now coturn
```

Open the ports (3478, 49160-49200/UDP) the same way as in the Oracle section.

---

## Embedding into another app

The entire signaling server lives in a single `server.py` built on FastAPI. You can:
- **Mount it as a sub-app:** `parent_app.mount("/voice", app)` in another FastAPI project.
- **Use it just as a container** alongside other services (add it to a shared `docker-compose.yml`).
- The UI is a single `index.html` with no build step — easy to drop into any frontend.

---

## Notes / limitations

- **Rooms are kept in memory** — they disappear on server restart. Fine for a group of friends.
- **Mesh limits:** ~8 people for voice. More than that -> you'd need an SFU (LiveKit/mediasoup).
- **Privacy (GDPR):** audio is not recorded; add a privacy policy if you monetize.
- **Security:** never commit `.env` (which holds `TURN_SECRET`) to git.

---

## License

Proprietary — all rights reserved. Use, copying, or deployment without the
author's written permission is prohibited. See [LICENSE](LICENSE).
