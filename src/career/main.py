"""FastAPI application entrypoint.

Exposes a liveness/readiness ``/health`` endpoint that verifies Postgres and
Redis connectivity — used by the Compose healthchecks. No PII is ever logged or
returned here (§15.13).
"""

from __future__ import annotations

import logging

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from career import __version__
from career.config import get_settings
from career.db.session import SessionLocal, app_engine
from career.logging_filters import install_secret_redaction
from career.salla.webhook import WebhookStatus, receive_webhook

logging.basicConfig(level=get_settings().log_level)
# Scrub secrets from every log record before any external integration (§15.13).
install_secret_redaction()
logger = logging.getLogger("career")

app = FastAPI(title="Career Platform", version=__version__)

# Salla event types we accept on the webhook endpoint (whitepaper §09).
_SALLA_EVENTS = frozenset({
    "order.created", "order.payment.updated", "order.cancelled",
    "order.canceled", "order.refunded", "order.chargeback",
})


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


@app.post("/webhooks/salla")
async def salla_webhook(request: Request) -> JSONResponse:
    """Fast intake: verify signature, dedupe, persist, return 200. Provisioning
    is done by a separate worker (process_pending_webhooks) — never in-request."""
    raw_body = await request.body()
    event_type = request.headers.get("X-Salla-Event", "")
    signature = request.headers.get("X-Salla-Signature")
    secret = get_settings().salla_webhook_secret

    if event_type not in _SALLA_EVENTS:
        # Unknown/absent event type — acknowledge without persisting.
        return JSONResponse(status_code=200, content={"status": "ignored"})

    session = SessionLocal()
    try:
        result = receive_webhook(
            session, provider="salla", event_type=event_type,
            raw_body=raw_body, header_signature=signature, secret=secret,
        )
    finally:
        session.close()

    if result.status is WebhookStatus.INVALID_SIGNATURE:
        logger.warning("salla webhook rejected: invalid signature")
        return JSONResponse(status_code=401, content={"status": "invalid_signature"})
    # accepted or duplicate → 200 so Salla stops retrying (idempotent).
    return JSONResponse(status_code=200, content={"status": result.status.value})


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
