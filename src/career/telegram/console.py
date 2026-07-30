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
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import (
    CustomerChannel,
    Delivery,
    DiscoveryRun,
    FunnelSession,
    OnboardingSession,
    OutcomeEvent,
    Subscription,
    Tenant,
    TenantDayState,
    TenantJobSuppression,
    UsageEvent,
)
from career.telegram import views
from career.telegram.admin import Keyboard
from career.whatsapp.window import window_state

logger = logging.getLogger("career.telegram.console")

_RIYADH = ZoneInfo("Asia/Riyadh")

EXPIRED_BUTTON_AR = "انتهت صلاحية الزر — أرسل /start"
ACTION_EXPIRED_AR = "انتهت صلاحية التأكيد (٥ دقائق) — أعد المحاولة"

#: Double-confirm nonces for mutating actions (design doc §4). Single operator,
#: single process → an in-memory map is enough; each nonce is one-shot and
#: expires in 5 minutes. nonce → (action, code, expiry).
_ACTION_TTL_SECONDS = 300
_pending_actions: dict[str, tuple[str, str, datetime]] = {}

#: The mutating actions the console may perform — all pure-DB authorities.
_ACTIONS = {
    "pause": ("⏸️ إيقاف مؤقت", "أوقفنا خدمة {code} مؤقتًا"),
    "resume": ("▶️ استئناف", "استأنفنا خدمة {code}"),
}


class HealthProbes(Protocol):
    """Liveness facts for the health screen; every value may be None
    (= unknown, rendered honestly as ⚪)."""

    def collect(self) -> dict[str, Any]: ...

    def error_lines(self) -> list[str]:
        """Recent error lines — collector contract: logger + static message
        only (our loggers are PII-free by §15.13; payloads never logged)."""
        ...


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
    if name == "customers":
        try:
            page = max(0, int(arg or "0"))
        except ValueError:
            return None
        return views.render_customers(*_customers_data(session, page=page, now=now))
    if name == "tenant":
        card = _tenant_card(session, code=arg, now=now)
        return views.render_tenant_card(card) if card else None
    if name == "business":
        if arg not in ("7", "30", "all"):
            return None
        return views.render_business(arg, _business_data(session, arg, now=now))
    if name == "errors":
        return views.render_errors(probes.error_lines())
    if name == "log":
        # §14 manual counters: v1|log|<TEN>|<support5|review>
        parts2 = arg.split("|")
        if len(parts2) != 2:
            return None
        code, kind = parts2
        if not _log_manual_usage(session, code=code, kind=kind, now=now):
            return None
        card = _tenant_card(session, code=code, now=now)
        return views.render_tenant_card(card) if card else None
    if name == "act":
        # v1|act|<TEN>|<action> → a confirm card carrying a one-shot nonce
        parts2 = arg.split("|")
        if len(parts2) != 2 or parts2[1] not in _ACTIONS:
            return None
        code, action = parts2
        if _tenant_card(session, code=code, now=now) is None:
            return None
        nonce = _new_nonce(action, code, now)
        label = _ACTIONS[action][0]
        return (
            f"⚠️ تأكيد: {label} للعميل {code}؟\nالزر صالح ٥ دقائق.",
            [[("✅ تأكيد نهائي", f"v1|confirm|{nonce}")],
             [("↩️ إلغاء", f"v1|tenant|{code}")]],
        )
    if name == "confirm":
        result = _run_action(session, nonce=arg, now=now)
        if result is None:
            return None
        code, done_msg = result
        card = _tenant_card(session, code=code, now=now)
        if card is None:
            return (done_msg, [[("🏠 الرئيسية", "v1|menu")]])
        text, keyboard = views.render_tenant_card(card)
        return (f"✅ {done_msg}\n\n{text}", keyboard)
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


