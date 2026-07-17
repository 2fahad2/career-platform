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
        [("📊 اليوم", "v1|today"), ("👥 العملاء", "v1|soon|customers")],
        [("🩺 الصحة", "v1|health"), ("💰 الأعمال", "v1|soon|business")],
        [("🧾 الأخطاء", "v1|soon|errors"), ("⚙️ إجراءات", "v1|soon|actions")],
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
