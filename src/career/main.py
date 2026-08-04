"""FastAPI application entrypoint.

Exposes a liveness/readiness ``/health`` endpoint that verifies Postgres and
Redis connectivity — used by the Compose healthchecks. No PII is ever logged or
returned here (§15.13).
"""

from __future__ import annotations

import json
import logging

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from career import __version__
from career.config import get_settings
from career.db.session import SessionLocal, app_engine
from career.fingerprint import source_fingerprint
from career.logging_filters import install_secret_redaction
from career.salla.webhook import WebhookStatus, receive_webhook
from career.whatsapp.signature import verify_challenge
from career.whatsapp.webhook import WhatsAppIntakeStatus, receive_whatsapp_webhook

logging.basicConfig(level=get_settings().log_level)
# Scrub secrets from every log record before any external integration (§15.13).
install_secret_redaction()
logger = logging.getLogger("career")

# The interactive docs and the OpenAPI schema are a map of the attack surface;
# they exist only in development. Caddy's allow-list already hides them, but a
# second server or a misconfigured proxy should not be all that stands between
# the internet and a full route listing.
_DEV = get_settings().env in {"dev", "development", "local"}
app = FastAPI(
    title="Career Platform", version=__version__,
    docs_url="/docs" if _DEV else None,
    redoc_url="/redoc" if _DEV else None,
    openapi_url="/openapi.json" if _DEV else None,
)

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
    # Salla carries the event name in the JSON body ("event"), not in a header
    # (verified live 2026-07-15: install events arrived with no X-Salla-Event).
    # Keep the header as a fallback; the signature is over the raw body either
    # way, so routing on the body field grants no forgery power.
    try:
        parsed = json.loads(raw_body)
        event_type = str(parsed.get("event", "")) if isinstance(parsed, dict) else ""
    except ValueError:
        event_type = ""
    if not event_type:
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


#: /health is reachable from the public internet (Caddy allows it so Meta and
#: Salla can see the service is up), and each call opened a fresh Postgres AND
#: Redis connection — an unauthenticated way to exhaust the pool by holding
#: down refresh. The result is cached for a few seconds: Docker probes every
#: ten, so accuracy is unchanged, while a flood costs one connection pair per
#: window instead of one per request.
_HEALTH_TTL_SECONDS = 5.0
_health_cache: tuple[float, dict[str, bool]] | None = None

#: Computed once at import: the tree cannot change under a running process.
_SOURCE_FINGERPRINT = source_fingerprint()


def _health_checks() -> dict[str, bool]:
    global _health_cache
    import time

    now = time.monotonic()
    cached = _health_cache
    if cached is not None and now - cached[0] < _HEALTH_TTL_SECONDS:
        return cached[1]
    checks = {"database": _check_db(), "redis": _check_redis()}
    _health_cache = (now, checks)
    return checks


@app.get("/health")
def health() -> JSONResponse:
    checks = _health_checks()
    ok = all(checks.values())
    status_code = 200 if ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if ok else "degraded",
            "env": get_settings().env,
            "version": __version__,
            # The one field that reveals a container still serving code from
            # four days ago while the repository says otherwise. A content
            # hash discloses nothing: it is not a version, a path, or a
            # dependency list, and it cannot be reversed into source.
            "source": _SOURCE_FINGERPRINT,
            "checks": checks,
        },
    )
