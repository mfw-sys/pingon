# Ping Monitor — Dashboard + Python Ping Engine

Web dashboard for ICMP ping monitoring. All ICMP pings happen in the
Python backend (`backend/ping/`, unchanged from the standalone engine)
— the browser never touches ICMP directly.

```
Browser (glassmorphic dashboard)
   │  fetch() → REST API
   ▼
FastAPI backend
   │
   ▼
ping/  (PingMonitor, ping_host — reused from the standalone engine)
   │
   ▼
native `ping` subprocess → ICMP → target host
```

## Project structure

```
ping-monitor/
├── backend/
│   ├── main.py                  # FastAPI app, CORS, static frontend mount, lifespan
│   ├── db.py                    # SQLite schema + queries (targets, monitoring_results)
│   ├── api/
│   │   ├── routes_monitor.py    # /api/health, /api/ping, /api/dashboard
│   │   └── routes_targets.py    # /api/targets CRUD + /api/targets/{id}/history
│   ├── ping/                    # <- reused verbatim from the standalone ping engine
│   │   ├── ping_engine.py
│   │   ├── ping_parser.py
│   │   ├── ping_monitor.py
│   │   ├── models.py
│   │   ├── exceptions.py
│   │   └── logger.py
│   ├── models/
│   │   └── schemas.py           # Pydantic request/response contracts
│   ├── services/
│   │   └── monitor_service.py   # one asyncio task per enabled target
│   ├── tests/
│   │   └── test_api.py
│   ├── requirements.txt
│   └── requirements-dev.txt
└── frontend/
    └── index.html                # standalone: HTML + CSS + JS in one file
                                   # (Tailwind, Google Fonts, uPlot, Lucide
                                   # remain external CDN includes)
```

## 🚀 Quick Start (Docker - Recommended)

```bash
cp .env.example .env
docker compose up -d --build
```
Akses dashboard di browser: **`http://localhost:8000`** (Default: `admin` / `admin123`).

📖 **[Baca Panduan Lengkap Deployment dari GitHub ke Ubuntu Server (DEPLOYMENT.md)](./DEPLOYMENT.md)**

---

## 💻 Local Development (Without Docker)

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000** — the same FastAPI process serves the
dashboard (static files) and the API, so this single command is
enough. No separate frontend server needed.

The SQLite file is created automatically at `backend/data/ping_monitor.db`
on first run.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check |
| POST | `/api/ping` | Stateless on-demand ping (the "Ping" button) |
| GET | `/api/dashboard` | Summary counts + all targets with live status |
| GET | `/api/targets` | List targets |
| POST | `/api/targets` | Create a target (starts its monitoring loop) |
| GET | `/api/targets/{id}` | Get one target |
| PUT | `/api/targets/{id}` | Update a target (restarts its monitoring loop) |
| DELETE | `/api/targets/{id}` | Delete a target (stops its monitoring loop) |
| GET | `/api/targets/{id}/history?hours=24` | History for charts |

Interactive docs: `http://127.0.0.1:8000/docs`.

### Why `/api/ping` never touches saved-target state

`POST /api/ping` accepts an arbitrary `host` (not a target id) and
returns a result computed directly from the ping engine. It's what the
"Ping Now" button calls, and it deliberately does not write to
`monitoring_results` or affect a target's `raw_status`/`effective_status`
— those are owned exclusively by the background monitoring loop, so a
manual ping click can never distort a target's flapping-protection
state.

## Design notes / things reviewed for correctness

- **No false latency (section 36):** every latency value returned by
  the API — both from `/api/ping` and from background monitoring — is
  read from the native `ping` binary's own `time=` field, never from
  Python-side wall-clock around the HTTP request or the subprocess
  call. Verified in `test_on_demand_ping_localhost_up` (asserts
  latency stays in single-digit ms range on loopback, not the ~10s+ an
  HTTP-round-trip-based measurement would show).
- **No duplicate monitoring (section 37):** exactly one `asyncio.Task`
  runs per enabled target, created at startup or on target
  create/enable, and cancelled on delete/disable/update. Frontend
  polling only ever reads `/api/dashboard`, never triggers a ping.
  Verified in `test_disabling_target_stops_history_growth`.
- **Race conditions:** target create/update/delete handlers are
  `async def` and directly `await` the monitor service (rather than
  `asyncio.create_task` from a sync/threadpool context, which would
  have no event loop to attach to). An `asyncio.Lock` guards the
  service's internal task/monitor dicts against concurrent
  spawn/cancel races during rapid updates.
- **History growth:** `monitoring_results` is capped at 10,000 rows
  per target; `db.insert_result` deletes the oldest excess rows only
  when the cap is exceeded, so steady-state writes stay a single
  INSERT.
- **No shell injection (section 23):** `ping_engine.py` always calls
  `subprocess.run` with an argument list, never `shell=True`, and
  `validate_host()` rejects non-IP/non-hostname input before any
  command is built.
- **CORS:** explicit dev origins only, never `"*"` (see `main.py`).

## Testing

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

Covers: health check, dashboard (empty + populated), target CRUD,
404s, 422 validation (invalid host, missing name, out-of-range ping
params), on-demand ping against `127.0.0.1` (UP) and an invalid host
(ERROR), background monitoring producing history + dashboard state
within a live asyncio loop, and disabling a target actually stopping
its monitoring loop (no leaked task).

Manual smoke test targets suggested in the brief:
```bash
curl -X POST localhost:8000/api/targets -H "Content-Type: application/json" \
  -d '{"name":"Google DNS","host":"8.8.8.8","interval":10,"count":4,"timeout":1,"enabled":true}'
curl -X POST localhost:8000/api/targets -H "Content-Type: application/json" \
  -d '{"name":"Cloudflare","host":"1.1.1.1","interval":10,"count":4,"timeout":1,"enabled":true}'
curl -X POST localhost:8000/api/targets -H "Content-Type: application/json" \
  -d '{"name":"Localhost","host":"127.0.0.1","interval":10,"count":4,"timeout":1,"enabled":true}'
curl -X POST localhost:8000/api/targets -H "Content-Type: application/json" \
  -d '{"name":"Unreachable","host":"192.0.2.1","interval":10,"count":4,"timeout":1,"enabled":true}'
```

## Known environment caveat

`ping` requires the OS binary to be installed and reachable — some
sandboxed/containerized environments restrict raw ICMP even for a
non-root `ping` binary. If every target shows `DOWN` including known-good
hosts, check that `ping <host>` works from the same shell the backend
runs in.
