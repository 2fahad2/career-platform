"""Watchtower console views — pure renderers (design doc §4).

Every function maps already-PII-free data (TEN codes, counts, statuses) to an
Arabic screen: ``(text, keyboard)``. No I/O, no clocks, no sessions — golden-
tested. The console layer is the only caller.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
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
    "SKIPPED_OPTED_OUT": "🚫 موقف الرسائل — تُخطي",
}

_RUN_AR = {
    "completed": "✅ اكتملت",
    "partial": "🟠 جزئية",
    "discovery_failed": "🔴 فشلت",
    "no_active_tenants": "⚪ لا عملاء نشطين",
    "running": "⏳ جارية",
}

_HOME = ("🏠 الرئيسية", "v1|menu")


def state_label(state: str) -> str:
    """The Arabic word for an honest day state, or the raw token when there
    is none. Public because the console renders day states inside the
    guarantee facts too, and two tables of the same eight words would drift."""
    return _STATE_AR.get(str(state), str(state))


def render_menu() -> tuple[str, Keyboard]:
    text = "🏰 برج المراقبة — منصة التوظيف\nاختر شاشة:"
    keyboard: Keyboard = [
        [("📊 اليوم", "v1|today"), ("👥 العملاء", "v1|customers|0")],
        [("🩺 الصحة", "v1|health"), ("💰 الأعمال", "v1|business|7")],
        [("🧾 الأخطاء", "v1|errors"), ("⚙️ إجراءات", "v1|soon|actions")],
    ]
    return text, keyboard


def _status_lines(label: str, status: Any) -> list[str]:
    """``label: <arabic status>`` — an UNKNOWN status keeps its own line so a
    raw Latin value never lands inside an Arabic one (Fahad's client
    scrambles a mixed line)."""
    known = _RUN_AR.get(str(status))
    if known:
        return [f"{label}: {known}"]
    return [f"{label}:", str(status)]


def render_today(
    run_date: date,
    run: dict[str, Any] | None,
    states: list[tuple[str, str, dict[str, Any]]],
    last_run: dict[str, Any] | None = None,
) -> tuple[str, Keyboard]:
    """``run``: {status, counts} of TODAY's discovery run — None means today
    has not run, said in those words (never yesterday's numbers under a
    «today» heading). ``last_run``: {run_date, status} of the most recent run
    on any day, shown only in that case. ``states``: (tenant_code, state,
    counts) rows for the day — TEN codes only."""
    # date on its own line: a digit inside an Arabic line is scrambled
    lines = ["📊 تقرير اليوم", run_date.isoformat()]
    if run is None:
        lines.append("التشغيلة: 🔴 ما صارت تشغيلة اليوم")
        if last_run:
            lines.append("آخر تشغيلة مسجلة كانت بتاريخ")
            lines.append(str(last_run.get("run_date")))
            lines.extend(_status_lines("وحالتها", last_run.get("status")))
        else:
            lines.append("ولا توجد أي تشغيلة مسجلة من قبل")
    else:
        counts = run.get("counts") or {}
        lines.extend(_status_lines("التشغيلة", run.get("status")))
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
    templates {status: count}, virus_scanner (a ScannerHealth), deployed_source
    (running, repository), backup_age_hours."""
    lines = ["🩺 صحة المنصة"]
    lines.append("عامل المحادثة: " + _light(h.get("worker_active"), "يعمل", "متوقف"))
    timer_next = h.get("timer_next")
    lines.append(
        f"مؤقت التسليم: 🟢 التالي {timer_next}" if timer_next
        else "مؤقت التسليم: ⚪ غير معروف"
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
    # The §11 malware scanner, and the reason this line exists at all: for the
    # whole live period the worker injected a stand-in whose scan() returned
    # «clean» for every file, and this screen — the operator's only window —
    # said nothing about uploads whatsoever. There are exactly three states and
    # each gets its own colour, because «not installed» and «installed and
    # dead» are different facts he acts on differently. The absent line says
    # plainly that files are passing unscanned rather than hiding behind an
    # amber word: it is a chosen posture, and a chosen posture has to be
    # visible or it is just an undisclosed one.
    from career.onboarding.upload import (
        HEALTH_KEY,
        SCANNER_ABSENT,
        SCANNER_READY,
        SCANNER_UNREACHABLE,
    )

    scanner_state = getattr(h.get(HEALTH_KEY), "state", None)
    if scanner_state == SCANNER_READY:
        lines.append("فاحص الملفات: 🟢 يعمل ويجيب")
    elif scanner_state == SCANNER_ABSENT:
        lines.append("فاحص الملفات: 🟠 غير مركّب — الملفات تمر بلا فحص")
    elif scanner_state == SCANNER_UNREACHABLE:
        lines.append("فاحص الملفات: 🔴 مركّب ولا يستجيب")
        # The slug says WHICH failure (timeout · unreachable · unrecognized
        # reply), which is the difference between restarting a daemon and
        # fixing a path. It is Latin, so it lives on its own line — the same
        # rule the deployed-source hashes below follow, and for the same
        # client that scrambles a mixed run.
        detail = getattr(h.get(HEALTH_KEY), "detail", None)
        if detail:
            lines.append(str(detail))
    else:
        lines.append("فاحص الملفات: ⚪ غير معروف")
    # «Committed» is not «deployed». Every other light on this screen was green
    # for four days while the container served code from before the fixes —
    # because the worker and the timers really were healthy and nothing
    # compared the running image to the repository (see career.fingerprint).
    deployed = h.get("deployed_source")
    if deployed is None:
        lines.append("الكود المنشور: ⚪ ما قدرنا نسأل الواجهة")
    elif deployed[0] == deployed[1]:
        lines.append("الكود المنشور: 🟢 مطابق للمستودع")
    else:
        # Hashes are Latin — each on its own line or the operator's client
        # scrambles them into an Arabic sentence.
        lines.append("الكود المنشور: 🔴 يختلف عن المستودع — أعد بناء الحاوية")
        lines.append("الحاوية تشغّل:")
        lines.append(str(deployed[0]))
        lines.append("والمستودع فيه:")
        lines.append(str(deployed[1]))
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

#: The plan codes in the DATABASE, mapped to what the customer buys today.
#: «elite» never existed as a plan code, while «executive» — the one that
#: does — had no label at all, so لمّاح+ rendered as a raw Latin token inside
#: an Arabic line: a missing translation and a direction break in one.
#: «basic» is retired from sale but kept: historical subscriptions point at
#: it and the card must still name it.
_PLAN_AR = {
    "cv_analysis": "تقييم لمّاح",
    "professional": "لمّاح",
    "executive": "لمّاح+",
    "basic": "أساسي (متوقّف)",
}

#: Plans whose customers are answered first — and the ONLY thing that makes
#: that promise real. لمّاح+ used to sell «أولوية في الطابور» backed by a
#: database column nothing read: no queue, no ordering, no marker, so the
#: operator could not tell a 449 customer from a 199 one while replying. There
#: is no automated queue to reorder (replies are the operator's own hand), so
#: the honest form of priority is to make the tier VISIBLE at the moment of
#: choosing whom to answer — here, and on the card itself.
_PRIORITY_PLANS: frozenset[str] = frozenset({"executive"})

_JOURNEY_AR = {
    "ACTIVE": "✅ نشط",
    "DONE": "✅ مكتمل",
}

_WINDOW_AR = {
    "open": "💬 نافذة مفتوحة",
    "closed": "🌙 نافذة مقفولة",
    "opted_out": "🚫 أوقف الرسائل",
}

#: The delivery status word, said in Arabic. A raw Latin status inside an
#: Arabic line is scrambled by the operator's client, and «PARTIAL» told them
#: nothing about whether anything actually reached the customer.
_DELIVERY_AR = {
    "COMPLETED": "✅ وصلت كاملة",
    "PARTIAL": "🟠 وصل جزء منها",
    "PENDING_WINDOW": "⏳ محفوظة بانتظار فتح النافذة",
    "OPENED": "⏳ قيد الإرسال",
    "NO_SEND": "🚫 لم تُرسل — العميل أوقف الرسائل",
    "EXPIRED_WINDOW": "⌛ انتهت مهلتها دون تسليم",
}


def _delivery_ar(last: dict[str, Any]) -> str:
    """The honest one-liner for the last delivery. A PARTIAL that delivered
    NOTHING is a failure, not a partial success — the operator must not read
    «وصل جزء منها» when zero messages landed (§15.12)."""
    status = str(last.get("status"))
    delivered = last.get("delivered")
    if status == "PARTIAL" and delivered is not None and int(delivered) == 0:
        return "🔴 لم يصل منها شيء"
    return _DELIVERY_AR.get(status, status)


def _plan(code: str | None) -> str:
    return _PLAN_AR.get(str(code), str(code or "—"))


def _plan_marked(code: str | None) -> str:
    """The plan, starred when its holder is owed a faster human reply."""
    label = _plan(code)
    return f"⭐ {label}" if str(code) in _PRIORITY_PLANS else label


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
        label = f"{row['code']} · {_plan_marked(row.get('plan_code'))} · {journey}"
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


#: The 72-hour start guarantee, in the words of what it means for a customer.
#: A raw status token would be a Latin run inside an Arabic line, and «MET»
#: tells the operator nothing about whether he owes anybody money.
_GUARANTEE_AR = {
    "WATCHING": "⏳ ضمان الـ٧٢ ساعة: المهلة تمشي",
    "MET": "🛡️ ضمان الـ٧٢ ساعة: وفينا به",
    "BREACHED": "🔴 ضمان الـ٧٢ ساعة: انكسر — والعميل يختار استرداده أو تمديده",
    "SETTLED": "✅ ضمان الـ٧٢ ساعة: انكسر وسُوّي",
}

#: The لمّاح+ career session — «جلسة مسار واحدة متى طلبتها».
_SESSION_AR = {
    "REQUESTED": "📅 جلسة المسار: طلبها ولم يُنسَّق له موعد",
    "SCHEDULED": "📅 جلسة المسار: تم التنسيق",
    "COMPLETED": "📅 جلسة المسار: تمت",
    "CANCELED": "📅 جلسة المسار: ألغاها",
}

#: What the refund page deducts for a session the customer really received —
#: «تُخصم قيمة الخدمات البشرية اللي استلمتها فعليًا (جلسة المسار ١٥٠ ريالًا)».
#: It is printed on the card of a customer who HAS received one, because the
#: moment that number is needed is the moment somebody is asking for a refund.
_SESSION_DEDUCTION_AR = "وتُخصم قيمتها من أي استرداد:"


def _promise_lines(card: dict[str, Any]) -> list[str]:
    """The three sold promises, said plainly on the customer's own card.

    They live on the card and not only on their own screen because the
    question «هل أنا مدين لهذا العميل بشيء؟» is asked while looking at HIM —
    and a promise the operator has to remember to go and check is a promise
    that gets remembered late.
    """
    lines: list[str] = []
    guarantee = card.get("guarantee")
    if guarantee:
        lines.append(_GUARANTEE_AR.get(
            str(guarantee.get("status")), str(guarantee.get("status"))
        ))
        if guarantee.get("age"):
            lines.append(str(guarantee["age"]))
    session = card.get("career_session")
    if session:
        lines.append(_SESSION_AR.get(
            str(session.get("status")), str(session.get("status"))
        ))
        if session.get("age"):
            lines.append(str(session["age"]))
        if session.get("overdue"):
            lines.append("⚠️ تجاوز مهلة الرد المعلنة لمشتركي لمّاح+")
        if str(session.get("status")) == "COMPLETED" and session.get("deduction"):
            lines.append(_SESSION_DEDUCTION_AR)
            lines.append(str(session["deduction"]))
    elif card.get("session_entitled"):
        lines.append("📅 جلسة المسار: من حقه، ولا طلبها بعد")
    lock = card.get("price_lock")
    if lock:
        # The locked price is a number and a currency — Latin, so its own
        # line — and it exists on the card because it is what a founder was
        # promised, and the operator is the only one who can notice if the
        # store ever starts charging him something else.
        lines.append("🔒 سعره مقفول عند:")
        lines.append(str(lock))
    return lines


def render_tenant_card(card: dict[str, Any]) -> tuple[str, Keyboard]:
    """One customer, zero PII: TEN code + plan + states + counters only."""
    code = str(card.get("code"))
    lines = [f"👤 {code}"]
    # Two lines, not one. The subscription status is a Latin token
    # (ACTIVE / PAUSED / EXPIRED …) and the rest of this file goes out of its
    # way to keep TEN codes and dates on their own lines for exactly this
    # reason — a mixed run re-orders in a right-to-left client and the
    # operator reads a scrambled plan and status.
    lines.append(f"الخطة: {_plan_marked(card.get('plan_code'))}")
    lines.append("الحالة:")
    lines.append(str(card.get("sub_status") or "—"))
    age = card.get("sub_age_days")
    if age is not None:
        # Two defects in one line. The number sat INSIDE an Arabic sentence,
        # which the operator's client re-orders — the rule the rest of this
        # module keeps carefully. And it went NEGATIVE: the age is computed as
        # (now - created_at).days, so a subscription row created microseconds
        # ahead of the console's clock — or any clock skew between the API
        # container and the host — rendered «عمر الاشتراك: -22 يوم». A
        # negative age reads as corruption and sends the operator hunting a
        # data problem that does not exist, so today is floored at zero and
        # says so in words.
        days = max(0, int(age))
        lines.append("عمر الاشتراك:")
        lines.append("اليوم" if days == 0 else f"{days} يوم")
    journey = card.get("journey_state")
    lines.append(
        "الرحلة: " + _JOURNEY_AR.get(str(journey), str(journey or "لم تبدأ"))
    )
    window = card.get("window")
    if window:
        lines.append("الواتساب: " + _WINDOW_AR.get(str(window), str(window)))
    last = card.get("last_delivery")
    if last:
        # date on its own line: a Latin digit inside an Arabic line scrambles
        lines.append("آخر تسليمة")
        lines.append(str(last["run_date"]))
        lines.append(_delivery_ar(last))
    else:
        lines.append("آخر تسليمة: لا تسليمات بعد")
    outcomes = card.get("outcomes") or {}
    lines.append(
        f"قراراته: قدّم {outcomes.get('applied', 0)}"
        f" · تجاهل {outcomes.get('ignored', 0)}"
    )
    lines.append(f"وظائف محجوبة (مكررة): {card.get('suppressions', 0)}")
    support_min = card.get("support_minutes")
    review_count = card.get("review_count")
    if support_min is not None or review_count is not None:
        lines.append(
            f"⏱ دقائق دعم: {support_min or 0}"
            f" · 👁 مراجعات بشرية: {review_count or 0}"
        )
    lines.extend(_promise_lines(card))
    # a held bundle is reported whether or not it can be re-attempted right
    # now — and when it cannot, the card says why instead of hiding the fact
    if card.get("held_bundles"):
        lines.append("📤 توجد حزمة محفوظة لم تُسلَّم بعد")
        if str(card.get("window")) != "open":
            lines.append("لا يمكن إعادة إرسالها والنافذة مقفولة — تنزل حين يراسلنا")
    # the mutating action depends on the subscription state (design doc §4)
    if str(card.get("sub_status")) == "PAUSED":
        action_button = ("▶️ استئناف الخدمة", f"v1|act|{code}|resume")
    else:
        action_button = ("⏸️ إيقاف مؤقت", f"v1|act|{code}|pause")
    keyboard: Keyboard = [
        [("⏱ +5 دقائق دعم", f"v1|log|{code}|support5"),
         ("👁 +مراجعة بشرية", f"v1|log|{code}|review")],
    ]
    # A broken guarantee gets its remedies on the card itself. The customer
    # chooses which one — «أنت تختار» — so all three are offered side by side
    # and none of them is a default the operator can tap past.
    if str((card.get("guarantee") or {}).get("status")) == "BREACHED":
        keyboard.append([
            ("💸 استرداد", f"v1|act|{code}|g_refund"),
            ("📆 تمديد", f"v1|act|{code}|g_extend"),
            ("🤝 تنازل", f"v1|act|{code}|g_waive"),
        ])
    session_status = str((card.get("career_session") or {}).get("status") or "")
    if session_status == "REQUESTED":
        keyboard.append([("📅 تم التنسيق", f"v1|act|{code}|cs_scheduled")])
    elif session_status == "SCHEDULED":
        keyboard.append([("✅ تمت الجلسة", f"v1|act|{code}|cs_completed")])
    elif not session_status and card.get("session_entitled"):
        # The request itself is recorded by hand, because that is how it
        # actually arrives: لمّاح+ sells «تكتب لي أنا مباشرة على نفس
        # المحادثة», so the customer asks a human in a sentence, not a
        # keyword. The button is what turns that sentence into a record with
        # a clock on it.
        keyboard.append([("📅 سجّل طلب جلسة مسار", f"v1|act|{code}|cs_request")])
    # offered ONLY when a held bundle can actually be re-attempted
    if card.get("can_resend"):
        keyboard.append([("📤 إعادة إرسال الحزمة", f"v1|act|{code}|resend")])
    # one free-form reply to the customer, straight from the watchtower —
    # its own callback prefix, never the generic act/confirm path
    if card.get("can_reply"):
        keyboard.append([("✍️ رد على العميل", f"v1|reply|{code}")])
    keyboard.append([action_button])
    keyboard.append([("👥 القائمة", "v1|customers|0"), _HOME])
    return "\n".join(lines), keyboard


PROMISES_TITLE_AR = "🤝 الوعود المستحقة"
PROMISES_NONE_AR = "🟢 لا شيء مستحق — الضمانات موفّى بها ولا جلسة معلّقة"


def render_promises(
    breaches: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> tuple[str, Keyboard]:
    """Everything the store has promised and not yet delivered, in one screen.

    Two lists that have nothing technical in common and one thing in common
    that matters more: each row is a customer who has been told something and
    is waiting. Ordered oldest-first inside each list by the caller, because
    age is the only priority a promise has.

    Every row is a TEN code on its own line (§15.13) with its age underneath —
    the age IS the point, and it was the missing half of both promises: a
    breach nobody was told about and a request nobody could see waiting.
    """
    lines = [PROMISES_TITLE_AR]
    if not breaches and not sessions:
        lines.append(PROMISES_NONE_AR)
    keyboard: Keyboard = []
    if breaches:
        lines.append("")
        lines.append("🛡️ ضمانات مكسورة تنتظر قرارك")
        for row in breaches:
            lines.append("")
            lines.append(str(row["code"]))
            lines.append(str(row.get("age") or ""))
            if row.get("first_delivery"):
                lines.append("ووصلته أول فرصة بعد المهلة")
            else:
                lines.append("ولا وصلته أي فرصة إلى الآن")
            for fact in row.get("fact_lines") or []:
                lines.append(str(fact))
            keyboard.append([(f"↪️ {row['code']}", f"v1|tenant|{row['code']}")])
    if sessions:
        lines.append("")
        lines.append("📅 جلسات مسار معلّقة")
        for row in sessions:
            lines.append("")
            lines.append(str(row["code"]))
            lines.append(_SESSION_AR.get(str(row.get("status")),
                                         str(row.get("status"))))
            lines.append(str(row.get("age") or ""))
            if row.get("overdue"):
                lines.append("⚠️ تجاوز مهلة الرد المعلنة")
            keyboard.append([(f"↪️ {row['code']}", f"v1|tenant|{row['code']}")])
    keyboard.append([("🔄 تحديث", "v1|promises"), _HOME])
    return "\n".join(lines), keyboard


_RANGE_AR = {"7": "٧ أيام", "30": "٣٠ يومًا", "all": "من البداية"}


def cost_lines(data: dict[str, Any]) -> list[str]:
    """The §14 spend block, shared by the business screen and the weekly
    report so both quote ONE set of numbers.

    Bidi discipline (Fahad's client scrambles mixed lines): every Arabic line
    is pure Arabic, and every line carrying Latin category keys, TEN codes or
    Latin digits is a line of its own. That is why the labels and the numbers
    are never on the same line here.
    """
    total = data.get("total_cost_usd")
    if total is None:
        return []
    lines = ["━━━━━━━━━━━━━━", "💵 التكلفة بالدولار — الإجمالي", f"{total:.4f}"]

    tenants = int(data.get("cost_tenants") or 0)
    if tenants:
        lines.append("متوسط تكلفة العميل الواحد")
        lines.append(f"{Decimal(total) / Decimal(tenants):.4f}")

    by_category = data.get("spend_by_category") or {}
    priced = [
        (kind, events, cost)
        for kind, (events, cost) in by_category.items()
        if Decimal(cost) > 0
    ]
    if priced:
        lines.append("تفصيل البنود")
        for kind, events, cost in sorted(priced, key=lambda r: -Decimal(r[2])):
            lines.append(f"{kind} x{events} {Decimal(cost):.4f}")

    top = data.get("top_cost_tenants") or []
    if top:
        lines.append("أعلى العملاء تكلفة")
        for code, cost in top:
            lines.append(f"{code} {Decimal(cost):.4f}")
    return lines


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
    # the store page shows a live seat count — the operator sees the same truth
    if data.get("seats_cap") is not None:
        from career.salla.seats import Seats, seats_line_ar

        lines.append(seats_line_ar(Seats(
            cap=int(data["seats_cap"]), taken=int(data.get("seats_taken", 0)),
        )))
    # The ceiling that stops discovery for EVERYONE at once, and the only one
    # the operator could not see: the provider sells a fixed block of searches
    # per month, and it is the number of CAREER PATHS that consumes it — not
    # the number of customers. Digits stay on their own line (bidi).
    searches = data.get("searches_this_month")
    if searches is not None:
        lines.append("بحثات هذا الشهر:")
        lines.append(f"{searches} من 10000")
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
        lines.append(f"نداءات Claude: {llm}{cost_part}")
    statuses = data.get("message_statuses") or {}
    if statuses:
        read = int(statuses.get("read", 0))
        landed = read + int(statuses.get("delivered", 0))
        sent_only = int(statuses.get("sent", 0))
        failed = int(statuses.get("failed", 0))
        part = f"رسائلنا: قُرئ {read} · وصل {landed}"
        if sent_only:
            part += f" · أُرسل {sent_only}"
        if failed:
            part += f" · فشل {failed}"
        lines.append(part)
    # §14: what a customer actually costs, and who the runaway is — last,
    # as its own block, so the numbers never share a line with Arabic
    lines.extend(cost_lines(data))
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
