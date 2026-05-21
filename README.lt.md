# Voice Chat

*Skaityti kitomis kalbomis: [English](README.md)*

Privatus balso pokalbių (P2P WebRTC) programėlė. Garsas keliauja **tiesiogiai
tarp dalyvių** ir yra šifruojamas (DTLS-SRTP) — serveris jo nemato ir nesaugo.
Skirta 5–8 žmonių kambariams (mesh topologija).

## Komponentai

| Servisas | Vaidmuo |
|----------|---------|
| `app` (FastAPI) | Aptarnauja UI, signalizacija per WebSocket, išduoda laikinus TURN kredencialus |
| `coturn` | TURN/STUN serveris — leidžia jungtis ir už griežtų NAT |
| Caddy (hoste) | HTTPS reverse proxy (sertifikatas automatiškai) |

## Funkcijos

- **Balso pokalbis** — P2P mesh, šifruota (DTLS-SRTP), serveris garso nemato.
- **Individualus garsumas** — kiekvienas dalyvis lokaliai reguliuoja, kaip garsiai
  girdi kitus (0–200%, gali patildyti arba pagarsinti). Keičia tik savo klausymą.
- **Tekstinis pokalbis** — keliauja per WebRTC DataChannel (P2P, šifruota) —
  serveris žinučių nemato, kaip ir balso.
- **Ryšio kokybės indikatorius** — spalvotas taškelis (žalias/geltonas/raudonas)
  ant kiekvieno dalyvio iš WebRTC statistikos (paketų praradimas + RTT).
- **Prisijungimo/išėjimo garseliai ir pranešimai** — girdimas signalas ir pranešimas,
  kai kas nors prisijungia ar atsijungia.
- **Kalbėjimo indikatorius** — kalbančio dalyvio blokas pažymimas žaliu švytėjimu.
- **Garso atrakinimas** — pranešimas, jei naršyklė blokuoja automatinį paleidimą (mobiliuosiuose).
- **Kvietimas** — kambario kodas + nuoroda (`?room=KODAS`), telefone per sisteminį dalijimąsi.

---

## Lokalus bandymas

```bash
pip install -r requirements.txt
python server.py
# http://localhost:8000
```

Lokaliai TURN nereikia — be `.env` naudojami tik vieši STUN serveriai.
Mikrofonas naršyklėje veikia per `localhost` arba `https://` (ne per `http://` su IP).

---

## Diegimas Oracle serveryje (Docker)

### 1. Paruošk `.env`

```bash
cp .env.example .env
# Sugeneruok stiprų raktą:
openssl rand -hex 32   # -> įkelk į TURN_SECRET
# Užpildyk TURN_HOST, TURN_REALM, PUBLIC_IP
```

### 2. Nukreipk DNS

- `voice.tavo-domenas.lt`  -> serverio viešas IP (programėlei)
- `turn.tavo-domenas.lt`   -> tas pats IP (coturn)

### 3. Atidaryk portus

**Oracle Security List** (VCN -> Subnet -> Security List) **ir** serverio ugniasienė:

| Portas | Protokolas | Kam |
|--------|-----------|-----|
| 443 | TCP | HTTPS (Caddy) |
| 3478 | TCP + UDP | TURN/STUN |
| 49160–49200 | UDP | TURN relay diapazonas |

```bash
# Ubuntu/Oracle Linux pavyzdys (iptables):
sudo iptables -I INPUT -p udp --dport 3478 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 3478 -j ACCEPT
sudo iptables -I INPUT -p udp --dport 49160:49200 -j ACCEPT
sudo netfilter-persistent save
```

> Oracle instance turi ir vidinę ugniasienę (firewalld/iptables), ir VCN
> Security List. Reikia atidaryti **abi**.

### 4. Caddy

Įterpk `Caddyfile.example` turinį į savo Caddyfile ir perkrauk Caddy.

### 5. Paleisk

```bash
docker compose up -d --build
docker compose logs -f
```

Atidaryk `https://voice.tavo-domenas.lt`.

---

## Patikrinimas

- **Ar veikia TURN?** Atidaryk https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/ ,
  įvesk `turn:turn.tavo-domenas.lt:3478`, vartotojo vardą ir slaptažodį iš `GET /api/ice`
  atsakymo. Turi pasirodyti `relay` kandidatas.
- **Ar veikia signalizacija?** `docker compose logs app` — matysi WebSocket prisijungimus.

---

## Diegimas be Nginx / be proxy hoste

Jei serveryje nėra Caddy ar Nginx, HTTPS gali tvarkyti pats stack'as.
Pasirink vieną iš dviejų variantų.

### Variantas A — Caddy konteineryje (rekomenduojamas)

Caddy pats gauna Let's Encrypt sertifikatą. Pridėk į `docker-compose.yml`:

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

Sukurk `Caddyfile` (be `.example`):

```
voice.tavo-domenas.lt {
    reverse_proxy app:8000
}
```

> Pastaba: pakeisk `app` porto publikavimą į `expose: ["8000"]` (vietoj
> `ports: 127.0.0.1:8000`), kad app būtų pasiekiamas tik vidiniam tinklui.
> Paleidi taip pat: `docker compose up -d --build`.

### Variantas B — uvicorn tiesiogiai su TLS (be jokio proxy)

Tinka greitam diegimui. Gauk sertifikatą (pvz. `certbot certonly --standalone`)
ir paleisk app'ą tiesiai per 443:

```bash
uvicorn server:app --host 0.0.0.0 --port 443 \
  --ssl-keyfile /etc/letsencrypt/live/DOMENAS/privkey.pem \
  --ssl-certfile /etc/letsencrypt/live/DOMENAS/fullchain.pem
```

> Trūksta automatinio sertifikato atnaujinimo — reikia `certbot renew` cron.
> coturn paleidi atskirai (žr. žemiau) arba per tą patį docker-compose.

### coturn be Docker (systemd)

```bash
sudo apt install coturn
sudo nano /etc/turnserver.conf   # įjunk: use-auth-secret, static-auth-secret=..., realm=..., external-ip=...
echo "TURNSERVER_ENABLED=1" | sudo tee /etc/default/coturn
sudo systemctl enable --now coturn
```

Portai (3478, 49160-49200/UDP) atidaromi taip pat, kaip Oracle skyriuje.

---

## Įterpimas į kitą programėlę

Visas signalizacijos serveris yra viename `server.py` su FastAPI. Galima:
- **Montuoti kaip sub-app:** `parent_app.mount("/voice", app)` kitame FastAPI projekte.
- **Naudoti tik kaip konteinerį** šalia kitų servisų (pridėk į bendrą `docker-compose.yml`).
- UI yra vienas `index.html` be build'o — lengva įdėti į bet kurį frontend'ą.

---

## Pastabos / apribojimai

- **Kambariai laikomi atmintyje** — perkrovus serverį dingsta. Draugų grupelei pakanka.
- **Mesh ribos:** ~8 žmonės balsui. Daugiau -> reikėtų SFU (LiveKit/mediasoup).
- **Privatumas (BDAR):** garsas neįrašomas; pridėk privatumo politiką, jei monetizuosi.
- **Saugumas:** `.env` su `TURN_SECRET` niekada neįkelk į git.

---

## Licencija

Proprietary — visos teisės saugomos. Naudojimas, kopijavimas ar diegimas be
autoriaus rašytinio leidimo draudžiamas. Žr. [LICENSE](LICENSE).
