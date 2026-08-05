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
    SupportEvent,
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

#: Pause / resume answers. INCIDENT (⏸️ crash loop): the action path had no
#: guard at all, so `privacy._subscription` raising RequestNotFound for a
#: tenant with no live subscription escaped `handle_update` — and the runner
#: stores the Telegram offset only AFTER handle_update returns, so the same
#: update was re-fed every five seconds forever and the operator's whole
#: watchtower was dead until someone restarted the service. Shell tenants are
#: routine (every §04 upgrade leaves one) and the customers list filters
#: nothing, so the pause button sat live on their cards.
#:
#: The second half of the same incident is the opposite failure: privacy
#: returns the row UNCHANGED when the state cannot pause / is not paused,
#: while the console printed «✅ أوقفنا / استأنفنا» unconditionally. So every
#: reply below is derived from what the subscription ACTUALLY is after the
#: call — never from the fact that the call returned. Lines stay
#: direction-pure: the TEN code and the raw status token each stand alone.
ACTION_NO_SUB_AR = "⚪ لا يوجد اشتراك لهذا العميل — لم نغيّر شيئًا\n{code}"
PAUSE_ALREADY_AR = "ℹ️ الخدمة موقوفة مؤقتًا أصلًا — لم نغيّر شيئًا\n{code}"
PAUSE_REFUSED_AR = (
    "⚪ لا يقبل هذا الاشتراك الإيقاف المؤقت وحالته:\n{status}\n"
    "لم نغيّر شيئًا\n{code}"
)
RESUME_NOT_PAUSED_AR = (
    "⚪ الاشتراك ليس موقوفًا حتى نستأنفه وحالته:\n{status}\n"
    "لم نغيّر شيئًا\n{code}"
)
#: Last resort for this action only: something inside the subscription layer
#: refused in a way we do not have a name for. Never a traceback — the
#: operator gets the situation, the log gets the exception.
ACTION_FAILED_AR = (
    "🔴 لم ينفَّذ الإجراء ولم نغيّر شيئًا\n"
    "التفاصيل في شاشة الأخطاء\n{code}"
)
#: The barrier's own answer — see :func:`handle_update`.
SCREEN_FAILED_AR = "🔴 تعذّر تنفيذ هذه الخطوة — التفاصيل في شاشة الأخطاء"

#: ── «إصدار رابط تفعيل»: the operator's fallback when zero-touch cannot ──
#:
#: The standing deep link used to be posted to this channel on every sale and
#: sat there live for seven days per order; it was removed and replaced by
#: ``salla.provisioning.issue_activation_link`` — on demand, sixty minutes,
#: rotating, refused for a claimed order. That left the capability with no
#: TRIGGER: an API the operator could not call is not a fallback, and the
#: buyer whose order carries no usable phone stays unactivated.
#:
#: Three things the operator must be told, because each one otherwise reads as
#: a bug: the link dies in an hour, it is single-use, and issuing a new one
#: retires the old one — an expired or retired link answers the buyer «رمز
#: التفعيل مستخدم مسبقًا», which looks exactly like a broken system to
#: whoever did not know they replaced it.
LINK_ISSUED_AR = (
    "🔑 أصدرنا رابط تفعيل جديد للعميل\n"
    "{code}\n"
    "صالح {ttl} دقيقة فقط، ولمرة واحدة\n"
    "أرسله للمشتري في محادثته وحده — لا تنشره ولا تعيد توجيهه\n"
    "أي رابط سابق لهذا الطلب أُلغي الآن، ولو استخدمه المشتري بيجيه\n"
    "«رمز التفعيل مستخدم مسبقًا» وهذا ليس عطلًا\n"
    "{link}"
)
#: Refusing OUT LOUD. Silence here is worse than a refusal: the operator taps
#: because a buyer is waiting on the phone, and an action that answers nothing
#: sends them looking for a fault that is not there.
LINK_ALREADY_ACTIVATED_AR = (
    "⚪ طلب هذا العميل مُفعَّل أصلًا — لم نصدر أي رابط\n"
    "الرابط يُصدر فقط لطلب مدفوع لم يُطالَب به بعد\n{code}"
)
LINK_NO_ORDER_AR = (
    "⚪ لا يوجد طلب مدفوع بانتظار التفعيل لهذا العميل — لم نصدر شيئًا\n{code}"
)

