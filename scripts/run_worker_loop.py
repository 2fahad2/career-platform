"""The live conversation worker loop — C7.8 canary runner.

Polls pending WhatsApp webhook events every few seconds and feeds them to the
tested worker with REAL boundaries: the Graph client (documents resolve from
tenant storage), the REAL Claude extractor, and the onboarding orchestrator.
The admin channel logs to the journal (TEN codes only) until Telegram is
wired. The §11 upload pipeline stays fully armed — the canary scanner only
stands in for the external AV hook, which was never in scope.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from career.config import get_settings
from career.logging_filters import install_secret_redaction
from career.onboarding.extraction import AnthropicExtractor
from career.onboarding.orchestrator import Deps
from career.salla.client import HttpSallaClient
from career.salla.provisioning import process_pending_webhooks
from career.storage import FilesystemStorageAdapter
from career.whatsapp.client import HttpWhatsAppClient
from career.whatsapp.worker import process_pending_whatsapp

logger = logging.getLogger("career.worker_loop")

POLL_SECONDS = 3.0


def _salla_catalog() -> dict[str, str]:
    import json
    import os

    raw = os.environ.get("SALLA_PRODUCT_CATALOG", "{}")
    try:
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}
    except ValueError:
        return {}


class JournalAdminClient:
    """Admin messages → the journal (PII-free by contract, §15.13)."""

    def send_admin(self, text: str) -> str:
        logger.info("ADMIN: %s", text)
        return "journal"


class CanaryScanner:
    """Stands in for the external AV hook only — every §11 structural check
    (magic sniff, PDF/DOCX inspection, sanitization, sandboxed extraction)
    still runs at full strength."""

    def scan(self, data: bytes) -> str | None:
        return None


def main() -> None:  # pragma: no cover — the C7.8 live runner
    install_secret_redaction()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.whatsapp_access_token:
        raise SystemExit("WHATSAPP_ACCESS_TOKEN is empty")

    engine = create_engine(settings.owner_database_url, future=True)
    storage = FilesystemStorageAdapter(settings.storage_root)
    whatsapp = HttpWhatsAppClient(
        settings.whatsapp_access_token,
        settings.whatsapp_phone_number_id,
        storage=storage,
    )
    deps = Deps(
        whatsapp_client=whatsapp,
        scanner=CanaryScanner(),
        storage=storage,
        extractor=AnthropicExtractor(api_key=settings.anthropic_api_key),
    )
    admin = JournalAdminClient()
    logger.info("worker loop up — polling every %.0fs", POLL_SECONDS)

    salla = HttpSallaClient(settings.salla_api_key)
    catalog = _salla_catalog()

    while True:
        try:
            with Session(engine) as session:
                counts = process_pending_whatsapp(
                    session, whatsapp_client=whatsapp, admin_client=admin,
                    now=datetime.now(UTC), onboarding=deps,
                )
                salla_counts = process_pending_webhooks(
                    session, salla_client=salla, product_catalog=catalog,
                )
            if counts.get("messages") or counts.get("statuses"):
                logger.info("processed: %s", counts)
            if salla_counts:
                logger.info("salla processed: %d orders", len(salla_counts))
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.error("worker cycle failed", exc_info=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
