"""The watchtower console runner — long-polls Telegram and serves the operator.

Thin composition over the tested console: getUpdates → handle_update →
execute outcomes. The cursor lives in admin_bot_state (id=1) so restarts
neither replay nor lose commands, and it advances BEFORE the work so that no
single update can wedge the loop (see process_one_update, which owns that
trade-off). Live health probes are gathered here (the only place allowed to
touch systemd/Graph/SearchAPI directly) and injected into the pure console.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import IO, Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career import notify as sd_notify
from career.config import get_settings
from career.fingerprint import source_fingerprint
from career.logging_filters import install_secret_redaction
from career.onboarding.upload import HEALTH_KEY, build_scanner, scanner_health
from career.storage import FilesystemStorageAdapter
from career.telegram.admin import HttpTelegramAdminClient, TelegramSendError
from career.telegram.console import handle_update
from career.whatsapp.client import HttpWhatsAppClient

logger = logging.getLogger("career.admin_bot")

POLL_TIMEOUT = 50
ERROR_PAUSE = 5.0
#: Fallback Salla access-token expiry — overridden by SALLA_TOKEN_EXPIRES_AT.
_SALLA_EXPIRES_DEFAULT = datetime(2026, 7, 29, tzinfo=UTC)


def _salla_expiry(settings: Any) -> datetime:
    raw = getattr(settings, "salla_token_expires_at", "") or ""
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return _SALLA_EXPIRES_DEFAULT


class LiveProbes:  # pragma: no cover — live boundary, facts only
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def _systemd_active(self, unit: str) -> bool | None:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True, text=True, timeout=5,
            )
            return out.stdout.strip() == "active"
        except Exception:  # noqa: BLE001
            return None

    def _timer_next(self) -> str | None:
        try:
            out = subprocess.run(
                ["systemctl", "show", "career-engine-nightly.timer",
                 "-p", "NextElapseUSecRealtime", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            value = out.stdout.strip()
            if not value:
                return None
            # "Sat 2026-07-18 03:30:00 CEST" (server-local) → Arabic Riyadh
            local = datetime.strptime(
                " ".join(value.split()[1:3]), "%Y-%m-%d %H:%M:%S"
            ).astimezone()
            riyadh = local.astimezone(ZoneInfo("Asia/Riyadh"))
            days_ar = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس",
                       "الجمعة", "السبت", "الأحد"]
            return (f"{days_ar[riyadh.weekday()]}"
                    f" {riyadh.strftime('%H:%M')} بتوقيت الرياض")
        except Exception:  # noqa: BLE001
            return None

    def _meta_token_ok(self) -> bool | None:
        from urllib.error import HTTPError

        try:
            with urlopen(
                "https://graph.facebook.com/v21.0/"
                f"{self._settings.whatsapp_phone_number_id}"
                f"?fields=id&access_token={self._settings.whatsapp_access_token}",
                timeout=10,
            ) as resp:
                return bool(json.load(resp).get("id"))
        except HTTPError:
            return False        # Graph answered and refused — a real red
        except Exception:  # noqa: BLE001
            return None         # transient network trouble ≠ token failure

    def _searchapi(self) -> tuple[int, int] | None:
        try:
            with urlopen(
                "https://www.searchapi.io/api/v1/me?api_key="
                + self._settings.searchapi_api_key,
                timeout=10,
            ) as resp:
                account = json.load(resp).get("account") or {}
            return (
                int(account.get("monthly_allowance") or 0),
                int(account.get("remaining_credits") or 0),
            )
        except Exception:  # noqa: BLE001
            return None

    def _templates(self) -> dict[str, int] | None:
        try:
            url = (
                "https://graph.facebook.com/v21.0/"
                f"{self._settings.whatsapp_waba_id}/message_templates"
                "?fields=status&limit=50&access_token="
                + self._settings.whatsapp_access_token
            )
            with urlopen(url, timeout=10) as resp:
                rows = json.load(resp).get("data") or []
            counts: dict[str, int] = {}
            for row in rows:
                status = str(row.get("status"))
                counts[status] = counts.get(status, 0) + 1
            return counts
        except Exception:  # noqa: BLE001
            return None

    def _deployed_source(self) -> tuple[str, str] | None:
        """(what the API container runs, what this checkout holds).

        The watchtower runs on the host from the repository checkout, so the
        second half is simply this process's own package. The first half has
        to be asked of the API, because its image is baked at build time —
        which is precisely how thirty-two commits stayed committed and
        undeployed for four days behind an all-green health screen.
        """
        try:
            url = f"http://127.0.0.1:{self._settings.api_publish_port}/health"
            with urlopen(url, timeout=5) as resp:
                running = json.load(resp).get("source")
            if not running:
                return None          # an API too old to report it at all
            return (str(running), source_fingerprint())
        except Exception:  # noqa: BLE001 — a probe never breaks the screen
            return None

    def _backup_age_hours(self) -> float | None:
        try:
            out = subprocess.run(
                ["systemctl", "show", "career-backup.service",
                 "-p", "ExecMainExitTimestamp", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            value = out.stdout.strip()
            if not value or value == "n/a":
                return None
            last = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z")
            return (datetime.now() - last).total_seconds() / 3600.0
        except Exception:  # noqa: BLE001
            return None

    def error_lines(self) -> list[str]:
        """Recent ERROR lines from our two services — logger + static message
        only (our log discipline is PII-free; no payloads are ever logged)."""
        try:
            out = subprocess.run(
                ["journalctl", "-u", "career-worker.service",
                 "-u", "career-engine-nightly.service",
                 "-u", "career-admin-bot.service",
                 "--since", "-24h", "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=10,
            )
            lines = []
            for raw in out.stdout.splitlines():
                if raw.startswith("ERROR:"):
                    parts = raw.split(":", 2)
                    if len(parts) == 3:
                        lines.append(f"{parts[1]} · {parts[2].strip()}")
            return lines[-10:]
        except Exception:  # noqa: BLE001
            return []

    def _scanner(self) -> Any:
        """The §11 upload scanner, asked the same question the worker asks.

        Built from ``self._settings`` — the same two fields the worker passes
        to ``build_scanner`` — and not from a path typed again here, because a
        screen that reports a DIFFERENT scanner than the one guarding uploads
        is worse than no light at all: it is a green one over a control nobody
        is running. It is only a PING, so it is cheap enough to redraw on every
        tap of «تحديث» and it never sends a customer's bytes anywhere.
        """
        return scanner_health(build_scanner(
            self._settings.cv_scan_clamd_socket,
            timeout_s=self._settings.cv_scan_timeout_s,
        ))

    def collect(self) -> dict[str, Any]:
        days_left = (_salla_expiry(self._settings) - datetime.now(UTC)).days
        return {
            HEALTH_KEY: self._scanner(),
            "worker_active": self._systemd_active("career-worker.service"),
            "timer_next": self._timer_next(),
            "meta_token_ok": self._meta_token_ok(),
            "salla_days_left": days_left,
            "searchapi": self._searchapi(),
            "templates": self._templates(),
            "backup_age_hours": self._backup_age_hours(),
            "deployed_source": self._deployed_source(),
        }


def _load_offset(session: Session) -> int:
    return int(session.execute(
        sql_text("SELECT update_offset FROM admin_bot_state WHERE id = 1")
    ).scalar_one())


def _store_offset(session: Session, offset: int) -> None:
    session.execute(
        sql_text("UPDATE admin_bot_state SET update_offset = :o,"
                 " updated_at = now() WHERE id = 1"),
        {"o": offset},
    )


#: What the operator sees when an update was dropped. No detail: the screen is
#: not the place for a traceback, and the journal already has it.
_SKIPPED_AR = (
    "⚠️ أمر في البرج فشل تنفيذه وتم تخطيه — أعد المحاولة، "
    "وإذا تكرر راجع السجل"
)


def _skip_poisoned_update(client: HttpTelegramAdminClient) -> None:
    """Say out loud that an update was dropped.

    Advancing the cursor past a failure is only defensible if the failure is
    impossible to miss, so this is the other half of that decision — and it is
    itself best-effort: if Telegram is the thing that is broken, the journal
    line above still stands and the loop still moves on.

    «Best-effort» has to mean every way this can fail, not the one we named.
    ``TelegramSendError`` is raised for a refusal Telegram ARTICULATED; the
    transport underneath is ``requests``, and a ``ConnectionError`` or a
    ``ReadTimeout`` — the normal weather on a long-poll socket — is neither
    caught by that name nor a reason to abandon the batch. It escaped from
    inside ``process_one_update``'s own except block, which is the one place
    an exception is least expected and most expensive: the announcement never
    happened, the cycle died, and the at-most-once bargain («we drop updates,
    but never quietly») was broken in exactly the case it was made for.
    """
    try:
        client.send_admin(_SKIPPED_AR)
    except Exception:  # noqa: BLE001 — see above: the notice is best-effort
        logger.warning("skip notice failed", exc_info=True)


def deliver_outcomes(
    client: HttpTelegramAdminClient, outcomes: list[Any]
) -> None:
    """Push one update's screens to Telegram; one failure never eats the rest.

    Lifted out of the poll loop for the same reason ``process_one_update``
    was: it needs to be testable, because the failure it handles is a network
    failure and those do not happen on demand.

    The narrow ``except TelegramSendError`` this replaces was written for the
    routine case — «message is not modified» on an unchanged refresh, which is
    an answer, not a fault — and quietly assumed the transport never failed
    any other way. It does: ``requests`` raises ``ConnectionError`` and
    ``ReadTimeout`` on its own, those names are not ``TelegramSendError``, and
    one of them aborted the whole poll cycle. The work was already committed
    and the cursor already past it, so the operator lost the screens for every
    update after the failure with nothing to say so.

    The two failures are still logged differently on purpose. A refusal
    Telegram spelled out is routine and stays at INFO; anything else is an
    ERROR, because ERROR is the level the journal harvester forwards to the
    ⚠️ الأخطاء screen and «the console cannot reach Telegram» is the kind of
    fact the operator has to be able to find.
    """
    for outcome in outcomes:
        try:
            if outcome.kind == "send":
                client.send_screen(
                    outcome.text, outcome.keyboard,
                    force_reply=outcome.force_reply,
                )
            elif outcome.kind == "edit" and outcome.message_id:
                client.edit_screen(
                    outcome.message_id, outcome.text, outcome.keyboard
                )
            elif outcome.kind == "ack" and outcome.callback_query_id:
                client.answer_callback(
                    outcome.callback_query_id, outcome.text
                )
        except TelegramSendError:
            logger.info("outcome delivery skipped", exc_info=True)
        except Exception:  # noqa: BLE001 — a dead socket is not a dead console
            logger.error("outcome delivery failed", exc_info=True)


def process_one_update(
    engine: Any,
    update: dict[str, Any],
    *,
    update_id: int,
    handler: Callable[[Session, dict[str, Any]], list[Any]],
    on_skip: Callable[[], None],
) -> list[Any]:
    """Advance the cursor, THEN do the work. Returns the outcomes to deliver.

    The cursor used to move last, inside the same transaction as the work, so
    anything escaping the handler — or a commit that failed after the outcomes
    were built — left the offset where it was and ``getUpdates(offset + 1)``
    handed back the very same update five seconds later, forever. The console
    is the operator's only window into the system and one tap could close it
    until someone restarted the service. Worse than the wedge: a handler that
    sends before it fails (a resend re-runs a customer's delivery) repeated
    that send at a real customer's phone every five seconds.

    The cost is deliberate and is the smaller one. An update whose processing
    genuinely failed is DROPPED rather than retried — but every screen here is
    operator-initiated and re-tappable, the keyboard is still on their phone,
    and the skip is announced and journalled. At-most-once on a control
    channel beats at-least-once with real customer sends behind it, and it
    beats a dead console outright.

    The two halves are separate transactions on purpose: a cursor that only
    advances when the work commits is exactly the coupling being removed here.
    The outcomes are built inside the work's transaction and describe what it
    wrote, so they are returned only once that transaction has landed — a
    screen drawn over a rolled-back commit is a lie the operator acts on.
    """
    with Session(engine) as session:
        _store_offset(session, update_id)
        session.commit()
    try:
        with Session(engine) as session:
            outcomes = handler(session, update)
            session.commit()
    except Exception:  # noqa: BLE001 — one update, never the loop
        logger.error("watchtower update failed — skipped", exc_info=True)
        on_skip()
        return []
    return outcomes


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


def main() -> None:  # pragma: no cover — live runner over tested parts
    _lock = _acquire_single_instance_lock("admin-bot")  # noqa: F841
    logging.basicConfig(level=logging.INFO)
    install_secret_redaction()  # AFTER basicConfig — arms the handler it made
    settings = get_settings()
    if not (settings.telegram_admin_bot_token and settings.telegram_admin_chat_id):
        raise SystemExit("telegram admin credentials are empty")

    engine = create_engine(settings.owner_database_url, future=True)
    client = HttpTelegramAdminClient(
        settings.telegram_admin_bot_token, settings.telegram_admin_chat_id
    )
    # the ONLY side-effecting client the console needs: «إعادة إرسال»
    # re-attempts a held bundle and «رد على العميل» sends one operator-typed
    # message. Built here (same style as the worker loop) and injected — the
    # console never constructs transports itself.
    whatsapp = HttpWhatsAppClient(
        settings.whatsapp_access_token,
        settings.whatsapp_phone_number_id,
        storage=FilesystemStorageAdapter(settings.storage_root),
    )
    probes = LiveProbes(settings)
    try:
        # `/start` on a line of its own: inside the Arabic line the operator's
        # client reversed it, and alone it is tappable.
        client.send_admin("🏰 برج المراقبة جاهز — أرسل\n/start")
    except TelegramSendError:
        logger.warning("watchtower heartbeat failed", exc_info=True)
    logger.info("watchtower up — long-polling")

    # ── the heartbeat (career-admin-bot.service, WatchdogSec) ─────────────
    # After start-up, before the first poll. The asymmetry that makes this
    # unit matter more than it looks: when the WORKER wedges, customers stop
    # being answered and someone eventually notices; when the WATCHTOWER
    # wedges, the operator's screen just goes quiet — and a quiet screen is
    # what a healthy day looks like.
    sd_notify.ready()
    armed = sd_notify.watchdog_interval_s()
    if armed:
        logger.info("systemd watchdog armed: a poll cycle must complete every "
                    "%.0fs or this watchtower is killed and restarted", armed)
    else:
        logger.warning("no systemd watchdog on this process — a wedged poll "
                       "will NOT be noticed by anything")

    while True:
        try:
            with Session(engine) as session:
                offset = _load_offset(session)
            updates = client.get_updates(offset + 1, timeout=POLL_TIMEOUT)
            for update in updates:
                raw_id = update.get("update_id")
                if raw_id is None:
                    # The cursor may only ever move FORWARD. This defaulted to
                    # the CURRENT offset, which for any update after the first
                    # in a batch is a step BACKWARDS — and a cursor that goes
                    # backwards re-fetches updates this same loop has already
                    # handled, which for «إعادة إرسال» is a second real
                    # delivery to a real customer. Telegram always sends the
                    # field; a shape we cannot place is skipped, not guessed.
                    logger.error("watchtower update carried no update_id — "
                                 "skipped rather than moving the cursor back")
                    continue
                update_id = int(raw_id)
                outcomes = process_one_update(
                    engine, update, update_id=update_id,
                    handler=lambda session, upd: handle_update(
                        session, upd,
                        admin_chat_id=settings.telegram_admin_chat_id,
                        probes=probes, now=datetime.now(UTC),
                        whatsapp_client=whatsapp,
                    ),
                    on_skip=lambda: _skip_poisoned_update(client),
                )
                deliver_outcomes(client, outcomes)
            # THE LAST STATEMENT OF THE CYCLE — see run_worker_loop.py for the
            # full reasoning. Here it means: getUpdates RETURNED (an empty
            # long poll is a completed cycle, which is why a quiet day still
            # beats every ~50s) and every update it carried was delivered. A
            # poll that blocks forever, a Telegram token that stopped being
            # accepted, a database the cursor cannot be read from — none of
            # them reach this line, and none of them stop this loop either.
            sd_notify.watchdog()
        except Exception:  # noqa: BLE001 — the console must survive anything
            logger.error("watchtower cycle failed", exc_info=True)
            time.sleep(ERROR_PAUSE)


if __name__ == "__main__":
    main()
