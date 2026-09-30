# Deploying the AI Math Tutor (Docker)

One command builds and starts everything — LiveKit, Postgres, the FastAPI server, the voice worker
and the web app (Caddy, which also terminates TLS):

```bash
docker compose -f docker-compose.deploy.yml up -d --build
```

Then open the site, type a name, and talk to the tutor. The same name later brings back that
student's board, memory and unfinished lesson.

## 1. Configure `.env` (repo root)

The containers read the provider keys and models from `.env` (it is never baked into an image).
On top of what local development needs, set:

| Variable | Why |
| :-- | :-- |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | shared by LiveKit, the API and the worker. Use a secret of **32+ characters** on a server. |
| `LIVEKIT_NODE_IP` | the server's **public IP** (media is sent to it). Default `127.0.0.1` only works when the browser runs on the same machine. |
| `SITE_ADDRESS` | a DNS name pointing at the server (e.g. `tutor.example.com`) to get automatic HTTPS. Default `:80` = plain http. |
| `POSTGRES_PASSWORD` | optional, default `postgres`. |
| `GROQ_API_KEYS`, `OPENCODE_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, … | as for local development. |

`FEATURE_MEMORY` and `FEATURE_REHYDRATE` are forced on by the compose file (name login relies on
them). `DATABASE_URL` and `LIVEKIT_URL` are set by the compose file to the internal services.

**Microphone rule:** browsers only allow the mic on `https://` or `http://localhost`. For an
interviewer on another machine, set `SITE_ADDRESS` to a domain (Caddy gets the certificate) —
a bare `http://<ip>` page will load but the mic will be blocked.

## 2. Open these ports on the host / cloud firewall

| Port | Use |
| :-- | :-- |
| 80, 443 tcp | web app, `/api` (FastAPI) and `/livekit` (LiveKit signalling) through Caddy |
| 7881 tcp | LiveKit media over TCP (fallback) |
| 50000-50100 udp | LiveKit media (WebRTC) |

## 3. Everyday commands

```bash
docker compose -f docker-compose.deploy.yml ps              # status
docker compose -f docker-compose.deploy.yml logs -f worker  # the voice agent's log
docker compose -f docker-compose.deploy.yml up -d --build   # rebuild after a code change
docker compose -f docker-compose.deploy.yml down            # stop (data volumes are kept)
```

Smoke check: `curl http://<site>/api/health` returns `{"status":"ok"}`.

## 4. What runs where

```
browser ──https──> web (Caddy) ── /api/*     ──> api    (uvicorn app.api.server:app)
                              └── /livekit/* ──> livekit (signalling)
browser ──udp/tcp media──────────────────────> livekit :50000-50100/udp, :7881/tcp
worker (python backend/app/main.py start) ──ws──> livekit ; api + worker ──> postgres
```

Name login: `POST /api/token {"name": "..."}` maps the name to a stable user id and that user's
most recently used board; the worker rehydrates the board state (memory, paused lesson,
pages) for that board on join. It is an identity, not a password — do not put real student
data behind it.