#: The mutating actions the console may perform. pause/resume are pure-DB;
#: resend re-attempts a held bundle over WhatsApp (needs an injected client);
#: issue_link mints a one-hour activation credential for a waiting order.
#: NOTE «رد على العميل» is deliberately NOT here: it does not use the
#: confirm-card path (the typed text is the confirmation), so keeping it out
#: makes a forged ``v1|confirm|<reply nonce>`` fall through _run_action's
#: ``action not in _ACTIONS`` guard and do nothing.
#: The ✅ lines are reachable ONLY after the new status has been read back
#: from the subscription — see :func:`_run_subscription_action`. The second
#: element of an entry whose runner composes its own answer (resend,
#: issue_link) is the REFUSAL, never a tick: nothing here may inherit a ✅.
_ACTIONS = {
    "pause": ("⏸️ إيقاف مؤقت", "✅ أوقفنا الخدمة مؤقتًا للعميل\n{code}"),
    "resume": ("▶️ استئناف", "✅ استأنفنا الخدمة للعميل\n{code}"),
    "resend": ("📤 إعادة إرسال الحزمة", RESEND_NOTHING_AR),
    "issue_link": ("🔑 إصدار رابط تفعيل", LINK_NO_ORDER_AR),
}

#: ── open support tickets ────────────────────────────────────────────────────
#: `support_events` has been written since C4 — «دعم» from a customer, and now
#: the funnel's consent stall — and NOTHING has ever read the table. The
#: operator is paged once, at the moment it happens, and after that the ticket
#: exists only in the database. This screen is the minimum that makes them
#: visible: what is open, for how long, and whose (TEN code only). Assignment,
#: resolution and SLA belong to a later wave.
TICKETS_TITLE_AR = "🎫 التذاكر المفتوحة"
TICKETS_NONE_AR = "🟢 لا توجد تذاكر مفتوحة"
#: Ticket kinds, in the words of what actually happened to the customer.
_TICKET_KIND_AR = {
    "support_request": "طلب التواصل مع الدعم",
    "funnel_consent_stuck": "متعثّر عند بوابة الموافقة",
}
#: How many tickets one screen shows. A watchtower screen the operator has to
#: scroll is a screen they stop reading; the count line stays truthful about
#: the rest.
TICKETS_PAGE = 10

_WESTERN_TO_ARABIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

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
#: The half-success nobody had a word for: واتساب accepted the message and our
#: own ledger write failed. «لم يصل» would have the operator type it again and
#: the customer read it twice, «✅» would hide a hole in §14's conversation log.
REPLY_SENT_UNLOGGED_AR = (
    "🟠 وصل ردك للعميل ولم نتمكن من تسجيله في سجل المحادثة\n"
    "لا تعد إرساله\n{code}"
)
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
        return _menu_screen()
    if name == "tickets":
        return _tickets_screen(session, now=now)
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
        return _tenant_screen(card) if card else None
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
        return _tenant_screen(card) if card else None
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
        # NOTE issue_link has no such gate on purpose. A doomed resend costs
        # one of three delivery attempts, so it is refused before the nonce
        # exists; a doomed issue costs nothing and its refusal is the very
        # answer the operator needs («the order is already claimed»). Hiding
        # it behind an «انتهت صلاحية الزر» would be the silent failure the
        # button was added to end.
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
        text, keyboard = _tenant_screen(card)
        return (f"{done_msg}\n\n{text}", keyboard)
    if name == "soon":
        return views.render_soon(arg)
    return None


def _ar_digits(value: Any) -> str:
    """Western digits → Arabic-Indic, so a number can live INSIDE an Arabic
    line without scrambling it (§16). The alternative — a bare Latin numeral
    on a line of its own — is what the rest of this file does for values we
    do not control; these we do."""
    return str(value).translate(_WESTERN_TO_ARABIC)