def _window_of(
    last_inbound_at: datetime | None, opt_out_at: datetime | None, now: datetime
) -> str:
    return window_state(
        last_inbound_at=last_inbound_at, opt_out_at=opt_out_at, now=now
    ).value


def _latest_subs(session: Session) -> dict[Any, Subscription]:
    """tenant_id → newest subscription (tiny scale; reduced in Python)."""
    latest: dict[Any, Subscription] = {}
    for sub in session.execute(
        select(Subscription).order_by(Subscription.created_at)
    ).scalars():
        latest[sub.tenant_id] = sub
    return latest


def _customers_data(
    session: Session, *, page: int, now: datetime
) -> tuple[list[dict[str, Any]], int, int]:
    """(page_rows, page, total). TEN codes + plan + states only — the queries
    NEVER select names/phones (§15.13)."""
    tenants = session.execute(
        select(Tenant.id, Tenant.code).order_by(Tenant.code)
    ).all()
    total = len(tenants)
    start = page * views.PAGE_SIZE
    page_tenants = tenants[start:start + views.PAGE_SIZE]
    subs = _latest_subs(session)
    journeys = {
        tid: state for tid, state in session.execute(
            select(OnboardingSession.tenant_id, OnboardingSession.state)
        ).all()
    }
    funnels = {
        tid: state for tid, state in session.execute(
            select(FunnelSession.tenant_id, FunnelSession.state)
        ).all()
    }
    channels = {
        tid: (last, opt) for tid, last, opt in session.execute(
            select(CustomerChannel.tenant_id, CustomerChannel.last_inbound_at,
                   CustomerChannel.opt_out_at)
        ).all()
    }
    rows = []
    for tid, code in page_tenants:
        sub = subs.get(tid)
        channel = channels.get(tid)
        rows.append({
            "code": str(code),
            "plan_code": sub.plan_code if sub else None,
            "journey_state": journeys.get(tid) or funnels.get(tid),
            "window": _window_of(channel[0], channel[1], now) if channel else None,
        })
    return rows, page, total


def _tenant_card(
    session: Session, *, code: str, now: datetime
) -> dict[str, Any] | None:
    tenant = session.execute(
        select(Tenant).where(Tenant.code == code)
    ).scalars().first()
    if tenant is None:
        return None
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
        .order_by(Subscription.created_at.desc())
    ).scalars().first()
    journey = session.execute(
        select(OnboardingSession.state)
        .where(OnboardingSession.tenant_id == tenant.id)
    ).scalar_one_or_none() or session.execute(
        select(FunnelSession.state)
        .where(FunnelSession.tenant_id == tenant.id)
    ).scalar_one_or_none()
    channel = session.execute(
        select(CustomerChannel.last_inbound_at, CustomerChannel.opt_out_at)
        .where(CustomerChannel.tenant_id == tenant.id)
    ).first()
    delivery = session.execute(
        select(Delivery.run_date, Delivery.status)
        .where(Delivery.tenant_id == tenant.id)
        .order_by(Delivery.created_at.desc()).limit(1)
    ).first()
    outcomes = {
        outcome: count for outcome, count in session.execute(
            select(OutcomeEvent.outcome, func.count())
            .where(OutcomeEvent.tenant_id == tenant.id)
            .group_by(OutcomeEvent.outcome)
        ).all()
    }
    suppressions = session.execute(
        select(func.count()).select_from(TenantJobSuppression)
        .where(TenantJobSuppression.tenant_id == tenant.id)
    ).scalar_one()
    support_minutes = session.execute(
        select(func.coalesce(func.sum(UsageEvent.input_tokens), 0)).where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.kind == "support_minutes",
        )
    ).scalar_one()
    review_count = session.execute(
        select(func.count()).select_from(UsageEvent).where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.kind == "human_review",
        )
    ).scalar_one()
    return {
        "support_minutes": int(support_minutes or 0),
        "review_count": int(review_count),
        "code": code,
        "plan_code": sub.plan_code if sub else None,
        "sub_status": sub.status if sub else None,
        "sub_age_days": (now - sub.created_at).days if sub else None,
        "journey_state": journey,
        "window": _window_of(channel[0], channel[1], now) if channel else None,
        "last_delivery": (
            {"run_date": delivery[0].isoformat(), "status": delivery[1]}
            if delivery else None
        ),
        "outcomes": outcomes,
        "suppressions": int(suppressions),
    }


