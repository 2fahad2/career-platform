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
import uuid
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
    DeliveryMessage,
    DiscoveryRun,
    FunnelSession,
    InboundMessage,
    OnboardingSession,
    OutcomeEvent,
    Subscription,
    SupportEvent,
    Tenant,
    TenantDayState,
    TenantJobSuppression,
    UsageEvent,
)
from career.salla import subscriptions as _sub_states
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

#: The command goes on a LINE OF ITS OWN — inside the Arabic sentence the
#: operator's client reversed it, and alone it is also tappable.
EXPIRED_BUTTON_AR = "انتهت صلاحية الزر — أرسل\n/start"
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
#: tenant with no live subscription escaped `handle_update` — and at the time
#: the runner stored the Telegram offset only AFTER handle_update returned, so
#: the same update was re-fed every five seconds forever and the operator's
#: whole watchtower was dead until someone restarted the service. That
#: ordering is gone (``run_admin_bot.process_one_update`` now stores the offset
#: BEFORE the work, in its own transaction, and announces the skip), so the
#: same escape today costs one dropped tap instead of the console — but the
#: guard stays, because a dropped tap on a live customer's card is still a
#: refusal nobody was told about. Shell tenants are routine (every §04 upgrade
#: leaves one) and the customers list filters nothing, so the pause button sat
#: live on their cards.
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
#: ── the two sold promises the operator has to be able to ACT on ────────────
#: Detection is the machine's; both of these are decisions. The 72-hour
#: guarantee's remedy moves money and the store page hands the choice to the
#: customer — «استرداد كامل أو تمديد الاشتراك — أنت تختار» — so nothing
#: refunds or extends on a timer; the operator records what the customer
#: chose and this applies the half a machine can apply. The career session is
#: a human appointment start to finish: what these buttons keep is the
#: ledger, and the ledger is what the refund page's «تُخصم … جلسة المسار ١٥٠
#: ريالًا» is computed from.
GUARANTEE_REFUND_AR = (
    "✅ سجّلنا اختياره: استرداد كامل\n{code}\n"
    "نفّذ الاسترداد من سلة بنفس وسيلة دفعه — ما حرّكنا أي مبلغ من هنا\n"
    "وأول ما يوصلنا إشعار الاسترداد تتوقف خدمته تلقائيًا"
)
GUARANTEE_EXTEND_AR = (
    "✅ مددنا اشتراكه بعدد الأيام اللي راحت عليه\n{code}\n"
    "الأيام:\n{days}\n"
    "وينتهي اشتراكه الآن في:\n{end}"
)
GUARANTEE_WAIVE_AR = (
    "✅ سجّلنا أن العميل ما طلب استردادًا ولا تمديدًا\n{code}"
)
#: Refusals. Each one names what is actually true, because «تم» on an action
#: that changed nothing is the failure this file keeps re-learning.
GUARANTEE_NOT_BREACHED_AR = (
    "⚪ لا يوجد ضمان مكسور لهذا العميل — لم نغيّر شيئًا\n{code}"
)
GUARANTEE_NO_PERIOD_AR = (
    "⚪ ما عنده مدة اشتراك قابلة للتمديد — لم نغيّر شيئًا\n"
    "سجّل الاسترداد أو التنازل بدلًا عنه\n{code}"
)
SESSION_LOGGED_AR = (
    "✅ سجّلنا طلب جلسة المسار\n{code}\n"
    "والمهلة المعلنة للرد أربع وعشرون ساعة"
)
SESSION_ALREADY_OPEN_AR = (
    "ℹ️ عنده طلب جلسة مفتوح أصلًا — ما سجّلنا طلبًا ثانيًا\n{code}"
)
SESSION_ALREADY_USED_AR = (
    "⚪ استلم جلسته لهذه الفترة — واحدة لكل اشتراك\n{code}"
)
SESSION_NOT_ENTITLED_AR = (
    "⚪ جلسة المسار غير مشمولة في خطته — ما سجّلنا شيئًا\n{code}"
)
SESSION_SCHEDULED_AR = "✅ سجّلنا أنكم اتفقتم على موعد\n{code}"
SESSION_COMPLETED_AR = (
    "✅ سجّلنا أن الجلسة تمت\n{code}\n"
    "وتُخصم قيمتها المعلنة من أي استرداد لاحق"
)
SESSION_NO_REQUEST_AR = (
    "⚪ لا يوجد طلب جلسة لهذا العميل — لم نغيّر شيئًا\n{code}"
)

_ACTIONS = {
    "pause": ("⏸️ إيقاف مؤقت", "✅ أوقفنا الخدمة مؤقتًا للعميل\n{code}"),
    "resume": ("▶️ استئناف", "✅ استأنفنا الخدمة للعميل\n{code}"),
    "resend": ("📤 إعادة إرسال الحزمة", RESEND_NOTHING_AR),
    "issue_link": ("🔑 إصدار رابط تفعيل", LINK_NO_ORDER_AR),
    "g_refund": ("💸 استرداد كامل (ضمان ٧٢ ساعة)", GUARANTEE_NOT_BREACHED_AR),
    "g_extend": ("📆 تمديد المدة (ضمان ٧٢ ساعة)", GUARANTEE_NOT_BREACHED_AR),
    "g_waive": ("🤝 تنازل العميل (ضمان ٧٢ ساعة)", GUARANTEE_NOT_BREACHED_AR),
    "cs_request": ("📅 تسجيل طلب جلسة مسار", SESSION_NOT_ENTITLED_AR),
    "cs_scheduled": ("📅 تم التنسيق للجلسة", SESSION_NO_REQUEST_AR),
    "cs_completed": ("✅ تمت جلسة المسار", SESSION_NO_REQUEST_AR),
}