def _menu_screen() -> tuple[str, Keyboard]:
    """The menu, plus the row that leads to the open tickets.

    The row is added here rather than in ``views`` for the same reason the
    activation-link button is (see :func:`_tenant_screen`): the screens this
    module can actually serve are defined in this module, and a trigger that
    lives beside its handler cannot be forgotten the way the activation link's
    was.
    """
    text, keyboard = views.render_menu()
    return text, [*keyboard, [("🎫 التذاكر المفتوحة", "v1|tickets")]]


def _tenant_screen(card: dict[str, Any]) -> tuple[str, Keyboard]:
    """The customer card, plus the actions this module owns.

    ``views`` renders the card and its standing buttons; the activation-link
    row is added here, next to ``_ACTIONS`` and its runner, because the whole
    incident behind this button was a capability that existed with no way to
    reach it. Keeping the trigger in the same file as the action table means
    the two cannot drift apart again.

    It is inserted BEFORE the navigation row so «الرئيسية» stays where the
    operator's thumb already expects it.
    """
    text, keyboard = views.render_tenant_card(card)
    if card.get("can_issue_link"):
        label = _ACTIONS["issue_link"][0]
        row = [(label, f"v1|act|{card['code']}|issue_link")]
        keyboard = [*keyboard[:-1], row, keyboard[-1]]
    return text, keyboard