def business_data(
    session: Session, range_key: str, *, now: datetime
) -> dict[str, Any]:
    """Public wrapper — the weekly-report runner reuses the same numbers."""
    return _business_data(session, range_key, now=now)


def week_day_states(session: Session, *, now: datetime) -> dict[str, int]:
    """The last 7 days' honest day-state tally (§15.12)."""
    from datetime import timedelta as _td

    cutoff = now - _td(days=7)
    rows = session.execute(
        select(TenantDayState.state, func.count())
        .where(TenantDayState.recorded_at >= cutoff)
        .group_by(TenantDayState.state)
    ).all()
    return {str(s): int(n) for s, n in rows}


def _business_data(
    session: Session, range_key: str, *, now: datetime
) -> dict[str, Any]:
    cutoff = None
    if range_key in ("7", "30"):
        cutoff = now - timedelta(days=int(range_key))

    subs_query = select(Subscription.plan_code, func.count(),
                        func.coalesce(func.sum(Subscription.amount_sar), 0))
    if cutoff is not None:
        subs_query = subs_query.where(Subscription.created_at >= cutoff)
    subs_by_plan: dict[str, int] = {}
    revenue = 0
    for plan_code, count, amount in session.execute(
        subs_query.group_by(Subscription.plan_code)
    ).all():
        subs_by_plan[str(plan_code)] = int(count)
        revenue += int(amount or 0)

    outcomes_query = select(OutcomeEvent.outcome, func.count())
    if cutoff is not None:
        outcomes_query = outcomes_query.where(OutcomeEvent.occurred_at >= cutoff)
    outcomes = {
        str(outcome): int(count) for outcome, count in session.execute(
            outcomes_query.group_by(OutcomeEvent.outcome)
        ).all()
    }

    # the store page promises a live seat count — read the real one
    from career.salla.seats import founding_seats

    seats = founding_seats(session)

    delivered_query = select(func.count()).select_from(TenantDayState).where(
        TenantDayState.state == "DELIVERED"
    )
    if cutoff is not None:
        delivered_query = delivered_query.where(
            TenantDayState.recorded_at >= cutoff
        )
    delivered_days = int(session.execute(delivered_query).scalar_one())

    usage_query = select(
        func.count(), func.coalesce(func.sum(UsageEvent.cost_usd), 0)
    ).where(UsageEvent.kind.in_(("llm_generation", "llm_extraction")))
    if cutoff is not None:
        usage_query = usage_query.where(UsageEvent.occurred_at >= cutoff)
    usage_row = session.execute(usage_query).one()

    # §14 reach metrics: outbound message statuses (read receipts flow into
    # delivery_messages.status via the Meta status callbacks)
    from career.db.models import DeliveryMessage
    statuses_query = select(DeliveryMessage.status, func.count())
    if cutoff is not None:
        statuses_query = statuses_query.where(
            DeliveryMessage.status_updated_at >= cutoff
        )
    message_statuses = {
        str(status): int(n) for status, n in session.execute(
            statuses_query.group_by(DeliveryMessage.status)
        ).all()
    }

    # funnel upgrade = a tenant holding BOTH a cv_analysis purchase and a
    # search subscription (the §04 inheritance path); the RANGE applies to
    # when the upgrade subscription was created (audit fix: was all-time)
    analysis_tenants = select(Subscription.tenant_id).where(
        Subscription.plan_code == "cv_analysis"
    )
    upgrades_query = (
        select(func.count(func.distinct(Subscription.tenant_id)))
        .where(Subscription.tenant_id.in_(analysis_tenants),
               Subscription.plan_code != "cv_analysis")
    )
    if cutoff is not None:
        upgrades_query = upgrades_query.where(Subscription.created_at >= cutoff)
    upgrades = int(session.execute(upgrades_query).scalar_one())

    return {
        "subs_by_plan": subs_by_plan,
        "revenue_sar": revenue,
        "funnel_upgrades": upgrades,
        "delivered_days": delivered_days,
        "outcomes": outcomes,
        "llm_generations": int(usage_row[0]),
        "llm_cost_usd": usage_row[1],
        "message_statuses": message_statuses,
        "seats_taken": seats.taken,
        "seats_remaining": seats.remaining,
        "seats_cap": seats.cap,
    }


