"""The watchtower console runner — long-polls Telegram and serves the operator.

Thin composition over the tested console: getUpdates → handle_update →
execute outcomes. The cursor lives in admin_bot_state (id=1) so restarts
neither replay nor lose commands. Live health probes are gathered here (the
only place allowed to touch systemd/Graph/SearchAPI directly) and injected
into the pure console.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.config import get_settings
from career.logging_filters import install_secret_redaction
from career.telegram.admin import HttpTelegramAdminClient, TelegramSendError
from career.telegram.console import handle_update

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

    def collect(self) -> dict[str, Any]:
        days_left = (_salla_expiry(self._settings) - datetime.now(UTC)).days
        return {
            "worker_active": self._systemd_active("career-worker.service"),
            "timer_next": self._timer_next(),
            "meta_token_ok": self._meta_token_ok(),
            "salla_days_left": days_left,
            "searchapi": self._searchapi(),
            "templates": self._templates(),
            "backup_age_hours": self._backup_age_hours(),
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


def main() -> None:  # pragma: no cover — live runner over tested parts
    install_secret_redaction()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not (settings.telegram_admin_bot_token and settings.telegram_admin_chat_id):
        raise SystemExit("telegram admin credentials are empty")

    engine = create_engine(settings.owner_database_url, future=True)
    client = HttpTelegramAdminClient(
        settings.telegram_admin_bot_token, settings.telegram_admin_chat_id
    )
    probes = LiveProbes(settings)
    try:
        client.send_admin("🏰 برج المراقبة جاهز — أرسل /start")
    except TelegramSendError:
        logger.warning("watchtower heartbeat failed", exc_info=True)
    logger.info("watchtower up — long-polling")

    while True:
        try:
            with Session(engine) as session:
                offset = _load_offset(session)
            updates = client.get_updates(offset + 1, timeout=POLL_TIMEOUT)
            for update in updates:
                update_id = int(update.get("update_id", offset))
                with Session(engine) as session:
                    outcomes = handle_update(
                        session, update,
                        admin_chat_id=settings.telegram_admin_chat_id,
                        probes=probes, now=datetime.now(UTC),
                    )
                    _store_offset(session, update_id)
                    session.commit()
                for outcome in outcomes:
                    try:
                        if outcome.kind == "send":
                            client.send_screen(outcome.text, outcome.keyboard)
                        elif outcome.kind == "edit" and outcome.message_id:
                            client.edit_screen(
                                outcome.message_id, outcome.text, outcome.keyboard
                            )
                        elif outcome.kind == "ack" and outcome.callback_query_id:
                            client.answer_callback(
                                outcome.callback_query_id, outcome.text
                            )
                    except TelegramSendError:
                        # e.g. "message is not modified" on an unchanged
                        # refresh — never kills the loop
                        logger.info("outcome delivery skipped", exc_info=True)
        except Exception:  # noqa: BLE001 — the console must survive anything
            logger.error("watchtower cycle failed", exc_info=True)
            time.sleep(ERROR_PAUSE)


if __name__ == "__main__":
    main()