def _age_ar(since: datetime | None, now: datetime) -> str:
    """«منذ ٣ ساعات» — how long this has been waiting, which is the whole
    point of showing a ticket at all. Rounded down to the largest unit that
    is not a lie: a ticket open for ninety minutes reads as an hour, and one
    open for three days reads as three days, not seventy-two hours."""
    if since is None:
        return "منذ وقت غير معروف"
    minutes = max(0, int((now - since).total_seconds() // 60))
    if minutes < 60:
        return f"منذ {_ar_digits(minutes)} دقيقة"
    hours = minutes // 60
    if hours < 24:
        return f"منذ {_ar_digits(hours)} ساعة"
    return f"منذ {_ar_digits(hours // 24)} يوم"


def _tickets_screen(
    session: Session, *, now: datetime
) -> tuple[str, Keyboard]:
    """Every open support ticket, oldest first — the table's first reader.

    Oldest first because age is the only priority signal this screen has: the
    ticket that has been open longest is the customer who has been waiting
    longest, and a paying customer who asked for a human is the one thing
    this product may not lose. TEN codes only (§15.13) — the card behind each
    one is a tap away and carries no PII either.
    """
    rows = session.execute(
        select(Tenant.code, SupportEvent.kind, SupportEvent.created_at)
        .join(Tenant, Tenant.id == SupportEvent.tenant_id)
        .where(SupportEvent.status == "open")
        .order_by(SupportEvent.created_at)
    ).all()
    lines = [f"{TICKETS_TITLE_AR}: {_ar_digits(len(rows))}"]
    if not rows:
        lines.append(TICKETS_NONE_AR)
    for code, kind, created_at in rows[:TICKETS_PAGE]:
        lines.append("")
        # an unknown kind keeps its own line: it is a raw Latin token and a
        # mixed Arabic+Latin line arrives scrambled on the operator's client
        known = _TICKET_KIND_AR.get(str(kind))
        lines.append(f"• {known}" if known else f"•\n{kind}")
        lines.append(str(code))
        lines.append(_age_ar(created_at, now))
    if len(rows) > TICKETS_PAGE:
        lines.append("")
        lines.append(
            f"وأقدم {_ar_digits(TICKETS_PAGE)} معروضة من أصل "
            f"{_ar_digits(len(rows))}"
        )
    keyboard: Keyboard = [
        [("🔄 تحديث", "v1|tickets"), ("🏠 الرئيسية", "v1|menu")],
    ]
    return "\n".join(lines), keyboard


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
        # «إصدار رابط تفعيل» is offered exactly where it can do anything: a
        # paid order still waiting to be claimed. It is NOT a gate on the
        # action — a stale card that has since been activated must still get
        # a spoken refusal, not a dead button (see the act branch).
        "can_issue_link": str(sub.status if sub else "") == "PAID_UNCLAIMED",
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


def _run_subscription_action(
    session: Session, *, tenant_id: Any, action: str, code: str
) -> str:
    """Pause or resume, then say what ACTUALLY happened to the subscription.

    Two halves of one incident live here.

    The first is the crash loop: the pause button is drawn on every card,
    including the shell tenants a §04 upgrade leaves behind, and for those
    ``privacy._subscription`` raises RequestNotFound. With no guard on this
    path the exception left ``handle_update``, the runner never advanced the
    Telegram offset, and one tap wedged the entire console into a five-second
    retry of the same dead update. So the live row is resolved HERE, with the
    same reader privacy uses (``current_subscription`` — its absence is a
    fact, not an error), and the two named refusals of the subscription layer
    are answered instead of propagating.

    The second is the false success. ``pause_subscription`` returns the row
    untouched from a state it may not leave, and ``resume_subscription``
    returns it untouched unless the status is exactly PAUSED — while the
    console printed «✅ استأنفنا الخدمة للعميل» either way. On the one action
    that puts a paying customer back in service, a green tick that means
    «nothing moved» is worse than an error. Every reply below is therefore
    read back from the subscription after the call, and the ✅ is emitted only
    when the status is the one we asked for.
    """
    from career.onboarding import privacy
    from career.salla import subscriptions as sub_states
    from career.salla.renewal import current_subscription

    subscription = current_subscription(session, tenant_id)
    if subscription is None:
        return ACTION_NO_SUB_AR.format(code=code)
    before = str(subscription.status)
    if action == "pause" and before == sub_states.PAUSED:
        # idempotent, and honest about it: a second ✅ reads as a fresh pause
        return PAUSE_ALREADY_AR.format(code=code)
    if action == "resume" and before != sub_states.PAUSED:
        return RESUME_NOT_PAUSED_AR.format(code=code, status=before)
    wanted = sub_states.PAUSED if action == "pause" else sub_states.ACTIVE
    try:
        if action == "pause":
            privacy.pause_subscription(session, tenant_id=tenant_id)
        else:
            privacy.resume_subscription(session, tenant_id=tenant_id)
    except sub_states.InvalidTransition:
        # the state machine has no edge for it — e.g. GRACE, which privacy
        # counts as pausable while `_ALLOWED` has no GRACE→PAUSED edge. The
        # refusal is a fact about the state, so name the state.
        logger.error("watchtower %s refused by the state machine", action,
                     exc_info=True)
        session.rollback()
        return _refused_ar(action).format(code=code, status=before)
    except privacy.RequestNotFound:
        # the live row vanished between our read and the call (a race we did
        # not create); nothing was written, so say nothing changed
        logger.error("watchtower %s lost its subscription", action,
                     exc_info=True)
        session.rollback()
        return ACTION_NO_SUB_AR.format(code=code)
    after = str(subscription.status)
    if after != wanted:
        # privacy declined silently — no exception, no change, no ✅
        session.rollback()
        return _refused_ar(action).format(code=code, status=after)
    session.commit()
    return _ACTIONS[action][1].format(code=code)


def _refused_ar(action: str) -> str:
    return PAUSE_REFUSED_AR if action == "pause" else RESUME_NOT_PAUSED_AR


def _run_issue_link(session: Session, *, code: str, now: datetime) -> str:
    """Mint one activation link for a waiting order and hand it back.

    The return value of this function is a LIVE CREDENTIAL: whoever opens the
    link binds their phone to that paid subscription. It therefore goes
    exactly one place — the string this function returns, which the console
    delivers to the operator's own chat as the answer to the button they just
    pressed. It is never logged (not even at DEBUG, not even on the failure
    paths), never sent through ``admin_client.send_admin``, and never put in
    an exception message. The previous design posted it to this same channel
    automatically on every sale, and that is the incident this whole path
    exists to undo — the redaction filter cannot cover for a mistake here,
    because the token rides in a ``text=`` query parameter and matches no key
    name and no provider token shape.

    ``issue_activation_link`` commits its own work (a new token row, the
    retirement of any outstanding one, an audit row), so there is nothing to
    commit here and nothing to roll back.
    """
    from career.salla.provisioning import (
        OPERATOR_LINK_TTL_MINUTES,
        LinkIssueStatus,
        issue_activation_link,
    )

    issue = issue_activation_link(
        session, tenant_code=code,
        whatsapp_number_e164=get_settings().whatsapp_number_e164, now=now,
    )
    if issue.status is LinkIssueStatus.ALREADY_ACTIVATED:
        return LINK_ALREADY_ACTIVATED_AR.format(code=code)
    if issue.status is not LinkIssueStatus.ISSUED or not issue.link:
        return LINK_NO_ORDER_AR.format(code=code)
    return LINK_ISSUED_AR.format(
        code=code, ttl=_ar_digits(OPERATOR_LINK_TTL_MINUTES), link=issue.link,
    )


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
    if action in ("pause", "resume"):
        return code, _run_subscription_action(
            session, tenant_id=tenant.id, action=action, code=code
        )
    if action == "issue_link":
        return code, _run_issue_link(session, code=code, now=now)
    if action == "resend":
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
    # Unreachable while _ACTIONS holds exactly the three above — and kept that
    # way deliberately. The old tail here committed and printed
    # ``_ACTIONS[action][1]`` for anything that fell through, which is how a
    # ✅ came to be emitted without anyone checking the outcome. A new action
    # must state its own verified result; inheriting a tick is not allowed.
    logger.error("watchtower action %s has no runner", action)
    return code, ACTION_FAILED_AR.format(code=code)


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
    try:
        record_out(
            session, tenant_id=tenant.id, channel_id=channel.id,
            kind=REPLY_KIND, wa_message_id=wa_message_id, now=now,
        )
        session.commit()
    except Exception:  # noqa: BLE001 — the send already happened
        # The message IS with the customer; only our ledger failed. Answering
        # «لم يصل» here would send the operator to type it again and the
        # customer would read it twice — so the reply says exactly which half
        # succeeded, and the missing row is an ERROR the operator can see.
        logger.error("operator reply landed but was not recorded", exc_info=True)
        session.rollback()
        return REPLY_SENT_UNLOGGED_AR.format(code=code)
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


def _failure_outcome(update: dict[str, Any]) -> list[Outcome]:
    """What the operator sees when a tap could not be served at all. A
    callback is ACKed so its spinner stops; a plain message is answered with a
    fresh one carrying the way home."""
    cbq_id = str((update.get("callback_query") or {}).get("id") or "")
    if cbq_id:
        return [Outcome(kind="ack", callback_query_id=cbq_id,
                        text=SCREEN_FAILED_AR)]
    return [Outcome(kind="send", text=SCREEN_FAILED_AR,
                    keyboard=[[("🏠 الرئيسية", "v1|menu")]])]


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
    «إعادة إرسال» button is never offered.

    This function also carries the last-resort barrier, and the reason is the
    BLAST RADIUS rather than any one bug. The runner stores the Telegram
    offset only after we return, so an exception escaping here is not a failed
    screen — it is a permanent outage: the same update is re-fed every five
    seconds, no button works, and the operator is told nothing at all. The
    barrier converts that into one failed tap.

    It is a barrier, not a blanket: the exception is logged at ERROR with its
    traceback, which puts it in front of the operator on the ⚠️ الأخطاء screen
    (the collector reads our own ERROR lines out of journalctl), and the reply
    names the situation instead of printing Python. The rollback matters as
    much as the catch — a session left in a failed transaction would make the
    runner's own ``_store_offset`` throw next, which is the very loop we are
    closing. Real failures still stop being invisible; they just stop taking
    the watchtower with them.

    Authorization stays OUTSIDE the barrier on purpose: a foreign chat must
    receive nothing, not even a failure notice.
    """
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return []
    if chat_id != str(admin_chat_id):
        logger.warning("watchtower: ignored update from foreign chat")
        return []
    try:
        return _dispatch(
            session, update, probes=probes, now=now,
            whatsapp_client=whatsapp_client,
        )
    except Exception:  # noqa: BLE001 — see the docstring: outage vs failed tap
        logger.error("watchtower update failed", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — a dead session must not loop either
            logger.error("watchtower rollback failed", exc_info=True)
        return _failure_outcome(update)


def _dispatch(
    session: Session,
    update: dict[str, Any],
    *,
    probes: HealthProbes,
    now: datetime,
    whatsapp_client: WhatsAppClient | None = None,
) -> list[Outcome]:
    """The router proper — reached only for the allow-listed operator."""
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
        text, keyboard = _menu_screen()
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
