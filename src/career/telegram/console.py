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
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.config import get_settings
from career.cv.close import rollup_costs
from career.cv.daily_run import close_from_delivery
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
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.delivery import (
    DELIVERY_COMPLETED,
    DELIVERY_PARTIAL,
    DELIVERY_PENDING,
    RESEND_OPTED_OUT,
    RESEND_WINDOW_CLOSED,
    record_out,
    resend_pending_delivery,
)
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

#: Honest per-outcome replies for «إعادة إرسال» — the operator is told exactly
#: what landed, never a blanket "done". Every line is DIRECTION-PURE: the TEN
#: code stands alone on its own line, because a mixed Arabic+Latin line is
#: scrambled by the operator's client.
RESEND_DONE_AR = "✅ أعدنا الإرسال ووصلت الحزمة كاملة للعميل\n{code}"
RESEND_PARTIAL_AR = "🟠 أعدنا الإرسال ووصل جزء من الحزمة فقط والباقي فشل\n{code}"
RESEND_FAILED_AR = (
    "🔴 حاولنا الإرسال ولم يصل شيء — الحزمة ما زالت محفوظة وتقبل محاولة أخرى"
    "\n{code}"
)
#: The exhaustion case. AUDIT: this used to answer with RESEND_PARTIAL_AR
#: («وصل جزء من الحزمة») although the results list was EMPTY — the operator
#: read that something reached the customer and stopped investigating while
#: zero messages had landed. The status alone is not the truth; what landed is.
RESEND_EXHAUSTED_AR = (
    "🔴 أعدنا الإرسال ولم يصل شيء للعميل — واستنفدنا المحاولات المسموحة\n"
    "أغلقنا يومه بحالة فشل واتساب\n{code}"
)
#: Refusing a doomed resend. A held bundle is held BECAUSE the window is
#: shut, and واتساب rejects every free-form message outside it — so trying
#: would spend one of three attempts to send nothing, and the third would
#: make the bundle terminal and unclaimable for a customer who taps later.
RESEND_CLOSED_AR = (
    "🌙 نافذة الأربع والعشرين ساعة مقفولة — لا نستطيع إعادة الإرسال الآن\n"
    "الحزمة محفوظة كما هي ولم نستهلك أي محاولة\n"
    "تنزل تلقائيًا لحظة ما يراسلنا العميل\n{code}"
)
RESEND_OPTED_OUT_AR = "🚫 العميل أوقف الرسائل — لن نرسل له شيئًا\n{code}"
RESEND_NOTHING_AR = "⚪ لا توجد حزمة معلّقة لإعادة إرسالها لهذا العميل\n{code}"

#: The mutating actions the console may perform. pause/resume are pure-DB;
#: resend re-attempts a held bundle over WhatsApp (needs an injected client).
#: NOTE «رد على العميل» is deliberately NOT here: it does not use the
#: confirm-card path (the typed text is the confirmation), so keeping it out
#: makes a forged ``v1|confirm|<reply nonce>`` fall through _run_action's
#: ``action not in _ACTIONS`` guard and do nothing.
_ACTIONS = {
    "pause": ("⏸️ إيقاف مؤقت", "✅ أوقفنا الخدمة مؤقتًا للعميل\n{code}"),
    "resume": ("▶️ استئناف", "✅ استأنفنا الخدمة للعميل\n{code}"),
    "resend": ("📤 إعادة إرسال الحزمة", RESEND_NOTHING_AR),
}

# ── the operator's one free-form reply (audit: «دعم» paged and left them
# hunting for the phone). Same one-shot nonce as every mutating action; the
# nonce travels inside the prompt text so the pure console needs no new state
# and no message-id bookkeeping — the operator's Telegram reply carries the
# prompt back to us. Every outbound is recorded with record_out (§14) and the
# admin channel still sees TEN codes only (§15.13): we never echo the body,
# and we never print the phone we sent to.
_REPLY_ACTION = "reply"
_REPLY_MARKER = "#R-"
_REPLY_MARKER_RE = re.compile(r"#R-([0-9a-f]{32})")
#: delivery_messages.kind for an operator-typed message (fits String(32)).
REPLY_KIND = "operator_reply"
#: Meta's free-form text ceiling; a longer body is refused, never truncated.
REPLY_MAX_CHARS = 4096

