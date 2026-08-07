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
import os
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
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

#: How long a customer's WhatsApp message may sit before this loop looks at it.
#:
#: THE DEADLINE IS CONVERSATIONAL, not contractual. Nothing in the store
#: promises a number of seconds; the thing being chased is a human who has just
#: pressed send and is watching the screen. Three seconds is under the delay
#: WhatsApp's own delivery ticks already cost, so the poll is not what the
#: customer perceives — the Claude turn is (a measured cycle with an LLM turn
#: is 18s). Halving this to 1.5s would move a number the customer cannot feel.
#:
#: WHAT IT COSTS, at the sizes this is priced for. `pending_whatsapp_events`
#: filters `provider + processing_status + (next_attempt_at IS NULL OR <= now)`
#: on `webhook_events`, and migration 0022 declined an index deliberately — so
#: every pass is a sequential scan, 28,800 of them a day.
#:
#:   today, 1 customer     283 rows live, ~10/day.  A scan is microseconds and
#:                         the table is one page set in cache. Free.
#:   10 customers          ~100 rows/day. ~100k rows in about three years.
#:   100 customers         ~1,000 rows/day. 100k in about a quarter.
#:   1000 customers        ~10,000 rows/day. 100k inside a fortnight, and
#:                         millions within the year — rows are redacted at 30
#:                         days (RAW_PAYLOAD_RETENTION_DAYS) but never deleted,
#:                         because the fingerprint is the idempotency record.
#:
#: So the number that must change with scale is NOT this one. 0022 names the
#: threshold (~100k rows) and the exact remedy: a partial index on
#: `(provider, received_at) WHERE processing_status = 'received'`, built
#: CONCURRENTLY outside a migration transaction. An index removes the scan
#: entirely; tightening the poll multiplies it. Loosening the poll to spare the
#: database would be paying for the wrong thing with the customer's wait.
POLL_SECONDS = 3.0

#: The housekeeping gate — and therefore the worst-case lateness of EVERY
#: clock swept below, because nothing else in this system reads them.
#:
#: The old note here justified this number against the §05 stall nudge alone.
#: That was true when the block held one sweep; it now drives eight, and a
#: number defended against the loosest deadline it happens to serve is how two
#: promises came to be measured 24× too coarsely in the first place. So the
#: division is written out, against the TIGHTEST:
#:
#:   لمّاح+ reply SLA        24h    4.2%   ← binding
#:   §05 stall nudge         24h    4.2%   ← binding
#:   forgotten support ticket 48h   2.1%
#:   72-hour start guarantee 72h    1.4%
#:   enrichment session TTL  72h    1.4%
#:   enrichment thin-role sweep 3d  1.4%
#:   outcome question       14d     0.3%
#:   the operator's evening window nudge — a four-hour band (window.py), so
#:   three firings land inside it even at the worst phase
#:
#: 4.2% of the binding deadline. The nightly-only cadence this replaced on
#: 2026-08-07 was 96% of it — a 24-hour promise very nearly measured after it
#: had already expired.
#:
#: WHY NOT TIGHTER, which is the question the fractions above invite. This
#: block is not free and its cost is not per-pass, it is per-customer-per-pass:
#: eight sequential passes over the active book inside ONE worker cycle, and
#: the enrichment sweep calls Claude once per due customer inside it.
#: career-worker.service's WatchdogSec=300 already carries the warning that
#: past roughly fifteen customers due in the same hour a single cycle can
#: exceed the watchdog ON MERIT and be killed as a wedge. Halving this interval
#: halves that headroom to buy 2% of a promise nobody measures in minutes. The
#: right move when the book grows is the one that unit names — give the sweep
#: its own timer — not a smaller number here.
#:
#: DURABLE ACROSS A RESTART, DELIBERATELY NOT ACROSS A REBOOT — the gate that
#: enforces that is :class:`HousekeepingGate`, and the paragraph that used to
#: sit here (a process-local float, «every restart runs the whole block
#: immediately, and that direction is the safe one») is the thing it replaced.
#: It was right about the direction and wrong about the bound: at RestartSec=5
#: a crash loop ran the whole block every five seconds, and two of the stanzas
#: below now PAGE.
REMINDER_SWEEP_SECONDS = 3600.0
_RIYADH = ZoneInfo("Asia/Riyadh")