#: ── open support tickets ────────────────────────────────────────────────────
#: `support_events` has been written since C4 — «دعم» from a customer, and now
#: the funnel's consent stall — and NOTHING has ever read the table. The
#: operator is paged once, at the moment it happens, and after that the ticket
#: exists only in the database. This screen is what makes them visible: what is
#: open, for how long, and whose (TEN code only).
TICKETS_TITLE_AR = "🎫 التذاكر المفتوحة"
TICKETS_NONE_AR = "🟢 لا توجد تذاكر مفتوحة"
#: Ticket kinds, in the words of what actually happened to the customer.
#: The لمّاح+ direct line carries the star, here and on the alert, for the
#: reason `telegram.messages._PRIORITY_PLANS` gives: the operator picks who to
#: answer from THIS list, so a tier he only learns by opening a card is a tier
#: he learns too late. The kind string itself is
#: `promises.career_session.DIRECT_MESSAGE_TICKET_KIND`; it is written out
#: rather than imported because this table is a pure render map and the module
#: is imported lazily everywhere else in this file.
_TICKET_KIND_AR = {
    "support_request": "طلب التواصل مع الدعم",
    "funnel_consent_stuck": "متعثّر عند بوابة الموافقة",
    "executive_direct_message": "⭐ رسالة مباشرة من مشترك لمّاح+",
    "career_session_overdue": "📅 طلب جلسة مسار تجاوز مهلة الرد",
    # Money taken for a service that cannot be delivered: he silenced his
    # messages and then bought, or renewed. Nothing else says it — the night
    # closes him SKIPPED_OPTED_OUT, which is an honest state and exits zero, so
    # the operator's screens read it as the customer's own choice rather than
    # as a charge against a service he will not receive.
    "paid_while_opted_out": "💸 دفع أو جدّد وهو موقف الرسائل",
}
#: How many tickets one screen shows. A watchtower screen the operator has to
#: scroll is a screen they stop reading; the count line stays truthful about
#: the rest.
TICKETS_PAGE = 10

#: ── closing a ticket ────────────────────────────────────────────────────────
#: The screen shipped read-only, and read-only was not a smaller version of
#: this feature — it was a screen with a fuse. `support_events.status` is
#: written «open» by ``whatsapp.worker`` and by the funnel, and NOTHING in the
#: codebase ever wrote it again; `resolved_at` was never written at all. So the
#: filter «status = open» matched every ticket that had ever existed, the list
#: is ordered oldest-first and cut at ten, and after the tenth ticket the
#: operator's screen freezes: the same ten forever, every later «دعم» invisible
#: behind them, the count line the only hint that anything else exists. The
#: table's first reader has to be able to write the one column that makes
#: reading it work.
#:
#: Closing is MANUAL and means «I have dealt with this human», not «the system
#: decided it was over» — nothing about the ticket's own data can know that, so
#: nothing here closes one automatically. It travels the same one-shot nonce as
#: every other mutating action; it keeps its own callback names because the
#: `_ACTIONS` table is keyed by TEN code and a ticket is not a tenant.
_TICKET_ACTION = "ticket_close"
#: `support_events.status`. «open» is written by ``whatsapp.worker`` and by the
#: funnel's consent stall; «resolved» is written HERE and nowhere else, which
#: is why the tests may no longer conjure it with raw SQL — a state only a test
#: can produce is a state that was never really tested.
TICKET_OPEN = "open"
TICKET_RESOLVED = "resolved"
TICKET_CONFIRM_AR = (
    "⚠️ تأكيد إغلاق تذكرة العميل:\n{code}\n"
    "الإغلاق يعني أنك تكفّلت بها — ما راح تظهر في القائمة بعدها\n"
    "الزر صالح ٥ دقائق."
)
TICKET_CLOSED_AR = "✅ أغلقنا التذكرة\n{code}"
TICKET_ALREADY_AR = "⚪ التذكرة مغلقة أصلًا — لم نغيّر شيئًا\n{code}"
TICKET_GONE_AR = "⚪ لم نجد هذه التذكرة — قد تكون أُغلقت من شاشة أخرى"

#: ── the forgotten ticket ────────────────────────────────────────────────────
#: THREE paths refuse to raise a second ticket while one is open for the same
#: customer — the لمّاح+ direct line (`promises.career_session`), the funnel's
#: consent stall (`funnel.flow`) and the paid-while-opted-out alert
#: (`whatsapp.activation_flow`) — and every one of them is right to. A customer
#: who writes four messages in a row is one human waiting, not four, and paging
#: per message is how an operator learns to stop reading the channel. (The
#: «دعم» keyword in `whatsapp.worker` is the exception that proves it: it
#: dedupes on nothing, because a customer who types the word twice has asked
#: twice.)
#:
#: What none of them has is a way OUT. Nothing closed a ticket but the button
#: above, and nothing swept a forgotten one — so one ticket left open by
#: accident silenced that customer's line for as long as it stayed open, and
#: the ticket kept the age and the ``inbound_message_id`` of the FIRST message
#: forever: the queue said «since when» and never «about what». The direct
#: line's own docstring states both as residue
#: (``promises.career_session.escalate_direct_message``); this is the sweep it
#: asks for.
#:
#: WHAT MAKES A TICKET FORGOTTEN — age alone, deliberately.
#:
#: «Age plus no operator action» was the obvious alternative and it is not
#: available honestly. The tier sells «تلقاني على نفس المحادثة»: the common
#: route is that the operator answers the customer in WhatsApp from his own
#: phone, and that route leaves nothing in our tables at all. So an activity
#: test would report «nobody did anything» about every ticket he handled the
#: way the store page promises.
#:
#: AUDIT 2026-08-07 — that used to say NOTHING he does reaches our tables,
#: which is a stronger claim than the facts and it is false: `_run_reply`, the
#: console's own reply box, sends the customer WhatsApp text and writes a
#: `delivery_messages` row through `record_out(kind=REPLY_KIND)`. The rule
#: survives the correction because the weaker sentence is the one it needed.
#: That row carries a tenant and a channel and NO ticket, so at best it says
#: «this customer was written to», never «THIS ticket was dealt with» — and it
#: exists only for the replies that went through the console. An activity test
#: built on it would still be silent about every answer sent from his phone,
#: and would still be unable to attribute the ones it can see.
#:
#: The one action that means «I have dealt with this human» is the close
#: button, and a ticket still open is precisely the absence of it. Age it is.
#:
#: The distinction the age cannot make — unanswered vs merely unnoticed — is
#: put in the ALERT instead of in the rule: it carries how many messages the
#: customer has sent since, which separates a person still writing into
#: silence from one who was answered off-console and left a row behind. It
#: colours the sentence; it never decides the release.
TICKET_FORGOTTEN_AFTER = timedelta(hours=48)
#: `support_events.status`, third value: open, then RELEASED, then resolved.
#: Not a closure — a released ticket is still owed, still on the screen below,
#: still carrying its original age, and still waiting for the operator's own
#: button. What it stops being is a MUTE. The dedupes above all read «status =
#: open», so this one word is what lets the customer's next message raise a
#: ticket of its own, with its own age and its own message id.
#:
#: Auto-CLOSING was the alternative, and it is the one thing this screen must
#: never do: «resolved» is a claim that a human was dealt with, and a claim
#: nobody can make on the operator's behalf. It would also take the customer
#: off the queue entirely — the promise dropped silently, which is worse than
#: the mute it was fixing.
#:
#: The uniform rule (any kind, not a list of the kinds that dedupe today) is
#: deliberate: it states one invariant — NO TICKET MUTES ANYTHING FOR LONGER
#: THAN THIS — that stays true when a fifth dedupe is written tomorrow. A list
#: of kinds copied from four other modules is a list that rots silently, which
#: is the failure this sweep exists to end, rebuilt one level up.
TICKET_RELEASED = "released"
#: The storm the dedupe exists to prevent is NOT rebuilt, and the arithmetic
#: is the same as `scripts/alert_unit_failure.sh`'s: a release is a one-way
#: edge written once per ticket, so it pages at most once per ticket, ever —
#: and the next ticket for that customer cannot exist until he writes again
#: AND ages another two days. Ceiling: one page per customer per two days,
#: each one carrying a different message, a different id and a different age.
#: Repetition is what gets muted; escalation with a changed number is not.
#:
#: The one-way edge is only half of that «ever», and the half it does not
#: cover is what :func:`release_forgotten_tickets` had to buy with a lock: an
#: edge written once still pages twice if two transactions read the row open
#: before either wrote it. The ceiling above is the SELECT's claim as much as
#: the column's.
TICKET_FORGOTTEN_ALERT_AR = (
    "🔁 تذكرة عميل مفتوحة من زمان وما أحد أغلقها\n"
    "{kind}\n"
    "{code}\n"
    "{age}\n"
    "{traffic}\n"
    "فتحنا خطه من جديد — أي رسالة جديدة منه ترفع تذكرة وتوصلك\n"
    "والتذكرة نفسها باقية في القائمة لين تغلقها"
)
TICKET_TRAFFIC_SINCE_AR = "وراسلك بعدها مرات عددها: {count}"
#: No new message since. Said out loud rather than left blank, because it is
#: the likeliest shape of «you answered him in واتساب and the row stayed».
TICKET_TRAFFIC_QUIET_AR = "وما راسلك بعدها ولا مرة"
#: On the screen, under a released ticket.
TICKET_RELEASED_MARK_AR = "⏳ مفتوحة من زمان — فتحنا خط العميل ومازالت تنتظرك"