REPLY_PROMPT_AR = (
    "✍️ اكتب رسالتك للعميل\n{code}\n"
    "ردًّا على هذه الرسالة نفسها — ترسل كما هي بلا تعديل\n"
    "المهلة خمس دقائق\n{marker}"
)
REPLY_SENT_AR = "✅ أرسلنا ردك للعميل\n{code}"
REPLY_FAILED_AR = (
    "🔴 لم يصل الرد — واتساب رفض الإرسال ولم نسجّل شيئًا؛ أعد المحاولة"
    "\n{code}"
)
REPLY_CLOSED_AR = (
    "🌙 نافذة الأربع والعشرين ساعة مقفولة — لا يمكن إرسال رسالة حرة الآن\n"
    "انتظر حتى يراسلك العميل ثم رد عليه\n{code}"
)
REPLY_OPTED_OUT_AR = "🚫 العميل أوقف الرسائل — لن نرسل له شيئًا\n{code}"
REPLY_NO_CHANNEL_AR = "⚪ لا توجد قناة واتساب لهذا العميل — لا يوجد لمن نرد\n{code}"
REPLY_EMPTY_AR = "⚪ لم نستلم نصًّا صالحًا — لم نرسل شيئًا\n{code}"
REPLY_TOO_LONG_AR = "⚪ الرسالة أطول مما يقبله واتساب — اختصرها وأعد الإرسال\n{code}"


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
    #: "send" only — ask Telegram to open the reply box on this message.
    #: force_reply is not a valid markup for editMessageText, which is why
    #: the reply prompt is a fresh message and not an in-place edit.
    force_reply: bool = False


