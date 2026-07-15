"""FastAPI application entrypoint.

Exposes a liveness/readiness ``/health`` endpoint that verifies Postgres and
Redis connectivity — used by the Compose healthchecks. No PII is ever logged or
returned here (§15.13).
"""

from __future__ import annotations

import logging

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from career import __version__
from career.config import get_settings
from career.db.session import SessionLocal, app_engine
from career.logging_filters import install_secret_redaction
from career.salla.webhook import WebhookStatus, receive_webhook
from career.whatsapp.signature import verify_challenge
from career.whatsapp.webhook import WhatsAppIntakeStatus, receive_whatsapp_webhook

logging.basicConfig(level=get_settings().log_level)
# Scrub secrets from every log record before any external integration (§15.13).
install_secret_redaction()
logger = logging.getLogger("career")

app = FastAPI(title="Career Platform", version=__version__)

# Salla event types we accept on the webhook endpoint (whitepaper §09).
# app.store.authorize carries the Easy-Mode access/refresh tokens on install —
# it must be persisted (signature-verified) or the tokens are lost.
_SALLA_EVENTS = frozenset({
    "order.created", "order.payment.updated", "order.status.updated",
    "order.cancelled", "order.canceled", "order.refunded", "order.chargeback",
    "app.store.authorize",
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

    # App lifecycle events (app.installed, app.store.authorize, …) are rare and
    # valuable — the authorize one carries the Easy-Mode tokens — so accept the
    # whole app.* family; exact names vary across Salla flows.
    known = event_type in _SALLA_EVENTS or event_type.startswith("app.")
    if not known:
        # Unknown/absent event type — acknowledge without persisting, but leave
        # a trace so silently-dropped event names are visible (§15.12).
        logger.info("salla webhook ignored: event_type=%r", event_type)
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


@app.get("/webhooks/whatsapp")
async def whatsapp_verify(request: Request) -> PlainTextResponse:
    """Meta subscription handshake — echo hub.challenge if the verify token matches."""
    q = request.query_params
    challenge = verify_challenge(
        mode=q.get("hub.mode"),
        token=q.get("hub.verify_token"),
        challenge=q.get("hub.challenge"),
        expected_token=get_settings().whatsapp_verify_token,
    )
    if challenge is None:
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge, status_code=200)


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request) -> JSONResponse:
    """Fast intake: verify X-Hub-Signature-256, dedupe, persist, return 200.
    The inbound worker (process_pending_whatsapp) does the real work."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    secret = get_settings().whatsapp_app_secret

    session = SessionLocal()
    try:
        result = receive_whatsapp_webhook(
            session, raw_body=raw_body, header_signature=signature, app_secret=secret,
        )
    finally:
        session.close()

    if result.status is WhatsAppIntakeStatus.INVALID_SIGNATURE:
        logger.warning("whatsapp webhook rejected: invalid signature")
        return JSONResponse(status_code=401, content={"status": "invalid_signature"})
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