#: WHICH message opened the ticket. `inbound_message_id` has been stored since
#: the direct line shipped and rendered NOWHERE, which is half of «the queue
#: says since when, never about what». The body is PII and stays out of this
#: channel forever (§15.13) — what is safe, and what actually answers the
#: operator's question, is the SHAPE: a customer who wrote to you, versus a
#: stale card tapped by a thumb. (The worker refuses to escalate a tap today;
#: tickets raised before that rule are still in this table, and the funnel and
#: the «دعم» paths still point at messages of every shape.)
_TICKET_OPENER_AR = {
    "text": "فتحتها رسالة كتبها العميل",
    "audio": "فتحتها رسالة صوتية من العميل",
    "voice": "فتحتها رسالة صوتية من العميل",
    "image": "فتحتها صورة أرسلها العميل",
    "video": "فتحتها مقطع أرسله العميل",
    "document": "فتحتها ملف أرسله العميل",
    "sticker": "فتحتها ملصق أرسله العميل",
    "button": "فتحتها ضغطة زر — يمكن ما قصد يراسلك",
    "interactive": "فتحتها ضغطة زر — يمكن ما قصد يراسلك",
}
#: A shape we have no Arabic word for keeps the raw token on a line of its own
#: — a Latin word inside an Arabic line arrives reversed on his client.
TICKET_OPENER_OTHER_AR = "فتحتها رسالة من نوع:"
#: Nothing to point at — the funnel's consent stall, the session SLA, the
#: paid-while-silenced alert. All three are raised BY US about a customer's
#: situation rather than about one message, and none of them stores an id.
#:
#: A ticket whose message row was deleted underneath it was drafted as a
#: second sentence here and removed as fiction: the FK is ON DELETE SET NULL,
#: but the only path that deletes an inbound message is a §12 data deletion,
#: which deletes `customer_channels` — and support_events cascades from THAT.
#: The customer's tickets go with his channel, so a ticket pointing at a
#: vanished message is not a state this system can reach, and a line for it
#: would be a line no operator will ever see and no test can honestly produce.
TICKET_OPENER_SYSTEM_AR = "ما فيها رسالة — المنظومة هي اللي فتحتها"

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
    if name == "promises":
        return _promises_screen(session, now=now)
    if name == "tclose":
        # v1|tclose|<ticket uuid> → the confirm card carrying a one-shot nonce
        return _ticket_close_card(session, ticket_id=arg, now=now)
    if name == "tdone":
        # v1|tdone|<nonce> → the close itself, then the refreshed list
        return _run_ticket_close(session, nonce=arg, now=now)
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
    return text, [
        *keyboard,
        [("🎫 التذاكر المفتوحة", "v1|tickets"),
         # The two sold promises with a customer waiting behind them. It sits
         # on the menu, not behind a customer's card, because the question it
         # answers — «هل عليّ شيء لأحد؟» — is not asked about anyone in
         # particular, and a promise you have to go looking for is a promise
         # that is answered late.
         ("🤝 الوعود المستحقة", "v1|promises")],
    ]


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


def _opener_lines(opener_type: str | None) -> list[str]:
    """The shape of the message behind a ticket — never one character of it."""
    if opener_type is None:
        return [TICKET_OPENER_SYSTEM_AR]
    known = _TICKET_OPENER_AR.get(str(opener_type))
    if known is not None:
        return [known]
    return [TICKET_OPENER_OTHER_AR, str(opener_type)]


