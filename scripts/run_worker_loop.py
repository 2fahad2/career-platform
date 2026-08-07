"""The live conversation worker loop — C7.8 canary runner.

Polls pending WhatsApp webhook events every few seconds and feeds them to the
tested worker with REAL boundaries: the Graph client (documents resolve from
tenant storage), the REAL Claude extractor, the §11 upload pipeline with the
scanner this host actually has, and the onboarding orchestrator. The admin
channel logs to the journal (TEN codes only) until Telegram is wired.

It also runs the P0-8 boot verification before the first poll: the selling
environment checked against the database, loudly, without ever refusing to
start — see :func:`career.engine.cli.report_environment` for why the answer
is «warn» and not «fail hard».
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime
from typing import IO
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from career import notify as sd_notify
from career.config import get_settings
from career.db.models import CustomerChannel
from career.engine.cli import (
    parse_product_catalog,
    parse_product_pricing,
    report_environment,
)
from career.logging_filters import install_secret_redaction
from career.onboarding.achievement_render import (
    AnthropicAchievementRenderer,
    AnthropicExamplesWriter,
)
from career.onboarding.bullet_panel import AnthropicBulletJudge
from career.onboarding.extraction import AnthropicExtractor
from career.onboarding.intent import AnthropicIntentClassifier
from career.onboarding.orchestrator import Deps, send_due_reminders
from career.onboarding.upload import build_scanner, scanner_health
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


#: P0-8: both parsers moved to ``career.engine.cli`` so the boot check and
#: the loop can never read the same environment two different ways — the
#: check is only worth having if it inspects the map the loop actually uses.
_salla_catalog = parse_product_catalog
_salla_pricing = parse_product_pricing


class JournalAdminClient:
    """Admin messages → the journal (PII-free by contract, §15.13)."""

    def send_admin(self, text: str) -> str:
        logger.info("ADMIN: %s", text)
        return "journal"


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
    logging.basicConfig(level=logging.INFO)
    install_secret_redaction()  # AFTER basicConfig — arms the handler it made
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
    # The operator channel is resolved BEFORE the deps that carry it. The
    # funnel's consent stall pages the operator only «when admin_client is
    # wired» (funnel.flow._escalate_consent), and this construction did not
    # wire it — so the one escalation a conversation can raise on its own
    # opened its support_events ticket, logged its WARNING, and reached
    # nobody. The ticket table has no reader yet either, which is what made
    # the missing line invisible rather than merely quiet.
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
    # The §11 malware scanner this host actually has. What stood here was a
    # CanaryScanner whose scan() returned None — the value that means «clean» —
    # for every byte of every upload, so the pipeline presented a control it
    # was not running and every cv_uploads row read `clean`. build_scanner
    # returns a real clamd client when a socket is configured and the honest
    # UnconfiguredScanner when none is; the latter RAISES rather than passing,
    # and validate_upload's declared policy stamps the row `unscanned`.
    scanner = build_scanner(
        settings.cv_scan_clamd_socket, timeout_s=settings.cv_scan_timeout_s
    )
    deps = Deps(
        whatsapp_client=whatsapp,
        scanner=scanner,
        storage=storage,
        extractor=AnthropicExtractor(api_key=settings.anthropic_api_key),
        achievement_renderer=AnthropicAchievementRenderer(
            api_key=settings.anthropic_api_key),
        examples_writer=AnthropicExamplesWriter(
            api_key=settings.anthropic_api_key),
        bullet_judge=AnthropicBulletJudge(api_key=settings.anthropic_api_key),
        intent_classifier=AnthropicIntentClassifier(
            api_key=settings.anthropic_api_key),
        # The SAME client the worker pages «دعم» on — one operator, one
        # channel. A JournalAdminClient fallback is still a wiring: the
        # escalation lands in the journal, which the watchtower's error
        # screen harvests, instead of nowhere.
        admin_client=admin,
    )
    logger.info("worker loop up — polling every %.0fs", POLL_SECONDS)

    salla = HttpSallaClient(settings.salla_api_key)
    catalog = _salla_catalog(settings.salla_product_catalog)
    pricing = _salla_pricing(settings.salla_product_pricing)
    # AUDIT ح-4 / P0-8: the triple-match gate fails closed per product, and
    # what used to be announced at boot was only the coverage hole (a
    # cataloged product with no price). That missed the worse shape: a
    # catalog that is FULLY covered and entirely wrong — for twelve days it
    # sold a retired plan at a retired price, looking configured the whole
    # time. The check now compares the environment against the database and
    # the canonical sale table, and it warns rather than refusing to start:
    # this process is serving customers who already paid (report_environment).
    # alert=False: this unit is Restart=always/RestartSec=5, so a crash loop
    # would put the same alert on his phone every five seconds. The ERROR
    # lines still reach him through the journal harvester, and the nightly
    # oneshot sends the one Telegram copy a day.
    with Session(engine) as session:
        report_environment(settings=settings, session=session,
                           admin_client=admin, alert=False)
    # The second thing this boot says out loud, for the same reason as the
    # first: a control that is not running must not look like one that is. The
    # environment report above catches a catalog that is configured and wrong;
    # this catches a scanner that is configured and dead — a socket path with a
    # typo, or a clamd that failed to come back after a host reboot — before a
    # customer's upload is the thing that discovers it.
    #
    # Logged, never alerted, exactly like report_environment's alert=False just
    # above: this unit is Restart=always with RestartSec=5, so an alerting boot
    # check would put the same message on his phone twelve times a minute
    # through a crash loop. The WARNING still reaches him — the journal
    # harvester feeds the watchtower's error screen — and the health screen
    # renders the live state on every tap.
    boot_scan_health = scanner_health(scanner)
    if boot_scan_health.ok:
        logger.info("upload scanner ready: %s", boot_scan_health.engine)
    else:
        # detail is a PII-free slug by contract (§15.13) — safe to journal
        logger.warning(
            "upload scanner is NOT scanning: %s (%s) — uploads are recorded"
            " as unscanned",
            boot_scan_health.state, boot_scan_health.detail,
        )
    # ── the heartbeat (career-worker.service, WatchdogSec) ────────────────
    # READY=1 goes here and nowhere else: AFTER the boot checks, so systemd
    # calls this unit «started» only once the selling environment and the
    # upload scanner have actually been looked at, and BEFORE the loop, so the
    # watchdog deadline starts counting from the first cycle. One send only —
    # sd_notify's READY is a start-up edge, not a heartbeat.
    #
    # Outside systemd (the C7.8 canary by hand, the logging-wiring probes)
    # NOTIFY_SOCKET is absent, every call below is a no-op, and nothing about
    # this loop changes. See src/career/notify.py for the two incidents that
    # bought this.
    sd_notify.ready()
    armed = sd_notify.watchdog_interval_s()
    if armed:
        # Printed so the operator can prove the watchdog is real from the
        # journal after a deploy, instead of trusting the unit file.
        logger.info("systemd watchdog armed: a cycle must complete every "
                    "%.0fs or this worker is killed and restarted", armed)
    else:
        logger.warning("no systemd watchdog on this process — a wedged cycle "
                       "will NOT be noticed by anything")
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
                # F-ENRICH housekeeping: 3-day sweep + 72h auto-close
                from career.onboarding.enrichment import run_hourly_sweep
                with Session(engine) as session:
                    enr_counts = run_hourly_sweep(
                        session, whatsapp_client=whatsapp,
                        examples_writer=deps.examples_writer, now=now,
                    )
                    session.commit()
                if any(enr_counts.values()):
                    logger.info("enrichment sweep: %s", enr_counts)
                # §20: ask what happened to applications made two weeks ago.
                # Hourly is right — it only ever fires inside an open window,
                # so it has to be looking whenever the customer writes to us.
                from career.cv.outcome_followup import sweep_outcome_questions
                with Session(engine) as session:
                    oc_counts = sweep_outcome_questions(
                        session, whatsapp_client=whatsapp, now=now,
                    )
                    session.commit()
                if any(oc_counts.values()):
                    logger.info("outcome questions: %s", oc_counts)
                # canary evening nudge: keep the operator's own 24h window
                # open for tomorrow's 11:00 delivery (template-independence)
                today = now.astimezone(_RIYADH).date()
                if settings.canary_test_phone and last_window_nudge != today:
                    with Session(engine) as session:
                        from career.whatsapp.phones import phone_variants

                        ch = session.execute(
                            select(CustomerChannel).where(
                                CustomerChannel.phone_e164.in_(
                                    phone_variants(settings.canary_test_phone)
                                )
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
                            "بكرة — أرسل أي رسالة لرقم الخدمة الآن عشان "
                            "توصلك الفرص مباشرة"
                        )
                # Weekly report — the week is claimed in the DATABASE, not in
                # this process. `last_weekly_report` was a local of main(), and
                # this unit is Restart=always: every restart inside the old
                # Sunday 06:00–12:00 window reset it and sent the whole report
                # again (a deploy with three restarts sent three), while an
                # outage across that window dropped the week in total silence
                # because nothing outside the process remembered it was owed.
                # The claim is idempotent per week, so a future
                # career-weekly-report.timer can call the same code path and
                # only one of the two will ever send.
                riyadh_now = now.astimezone(_RIYADH)
                from career.telegram import weekly_report as weekly

                week_ending = weekly.due_week_ending(riyadh_now)
                with Session(engine) as session:
                    previous = weekly.weekly_report_marker(session)
                    claimed = weekly.claim_weekly_report(
                        session, week_ending=week_ending)
                if claimed:
                    from career.telegram.console import (
                        business_data,
                        week_day_states,
                    )
                    with Session(engine) as session:
                        biz = business_data(session, "7", now=now)
                        states_week = week_day_states(session, now=now)
                    try:
                        admin.send_admin(weekly.format_weekly_report(
                            week_ending, biz, states_week,
                            covering_through=riyadh_now.date(),
                        ))
                    except Exception:  # noqa: BLE001 — an unsent week is owed
                        logger.error("weekly report send failed — releasing "
                                     "the claim for the next sweep",
                                     exc_info=True)
                        with Session(engine) as session:
                            weekly.release_weekly_report(
                                session, restore_to=previous)
            # THE LAST STATEMENT OF THE CYCLE, and it has to stay that way.
            # Not in a `finally`, not in the `except` below: the two failures
            # this exists to catch — 100s of a restarting Postgres on
            # 2026-08-04, and every cycle since 2026-08-06 06:58 raising on a
            # column staging does not have — both kept this loop LOOPING. A
            # dog petted by the loop is a dog petted by the incident. Only a
            # cycle that got all the way here processed anything.
            sd_notify.watchdog()
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.error("worker cycle failed", exc_info=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
