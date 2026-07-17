"""Watchtower console views — pure renderers (design doc §4).

Every function maps already-PII-free data (TEN codes, counts, statuses) to an
Arabic screen: ``(text, keyboard)``. No I/O, no clocks, no sessions — golden-
tested. The console layer is the only caller.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from career.telegram.admin import Keyboard

_STATE_AR = {
    "DELIVERED": "✅ سُلّم",
    "NO_MATCHES": "🟡 لا فرص مطابقة",
    "PARTIAL_DELIVERY": "🟠 تسليم جزئي",
    "DISCOVERY_FAILED": "🔴 فشل الاكتشاف",
    "CV_GENERATION_FAILED": "🔴 فشل توليد السيرة",
    "WHATSAPP_FAILED": "🔴 فشل واتساب",
    "LEDGER_FAILED": "🔴 فشل السجل",
}

_RUN_AR = {
    "completed": "✅ اكتملت",
    "partial": "🟠 جزئية",
    "discovery_failed": "🔴 فشلت",
    "no_active_tenants": "⚪ لا عملاء نشطين",
    "running": "⏳ جارية",
}

_HOME = ("🏠 الرئيسية", "v1|menu")


def render_menu() -> tuple[str, Keyboard]:
    text = "🏰 برج المراقبة — منصة التوظيف\nاختر شاشة:"
    keyboard: Keyboard = [
        [("📊 اليوم", "v1|today"), ("👥 العملاء", "v1|customers|0")],
        [("🩺 الصحة", "v1|health"), ("💰 الأعمال", "v1|business|7")],
        [("🧾 الأخطاء", "v1|errors"), ("⚙️ إجراءات", "v1|soon|actions")],
    ]
    return text, keyboard


def render_today(
    run_date: date,
    run: dict[str, Any] | None,
    states: list[tuple[str, str, dict[str, Any]]],
) -> tuple[str, Keyboard]:
    """``run``: {status, counts} of the latest discovery run; ``states``:
    (tenant_code, state, counts) rows for the day — TEN codes only."""
    lines = [f"📊 يوم {run_date.isoformat()}"]
    if run is None:
        lines.append("التشغيلة: ⚪ لا تشغيلة مسجلة بعد")
    else:
        status = _RUN_AR.get(str(run.get("status")), str(run.get("status")))
        counts = run.get("counts") or {}
        lines.append(f"التشغيلة: {status}")
        fetched = counts.get("fetched")
        passed = counts.get("passed")
        if fetched is not None or passed is not None:
            lines.append(
                f"المكتشف: {fetched if fetched is not None else '؟'}"
                f" · عبر البوابة: {passed if passed is not None else '؟'}"
            )
    lines.append("━━━━━━━━━━━━━━")
    if not states:
        lines.append("لا حالات عملاء مسجلة لليوم")
    for code, state, counts in states:
        label = _STATE_AR.get(state, state)
        delivered = counts.get("delivered", 0)
        failed = counts.get("failed_sends", 0)
        lines.append(f"{code} · {label} · سُلّم {delivered} · فشل {failed}")
    keyboard: Keyboard = [[("🔄 تحديث", "v1|today"), _HOME]]
    return "\n".join(lines), keyboard


def _light(ok: bool | None, ok_text: str, bad_text: str) -> str:
    if ok is None:
        return "⚪ غير معروف"
    return f"🟢 {ok_text}" if ok else f"🔴 {bad_text}"


def render_health(h: dict[str, Any]) -> tuple[str, Keyboard]:
    """``h`` keys (all optional, None = unknown): worker_active, timer_next,
    meta_token_ok, salla_days_left, searchapi (allowance, remaining),
    templates {status: count}, backup_age_hours."""
    lines = ["🩺 صحة المنصة"]
    lines.append("عامل المحادثة: " + _light(h.get("worker_active"), "يعمل", "متوقف"))
    timer_next = h.get("timer_next")
    lines.append(
        f"مؤقت الفجر: 🟢 التالي {timer_next}" if timer_next
        else "مؤقت الفجر: ⚪ غير معروف"
    )
    lines.append("توكن ميتا: " + _light(h.get("meta_token_ok"), "دائم ويعمل", "لا يستجيب"))
    days = h.get("salla_days_left")
    if days is None:
        lines.append("توكن سلة: ⚪ غير معروف")
    elif days <= 0:
        lines.append("توكن سلة: 🔴 منتهٍ")
    elif days <= 7:
        lines.append(f"توكن سلة: 🟠 ينتهي بعد {days} يوم")
    else:
        lines.append(f"توكن سلة: 🟢 باقي {days} يوم")
    quota = h.get("searchapi")
    if quota is None:
        lines.append("رصيد البحث: ⚪ غير معروف")
    else:
        allowance, remaining = quota
        if int(allowance) <= 0:
            lines.append("رصيد البحث: 🟡 خطة تجريبية")
        else:
            lines.append(f"رصيد البحث: 🟢 باقي {remaining} من {allowance}")
    templates = h.get("templates")
    if templates is None:
        lines.append("القوالب: ⚪ غير معروف")
    else:
        approved = int(templates.get("APPROVED", 0))
        pending = int(templates.get("PENDING", 0))
        rejected = int(templates.get("REJECTED", 0))
        part = f"القوالب: ✅ {approved} معتمد · ⏳ {pending} معلق"
        if rejected:
            part += f" · 🔴 {rejected} مرفوض"
        lines.append(part)
    age = h.get("backup_age_hours")
    if age is None:
        lines.append("آخر نسخة احتياطية: ⚪ غير معروف")
    elif age <= 26:
        lines.append(f"آخر نسخة احتياطية: 🟢 قبل {int(age)} ساعة")
    else:
        lines.append(f"آخر نسخة احتياطية: 🔴 قبل {int(age)} ساعة")
    keyboard: Keyboard = [[("🔄 تحديث", "v1|health"), _HOME]]
    return "\n".join(lines), keyboard


PAGE_SIZE = 8

_PLAN_AR = {
    "basic": "أساسي",
    "professional": "احترافي",
    "elite": "نخبة",
    "cv_analysis": "تحليل CV",
}

_JOURNEY_AR = {
    "ACTIVE": "✅ نشط",
    "DONE": "✅ مكتمل",
}

_WINDOW_AR = {
    "open": "💬 نافذة مفتوحة",
    "closed": "🌙 نافذة مقفولة",
    "opted_out": "🚫 أوقف الرسائل",
}


def _plan(code: str | None) -> str:
    return _PLAN_AR.get(str(code), str(code or "—"))


def render_customers(
    rows: list[dict[str, Any]], page: int, total: int
) -> tuple[str, Keyboard]:
    """``rows``: this page only — {code, plan_code, journey_state, window}.
    Each customer IS a button (tap → the card); TEN codes only."""
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = f"👥 العملاء — {total} إجمالًا (صفحة {page + 1} من {pages})"
    keyboard: Keyboard = []
    for row in rows:
        journey = _JOURNEY_AR.get(
            str(row.get("journey_state")), str(row.get("journey_state") or "⏳")
        )
        window = _WINDOW_AR.get(str(row.get("window")), "")
        label = f"{row['code']} · {_plan(row.get('plan_code'))} · {journey}"
        if window:
            label += f" · {window}"
        keyboard.append([(label[:60], f"v1|tenant|{row['code']}")])
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("⬅️ السابق", f"v1|customers|{page - 1}"))
    if page + 1 < pages:
        nav.append(("التالي ➡️", f"v1|customers|{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([_HOME])
    if not rows:
        text += "\nلا عملاء بعد"
    return text, keyboard


def render_tenant_card(card: dict[str, Any]) -> tuple[str, Keyboard]:
    """One customer, zero PII: TEN code + plan + states + counters only."""
    code = str(card.get("code"))
    lines = [f"👤 {code}"]
    lines.append(
        f"الخطة: {_plan(card.get('plan_code'))}"
        f" · الحالة: {card.get('sub_status') or '—'}"
    )
    age = card.get("sub_age_days")
    if age is not None:
        lines.append(f"عمر الاشتراك: {age} يوم")
    journey = card.get("journey_state")
    lines.append(
        "الرحلة: " + _JOURNEY_AR.get(str(journey), str(journey or "لم تبدأ"))
    )
    window = card.get("window")
    if window:
        lines.append("الواتساب: " + _WINDOW_AR.get(str(window), str(window)))
    last = card.get("last_delivery")
    if last:
        lines.append(f"آخر تسليمة: {last['run_date']} · {last['status']}")
    else:
        lines.append("آخر تسليمة: لا تسليمات بعد")
    outcomes = card.get("outcomes") or {}
    lines.append(
        f"قراراته: قدّم {outcomes.get('applied', 0)}"
        f" · تجاهل {outcomes.get('ignored', 0)}"
    )
    lines.append(f"وظائف محجوبة (مكررة): {card.get('suppressions', 0)}")
    keyboard: Keyboard = [
        [("👥 القائمة", "v1|customers|0"), _HOME],
    ]
    return "\n".join(lines), keyboard


_RANGE_AR = {"7": "٧ أيام", "30": "٣٠ يومًا", "all": "من البداية"}


def render_business(range_key: str, data: dict[str, Any]) -> tuple[str, Keyboard]:
    """Owner numbers: sales, funnel conversions, outcomes, delivery, cost."""
    title = _RANGE_AR.get(range_key, range_key)
    lines = [f"💰 الأعمال — {title}"]
    subs = data.get("subs_by_plan") or {}
    if subs:
        parts = " · ".join(f"{_plan(k)}: {v}" for k, v in sorted(subs.items()))
        lines.append(f"اشتراكات جديدة: {parts}")
    else:
        lines.append("اشتراكات جديدة: لا شيء")
    revenue = data.get("revenue_sar")
    if revenue is not None:
        lines.append(f"الإيراد: {revenue} ريال")
    upgrades = data.get("funnel_upgrades")
    if upgrades is not None:
        lines.append(f"ترقيات القمع (تحليل ← اشتراك): {upgrades}")
    delivered = data.get("delivered_days")
    if delivered is not None:
        lines.append(f"أيام تسليم ناجحة: {delivered}")
    outcomes = data.get("outcomes") or {}
    applied = int(outcomes.get("applied", 0))
    ignored = int(outcomes.get("ignored", 0))
    total_dec = applied + ignored
    rate = f" ({round(100 * applied / total_dec)}٪ تقديم)" if total_dec else ""
    lines.append(f"قرارات العملاء: قدّم {applied} · تجاهل {ignored}{rate}")
    llm = data.get("llm_generations")
    cost = data.get("llm_cost_usd")
    if llm is not None:
        cost_part = f" · ${cost}" if cost is not None else ""
        lines.append(f"توليدات Claude: {llm}{cost_part}")
    keyboard: Keyboard = [
        [("٧ أيام", "v1|business|7"), ("٣٠ يومًا", "v1|business|30"),
         ("الكل", "v1|business|all")],
        [_HOME],
    ]
    return "\n".join(lines), keyboard


def render_errors(lines_in: list[str]) -> tuple[str, Keyboard]:
    """Last error lines — already secret-redacted AND class-names-only by the
    collector contract; this renderer just frames them."""
    lines = ["🧾 آخر الأخطاء المسجلة"]
    if not lines_in:
        lines.append("🟢 لا أخطاء حديثة — نظيف")
    else:
        lines.extend(f"• {line[:120]}" for line in lines_in[:10])
    keyboard: Keyboard = [[("🔄 تحديث", "v1|errors"), _HOME]]
    return "\n".join(lines), keyboard


_SOON_TITLES = {
    "customers": "👥 العملاء",
    "business": "💰 الأعمال",
    "errors": "🧾 الأخطاء",
    "actions": "⚙️ إجراءات",
}


def render_soon(key: str) -> tuple[str, Keyboard]:
    title = _SOON_TITLES.get(key, key)
    return (
        f"{title}\n🚧 هذه الشاشة تصل في المرحلة القادمة من برج المراقبة",
        [[_HOME]],
    )