def _tickets_screen(
    session: Session, *, now: datetime
) -> tuple[str, Keyboard]:
    """Every open support ticket, oldest first — the table's first reader.

    Oldest first because age is the only priority signal this screen has: the
    ticket that has been open longest is the customer who has been waiting
    longest, and a paying customer who asked for a human is the one thing
    this product may not lose. TEN codes only (§15.13) — the card behind each
    one is a tap away and carries no PII either.

    Each listed ticket carries a NUMBER and a matching «إغلاق» button. The
    number exists because the button is the only place the two could be tied
    together: a label reading «إغلاق TEN-0002» mixes Arabic and Latin on one
    line and arrives reversed on the operator's client, so the tie is an
    Arabic-Indic numeral (which is what :func:`_ar_digits` is for) and the
    code stays alone on its own line in the body.

    RELEASED tickets are listed here too, and that is the whole reason the
    release is not a close: the sweep takes a forgotten ticket's power to
    silence a customer away from it, and takes nothing else — the row keeps
    its own age, keeps its place at the top of a list ordered oldest-first,
    and keeps waiting for the one button that means a human was dealt with.

    Each ticket also says WHICH message opened it, in shape only. See
    :data:`_TICKET_OPENER_AR`: with a dedupe upstream, «a customer wrote to
    you» and «a thumb hit a card from three months ago» are the same row
    otherwise, and they are not the same thing to answer.
    """
    rows = session.execute(
        select(SupportEvent.id, Tenant.code, SupportEvent.kind,
               SupportEvent.created_at, SupportEvent.status,
               InboundMessage.message_type)
        .join(Tenant, Tenant.id == SupportEvent.tenant_id)
        .outerjoin(InboundMessage,
                   InboundMessage.id == SupportEvent.inbound_message_id)
        .where(SupportEvent.status.in_((TICKET_OPEN, TICKET_RELEASED)))
        .order_by(SupportEvent.created_at)
    ).all()
    lines = [f"{TICKETS_TITLE_AR}: {_ar_digits(len(rows))}"]
    if not rows:
        lines.append(TICKETS_NONE_AR)
    close_buttons: list[tuple[str, str]] = []
    for number, row in enumerate(rows[:TICKETS_PAGE], start=1):
        ticket_id, code, kind, created_at, status, opener_type = row
        marker = _ar_digits(number)
        lines.append("")
        # an unknown kind keeps its own line: it is a raw Latin token and a
        # mixed Arabic+Latin line arrives scrambled on the operator's client
        known = _TICKET_KIND_AR.get(str(kind))
        lines.append(f"{marker} • {known}" if known else f"{marker} •\n{kind}")
        lines.append(str(code))
        lines.append(_age_ar(created_at, now))
        lines.extend(_opener_lines(opener_type))
        if status == TICKET_RELEASED:
            lines.append(TICKET_RELEASED_MARK_AR)
        close_buttons.append((f"✅ إغلاق {marker}", f"v1|tclose|{ticket_id}"))
    if len(rows) > TICKETS_PAGE:
        lines.append("")
        lines.append(
            f"وأقدم {_ar_digits(TICKETS_PAGE)} معروضة من أصل "
            f"{_ar_digits(len(rows))}"
        )
        # The count line was already honest about the number hidden; what it
        # could not say, before there was any way to close one, was that the
        # rest were unreachable rather than merely next.
        lines.append("أغلق الظاهرة ليطلع اللي بعدها")
    keyboard: Keyboard = [
        close_buttons[i:i + 3] for i in range(0, len(close_buttons), 3)
    ]
    keyboard.append([("🔄 تحديث", "v1|tickets"), ("🏠 الرئيسية", "v1|menu")])
    return "\n".join(lines), keyboard


