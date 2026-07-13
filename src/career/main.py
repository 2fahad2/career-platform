"""FastAPI application entrypoint.

Exposes a liveness/readiness ``/health`` endpoint that verifies Postgres and
Redis connectivity — used by the Compose healthchecks. No PII is ever logged or
returned here (§15.13).
"""

from __future__ import annotations

import logging

import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from career import __version__
from career.config import get_settings
from career.db.session import app_engine

logging.basicConfig(level=get_settings().log_level)
logger = logging.getLogger("career")

app = FastAPI(title="Career Platform", version=__version__)


def _check_db() -> bool:
    try:
        with app_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — health probe reports, never raises
        logger.warning("health: database check failed", exc_info=True)
        return False


def _check_redis() -> bool:
    s = get_settings()
    try:
        client = redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            password=s.redis_password or None,
            socket_connect_timeout=2,
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        logger.warning("health: redis check failed", exc_info=True)
        return False


@app.get("/health")
def health() -> JSONResponse:
    checks = {"database": _check_db(), "redis": _check_redis()}
    ok = all(checks.values())
    status_code = 200 if ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if ok else "degraded",
            "env": get_settings().env,
            "version": __version__,
            "checks": checks,
        },
    )
