"""The watchtower weekly report (design doc §5, phase 5).

A pure Arabic formatter over the same 7-day business data the console shows,
plus the week's honest day-state tally. The runner sends it to the admin
channel every Sunday morning; the format function is golden-tested with zero
I/O.

The plan names come from the console's map and are NOT restated here. This
file used to keep its own copy, and a copy is how one product ended up with
three names: the report called ``professional`` «احترافي» while the customer
card called it «لمّاح», it carried «elite» — a plan code that has never
existed in the database — and it had no entry for ``executive`` at all, so
لمّاح+ arrived every Sunday as a raw Latin token inside an Arabic line (a
missing translation and a bidi break in one, on the one screen the operator
reads weekly). One authority, imported: a label added on the card is a label
this report already speaks.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from career.db.models import AdminBotState

# The underscore is deliberate and temporary: ``views._plan`` is the authority
# today, views.py belongs to another change in flight, and inventing a second
# public name here would be the third copy. The architect note asks for it to
# be renamed ``views.plan_label`` (or moved whole to ``telegram/plans.py``) —
# one import line moves with it.
from career.telegram.views import _plan as _plan_ar

logger = logging.getLogger("career.telegram")

#: The day-state words for the TALLY LINE, which is why they are not
#: ``views._STATE_AR`` and why importing that map here would be a regression
#: rather than the de-duplication it looks like.
#:
#: The console shows ONE state per line, per customer, with a traffic light in
#: front of it: «TEN-0002 · 🔴 فشل توليد السيرة · سُلّم 0 · فشل 1». This report
#: shows ALL of them joined by « · » on a single line, and the two demands are
#: opposite. The lights are the first thing to go: eight coloured circles in
#: one line read as decoration, not as severity, and the line they decorate is
#: the one the operator scans in two seconds on a Sunday morning. The words go
#: next: «فشل الاكتشاف» and «لا فرص مطابقة» are right when a line has room for
#: nothing else, and «فشل اكتشاف» / «لا فرص» say the same thing in a line that
#: has to hold seven more.
#:
#: So the WORDING is deliberate and stays. The KEYS are not ours to differ on:
#: a state that exists on one screen and not the other is a raw Latin token
#: inside an Arabic line on whichever screen forgot it, which Fahad's client
#: scrambles — the exact defect that cost this file its plan map. The keysets
#: of the two maps are therefore asserted equal (test_admin_console.py), and
#: both are asserted to cover ``cv.close.DAILY_STATES``.
_STATE_AR = {
    "DELIVERED": "سُلّم",
    "PARTIAL_DELIVERY": "تسليم جزئي",
    "NO_MATCHES": "لا فرص",
    "SKIPPED_OPTED_OUT": "موقف الرسائل",
    "WHATSAPP_FAILED": "فشل واتساب",
    "CV_GENERATION_FAILED": "فشل توليد",
    "DISCOVERY_FAILED": "فشل اكتشاف",
    "LEDGER_FAILED": "فشل سجل",
}

#: The hour (Riyadh) from which the Sunday report is owed. Before it, the week
#: that is owed is still the previous one.
REPORT_HOUR = 6


def due_week_ending(now_riyadh: datetime) -> date:
    """The Sunday whose report is owed at this instant.

    Not «is it Sunday between 06:00 and noon». That question is why a restart
    at 06:05 sent the report twice and an outage from Saturday night to Sunday
    afternoon dropped the week without a trace: the window was the ONLY memory
    the report had, so being outside it meant both «already done» and «never
    going to happen». The question that has an answer worth storing is «which
    week is owed», and it is answerable at any moment of any day. Paired with
    the durable marker below, arriving late means arriving late — not missing.
    """
    today = now_riyadh.date()
    # Python's Sunday is weekday 6, so (weekday + 1) % 7 is «days since the
    # most recent Sunday», and 0 on a Sunday.
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    if sunday == today and now_riyadh.hour < REPORT_HOUR:
        return sunday - timedelta(days=7)   # Sunday, but before the report hour
    return sunday


def weekly_report_marker(session: Session) -> date | None:
    """The week-ending Sunday of the last report actually sent; None if none
    ever was. Read before a claim purely so a failed send can put it back."""
    return session.execute(
        select(AdminBotState.weekly_report_sent_for).where(AdminBotState.id == 1)
    ).scalars().first()


def claim_weekly_report(session: Session, *, week_ending: date) -> bool:
    """Take the right to send THIS week's report, once, everywhere.

    True for exactly one caller per week — the next restart, the next hourly
    sweep and a future ``career-weekly-report.timer`` all get False, because
    the claim is a single conditional UPDATE and PostgreSQL's row lock decides
    the winner. That is what makes it safe to add the timer later without
    removing this call: whoever arrives first sends, the other is a no-op.

    Claiming BEFORE sending is on purpose. The failure it prevents (three
    restarts, three identical reports) is certain and repeated; the failure it
    risks (the process is killed between the claim and the send, and the week
    is lost) needs a crash inside a few milliseconds, and a send that merely
    fails is handled — the caller releases the claim and the next sweep tries
    again an hour later.
    """
    if session.get(AdminBotState, 1) is None:
        # 0014 seeds this row, so a missing one means a hand-restored database
        # — and «the singleton is gone» must not silently become «no report
        # ever again», which is the failure this whole function exists to end.
        logger.warning("admin_bot_state singleton missing — recreating it")
        session.add(AdminBotState(id=1))
        session.commit()
    result = session.execute(
        update(AdminBotState)
        .where(AdminBotState.id == 1,
               or_(AdminBotState.weekly_report_sent_for.is_(None),
                   AdminBotState.weekly_report_sent_for < week_ending))
        .values(weekly_report_sent_for=week_ending, updated_at=func.now())
    )
    claimed = result.rowcount or 0  # type: ignore[attr-defined]
    session.commit()
    return bool(claimed)


def release_weekly_report(session: Session, *, restore_to: date | None) -> None:
    """Give the claim back after a send that did not happen.

    Only ever called on the failure path: the report was claimed, Telegram
    refused it, and leaving the marker forward would spend the week on a
    report nobody received.
    """
    session.execute(
        update(AdminBotState)
        .where(AdminBotState.id == 1)
        .values(weekly_report_sent_for=restore_to, updated_at=func.now())
    )
    session.commit()


def format_weekly_report(
    week_ending: date,
    business: dict[str, Any],
    day_states: dict[str, int],
    covering_through: date | None = None,
) -> str:
    """``business``: the 7-day _business_data dict; ``day_states``: state →
    count over the week. ``covering_through`` is the day the numbers actually
    run to — passed only when the report is late, because a catch-up report
    headed «حتى الأحد» carrying Wednesday's numbers is a lie the reader has no
    way of catching."""
    lines = [f"🗓 التقرير الأسبوعي — حتى {week_ending.isoformat()}"]
    if covering_through is not None and covering_through != week_ending:
        # the date on its own line — a digit inside an Arabic line is scrambled
        lines.append("⏰ تقرير متأخر، والأرقام لغاية")
        lines.append(covering_through.isoformat())
    lines.append("━━━━━━━━━━━━━━")

    subs = business.get("subs_by_plan") or {}
    if subs:
        parts = " · ".join(
            f"{_plan_ar(k)}: {v}" for k, v in sorted(subs.items())
        )
        lines.append(f"💰 اشتراكات جديدة: {parts}")
    else:
        lines.append("💰 اشتراكات جديدة: لا شيء")
    revenue = business.get("revenue_sar")
    if revenue is not None:
        lines.append(f"الإيراد: {revenue} ريال")
    upgrades = business.get("funnel_upgrades")
    if upgrades:
        lines.append(f"ترقيات القمع: {upgrades}")

    lines.append("━━━━━━━━━━━━━━")
    delivered_days = int(day_states.get("DELIVERED", 0)) + \
        int(day_states.get("PARTIAL_DELIVERY", 0))
    lines.append(f"📦 أيام تسليم: {delivered_days}")
    if day_states:
        tally = " · ".join(
            f"{_STATE_AR.get(s, s)}: {n}"
            for s, n in sorted(day_states.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"الحالات: {tally}")

    outcomes = business.get("outcomes") or {}
    applied = int(outcomes.get("applied", 0))
    ignored = int(outcomes.get("ignored", 0))
    total = applied + ignored
    if total:
        rate = round(100 * applied / total)
        lines.append(f"🎯 قرارات العملاء: قدّم {applied}/{total} ({rate}٪)")

    statuses = business.get("message_statuses") or {}
    if statuses:
        read = int(statuses.get("read", 0))
        lines.append(f"👁 رسائل مقروءة: {read}")

    llm = business.get("llm_generations")
    cost = business.get("llm_cost_usd")
    if llm is not None:
        cost_part = f" (${cost})" if cost is not None else ""
        lines.append(f"🧠 نداءات Claude: {llm}{cost_part}")

    # §14: the same spend block the business screen shows — ONE set of
    # numbers, and bidi-pure (numbers and Latin keys on their own lines).
    from career.telegram.views import cost_lines

    lines.extend(cost_lines(business))

    return "\n".join(lines)