def _screen(
    session: Session, name: str, arg: str, *, probes: HealthProbes,
    now: datetime, whatsapp_client: WhatsAppClient | None = None,
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
        card = _tenant_card(session, code=arg, now=now,
                            whatsapp_client=whatsapp_client)
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
        card = _tenant_card(session, code=code, now=now,
                            whatsapp_client=whatsapp_client)
        return views.render_tenant_card(card) if card else None
    if name == "act":
        # v1|act|<TEN>|<action> → a confirm card carrying a one-shot nonce
        parts2 = arg.split("|")
        if len(parts2) != 2 or parts2[1] not in _ACTIONS:
            return None
        code, action = parts2
        card = _tenant_card(session, code=code, now=now,
                            whatsapp_client=whatsapp_client)
        if card is None:
            return None
        if action == "resend" and not card.get("can_resend"):
            # never mint a confirmation for a resend that can only answer
            # "nothing to resend" (no held bundle / opted out / no client)
            return None
        nonce = _new_nonce(action, code, now)
        label = _ACTIONS[action][0]
        # every line direction-pure: the TEN code stands alone
        return (
            f"⚠️ تأكيد الإجراء التالي للعميل:\n{code}\n{label}\n"
            "الزر صالح ٥ دقائق.",
            [[("✅ تأكيد نهائي", f"v1|confirm|{nonce}")],
             [("↩️ إلغاء", f"v1|tenant|{code}")]],
        )
    if name == "confirm":
        result = _run_action(session, nonce=arg, now=now,
                             whatsapp_client=whatsapp_client)
        if result is None:
            return None
        code, done_msg = result
        card = _tenant_card(session, code=code, now=now,
                            whatsapp_client=whatsapp_client)
        if card is None:
            return (done_msg, [[("🏠 الرئيسية", "v1|menu")]])
        text, keyboard = views.render_tenant_card(card)
        return (f"{done_msg}\n\n{text}", keyboard)
    if name == "soon":
        return views.render_soon(arg)
    return None


def _today_data(
    session: Session, *, now: datetime
) -> tuple[
    Any, dict[str, Any] | None, list[tuple[str, str, dict[str, Any]]],
    dict[str, Any] | None,
]:
    """(run_date, today's run or None, today's states, last run or None).

    AUDIT FIX: the run used to be «the newest run, whatever day it belonged
    to», while the day-states beside it were already filtered to today. On a
    morning when the nightly timer did not fire, yesterday's counters sat at
    the top of a screen headed «today» and the operator read a silent failure
    as a good night. The run is now filtered to the SAME Riyadh date as the
    states; when there is none, the last recorded run is returned separately
    so the screen can say plainly that today has not run and still show when
    the last one was.
    """
    run_date = now.astimezone(_RIYADH).date()
    run_row = session.execute(
        select(DiscoveryRun.status, DiscoveryRun.counts)
        .where(DiscoveryRun.run_date == run_date)
        .order_by(DiscoveryRun.started_at.desc()).limit(1)
    ).first()
    run = {"status": run_row[0], "counts": dict(run_row[1] or {})} if run_row else None
    last_run: dict[str, Any] | None = None
    if run is None:
        # only consulted when today has no run at all — so this row can never
        # be mistaken for today's; it is labelled with its own date.
        last_row = session.execute(
            select(DiscoveryRun.run_date, DiscoveryRun.status)
            .order_by(DiscoveryRun.started_at.desc()).limit(1)
        ).first()
        if last_row is not None:
            last_run = {
                "run_date": last_row[0].isoformat(), "status": str(last_row[1]),
            }
    # TEN codes + states + counts ONLY — the admin channel never sees PII
    # (§15.13); no profile/channel columns are ever selected here.
    rows = session.execute(
        select(Tenant.code, TenantDayState.state, TenantDayState.counts)
        .join(Tenant, Tenant.id == TenantDayState.tenant_id)
        .where(TenantDayState.run_date == run_date)
        .order_by(Tenant.code)
    ).all()
    states = [(str(c), str(s), dict(k or {})) for c, s, k in rows]
    return run_date, run, states, last_run


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
    session: Session, *, code: str, now: datetime,
    whatsapp_client: WhatsAppClient | None = None,
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
        select(Delivery.run_date, Delivery.status, Delivery.bundle)
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
    # «إعادة إرسال» is offered ONLY when it can do something: a still-held
    # bundle, an OPEN 24h window, and a client to send with.
    held_bundles = session.execute(
        select(func.count()).select_from(Delivery).where(
            Delivery.tenant_id == tenant.id,
            Delivery.status == DELIVERY_PENDING,
        )
    ).scalar_one()
    window = _window_of(channel[0], channel[1], now) if channel else None
    return {
        "support_minutes": int(support_minutes or 0),
        "review_count": int(review_count),
        "code": code,
        "plan_code": sub.plan_code if sub else None,
        "sub_status": sub.status if sub else None,
        "sub_age_days": (now - sub.created_at).days if sub else None,
        "journey_state": journey,
        "window": window,
        "held_bundles": int(held_bundles),
        # AUDIT: the window predicate is not cosmetic. A held bundle is held
        # BECAUSE the window is shut, so without it the button was offered in
        # the single most common state, every doomed tap burned one of three
        # attempts, and the third made the bundle terminal and unclaimable.
        "can_resend": bool(
            whatsapp_client is not None
            and int(held_bundles) > 0
            and window == "open"
        ),
        # «رد على العميل» is offered whenever there is someone to reply to and
        # a client to send with. A CLOSED window does NOT hide it — the card
        # already shows the window state, and the tap answers with the real
        # reason (and no reply box), which is more useful than a button that
        # silently is not there. An opt-out does hide it: we never message
        # someone who stopped the messages.
        "can_reply": bool(
            whatsapp_client is not None
            and channel is not None
            and window != "opted_out"
        ),
        # what LANDED, not only the status word: a PARTIAL whose delivered
        # list is empty must never read as «وصل جزء منها» (§15.12).
        "last_delivery": (
            {
                "run_date": delivery[0].isoformat(),
                "status": delivery[1],
                "delivered": len(
                    ((delivery[2] or {}).get("results") or {}).get("delivered")
                    or []
                ),
                "failed": len(
                    ((delivery[2] or {}).get("results") or {}).get("failed")
                    or []
                ),
            }
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


def _searches_this_month(session: Session, *, now: datetime) -> int:
    """Search credits billed since the first of this Riyadh month."""
    from career.cv.close import SEARCH_KINDS

    local = now.astimezone(_RIYADH)
    month_start = local.replace(day=1, hour=0, minute=0, second=0,
                                microsecond=0).astimezone(now.tzinfo)
    # Derived from COST, not from the credit column, and that is the whole
    # trick. A family's credit count is written onto every tenant it serves
    # while the COST is split between them — so summing counts multiplies by
    # the audience, and taking the max per run reports only the biggest
    # family (one run of three families billing 5+3+2 read as 5 of a real 10).
    #
    # The split is exact, so summing cost across every row reconstructs what
    # the provider actually billed, and dividing by the per-search price gives
    # the credits. No schema change, and it stays right however many families
    # a run covers or how many tenants each serves.
    total_usd = session.execute(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.kind.in_(SEARCH_KINDS),
            UsageEvent.occurred_at >= month_start,
        )
    ).scalar_one() or 0
    price = Decimal(str(get_settings().searchapi_usd_per_search))
    if price <= 0:
        return 0
    return int((Decimal(str(total_usd)) / price).to_integral_value())


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

    # §14 spend. Closure audit: this used to count exactly TWO categories, so
    # the operator's «نداءات Claude» number silently ignored the panel, the
    # judge, the examples writer, the intent classifier, every SearchAPI credit
    # and every billed WhatsApp template. Now every metered kind lands here.
    from career.cv import close as close_mod

    spend_query = select(
        UsageEvent.kind, func.count(),
        func.coalesce(func.sum(UsageEvent.cost_usd), 0),
    ).where(UsageEvent.kind.in_(close_mod.SPEND_KINDS))
    if cutoff is not None:
        spend_query = spend_query.where(UsageEvent.occurred_at >= cutoff)
    spend: dict[str, tuple[int, Decimal]] = {
        str(kind): (int(events), Decimal(cost))
        for kind, events, cost in session.execute(
            spend_query.group_by(UsageEvent.kind)
        ).all()
    }
    # WhatsApp is derived from the delivery ledger (its send sites live in
    # modules this layer must not reach into) — same truth, same table.
    for kind, (events, cost) in close_mod.whatsapp_spend(
        session, since=cutoff
    ).items():
        prior_events, prior_cost = spend.get(kind, (0, Decimal("0")))
        spend[kind] = (prior_events + events, prior_cost + cost)

    llm_calls = sum(spend.get(k, (0, Decimal("0")))[0] for k in close_mod.LLM_KINDS)
    llm_cost = sum(
        (spend.get(k, (0, Decimal("0")))[1] for k in close_mod.LLM_KINDS),
        Decimal("0"),
    )
    total_cost = sum((cost for _, cost in spend.values()), Decimal("0"))

    # per-customer cost: the audit's actual question — «what does a customer
    # cost me?» — plus the runaway-bill signal (who is the most expensive?)
    per_tenant_query = select(
        Tenant.code, func.coalesce(func.sum(UsageEvent.cost_usd), 0)
    ).join(UsageEvent, UsageEvent.tenant_id == Tenant.id).where(
        UsageEvent.kind.in_(close_mod.SPEND_KINDS)
    )
    if cutoff is not None:
        per_tenant_query = per_tenant_query.where(UsageEvent.occurred_at >= cutoff)
    cost_tenants_query = select(
        func.count(func.distinct(UsageEvent.tenant_id))
    ).where(UsageEvent.kind.in_(close_mod.SPEND_KINDS))
    if cutoff is not None:
        cost_tenants_query = cost_tenants_query.where(
            UsageEvent.occurred_at >= cutoff
        )
    cost_tenants = int(session.execute(cost_tenants_query).scalar_one())

    top_cost_tenants = [
        (str(code), Decimal(cost))
        for code, cost in session.execute(
            per_tenant_query.group_by(Tenant.code)
            .order_by(func.coalesce(func.sum(UsageEvent.cost_usd), 0).desc())
            .limit(3)
        ).all()
        if Decimal(cost) > 0
    ]

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
        "llm_generations": llm_calls,
        "llm_cost_usd": llm_cost,
        # The real ceiling is not money, it is the monthly SEARCH allowance:
        # the provider sells a fixed block of searches, and it is the number
        # of CAREER PATHS that consumes it, not the number of customers. A
        # hundred customers across five paths use what twenty across the same
        # five use. Without this line the operator watches spend rise and has
        # no idea how close the allowance is to running out — the one limit
        # that stops discovery for EVERY customer at once.
        "searches_this_month": _searches_this_month(session, now=now),
        "spend_by_category": {k: (v[0], v[1]) for k, v in sorted(spend.items())},
        "total_cost_usd": total_cost,
        "cost_tenants": cost_tenants,
        "top_cost_tenants": top_cost_tenants,
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
    session: Session, *, nonce: str, now: datetime,
    whatsapp_client: WhatsAppClient | None = None,
) -> tuple[str, str] | None:
    """Consume the one-shot nonce and perform the action. Returns
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
    elif action == "resend":
        if whatsapp_client is None:      # no client injected → nothing to do
            return None
        result = resend_pending_delivery(
            session, tenant_id=tenant.id,
            whatsapp_client=whatsapp_client, now=now,
        )
        # read status AND what actually landed BEFORE the commit expires the
        # instance — the status alone cannot tell PARTIAL-with-something from
        # PARTIAL-with-nothing, and only one of those is «وصل جزء».
        status = result.status
        delivered = result.delivered_groups
        # Close the DAY, exactly as the customer-tap path does. Without this a
        # resend that actually landed left the tenant with no honest day state
        # (§15.12) and — worse — wrote no suppression, so the very same jobs
        # were eligible to be sent again tomorrow. The operator fixed the
        # delivery and silently created a duplicate.
        if result.delivery is not None:
            closed = close_from_delivery(
                session, delivery=result.delivery, now=now,
            )
            if closed is not None:
                try:
                    rollup_costs(
                        session, tenant_id=tenant.id, day=closed.run_date,
                    )
                except Exception:  # noqa: BLE001 — accounting never blocks
                    logger.warning("resend cost rollup failed", exc_info=True)
        session.commit()
        if result.outcome == RESEND_WINDOW_CLOSED:
            message = RESEND_CLOSED_AR
        elif result.outcome == RESEND_OPTED_OUT:
            message = RESEND_OPTED_OUT_AR
        elif status == DELIVERY_COMPLETED:
            message = RESEND_DONE_AR
        elif status == DELIVERY_PARTIAL and delivered:
            message = RESEND_PARTIAL_AR
        elif status == DELIVERY_PARTIAL:  # terminal, and nothing ever landed
            message = RESEND_EXHAUSTED_AR
        elif status == DELIVERY_PENDING:
            message = RESEND_FAILED_AR
        else:                            # nothing claimable at all
            message = RESEND_NOTHING_AR
        return code, message.format(code=code)
    session.commit()
    return code, _ACTIONS[action][1].format(code=code)


def _reply_target(
    session: Session, *, code: str, now: datetime
) -> tuple[Tenant, CustomerChannel | None, str | None] | None:
    """(tenant, channel, window) for the code, or None when no such tenant."""
    tenant = session.execute(
        select(Tenant).where(Tenant.code == code)
    ).scalars().first()
    if tenant is None:
        return None
    channel = session.execute(
        select(CustomerChannel).where(CustomerChannel.tenant_id == tenant.id)
        .order_by(CustomerChannel.created_at)
    ).scalars().first()
    window = None if channel is None else _window_of(
        channel.last_inbound_at, channel.opt_out_at, now
    )
    return tenant, channel, window


def _reply_nonce_in(text: str) -> str | None:
    """Pull our one-shot nonce back out of the prompt the operator replied to.
    Correlating through the prompt TEXT (not its message id) keeps the console
    pure: it never learns what id Telegram gave the message it asked us to
    send."""
    match = _REPLY_MARKER_RE.search(text or "")
    return match.group(1) if match else None


def _reply_prompt(
    session: Session, *, code: str, now: datetime,
    whatsapp_client: WhatsAppClient | None,
) -> tuple[str, str, Keyboard | None] | None:
    """Open the reply box — or refuse, out loud, with the real reason.

    Returns (kind, text, keyboard) where kind is "send" (a force-reply prompt
    carrying a fresh nonce) or "edit" (a refusal that replaces the card in
    place); None when the tenant is unknown.
    """
    target = _reply_target(session, code=code, now=now)
    if target is None:
        return None
    _tenant, channel, window = target
    back: Keyboard = [[("↩️ رجوع", f"v1|tenant|{code}")]]
    if whatsapp_client is None or channel is None:
        return "edit", REPLY_NO_CHANNEL_AR.format(code=code), back
    if window == "opted_out":
        return "edit", REPLY_OPTED_OUT_AR.format(code=code), back
    if window != "open":
        # §08: outside 24h only approved templates may go out. Refuse here
        # rather than accept text we would have to drop into the void.
        return "edit", REPLY_CLOSED_AR.format(code=code), back
    nonce = _new_nonce(_REPLY_ACTION, code, now)
    return (
        "send",
        REPLY_PROMPT_AR.format(code=code, marker=f"{_REPLY_MARKER}{nonce}"),
        None,
    )


def _run_reply(
    session: Session, *, nonce: str, body: str, now: datetime,
    whatsapp_client: WhatsAppClient | None,
) -> str | None:
    """Consume the one-shot nonce and send the operator's text to the
    customer. Returns the honest Arabic answer, or None (expired/unknown
    nonce → caller answers ACTION_EXPIRED_AR)."""
    entry = _pending_actions.pop(nonce, None)
    if entry is None:
        return None
    action, code, expiry = entry
    if action != _REPLY_ACTION or expiry < now:
        return None
    target = _reply_target(session, code=code, now=now)
    if target is None:
        return None
    tenant, channel, window = target
    text = (body or "").strip()
    if not text or text.startswith("/"):
        # a mis-fired command must never reach a paying customer
        return REPLY_EMPTY_AR.format(code=code)
    if len(text) > REPLY_MAX_CHARS:
        return REPLY_TOO_LONG_AR.format(code=code)
    if whatsapp_client is None or channel is None:
        return REPLY_NO_CHANNEL_AR.format(code=code)
    # re-checked at SEND time, not only at prompt time: the window can close
    # (or the customer can opt out) while the operator is typing.
    if window == "opted_out":
        return REPLY_OPTED_OUT_AR.format(code=code)
    if window != "open":
        return REPLY_CLOSED_AR.format(code=code)
    try:
        wa_message_id = whatsapp_client.send_text(channel.phone_e164, text)
    except Exception:  # noqa: BLE001 — a refused send is reported, not raised
        logger.error("operator reply send failed", exc_info=True)
        session.rollback()
        return REPLY_FAILED_AR.format(code=code)
    # §14: the customer received it, so the conversation log and the reach
    # metrics must carry it exactly like any other outbound.
    record_out(
        session, tenant_id=tenant.id, channel_id=channel.id, kind=REPLY_KIND,
        wa_message_id=wa_message_id, now=now,
    )
    session.commit()
    return REPLY_SENT_AR.format(code=code)


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
    whatsapp_client: WhatsAppClient | None = None,
) -> list[Outcome]:
    """``whatsapp_client`` is injected by the runner (same style as every other
    side-effecting path); without it the console stays read-only and the
    «إعادة إرسال» button is never offered."""
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return []
    if chat_id != str(admin_chat_id):
        logger.warning("watchtower: ignored update from foreign chat")
        return []

    message = update.get("message")
    if message is not None:
        replied_to = str(
            (message.get("reply_to_message") or {}).get("text") or ""
        )
        nonce = _reply_nonce_in(replied_to)
        if nonce is not None:
            # a reply to one of OUR prompts → this text goes to the customer
            done = _run_reply(
                session, nonce=nonce, body=str(message.get("text") or ""),
                now=now, whatsapp_client=whatsapp_client,
            )
            return [Outcome(
                kind="send", text=done or ACTION_EXPIRED_AR,
                keyboard=[[("👥 العملاء", "v1|customers|0"),
                           ("🏠 الرئيسية", "v1|menu")]],
            )]
        # any other text from the operator lands on the menu — one habit
        text, keyboard = views.render_menu()
        return [Outcome(kind="send", text=text, keyboard=keyboard)]

    callback = update.get("callback_query")
    if callback is not None:
        cbq_id = str(callback.get("id", ""))
        data = str(callback.get("data") or "")
        message_id = ((callback.get("message") or {}).get("message_id"))
        parts = data.split("|")
        if len(parts) == 3 and parts[0] == "v1" and parts[1] == _REPLY_ACTION:
            # the reply prompt must be a NEW message (force_reply is not a
            # legal markup for editMessageText), so it bypasses _screen.
            prompt = _reply_prompt(session, code=parts[2], now=now,
                                   whatsapp_client=whatsapp_client)
            if prompt is None:
                return [Outcome(kind="ack", callback_query_id=cbq_id,
                                text=EXPIRED_BUTTON_AR)]
            # its own name: `keyboard` below is bound as non-optional, and
            # the prompt's is optional (a force-reply prompt carries none)
            kind, text, reply_keyboard = prompt
            ack = Outcome(kind="ack", callback_query_id=cbq_id)
            if kind == "send":
                return [ack, Outcome(kind="send", text=text, force_reply=True)]
            if message_id is None:
                return [ack, Outcome(kind="send", text=text,
                                     keyboard=reply_keyboard)]
            return [ack, Outcome(kind="edit", message_id=int(message_id),
                                 text=text, keyboard=reply_keyboard)]
        screen = None
        is_confirm = len(parts) >= 2 and parts[0] == "v1" and parts[1] == "confirm"
        if len(parts) >= 2 and parts[0] == "v1":
            name = parts[1]
            arg = "|".join(parts[2:]) if len(parts) > 2 else ""
            screen = _screen(session, name, arg, probes=probes, now=now,
                             whatsapp_client=whatsapp_client)
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