#: How a breach's facts are said out loud. The packet the sweep stores is
#: PII-free and machine-shaped; these are the two lines that change what the
#: operator DOES — an empty market is a conversation about extending, our own
#: failures are a conversation about refunding before he is asked.
def _fact_lines_ar(facts: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    day_states = facts.get("day_states") or {}
    for state, count in sorted(day_states.items()):
        # `day_state_ar` rather than `state_label`: it RETURNS None instead of
        # the raw token, so «is this word safe inside an Arabic line?» is
        # answered by the type instead of by comparing two strings.
        label_ar = views.day_state_ar(str(state))
        if label_ar is None:
            # no Arabic word for this state — the Latin token gets its own line
            lines.append(str(state))
            # `×` (U+00D7), not an ASCII `x`: the same multiplication sign the
            # translated branch already uses, so the two read alike — and a
            # Latin letter beside an Arabic numeral reversed this line.
            lines.append(f"× {_ar_digits(count)}")
        else:
            lines.append(f"{label_ar} × {_ar_digits(count)}")
    if facts.get("weekend_days"):
        lines.append("ومرت المهلة على عطلة نهاية الأسبوع")
    if facts.get("paused"):
        lines.append("وكان موقوفًا مؤقتًا بطلبه")
    if facts.get("opted_out"):
        lines.append("وكان موقفًا للرسائل")
    elif facts.get("opted_out_before_window"):
        lines.append("ودخل المهلة وهو موقف للرسائل")
    if facts.get("subscription_status") in _sub_states.TERMINAL_STATES:
        # Read LIVE by `open_breaches`, not out of the frozen packet: this
        # screen sits days after the breach and beside the remedy buttons, and
        # a refund that landed yesterday must not still read as an open
        # account here.
        lines.append("واشتراكه منتهٍ ماليًا الآن — راجع حالته قبل أي تعويض")
    return lines


def _promises_screen(
    session: Session, *, now: datetime
) -> tuple[str, Keyboard]:
    """Every promise with a customer still waiting behind it.

    The 72-hour guarantee and the لمّاح+ career session were both sold with
    no reader: a breach nobody was told about and a request nobody could see
    ageing. This screen is the reader. It shows age above all else, because
    age is the only priority either promise has, and it carries no PII — TEN
    codes on their own lines, exactly like every other screen (§15.13).
    """
    from career.promises import career_session, guarantee

    breaches = [
        {
            "code": row.code,
            "age": _age_ar(row.deadline_at, now),
            "first_delivery": row.first_delivery_at is not None,
            "fact_lines": _fact_lines_ar(row.facts),
        }
        for row in guarantee.open_breaches(session)
    ]
    sessions = [
        {
            "code": row.code,
            "status": row.status,
            "age": _age_ar(row.requested_at, now),
            "overdue": row.overdue,
        }
        for row in career_session.open_sessions(session, now=now)
    ]
    return views.render_promises(breaches, sessions)


def _ticket_close_card(
    session: Session, *, ticket_id: str, now: datetime
) -> tuple[str, Keyboard] | None:
    """The confirm card for one ticket — same shape as ``v1|act``'s."""
    row = _ticket_row(session, ticket_id)
    if row is None:
        return None
    _ticket, code = row
    nonce = _new_nonce(_TICKET_ACTION, ticket_id, now)
    return (
        TICKET_CONFIRM_AR.format(code=code),
        [[("✅ تأكيد نهائي", f"v1|tdone|{nonce}")],
         [("↩️ إلغاء", "v1|tickets")]],
    )


def _ticket_row(
    session: Session, ticket_id: str
) -> tuple[SupportEvent, str] | None:
    """(ticket, its tenant's TEN code) — None for anything unparseable or
    absent, so a stale button answers «gone» instead of raising."""
    try:
        parsed = uuid.UUID(ticket_id)
    except ValueError:
        return None
    ticket = session.get(SupportEvent, parsed)
    if ticket is None:
        return None
    code = session.execute(
        select(Tenant.code).where(Tenant.id == ticket.tenant_id)
    ).scalars().first()
    return ticket, str(code or "TEN-????")


def _run_ticket_close(
    session: Session, *, nonce: str, now: datetime
) -> tuple[str, Keyboard] | None:
    """Consume the one-shot nonce, resolve the ticket, redraw the list.

    Returns None for an expired or forged nonce so the caller answers with
    ACTION_EXPIRED_AR, exactly like every other confirmed action. The answer
    is prepended to a FRESHLY read list rather than to the stale one the
    operator tapped: closing the top ticket is what lets the eleventh appear,
    and a screen that still shows the ticket you just closed is the same
    silence in a new place.
    """
    entry = _pending_actions.pop(nonce, None)
    if entry is None:
        return None
    action, ticket_id, expiry = entry
    if action != _TICKET_ACTION or expiry < now:
        return None
    row = _ticket_row(session, ticket_id)
    if row is None:
        done = TICKET_GONE_AR
    else:
        ticket, code = row
        # RELEASED closes exactly like OPEN. The sweep only took away the
        # ticket's power to mute the customer; it is still a human waiting,
        # still on the screen, and the button under it has to work — a listed
        # ticket whose own button answers «مغلقة أصلًا» is the contradiction
        # inside one screen that `_transition` was fixed for.
        if ticket.status not in (TICKET_OPEN, TICKET_RELEASED):
            done = TICKET_ALREADY_AR.format(code=code)
        else:
            ticket.status = TICKET_RESOLVED
            # `resolved_at` has existed on the model since C4 and had never
            # been written by anything — an SLA cannot be measured from a
            # column nobody fills, and «when was this dealt with» is the first
            # question anyone asks of a closed ticket.
            ticket.resolved_at = now
            session.commit()
            done = TICKET_CLOSED_AR.format(code=code)
    text, keyboard = _tickets_screen(session, now=now)
    return f"{done}\n\n{text}", keyboard


def release_forgotten_tickets(
    session: Session, *, now: datetime
) -> list[str]:
    """Take the mute off every ticket nobody has closed in two days.

    Returns one alert per released ticket — the operator's copy, ready for
    any channel that can send it. Nothing is sent from here: this function is
    a fact about the ledger, and every caller in this file hands its strings
    back as :class:`Outcome` s so the runner does the talking.

    THE ACT ITSELF IS THE SUPPRESSION. `scripts/alert_unit_failure.sh` settled
    the same question yesterday for a wedged unit — repetition gets muted,
    escalation does not — and paid for a stamp file in /run to keep 283
    identical messages a day down to 24 that each carry a changed number.
    Here the stamp is free and durable: «open → released» is a one-way edge on
    a row, so a ticket pages once. There is no cadence to tune and no state to
    lose, and the sweep is idempotent at any frequency, which is what makes it
    safe to call from a screen tap.

    THAT LAST SENTENCE COST A LOCK, and it is worth saying why it did not come
    free. «Released once, therefore paged once» is an argument about the row,
    and this function has two callers in two processes: :func:`handle_update`
    below, on every operator tap, and
    ``scripts/run_worker_loop.sweep_forgotten_tickets``, hourly. The select
    read ``status = 'open'`` and the loop then wrote ``released`` keyed on
    ``id`` alone, with no status predicate between them — so two transactions
    could read the same open row before either committed, and both would write
    and both would return an alert. The ROW was never at risk: the edge stayed
    one-way and the two writers agree on the value. The operator was: he was
    paged twice about one customer, on the channel whose entire value is that
    a message on it means something new happened.

    The read is therefore what had to become the claim: ``FOR UPDATE OF
    support_events SKIP LOCKED``. SKIP and not wait, deliberately — a blocking
    sweep would hang an operator's screen tap on the worker's open
    transaction, and the row another sweeper is holding is a row already being
    released and already being paged. Skipping loses no work; it is the second
    caller correctly finding nothing left to do. What that buys is exact and
    no more: at most one alert per ticket, ever, whatever the number of
    concurrent callers. Proven with two sessions and two transactions rather
    than argued (``tests/test_admin_console.py``).

    WHAT THIS DOES NOT DO, on purpose: it does not close, does not answer the
    customer, does not touch `resolved_at`, and does not remove the row from
    the operator's queue. A forgotten promise is not fixed by being tidied
    away; it is fixed by the operator, and all this does is stop it costing
    the customer his line while it waits for him.

    WHERE IT RUNS — both halves, now. From :func:`handle_update`, off the
    operator's own console traffic, which is honest and was never sufficient:
    an operator who has stopped opening the watchtower is exactly the operator
    who forgot the ticket, so the console alone releases fastest for the
    customers who need it least. The half that runs while he sleeps is
    ``scripts/run_worker_loop.sweep_forgotten_tickets`` — hourly, in the
    process that is up at 03:00, whether he taps or not. NOT ``cv.daily_run``:
    that module's own comments refuse a dependency on the operator console on
    purpose, and a delivery engine importing screens to send a ticket reminder
    would be the wrong debt.

    One thing that wiring decided rather than inherited, and the decision is
    written out where it is made: the release commits and the page is
    best-effort after it, so a Telegram outage loses a page that can never be
    raised again (the edge is one-way). That is the same trade
    ``promises.career_session.escalate_overdue`` already makes with
    ``escalated_at``, and it is the right one — the ticket stays on this
    screen either way — but it is a trade, not an oversight.
    """
    rows = session.execute(
        select(SupportEvent, Tenant.code)
        .join(Tenant, Tenant.id == SupportEvent.tenant_id)
        .where(SupportEvent.status == TICKET_OPEN,
               SupportEvent.created_at <= now - TICKET_FORGOTTEN_AFTER)
        .order_by(SupportEvent.created_at)
        .with_for_update(skip_locked=True, of=SupportEvent)
        # `of=SupportEvent` and not a bare FOR UPDATE: the join is only there
        # to read the TEN code, and locking `tenants` rows would put every
        # sweep in the way of every other writer that touches a tenant.
    ).all()
    alerts: list[str] = []
    for ticket, code in rows:
        ticket.status = TICKET_RELEASED
        # AUDIT 2026-08-07 — the ticket's OWN opening message has to be taken
        # out by ID, and `received_at > created_at` does not do it. The two
        # stamps come from two different clocks: the ticket carries the `now`
        # the worker loop captured BEFORE it opened its transaction, and the
        # inbound row carries `received_at`'s server default (`func.now()`,
        # the transaction clock), which is therefore always LATER. So every
        # لمّاح+ ticket counted the message that opened it and paged «وراسلك
        # بعدها مرات عددها: ١» about a customer who had written once and
        # waited — and TICKET_TRAFFIC_QUIET_AR, described as the likeliest
        # shape of all, was unreachable for the one kind that stores an id.
        # That is the whole distinction this line exists to draw: a person
        # still writing into silence versus one answered off-console.
        counted = [
            InboundMessage.tenant_id == ticket.tenant_id,
            InboundMessage.received_at > ticket.created_at,
        ]
        if ticket.inbound_message_id is not None:
            counted.append(InboundMessage.id != ticket.inbound_message_id)
        since = session.execute(
            select(func.count()).select_from(InboundMessage).where(*counted)
        ).scalar_one()
        kind = _TICKET_KIND_AR.get(str(ticket.kind)) or str(ticket.kind)
        alerts.append(TICKET_FORGOTTEN_ALERT_AR.format(
            kind=kind,
            code=str(code),
            age=_age_ar(ticket.created_at, now),
            traffic=(TICKET_TRAFFIC_SINCE_AR.format(count=_ar_digits(since))
                     if since else TICKET_TRAFFIC_QUIET_AR),
        ))
    if alerts:
        session.flush()
        logger.warning("released %d forgotten support tickets", len(alerts))
    return alerts


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


def _deduction_lines(session: Session, *, row: Any) -> str:
    """«كم يُخصم من استرداده» — and, inseparably, «عن أي فترة».

    AUDIT 2026-08-07. This line printed a CONSTANT: `f"{SESSION_VALUE_SAR}
    SAR"`, under a comment claiming it was computed, while
    ``career_session.refund_deduction_sar`` — the function that actually reads
    the ledger, and the one the refund page's «تُخصم قيمة الخدمات البشرية اللي
    استلمتها فعليًا» describes — had no production caller anywhere. Two
    separate things were wrong with that, and only one of them is arithmetic:

    * The number was not a fact about this customer. It happens to agree with
      the ledger in every state reachable today, and that is a coincidence of
      the catalog, not a property of the screen: `uq_career_sessions_live_per_
      subscription` allows one live row per period, so «completed sessions in
      this period» is 1 whenever this line renders at all. Nothing about that
      is visible from here, nothing keeps it true (a cancel path, a second
      session, a price change), and a screen that would keep printing 150
      after the ledger stopped agreeing is a screen the operator cannot use
      to answer a customer who is disputing money.
    * The screen never said WHICH period it had counted. «150 will be
      deducted» is not an answer without «from the refund of which order» —
      and this same card can show a session belonging to a DIFFERENT period
      than the current one (`career_session.current_session` deliberately
      falls back across periods so a renewal cannot hide an unanswered
      request), so the operator had no way to know the two lines above and
      below each other were even about the same subscription.

    There is no ambiguity to resolve at this call site, and that is worth
    writing down rather than assuming: the fallback only ever returns an OPEN
    row (REQUESTED/SCHEDULED), so a COMPLETED row on this card always came
    from the current period. The deduction is therefore scoped to
    ``row.subscription_id`` — the very row being displayed — instead of
    re-resolving «the current subscription» independently and risking the two
    halves of one screen describing two different worlds, which is the exact
    defect the 2026-08-06 audit fixed inside `career_session` itself.

    What the screen CANNOT know is which order the operator is refunding: a
    customer may be asking about a period that ended months ago. So the last
    line says so outright instead of letting a single number be read as «the
    deduction», full stop.
    """
    from career.db.models import Subscription
    from career.promises import career_session

    amount = career_session.refund_deduction_sar(
        session, tenant_id=row.tenant_id, subscription_id=row.subscription_id,
    )
    count = career_session.completed_sessions_count(
        session, tenant_id=row.tenant_id, subscription_id=row.subscription_id,
    )
    subscription = session.get(Subscription, row.subscription_id)
    start = getattr(subscription, "current_period_start", None)
    # Every line is direction-pure (§16): the amount is Latin and sits alone,
    # the period start is a date and sits alone, the counts are Arabic-Indic.
    lines = [f"{amount} SAR", f"عن جلسات فترة اشتراك واحدة، عددها المحسوب: "
             f"{_ar_digits(count)}"]
    if start is not None:
        lines.append("وهي الفترة التي تبدأ في")
        lines.append(start.astimezone(_RIYADH).date().isoformat()
                     if start.tzinfo else start.date().isoformat())
    lines.append("واسترداد أي فترة ثانية يُحسب على حدة")
    return "\n".join(lines)


def _promise_facts(
    session: Session, *, tenant_id: Any, now: datetime
) -> dict[str, Any]:
    """The three sold promises, as this customer's card sees them.

    Read here rather than in the view for the usual reason — views are pure —
    but gathered as ONE dict because they are one question: «هل عليّ شيء لهذا
    العميل؟». All three are PII-free by construction: a status, an age, a
    price.
    """
    from career.db.models import DeliveryGuarantee, PriceLock
    from career.promises import career_session

    guarantee_row = session.execute(
        select(DeliveryGuarantee)
        .where(DeliveryGuarantee.tenant_id == tenant_id)
    ).scalars().first()
    guarantee_card: dict[str, Any] | None = None
    if guarantee_row is not None:
        anchor = (
            guarantee_row.deadline_at
            if guarantee_row.status in ("BREACHED", "SETTLED")
            else guarantee_row.activated_at
        )
        guarantee_card = {
            "status": guarantee_row.status,
            "age": _age_ar(anchor, now),
        }

    session_row = career_session.current_session(session, tenant_id=tenant_id)
    session_card: dict[str, Any] | None = None
    if session_row is not None:
        session_card = {
            "status": session_row.status,
            "age": _age_ar(session_row.requested_at, now),
            "overdue": bool(
                session_row.status == career_session.REQUESTED
                and now >= session_row.requested_at + timedelta(
                    hours=career_session.RESPONSE_SLA_HOURS
                )
            ),
            # The published deduction, computed rather than remembered — the
            # refund page promises «ونعرض عليك الأرقام قبل موافقتك».
            "deduction": (
                _deduction_lines(session, row=session_row)
                if session_row.status == career_session.COMPLETED else None
            ),
        }

    lock = session.execute(
        select(PriceLock.amount_sar, PriceLock.currency)
        .where(PriceLock.tenant_id == tenant_id, PriceLock.lapsed_at.is_(None))
        .order_by(PriceLock.locked_at.desc())
    ).first()
    return {
        "guarantee": guarantee_card,
        "career_session": session_card,
        "session_entitled": career_session.entitled(session, tenant_id),
        "price_lock": f"{lock[0]} {lock[1]}" if lock else None,
    }


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
        # `Delivery.id` is selected for one reason and it is not display: it is
        # the join key for the refusal predicate below. Nothing renders it.
        select(Delivery.id, Delivery.run_date, Delivery.status, Delivery.bundle)
        .where(Delivery.tenant_id == tenant.id)
        .order_by(Delivery.created_at.desc()).limit(1)
    ).first()
    # What LANDED on the last bundle, counted once and used twice — the number
    # the card shows and the number the refusal predicate reads.
    landed = 0 if delivery is None else len(
        ((delivery[3] or {}).get("results") or {}).get("delivered") or []
    )
    # ── «إما ما رد العميل أو ميتا رفضت الإرسال» → a definite sentence ────────
    #
    # `views._delivery_ar` hedges because the card could not tell the two
    # causes of an expiry apart: a customer who never wrote back, and a
    # re-engagement template Meta DECLINED to hand over (131047/131049/131050
    # — `whatsapp.worker.META_REFUSAL_CODES`). Two of the six live expiries
    # were the second one, and the operator read «he ignored us» about all six.
    #
    # `delivery_messages.status == 'failed'` IS that fact, in full: every one
    # of Meta's refusal codes lands on that one word, deliberately — the
    # status column is load-bearing for money (`cv/close.whatsapp_spend` bills
    # everything not exactly `failed`), so the worker refused to invent a
    # «refused» rung for it. There is nothing else to join on.
    #
    # AND NOTHING LANDED, which is narrower than «any failed row» and is the
    # honest width. The sentence this unlocks is «ما وصلت العميل» — it did not
    # reach him — and on a bundle that delivered three CVs and had a fourth
    # refused that sentence is a NEW lie, in the opposite direction, on the one
    # screen whose entire purpose is not lying. Where a message DID land the
    # card keeps «وصل جزء منها»/«وصلت كاملة», which is true if less pointed;
    # where nothing landed the refusal is both true and the whole story. On the
    # expiries this change exists for the two predicates agree exactly (an
    # expired bundle delivered nothing), so the narrowing costs no case it was
    # written for and forecloses the one it was not.
    refused_by_meta = bool(
        delivery is not None
        and landed == 0
        and session.execute(
            select(func.count()).select_from(DeliveryMessage).where(
                DeliveryMessage.tenant_id == tenant.id,
                DeliveryMessage.delivery_id == delivery[0],
                DeliveryMessage.status == "failed",
            )
        ).scalar_one()
    )
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
    promises = _promise_facts(session, tenant_id=tenant.id, now=now)
    return {
        **promises,
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
                "run_date": delivery[1].isoformat(),
                "status": delivery[2],
                "delivered": landed,
                "failed": len(
                    ((delivery[3] or {}).get("results") or {}).get("failed")
                    or []
                ),
                # the fact that settles the expiry sentence; `views.REFUSED_KEY`
                # is the name the renderer reads it by.
                views.REFUSED_KEY: refused_by_meta,
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

    # Revenue is «the money that arrived and STAYED», and this screen used to
    # compute it here with no status predicate at all: a refunded order, a
    # cancelled one and a chargeback each counted forever, and a chargeback is
    # money that left the account twice. On the seeded data that overstated the
    # number by 312%. The correct definition already existed one module away —
    # cv.close.paid_subscriptions_by_plan, derived from the state machine's own
    # TERMINAL_STATES so a twelfth state cannot quietly become revenue — and
    # the operator prices the product off this figure. One definition, in the
    # file that owns money, read by everything that shows money.
    from career.cv import close as close_mod

    subs_by_plan, revenue = close_mod.paid_subscriptions_by_plan(
        session, since=cutoff
    )

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

    # §14 spend, from the ONE definition of «what did this cost» — the metered
    # usage_events kinds plus the WhatsApp half derived from the delivery
    # ledger. This screen used to spell that expression out by hand, which is
    # how the codebase came to hold two of them: the same sum lived here and in
    # close.rollup_costs, they disagreed about which kinds count, and only this
    # one was ever looked at. close.spend_by_kind is now that expression, and a
    # screen that re-implements money is a screen that will drift from it.
    from career.cv import close as close_mod

    spend: dict[str, tuple[int, Decimal]] = close_mod.spend_by_kind(
        session, since=cutoff
    )

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
    path the exception left ``handle_update``; the runner then advanced the
    Telegram offset only after the work, so it never advanced at all and one
    tap wedged the entire console into a five-second retry of the same dead
    update. The runner has since been turned around — the offset is stored
    first — so the wedge itself is closed, and what is left without this guard
    is a tap that silently does nothing to a paying customer's subscription.
    So the live row is resolved HERE, with the same reader privacy uses
    (``current_subscription`` — its absence is a fact, not an error), and the
    two named refusals of the subscription layer are answered instead of
    propagating.

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


def _run_guarantee_remedy(
    session: Session, *, tenant_id: Any, code: str, action: str,
    now: datetime,
) -> str:
    """Apply the remedy the CUSTOMER chose for a broken 72-hour guarantee.

    The refund half deliberately moves no money: the store page says the money
    returns «بنفس وسيلة دفعك عبر سلة», and when the operator makes it there,
    the `order.refunded` webhook is what stops the service. Recording REFUNDED
    from here would race that authority and could switch off a customer who is
    still owed the days he paid for.

    The extension half is applied, because adding days is something only this
    system can do correctly — and it is applied through a `subscription_events`
    row, so a paid period never changes without a trail.
    """
    from career.promises import guarantee

    remedy = {
        "g_refund": guarantee.REMEDY_REFUND,
        "g_extend": guarantee.REMEDY_EXTENSION,
        "g_waive": guarantee.REMEDY_WAIVED,
    }[action]
    result = guarantee.apply_remedy(
        session, tenant_id=tenant_id, remedy=remedy, now=now,
    )
    if result.outcome == "no_period":
        session.rollback()
        return GUARANTEE_NO_PERIOD_AR.format(code=code)
    if result.outcome != "applied":
        session.rollback()
        return GUARANTEE_NOT_BREACHED_AR.format(code=code)
    session.commit()
    if remedy == guarantee.REMEDY_REFUND:
        return GUARANTEE_REFUND_AR.format(code=code)
    if remedy == guarantee.REMEDY_WAIVED:
        return GUARANTEE_WAIVE_AR.format(code=code)
    end = result.new_period_end
    # the date stands alone: a Latin date inside an Arabic line is scrambled
    return GUARANTEE_EXTEND_AR.format(
        code=code, days=result.days,
        end=end.astimezone(_RIYADH).date().isoformat() if end else "؟",
    )


def _run_career_session(
    session: Session, *, tenant_id: Any, code: str, action: str,
    now: datetime,
) -> str:
    """Record a career session's one honest step: asked, arranged, or held.

    Every branch answers with what the LEDGER says afterwards, never with the
    fact that a function returned — the same rule pause/resume had to learn
    the hard way. «Already asked» and «already had one this period» are
    different sentences to a customer, so they are different sentences here.
    """
    from career.promises import career_session

    if action == "cs_request":
        result = career_session.request_session(
            session, tenant_id=tenant_id, now=now, source="operator",
        )
        if result.outcome == "created":
            session.commit()
            return SESSION_LOGGED_AR.format(code=code)
        session.rollback()
        if result.outcome == "already_open":
            return SESSION_ALREADY_OPEN_AR.format(code=code)
        if result.outcome == "already_used":
            return SESSION_ALREADY_USED_AR.format(code=code)
        return SESSION_NOT_ENTITLED_AR.format(code=code)

    if action == "cs_scheduled":
        outcome, _row = career_session.mark_scheduled(
            session, tenant_id=tenant_id, now=now
        )
        done = SESSION_SCHEDULED_AR
    else:
        outcome, _row = career_session.mark_completed(
            session, tenant_id=tenant_id, now=now
        )
        done = SESSION_COMPLETED_AR
    if outcome != "applied":
        session.rollback()
        return SESSION_NO_REQUEST_AR.format(code=code)
    session.commit()
    return done.format(code=code)


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
    if action in ("g_refund", "g_extend", "g_waive"):
        return code, _run_guarantee_remedy(
            session, tenant_id=tenant.id, code=code, action=action, now=now,
        )
    if action in ("cs_request", "cs_scheduled", "cs_completed"):
        return code, _run_career_session(
            session, tenant_id=tenant.id, code=code, action=action, now=now,
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
    BLAST RADIUS rather than any one bug. It was written when the runner stored
    the Telegram offset only after we returned, which made an exception here a
    permanent outage: the same update re-fed every five seconds, no button
    working, the operator told nothing at all. The runner now stores the offset
    BEFORE it calls us (``run_admin_bot.process_one_update``), so that outage
    is closed on the other side and this barrier is no longer the only thing
    standing between one bad tap and a dead console.

    It stays, and it is worth more than the runner's own catch, because the
    runner can only DROP the update: it logs, it posts «تم تخطيه», and the
    operator learns that something failed but not that anything is wrong. From
    inside, we still know which tap it was, we can roll the session back before
    it poisons the outcomes, and we can answer with a screen. Belt and braces
    on the operator's only window is the right amount.

    It is a barrier, not a blanket: the exception is logged at ERROR with its
    traceback, which puts it in front of the operator on the ⚠️ الأخطاء screen
    (the collector reads our own ERROR lines out of journalctl), and the reply
    names the situation instead of printing Python. The rollback matters as
    much as the catch — the runner commits this same session after we return,
    and a session left in a failed transaction turns one bad tap into a lost
    screen for a piece of work the operator believes landed. Real failures
    still stop being invisible; they just stop taking the watchtower with them.

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
        # BEFORE the screen is drawn, so the tickets list he is about to read
        # already shows what the sweep just changed. Best-effort in its own
        # right: a sweep that raised would cost the operator the tap he
        # actually made, and the ticket it could not release is still on the
        # screen with its age growing. See :func:`release_forgotten_tickets`
        # for why running off console traffic is honest but not sufficient.
        try:
            alerts = release_forgotten_tickets(session, now=now)
            # AUDIT 2026-08-07 — COMMITTED HERE, in its own unit of work, and
            # that is not tidiness. The sweep ran BEFORE `_dispatch`, in the
            # same transaction, and several dispatch actions end their refusal
            # path with `session.rollback()` (`_run_career_session` on «no
            # request on file», `_run_subscription_action` on a refused
            # transition, `_run_reply` on a failed send). Any one of them threw
            # the release away — and the alert below was still returned, so the
            # operator read «فتحنا خطه من جديد» about a ticket that was still
            # `open` and still muting the customer. Worse than a lost page: the
            # row stayed releasable, so the SAME page came back on his next
            # refusing tap, and the next — the exact repetition the one-way
            # edge was supposed to make impossible. `run_admin_bot` states the
            # rule this restores: a screen drawn over a rolled-back commit is a
            # lie the operator acts on.
            if alerts:
                session.commit()
        except Exception:  # noqa: BLE001 — never costs him the tap
            logger.error("forgotten-ticket sweep failed", exc_info=True)
            # …and the rollback is what makes that sentence true. A failed
            # statement leaves the transaction aborted, so without this every
            # query the screen below runs would raise too and the sweep WOULD
            # have cost him the tap — by the back door.
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                logger.error("sweep rollback failed", exc_info=True)
            alerts = []
        outcomes = _dispatch(
            session, update, probes=probes, now=now,
            whatsapp_client=whatsapp_client,
        )
        # Appended, never prepended: the ack that stops his spinner and the
        # screen he asked for go first, and the pages arrive behind them.
        return [*outcomes,
                *(Outcome(kind="send", text=text) for text in alerts)]
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
        # both names consume a one-shot nonce, so both deserve the message
        # that says «the confirmation expired» rather than «press /start»
        is_confirm = (len(parts) >= 2 and parts[0] == "v1"
                      and parts[1] in ("confirm", "tdone"))
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
