"""FastAPI application entrypoint.

Run with:
    cd backend
    uvicorn main:app --reload

This also serves the static frontend directly (see StaticFiles mount
below), so a single `uvicorn main:app` is enough to run the whole app —
no separate frontend server needed, per the "opsi kedua" preference in
the brief.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import db
from api.routes_monitor import router as monitor_router
from api.routes_targets import router as targets_router
from api.routes_groups import router as groups_router
from api.routes_auth import router as auth_router
from api.routes_users import router as users_router
from api.routes_database import router as database_router
from api.routes_settings import router as settings_router
from ping.logger import configure_logging
from services.monitor_service import monitor_service
from services.backup_service import backup_service
from auth.password import get_password_hash, verify_password

BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
UPLOAD_DIR = BACKEND_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Library logging: quiet by default (WARNING) so per-packet logs from
# the ping engine don't spam a running server; bump to INFO for
# development if you want to see per-cycle activity.
configure_logging(level=logging.INFO)
service_logger = logging.getLogger("ping_monitor.service")
service_logger.setLevel(logging.INFO)

def seed_admin():
    admin = db.get_user_by_username("admin")
    if not admin:
        logging.info("Seeding default administrator (admin / admin123)")
        db.create_user(
            username="admin", 
            password_hash=get_password_hash("admin123"),
            role="administrator",
            must_change_password=True
        )
    elif verify_password("admin123", admin["password_hash"]) and not admin.get("must_change_password"):
        db.update_user(admin["id"], must_change_password=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    seed_admin()
    await monitor_service.start_all()
    await backup_service.start()
    try:
        yield
    finally:
        await backup_service.stop()
        await monitor_service.stop_all()


app = FastAPI(title="Ping Monitoring Dashboard API", lifespan=lifespan)

# CORS: explicit dev origins only — never "*" (see prompt section 24).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware to transparently strip "/sd" prefix if accessed via a subpath proxy
@app.middleware("http")
async def strip_sd_prefix(request, call_next):
    if request.scope["path"].startswith("/sd/"):
        request.scope["path"] = request.scope["path"][3:]
    return await call_next(request)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(database_router)
app.include_router(settings_router)
app.include_router(monitor_router)
app.include_router(targets_router)
app.include_router(groups_router)

# Serve uploaded static files
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# Serve the static frontend last so it doesn't shadow the /api routes.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
