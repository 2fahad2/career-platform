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
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from career.config import get_settings
from career.db.models import CustomerChannel
from career.logging_filters import install_secret_redaction
from career.onboarding.extraction import AnthropicExtractor
from career.onboarding.orchestrator import Deps, send_due_reminders
from career.salla.client import HttpSallaClient
from career.salla.provisioning import process_pending_webhooks
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import HttpTelegramAdminClient
from career.whatsapp.client import HttpWhatsAppClient
from career.whatsapp.window import window_reminder_due
from career.whatsapp.worker import process_pending_whatsapp

logger = logging.getLogger("career.worker_loop")

POLL_SECONDS = 3.0
REMINDER_SWEEP_SECONDS = 3600.0  # §05 stall nudges — hourly is plenty for 24h
_RIYADH = ZoneInfo("Asia/Riyadh")


def _salla_catalog(raw: str) -> dict[str, str]:
    import json

    try:
        parsed = json.loads(raw or "{}")
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
    if settings.telegram_admin_bot_token and settings.telegram_admin_chat_id:
        admin = HttpTelegramAdminClient(
            settings.telegram_admin_bot_token, settings.telegram_admin_chat_id
        )
        try:
            admin.send_admin("🟢 عامل المحادثة انطلق")
        except Exception:  # noqa: BLE001 — heartbeat only
            logger.warning("admin heartbeat failed", exc_info=True)
            admin = JournalAdminClient()
    else:
        admin = JournalAdminClient()
    logger.info("worker loop up — polling every %.0fs", POLL_SECONDS)

    salla = HttpSallaClient(settings.salla_api_key)
    catalog = _salla_catalog(settings.salla_product_catalog)
    last_reminder_sweep = 0.0
    last_window_nudge: date | None = None

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
            if time.monotonic() - last_reminder_sweep >= REMINDER_SWEEP_SECONDS:
                last_reminder_sweep = time.monotonic()
                now = datetime.now(UTC)
                with Session(engine) as session:
                    nudged = send_due_reminders(session, deps=deps, now=now)
                    session.commit()
                if nudged:
                    logger.info("stall reminders sent: %d", nudged)
                # canary evening nudge: keep the operator's own 24h window
                # open for tomorrow's dawn delivery (template-independence)
                today = now.astimezone(_RIYADH).date()
                if settings.canary_test_phone and last_window_nudge != today:
                    with Session(engine) as session:
                        ch = session.execute(
                            select(CustomerChannel).where(
                                CustomerChannel.phone_e164
                                == settings.canary_test_phone
                            )
                        ).scalars().first()
                        # read INSIDE the session — rows expire on close
                        due = ch is not None and window_reminder_due(
                            last_inbound_at=ch.last_inbound_at,
                            opt_out_at=ch.opt_out_at, now=now,
                        )
                    if due:
                        last_window_nudge = today
                        admin.send_admin(
                            "🔔 نافذة واتساب حقتك بتكون مقفولة وقت تسليم "
                            "بكرة الفجر — أرسل أي رسالة لرقم الخدمة الآن "
                            "عشان توصلك الفرص مباشرة"
                        )
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.error("worker cycle failed", exc_info=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