def _new_nonce(action: str, code: str, now: datetime) -> str:
    # deterministic-free id from the DB (Math.random/uuid4 both fine live;
    # the clock is injected so tests stay reproducible)
    import uuid as _uuid

    nonce = _uuid.uuid4().hex
    # opportunistic sweep of expired nonces
    for key in [k for k, (_, _, exp) in _pending_actions.items() if exp < now]:
        _pending_actions.pop(key, None)
    _pending_actions[nonce] = (
        action, code, now + timedelta(seconds=_ACTION_TTL_SECONDS)
    )
    return nonce


def _run_action(
    session: Session, *, nonce: str, now: datetime
) -> tuple[str, str] | None:
    """Consume the one-shot nonce and perform the pure-DB action. Returns
    (code, done_message) or None (expired/unknown → caller acks EXPIRED)."""
    entry = _pending_actions.pop(nonce, None)
    if entry is None:
        return None
    action, code, expiry = entry
    if expiry < now or action not in _ACTIONS:
        return None
    tenant = session.execute(
        select(Tenant).where(Tenant.code == code)
    ).scalars().first()
    if tenant is None:
        return None
    from career.onboarding import privacy

    if action == "pause":
        privacy.pause_subscription(session, tenant_id=tenant.id)
    elif action == "resume":
        privacy.resume_subscription(session, tenant_id=tenant.id)
    session.commit()
    return code, _ACTIONS[action][1].format(code=code)


def _log_manual_usage(
    session: Session, *, code: str, kind: str, now: datetime
) -> bool:
    """§14 manual counters the platform can't capture automatically.
    support5 = +5 support minutes (stored in input_tokens as minutes —
    documented unit for kind=support_minutes); review = +1 human review."""
    tenant = session.execute(
        select(Tenant).where(Tenant.code == code)
    ).scalars().first()
    if tenant is None or kind not in ("support5", "review"):
        return False
    from career.cv.close import record_usage

    if kind == "support5":
        record_usage(session, tenant_id=tenant.id, kind="support_minutes",
                     now=now, input_tokens=5)
    else:
        record_usage(session, tenant_id=tenant.id, kind="human_review",
                     now=now, input_tokens=1)
    session.commit()
    return True


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
        is_confirm = len(parts) >= 2 and parts[0] == "v1" and parts[1] == "confirm"
        if len(parts) >= 2 and parts[0] == "v1":
            name = parts[1]
            arg = "|".join(parts[2:]) if len(parts) > 2 else ""
            screen = _screen(session, name, arg, probes=probes, now=now)
        if screen is None or message_id is None:
            # a stale/used confirmation nonce gets its own clearer message
            return [Outcome(kind="ack", callback_query_id=cbq_id,
                            text=ACTION_EXPIRED_AR if is_confirm
                            else EXPIRED_BUTTON_AR)]
        text, keyboard = screen
        return [
            Outcome(kind="ack", callback_query_id=cbq_id),
            Outcome(kind="edit", message_id=int(message_id),
                    text=text, keyboard=keyboard),
        ]
    return []
