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
from decimal import Decimal
from typing import IO
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from career.config import get_settings
from career.db.models import CustomerChannel
from career.logging_filters import install_secret_redaction
from career.onboarding.achievement_render import (
    AnthropicAchievementRenderer,
    AnthropicExamplesWriter,
)
from career.onboarding.extraction import AnthropicExtractor
from career.onboarding.orchestrator import Deps, send_due_reminders
from career.salla.client import HttpSallaClient
from career.salla.provisioning import process_pending_webhooks
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import HttpTelegramAdminClient, TelegramAdminClient
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


def _salla_pricing(raw: str) -> dict[str, tuple[Decimal, str]]:
    import json

    try:
        parsed = json.loads(raw or "{}")
        return {
            str(k): (Decimal(str(v[0])), str(v[1]))
            for k, v in parsed.items()
        }
    except (ValueError, LookupError, ArithmeticError):
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




def _acquire_single_instance_lock(name: str) -> IO[str]:
    """One instance per service — a second copy exits loudly instead of
    double-processing the queue (audit fix: no lock existed)."""
    import fcntl

    path = f"/run/lock/career-{name}.lock"
    handle = open(path, "w")  # noqa: SIM115 — held for process lifetime
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"another {name} instance holds {path} — refusing to start"
        ) from None
    return handle


def main() -> None:  # pragma: no cover — the C7.8 live runner
    _lock = _acquire_single_instance_lock("worker")  # noqa: F841
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
        achievement_renderer=AnthropicAchievementRenderer(
            api_key=settings.anthropic_api_key),
        examples_writer=AnthropicExamplesWriter(
            api_key=settings.anthropic_api_key),
    )
    admin: TelegramAdminClient
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
    pricing = _salla_pricing(settings.salla_product_pricing)
    last_reminder_sweep = 0.0
    last_window_nudge: date | None = None
    last_weekly_report: date | None = None

    while True:
        try:
            with Session(engine) as session:
                counts = process_pending_whatsapp(
                    session, whatsapp_client=whatsapp, admin_client=admin,
                    now=datetime.now(UTC), onboarding=deps,
                )
                salla_counts = process_pending_webhooks(
                    session, salla_client=salla, product_catalog=catalog,
                    admin_client=admin,
                    whatsapp_number_e164=settings.whatsapp_number_e164,
                    whatsapp_client=whatsapp,
                    expected_pricing=pricing,
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
                # weekly report — Sunday morning (Riyadh), once per week
                riyadh_now = now.astimezone(_RIYADH)
                if (riyadh_now.weekday() == 6 and 6 <= riyadh_now.hour < 12
                        and last_weekly_report != today):
                    last_weekly_report = today
                    from career.telegram.console import (
                        business_data,
                        week_day_states,
                    )
                    from career.telegram.weekly_report import format_weekly_report
                    with Session(engine) as session:
                        biz = business_data(session, "7", now=now)
                        states_week = week_day_states(session, now=now)
                    admin.send_admin(format_weekly_report(
                        riyadh_now.date(), biz, states_week
                    ))
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.error("worker cycle failed", exc_info=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
