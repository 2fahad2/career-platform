"""The watchtower weekly report (design doc §5, phase 5).

A pure Arabic formatter over the same 7-day business data the console shows,
plus the week's honest day-state tally. The runner sends it to the admin
channel every Sunday morning; the format function is golden-tested with zero
I/O.
"""

from __future__ import annotations

from datetime import date
from typing import Any

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

_PLAN_AR = {
    "basic": "أساسي", "professional": "احترافي",
    "elite": "نخبة", "cv_analysis": "تحليل CV",
}


def format_weekly_report(
    week_ending: date,
    business: dict[str, Any],
    day_states: dict[str, int],
) -> str:
    """``business``: the 7-day _business_data dict; ``day_states``: state →
    count over the week."""
    lines = [f"🗓 التقرير الأسبوعي — حتى {week_ending.isoformat()}"]
    lines.append("━━━━━━━━━━━━━━")

    subs = business.get("subs_by_plan") or {}
    if subs:
        parts = " · ".join(
            f"{_PLAN_AR.get(k, k)}: {v}" for k, v in sorted(subs.items())
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

    return "\n".join(lines)
