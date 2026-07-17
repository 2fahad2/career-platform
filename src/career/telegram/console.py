"""Watchtower console router (design doc §2/§3).

One entry point — :func:`handle_update` — maps a raw Telegram update to a list
of :class:`Outcome` effects (send / edit / ack). Pure over injected data:
the DB session for reads, a :class:`HealthProbes` provider for liveness facts,
and an explicit clock. The runner script executes outcomes via the HTTP
client; tests execute nothing.

Security: only the allow-listed operator chat is served — anything else is
ignored and logged (attempted-access trail, no reply). Screens are stateless:
every callback_data carries its full destination (``v1|screen|arg``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import DiscoveryRun, Tenant, TenantDayState
from career.telegram import views
from career.telegram.admin import Keyboard

logger = logging.getLogger("career.telegram.console")

_RIYADH = ZoneInfo("Asia/Riyadh")

EXPIRED_BUTTON_AR = "انتهت صلاحية الزر — أرسل /start"


class HealthProbes(Protocol):
    """Liveness facts for the health screen; every value may be None
    (= unknown, rendered honestly as ⚪)."""

    def collect(self) -> dict[str, Any]: ...


@dataclass
class Outcome:
    kind: str                       # "send" | "edit" | "ack"
    text: str = ""
    keyboard: Keyboard | None = field(default=None)
    message_id: int | None = None
    callback_query_id: str | None = None


def _screen(
    session: Session, name: str, arg: str, *, probes: HealthProbes, now: datetime
) -> tuple[str, Keyboard] | None:
    if name == "menu":
        return views.render_menu()
    if name == "today":
        return views.render_today(*_today_data(session, now=now))
    if name == "health":
        return views.render_health(probes.collect())
    if name == "soon":
        return views.render_soon(arg)
    return None


def _today_data(
    session: Session, *, now: datetime
) -> tuple[Any, dict[str, Any] | None, list[tuple[str, str, dict[str, Any]]]]:
    run_date = now.astimezone(_RIYADH).date()
    run_row = session.execute(
        select(DiscoveryRun.status, DiscoveryRun.counts)
        .order_by(DiscoveryRun.started_at.desc()).limit(1)
    ).first()
    run = {"status": run_row[0], "counts": dict(run_row[1] or {})} if run_row else None
    # TEN codes + states + counts ONLY — the admin channel never sees PII
    # (§15.13); no profile/channel columns are ever selected here.
    rows = session.execute(
        select(Tenant.code, TenantDayState.state, TenantDayState.counts)
        .join(Tenant, Tenant.id == TenantDayState.tenant_id)
        .where(TenantDayState.run_date == run_date)
        .order_by(Tenant.code)
    ).all()
    states = [(str(c), str(s), dict(k or {})) for c, s, k in rows]
    return run_date, run, states


def _chat_id_of(update: dict[str, Any]) -> str | None:
    message = update.get("message")
    if message:
        chat = message.get("chat") or {}
        return str(chat.get("id")) if chat.get("id") is not None else None
    callback = update.get("callback_query")
    if callback:
        sender = callback.get("from") or {}
        return str(sender.get("id")) if sender.get("id") is not None else None
    return None


def handle_update(
    session: Session,
    update: dict[str, Any],
    *,
    admin_chat_id: str,
    probes: HealthProbes,
    now: datetime,
) -> list[Outcome]:
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return []
    if chat_id != str(admin_chat_id):
        logger.warning("watchtower: ignored update from foreign chat")
        return []

    message = update.get("message")
    if message is not None:
        # any text from the operator lands on the menu — one habit to learn
        text, keyboard = views.render_menu()
        return [Outcome(kind="send", text=text, keyboard=keyboard)]

    callback = update.get("callback_query")
    if callback is not None:
        cbq_id = str(callback.get("id", ""))
        data = str(callback.get("data") or "")
        message_id = ((callback.get("message") or {}).get("message_id"))
        parts = data.split("|")
        screen = None
        if len(parts) >= 2 and parts[0] == "v1":
            name = parts[1]
            arg = parts[2] if len(parts) > 2 else ""
            screen = _screen(session, name, arg, probes=probes, now=now)
        if screen is None or message_id is None:
            return [Outcome(kind="ack", callback_query_id=cbq_id,
                            text=EXPIRED_BUTTON_AR)]
        text, keyboard = screen
        return [
            Outcome(kind="ack", callback_query_id=cbq_id),
            Outcome(kind="edit", message_id=int(message_id),
                    text=text, keyboard=keyboard),
        ]
    return []
