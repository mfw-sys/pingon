# ──────────────────────────────────────────────────────────────
#  PingOn – Ping Monitoring Dashboard
#  Multi-stage build: lean final image (~120 MB)
# ──────────────────────────────────────────────────────────────

# ---------- stage 1: build dependencies ----------
FROM python:3.12-slim AS builder

WORKDIR /build

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- stage 2: runtime ----------
FROM python:3.12-slim

LABEL maintainer="PingOn"
LABEL description="PingOn – Ping Monitoring Dashboard"

# iputils-ping  → provides the `ping` binary used by the engine
# tini          → proper PID-1 init for signal handling in containers
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping tini \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built Python packages from builder stage
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -r pingon && useradd -r -g pingon -m -s /bin/bash pingon

# Application code
WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Persistent data directories (will be mounted as volumes)
RUN mkdir -p /app/backend/data/backups \
    && chown -R pingon:pingon /app

USER pingon

# Expose the default Uvicorn port
EXPOSE 8000

# Health check – hit the /api/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

# Use tini as init, then start Uvicorn
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--app-dir", "/app/backend"]