#: Where the housekeeping block records that it ran. `/run` is tmpfs, and that
#: is the entire mechanism — see :class:`HousekeepingGate`.
HOUSEKEEPING_STAMP = "/run/career/worker-housekeeping"


class HousekeepingGate:
    """May the hourly block run now? — asked so a crash loop cannot answer yes.

    ── THE HAZARD THIS EXISTS FOR ───────────────────────────────────────────
    The gate used to be ``last_reminder_sweep = 0.0`` compared against
    ``time.monotonic()``, which on this host is ~2,170,615 — so the first pass
    of EVERY boot ran the whole block. Under ``Restart=always`` /
    ``RestartSec=5`` that is the whole block every five seconds for as long as
    the crash loop lasts, and ``StartLimitBurst=20`` in 600s bounds it at
    twenty only for a process that crashes FAST; one that dies every 45s never
    trips the limit and loops all night.

    Every idempotency guard downstream holds — one-way edges, advisory locks,
    a weekly report that claims its week in the database — so nothing
    double-stamps. What is not bounded is the PAGE: a pass killed between
    ``_alert(...)`` and its commit re-pages on the next boot, and that trade
    («duplicated by a crash, never lost») was priced when the block ran once a
    day. Two stanzas below now page — the 72-hour guarantee breach and the
    لمّاح+ SLA escalation — so at restart cadence the priced duplicate becomes
    a storm on the one channel whose whole value is that a message on it means
    something new happened. The cost is not only pages: the enrichment sweep
    calls Claude once per due customer, so a crash-looping worker also bought
    an LLM call per customer per five seconds.

    ── WHAT «CORRECT» IS HERE ───────────────────────────────────────────────
    Running housekeeping at boot is not the defect and must not be removed: a
    worker that has been down for six hours SHOULD catch up the moment it is
    back, and that is why the pattern exists at all. The defect is that a
    crash loop and a long outage were indistinguishable, because the only
    evidence was a float inside the process that had just died.

    So the evidence moves OUT of the process — into a stamp under ``/run``.
    Two lifetimes, and picking them apart is the whole design:

    * it survives the PROCESS, so twenty restarts in ten minutes see the same
      stamp and only the first of them sweeps;
    * it does NOT survive the HOST, because ``/run`` is tmpfs. A reboot means
      the machine was down, which is precisely the outage where catching up
      immediately is right. The same reading, in the same words, as
      ``scripts/alert_unit_failure.sh``: «a reboot is an intervention, and the
      first failure after one is news».

    Marked BEFORE the block runs, exactly as the float was set before it. A
    pass that dies halfway therefore defers the REST of that hour's
    housekeeping to the next hour instead of re-running it at every restart;
    the sweeps are idempotent and the nightly is a backstop for both paging
    ones, so the deferral costs at most the interval this file already prices
    as the worst-case lateness of every clock in the block.

    ── THE ALTERNATIVES, and why not them ───────────────────────────────────
    * **A row in the database**, the shape the weekly report already uses for
      its week. It is the strictly more durable answer and it was rejected:
      the extra durability is across a REBOOT, which is the one case where
      running immediately is the correct behaviour, so it buys nothing here —
      and it costs a migration, a table, and a write on the transactional path
      of a housekeeping tick. The weekly report needs the database because
      what it protects is a SEND that must happen once per week whatever
      happens to the host; what this protects is a rate, on this host, since
      this boot.
    * **Seeding the float with ``time.monotonic()``** so the block simply does
      not run at boot. One line, and it deletes the catch-up: a worker
      restarted after a six-hour outage would wait another hour, and every
      deploy would push every promise clock back by up to an hour. That is
      the failure this pattern exists to prevent, chosen on purpose.
    * **Jitter or a back-off at boot.** Reduces the storm without bounding it:
      twenty restarts still produce up to twenty passes, just less evenly.
    * **Sweeping at boot with the pages suppressed.** Quiet in a crash loop
      and quiet in a real outage too — it silences precisely the page that
      must not be lost, which is the entire subject.
    * **Leaning on ``StartLimitBurst``.** That is the bound today and it is
      twenty pages; a slower crash loop never reaches it at all.

    ── WHAT IT DOES NOT CLOSE ───────────────────────────────────────────────
    * A duplicate page is still POSSIBLE — a pass killed between the alert and
      its commit still re-pages on the next pass. This bounds the rate to one
      per interval; the ordering inside `promises.guarantee` chooses the
      duplicate over the silence, deliberately, and that choice is untouched.
    * If ``/run`` cannot be written the gate DEGRADES to a process-local timer
      seeded at start-up: no storm, and no catch-up either until the interval
      has passed. Chosen in that direction because the lost catch-up is
      bounded by the same hour the block already prices as its worst case and
      both paging sweeps keep a nightly backstop, while the storm is bounded
      by nothing. Detected in ``__init__`` — before the first pass — and not
      when the first ``mark`` fails, because a gate that finds out only after
      the block has run answers «no stamp, so catch up» on every boot and
      reproduces the storm exactly. It logs ERROR — the journal harvester puts
      those on the operator's error screen — so a silent host cannot silently
      lose it.
    * A wall clock that steps BACKWARDS over the stamp reads as «due» (doubt
      measures rather than stalls), so a clock step during a crash loop can
      still produce a second pass. Bounded by the step, not by the interval.
    """

    def __init__(
        self,
        *,
        path: str = HOUSEKEEPING_STAMP,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = Path(path)
        #: Wall clock for the STAMP (a durable, cross-process fact) and
        #: monotonic for the in-process fallback (immune to clock steps, and
        #: meaningless outside this process — which is exactly the split).
        self._wall = wall
        self._mono = mono
        self._local_since = mono()
        self._degraded = False
        # Proven, not assumed, and proven HERE: see the degradation note in
        # the class docstring for why finding this out at the first `mark`
        # would leave the storm exactly as it was.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            probe = self._tmp_path()
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            self._degrade("cannot write")

    def _tmp_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.{os.getpid()}")

    def due(self, interval: float) -> bool:
        """Has `interval` seconds passed since the last pass on this host?

        The interval is an ARGUMENT and not a field: this class is the
        mechanism (where the record lives and how long it lives), and the
        number is the policy that belongs beside the block it gates —
        REMINDER_SWEEP_SECONDS, whose whole justification is written up there
        against the deadlines it serves. It also keeps the call site readable
        as one sentence, which is what a reader and an AST probe both want.
        """
        # `_read` can itself degrade the gate (an unreadable stamp), and the
        # answer has to come from the mechanism that is still working — hence
        # the second check rather than an else.
        stamped = None if self._degraded else self._read()
        if not self._degraded:
            if stamped is None:
                # No stamp on this boot: either tmpfs was cleared by a reboot
                # or this host has never swept. Both mean «catch up now».
                return True
            age = self._wall() - stamped
            # A stamp from the future is not an age, it is a clock that
            # stepped. Doubt measures; it must never stall the block.
            return age >= interval or age < 0
        return self._mono() - self._local_since >= interval

    def mark(self) -> None:
        """Claim this interval BEFORE the block runs — see the class docstring
        for why the ordering is that way round."""
        self._local_since = self._mono()
        if self._degraded:
            return
        now = self._wall()
        tmp = self._tmp_path()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # The epoch first so the gate can read it, the human form after it
            # so `cat` answers «when did housekeeping last run» on a bad night.
            tmp.write_text(
                f"{now:.0f} {datetime.fromtimestamp(now, UTC).isoformat()}\n",
                encoding="utf-8",
            )
            # Atomic: a torn stamp would be an unreadable one, and an
            # unreadable one runs the block (see `_read`).
            os.replace(tmp, self._path)
        except OSError:
            tmp.unlink(missing_ok=True)
            self._degrade("cannot write")

    def _read(self) -> float | None:
        try:
            first = self._path.read_text(encoding="utf-8").split(" ", 1)[0]
        except FileNotFoundError:
            return None
        except OSError:
            self._degrade("cannot read")
            return None
        try:
            return float(first)
        except ValueError:
            # Not a timestamp, so not evidence. Nothing may be suppressed by a
            # stamp nobody can read; the next `mark` replaces it.
            logger.warning("housekeeping stamp %s is unreadable — sweeping",
                           self._path)
            return None

    def _degrade(self, why: str) -> None:
        if self._degraded:
            return
        self._degraded = True
        # ERROR and not WARNING: this silently changes when every clock in the
        # housekeeping block is first read after a restart, and the journal
        # harvester puts ERROR lines on the operator's screen.
        logger.error(
            "housekeeping stamp %s: %s — falling back to a process-local "
            "timer. The block will NOT catch up at boot: after a restart the "
            "first pass waits a full sweep interval, and the nightly run is "
            "the backstop for both promise sweeps until then",
            self._path, why, exc_info=True,
        )


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


def sweep_forgotten_tickets(
    session: Session, *, admin_client: TelegramAdminClient, now: datetime,
) -> int:
    """Lift the mute off every support ticket nobody has closed in 48h.

    The caller `telegram.console.release_forgotten_tickets` asked for and did
    not have. It has run off the operator's own console traffic until now,
    which — as its own docstring says — releases fastest for the customers who
    need it least: the operator who has stopped opening the watchtower is
    exactly the operator who forgot the ticket. This process is the one that
    is awake at 03:00, so the sweep runs here, hourly, whether he taps or not.
    Idempotent at any frequency: «open → released» is a one-way edge on the
    row, and the select now takes each row under ``FOR UPDATE … SKIP LOCKED``,
    so this caller and the console's own tap can read in the same millisecond
    and the ticket is still released once and paged once.

    ── THE ORDERING, which is a decision and not an inheritance ──────────────
    The release COMMITS, and the pages go out afterwards, best-effort and one
    at a time. So a Telegram outage across this minute loses a page that can
    never be raised again. That is a real loss and it is the right way round:

    * The two halves are not the same kind of thing. The release is the REPAIR
      — it is what lets the customer's next message raise a ticket and reach a
      human — and the page is the NOTICE. Ordering the notice first would make
      the customer's repair conditional on the operator's chat client being
      up, which is the same shape as the bug being fixed: a mute that outlives
      its reason because a mechanism nobody could see failed quietly.
    * Page-then-commit does not even buy what it looks like it buys. If the
      page lands and the commit then fails, the operator has been told we
      opened a line that is still shut — a false statement about our own
      ledger, on the one channel he trusts — and the next sweep releases and
      pages it again. It trades a lost notice for a wrong notice plus a
      duplicate, and a wrong notice is the more expensive of the two here,
      because his screen is the thing he acts on.
    * A lost page is RECOVERABLE by the operator and a lost release is not.
      The ticket stays on the console's queue either way, with its original
      age and its ⏳ released mark: the sweep never closes, never hides, never
      answers the customer. So the page is a nudge toward a screen that
      already says the same thing, while the release is the only thing that
      gives the customer his line back.

    That is also the trade `promises.career_session.escalate_overdue` makes
    with `escalated_at` and the trade the worker's own «دعم» branch makes with
    `support_events` — but the precedent is not the argument. The argument is
    the asymmetry above, and it would point the same way with no precedent at
    all. Where it would NOT point this way is a page carrying information that
    exists nowhere else; that is not this page.

    Per-alert `try` rather than one around the loop, for the same reason: a
    dead socket on the third page must not cost the fourth and fifth ones,
    which are about different customers.

    ── what a SECOND caller cost, and where it was paid ──────────────────────
    Adding this caller broke «pages at most once per ticket, ever» down to
    «once per ticket per sweeper»: `release_forgotten_tickets` read
    `status = 'open'` and wrote `released` with no status predicate between
    them, so this process and the admin bot's console tap could both read the
    same open row before either committed and both page it. The cost was
    bounded and it was the cheap half — a DUPLICATE NOTICE; the release stayed
    one-way, the ticket ended released exactly once, and no customer was
    affected, because the two writers agree on the value they write. It was
    still worth removing: a channel where a repeat means nothing new is a
    channel that stops being read.

    Fixed in the module that owns the query rather than worked around here —
    the select takes its rows with ``FOR UPDATE OF support_events SKIP
    LOCKED``, so a second sweeper finds nothing to do instead of finding the
    same work twice. SKIP and not wait matters for THIS caller in particular:
    a blocking sweep would hold the worker's transaction open behind an
    operator's screen tap, and this whole function is housekeeping that must
    never be in anything's way. Two sessions prove it in
    `tests/test_admin_console.py`.

    The whole sweep is a barrier too. It is housekeeping, and housekeeping
    that can take the message loop down with it is a bad trade at any hour —
    the cycle's own watchdog pet lives at the end of the loop body, so an
    escape from here would cost this cycle its heartbeat as well as its work.
    Returns the number of tickets RELEASED (pages attempted), never the number
    of pages delivered — this function cannot honestly claim the second.
    """
    from career.telegram.console import release_forgotten_tickets

    try:
        alerts = release_forgotten_tickets(session, now=now)
        session.commit()
    except Exception:  # noqa: BLE001 — a sweep never wedges the worker
        logger.error("forgotten-ticket sweep failed", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — a dead session must not loop either
            logger.error("forgotten-ticket rollback failed", exc_info=True)
        return 0
    for alert in alerts:
        try:
            admin_client.send_admin(alert)
        except Exception:  # noqa: BLE001 — the release is already committed
            # ERROR, not WARNING: the journal harvester forwards our ERROR
            # lines to the watchtower's error screen, and this is the one
            # notice in the system that cannot be raised a second time.
            logger.error("forgotten-ticket page failed — the release stands "
                         "and the ticket is still on the queue, but this "
                         "page is gone for good", exc_info=True)
    return len(alerts)


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
    #: A stamp under /run and not a local float, so «the first cycle of a boot
    #: sweeps» stays true of a worker that has been DOWN and stops being true
    #: of a worker that is merely restarting. See :class:`HousekeepingGate`:
    #: the direction is still toward freshness, the crash loop is no longer
    #: fresh twelve times a minute.
    housekeeping = HousekeepingGate()
    #: The one per-evening dedupe in this block that is NOT durable and is a
    #: SEND rather than a sweep, so a restart inside the 19:00–22:00 band costs
    #: the operator a duplicate line. Left as a local knowingly: the path is
    #: gated on `canary_test_phone`, the cost is one repeated sentence on his
    #: own channel bounded by the 20-starts-in-600s limit, and the durable fix
    #: is a claim row like `weekly_report`'s — which is the right shape only
    #: once this nudge is something a real customer's schedule depends on.
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
            if housekeeping.due(REMINDER_SWEEP_SECONDS):
                housekeeping.mark()
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
                # «ضمان البداية» — the 72-hour start guarantee, swept HOURLY.
                # The hour IS the promise: this sweep rode the 11:00 delivery
                # run alone, so a guarantee that ran out at 12:05 was measured
                # at 11:00 the NEXT morning — ~23 hours late on a promise the
                # store writes in hours. Here because this process is the one
                # that is awake at 03:00. `sweep_and_commit` commits its own
                # work and never raises (this block is a barrier); the nightly
                # keeps a backstop call and cannot double-page — its docstring
                # names every guard, including the advisory lock.
                from career.promises.guarantee import sweep_and_commit
                with Session(engine) as session:
                    gu_counts = sweep_and_commit(
                        session, now=now, admin_client=admin,
                    )
                if gu_counts["breached"] or gu_counts["met"] or gu_counts["alerted"]:
                    # never on "watching": every customer inside his first 72
                    # hours would log a line every hour, saying nothing
                    logger.info("guarantee sweep: %s", gu_counts)
                # «والرد خلال ٢٤ ساعة» — the لمّاح+ session SLA, swept HOURLY
                # for the same reason and with worse arithmetic: this one rode
                # the 11:00 delivery run ALONE, so a request that crossed 24
                # hours at 12:05 was ticketed at 11:00 the next morning — 23
                # hours late on a promise 24 hours long, i.e. very nearly
                # measured after it had already expired. Here for the same
                # reason as the guarantee above: this process is awake at
                # 03:00. `escalate_overdue_and_commit` commits its own work and
                # never raises (this block is a barrier); the nightly keeps a
                # backstop call and cannot double-escalate — `escalated_at` is
                # a one-way NULL→stamp edge, and a whole-pass advisory lock on
                # its OWN key (never the guarantee's) covers the two-in-flight
                # case. Its docstring also states what an hourly sweep still
                # does NOT measure: whether the operator then REPLIED.
                from career.promises.career_session import (
                    escalate_overdue_and_commit,
                )
                with Session(engine) as session:
                    sla_counts = escalate_overdue_and_commit(
                        session, now=now, admin_client=admin,
                    )
                if any(sla_counts.values()):
                    logger.info("career session SLA sweep: %s", sla_counts)
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
                # A forgotten support ticket mutes the customer it was raised
                # for — one open ticket per customer is the dedupe, so a ticket
                # nobody closed silences his direct line for as long as it
                # stays open. The console released them off the operator's own
                # taps and nothing released them while he slept. LAST in the
                # hourly block on purpose: it is housekeeping, and nothing
                # above it may be skipped by it.
                with Session(engine) as session:
                    released = sweep_forgotten_tickets(
                        session, admin_client=admin, now=now)
                if released:
                    logger.info("forgotten tickets released: %d", released)
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
