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

#: Western digits → Arabic-Indic, the same table ``telegram/console`` keeps.
#: It is two lines in each renderer that needs it rather than one import,
#: because what makes a number safe is that it is ARABIC where it is written,
#: and the guard that proves it (``tests/test_alert_direction_purity``) reads
#: the table and its folder together, per file. A mapping that cannot drift is
#: the one kind of copy that is cheaper than the indirection: 0 is ٠ forever.
_WESTERN_TO_ARABIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _ar_digits(value: Any) -> str:
    """A number that can live INSIDE an Arabic line without scrambling it.

    This is why these screens are still one line per fact. The other cure —
    every count on a line of its own — is correct and unreadable: it doubles
    the height of a dashboard whose whole job is to be scanned in two seconds.
    So: a bare COUNT, AMOUNT or PERCENTAGE is folded and stays in its sentence;
    an IDENTIFIER (a TEN code, a date, a hash, a URL, a plan or status token)
    is not a number and still gets a line of its own.
    """
    return str(value).translate(_WESTERN_TO_ARABIC)


def _fact_lines(prefix_ar: str, label_ar: str | None, raw: Any) -> list[str]:
    """«الوصف: الكلمة» on one line when we have an Arabic word for the token,
    and the raw token on a line of its own when we do not.

    Every ``X: Y`` on these screens whose Y comes out of the database goes
    through here. A token we have no Arabic for is a Latin run, and a Latin
    run inside an Arabic line arrives at the operator reversed — so the two
    cases cannot share a shape, and the choice cannot be left to each caller
    to remember.
    """
    if label_ar:
        return [f"{prefix_ar}: {label_ar}"]
    return [f"{prefix_ar}:", str(raw)]


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


def day_state_ar(state: str) -> str | None:
    """The Arabic word for an honest day state — None when there is none.

    The type is the point: a caller that wants to put a state INSIDE an Arabic
    sentence has to answer «and if there is no Arabic word for it?» before the
    compiler lets it, instead of discovering the answer as a reversed line on
    the operator's phone.
    """
    return _STATE_AR.get(str(state))


def state_label(state: str) -> str:
    """The Arabic word for an honest day state, or the raw token when there
    is none. Public because the console renders day states inside the
    guarantee facts too, and two tables of the same eight words would drift.

    The result is NOT safe inside an Arabic line on its own terms — half the
    time it is a Latin token — so every caller must distinguish the two cases
    (``telegram/console._fact_lines_ar`` does it by comparing the result with
    the token it passed in). :func:`day_state_ar` is the version that says
    which one it returned, and is what new code should call.
    """
    return day_state_ar(state) or str(state)


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
            lines.extend(_fact_lines(
                "وحالتها", _RUN_AR.get(str(last_run.get("status"))),
                last_run.get("status"),
            ))
        else:
            lines.append("ولا توجد أي تشغيلة مسجلة من قبل")
    else:
        counts = run.get("counts") or {}
        lines.extend(_fact_lines(
            "التشغيلة", _RUN_AR.get(str(run.get("status"))), run.get("status"),
        ))
        fetched = counts.get("fetched")
        passed = counts.get("passed")
        if fetched is not None or passed is not None:
            # two counts, one line: Arabic-Indic digits sit inside the sentence
            lines.append(
                f"المكتشف: {_ar_digits(fetched) if fetched is not None else '؟'}"
                f" · عبر البوابة:"
                f" {_ar_digits(passed) if passed is not None else '؟'}"
            )
    lines.append("━━━━━━━━━━━━━━")
    if not states:
        lines.append("لا حالات عملاء مسجلة لليوم")
    for code, state, counts in states:
        # The TEN code is Latin and takes the line above; the row under it is
        # then pure Arabic, counters included. Two lines per customer instead
        # of one is the price of a row he can read at all — and the counters
        # stay ON that row rather than each claiming a third line.
        lines.append(str(code))
        counters = (f"سُلّم {_ar_digits(counts.get('delivered', 0))}"
                    f" · فشل {_ar_digits(counts.get('failed_sends', 0))}")
        label = day_state_ar(state)
        if label:
            lines.append(f"{label} · {counters}")
        else:
            lines.append(str(state))
            lines.append(counters)
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
    # The probe already says this in Arabic («السبت 03:30 بتوقيت الرياض») and
    # the only Latin left in it is the clock, so folding the digits keeps the
    # whole thing on one line AND makes it readable — splitting would leave a
    # half-Arabic fragment on a line of its own, which fixes nothing.
    lines.append(
        f"مؤقت التسليم: 🟢 التالي {_ar_digits(timer_next)}" if timer_next
        else "مؤقت التسليم: ⚪ غير معروف"
    )
    lines.append("توكن ميتا: " + _light(h.get("meta_token_ok"), "دائم ويعمل", "لا يستجيب"))
    days = h.get("salla_days_left")
    if days is None:
        lines.append("توكن سلة: ⚪ غير معروف")
    elif days <= 0:
        lines.append("توكن سلة: 🔴 منتهٍ")
    elif days <= 7:
        lines.append(f"توكن سلة: 🟠 ينتهي بعد {_ar_digits(days)} يوم")
    else:
        lines.append(f"توكن سلة: 🟢 باقي {_ar_digits(days)} يوم")
    quota = h.get("searchapi")
    if quota is None:
        lines.append("رصيد البحث: ⚪ غير معروف")
    else:
        allowance, remaining = quota
        if int(allowance) <= 0:
            lines.append("رصيد البحث: 🟡 خطة تجريبية")
        else:
            lines.append(
                f"رصيد البحث: 🟢 باقي {_ar_digits(remaining)}"
                f" من {_ar_digits(allowance)}"
            )
    templates = h.get("templates")
    if templates is None:
        lines.append("القوالب: ⚪ غير معروف")
    else:
        approved = int(templates.get("APPROVED", 0))
        pending = int(templates.get("PENDING", 0))
        rejected = int(templates.get("REJECTED", 0))
        part = (f"القوالب: ✅ {_ar_digits(approved)} معتمد"
                f" · ⏳ {_ar_digits(pending)} معلق")
        if rejected:
            part += f" · 🔴 {_ar_digits(rejected)} مرفوض"
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
        lines.append(f"آخر نسخة احتياطية: 🟢 قبل {_ar_digits(int(age))} ساعة")
    else:
        lines.append(f"آخر نسخة احتياطية: 🔴 قبل {_ar_digits(int(age))} ساعة")
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
    "DONE": "🏁 مكتمل",
}

_WINDOW_AR = {
    "open": "💬 نافذة مفتوحة",
    "closed": "🌙 نافذة مقفولة",
    "opted_out": "🚫 أوقف الرسائل",
}

#: The same two facts as marks, for the one place that has no second line to
#: put them on: a list BUTTON is a single line and it must carry the TEN code,
#: so it cannot carry an Arabic word as well — a mixed button label is
#: scrambled exactly like a mixed message line. Nothing new to learn: these
#: are the very emoji the card spells out («✅ نشط», «💬 نافذة مفتوحة»), and
#: the words themselves are one tap away on the card the button opens.
_JOURNEY_MARK = {"ACTIVE": "✅", "DONE": "🏁"}

_WINDOW_MARK = {"open": "💬", "closed": "🌙", "opted_out": "🚫"}

#: The delivery status word, said in Arabic. A raw Latin status inside an
#: Arabic line is scrambled by the operator's client, and «PARTIAL» told them
#: nothing about whether anything actually reached the customer.
_DELIVERY_AR = {
    "COMPLETED": "✅ وصلت كاملة",
    "PARTIAL": "🟠 وصل جزء منها",
    "PENDING_WINDOW": "⏳ محفوظة بانتظار فتح النافذة",
    "OPENED": "⏳ قيد الإرسال",
    "NO_SEND": "🚫 لم تُرسل — العميل أوقف الرسائل",
    # «انتهت مهلتها دون تسليم» said one thing and was READ as another. A bundle
    # expires when it was held for the 24h window and the next run day arrived
    # before the window opened, and there are exactly two ways that happens:
    # the customer never wrote back, or the template we sent to re-open the
    # window was REFUSED by Meta (131049/131050/131047 — see
    # `whatsapp.worker.META_REFUSAL_CODES`). The old sentence named neither and
    # the operator supplied the first one himself, every time. Two of the six
    # expiries in the live data were the second one. So the line now names both
    # possibilities instead of implying one; when the card actually knows, the
    # definite sentence below replaces it.
    "EXPIRED_WINDOW": "⌛ انتهت المهلة دون تسليم — إما ما رد العميل أو ميتا رفضت الإرسال",
}

#: What the card must carry for the DEFINITE sentence — a bool, true when the
#: last outbound message on this bundle carries a Meta refusal code.
REFUSED_KEY = "refused_by_meta"


def _delivery_ar(last: dict[str, Any]) -> str:
    """The honest one-liner for the last delivery.

    Two lies this has told the operator, both of the same kind — a sentence
    about the CUSTOMER covering for a fact about US:

    * a PARTIAL that delivered NOTHING is a failure, not a partial success; he
      must not read «وصل جزء منها» when zero messages landed (§15.12);
    * an EXPIRED_WINDOW that Meta refused is not a customer who went quiet.
      ``last[REFUSED_KEY]`` is the fact that settles it, and it is checked
      FIRST because it is true whatever the status word says.

    When the card does not carry that key the wording in :data:`_DELIVERY_AR`
    names both possible causes rather than assuming one, so this screen is
    honest today and merely more PRECISE once the key arrives. Supplying it is
    one predicate in `telegram.console.tenant_card` — «does any
    delivery_messages row on this bundle sit at `failed`» — and that file
    belongs to another owner; it is written up in this change's report.
    """
    status = str(last.get("status"))
    delivered = last.get("delivered")
    if last.get(REFUSED_KEY):
        return "🔴 ميتا رفضت الإرسال — ما وصلت العميل، وما تجاهلها"
    if status == "PARTIAL" and delivered is not None and int(delivered) == 0:
        return "🔴 لم يصل منها شيء"
    return _DELIVERY_AR.get(status, status)


def plan_ar(code: str | None) -> str | None:
    """The Arabic name of a plan — None for a code we have no word for.

    The ONE authority for what a product is called (the weekly report reads it
    from here). None rather than the raw code, because a raw plan code is a
    Latin run: the caller decides where to put it, and every caller here puts
    it on a line of its own.
    """
    return _PLAN_AR.get(str(code))


def _plan_marked(code: str | None) -> str | None:
    """The plan, starred when its holder is owed a faster human reply.

    None — not the raw code — when we have no Arabic name for it: a plan code
    is a Latin run, and the caller decides which line it goes on.
    """
    label = plan_ar(code)
    if label and str(code) in _PRIORITY_PLANS:
        return f"⭐ {label}"
    return label


def plan_counts_lines(subs: dict[str, Any]) -> list[str]:
    """«💰 اشتراكات جديدة: لمّاح: ٢ · تقييم لمّاح: ١» — the sales breakdown on
    ONE line, counts in Arabic-Indic digits so they can stay inside it.

    Written once for both the business screen and the Sunday report, for the
    same reason the plan NAMES are: the last time this was two pieces of code,
    the two screens named the same product differently for months. A plan code
    we have no Arabic word for is a Latin run, so it goes on the line
    underneath — never dropped, and never inside the Arabic one.
    """
    named: list[str] = []
    raw: list[str] = []
    for code, count in sorted(subs.items()):
        label = plan_ar(code)
        if label:
            named.append(f"{label}: {_ar_digits(count)}")
        else:
            raw.append(f"{code}: {count}")
    if named and raw:
        return ["💰 اشتراكات جديدة: " + " · ".join(named), " · ".join(raw)]
    if named:
        return ["💰 اشتراكات جديدة: " + " · ".join(named)]
    if raw:
        return ["💰 اشتراكات جديدة:", " · ".join(raw)]
    return ["💰 اشتراكات جديدة: لا شيء"]


def render_customers(
    rows: list[dict[str, Any]], page: int, total: int
) -> tuple[str, Keyboard]:
    """``rows``: this page only — {code, plan_code, journey_state, window}.
    Each customer IS a button (tap → the card); TEN codes only."""
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = (f"👥 العملاء — {_ar_digits(total)} إجمالًا"
            f" (صفحة {_ar_digits(page + 1)} من {_ar_digits(pages)})")
    keyboard: Keyboard = []
    for row in rows:
        marks = " ".join(mark for mark in (
            "⭐" if str(row.get("plan_code")) in _PRIORITY_PLANS else "",
            _JOURNEY_MARK.get(str(row.get("journey_state")), "⏳"),
            _WINDOW_MARK.get(str(row.get("window")), ""),
        ) if mark)
        label = f"{row['code']} · {marks}" if marks else str(row["code"])
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


#: Past this many days a closed ticket stops being counted in days and starts
#: being counted in months. Not a policy — a READING rule: «منذ ٢١٤ يوم» is a
#: number the eye has to convert before it means anything, and the operator is
#: reading this card mid-conversation with a customer. Thirty days is also
#: where the answer changes character: a ticket closed inside the last month is
#: plausibly about what he is complaining about right now, and one closed seven
#: months ago is history that must not read as coverage of today.
_TICKET_MONTHS_AFTER_DAYS = 30


def _support_history_lines(card: dict[str, Any]) -> list[str]:
    """«هل تعامل معه أحد من قبل؟» — answered on the customer's own card.

    THE GAP THIS CLOSES. A closed ticket leaves the queue by design
    (`support.CLOSED_STATUSES`), which is right: the tickets screen is what is
    still OWED and an inbox that keeps everything is an inbox nobody empties.
    But it left «did anybody ever deal with this customer» with no answer
    anywhere on this console — the operator hears «راسلتكم وما رد أحد», opens
    the card, and cannot tell a ticket he closed last week from one that was
    never raised. `support_events.resolved_at` has held that answer since
    `console._run_ticket_close` started writing it and nothing read it.

    NOT A CLOSED-TICKETS SCREEN, and the argument is not mine — it is
    `whatsapp.activation_flow._raise_opted_out_ticket`'s, about this same
    table: «a second inbox is a second thing to forget». A watchtower screen
    nobody opens is worse than an absent one because it looks like coverage.
    The question is asked about ONE customer while looking at HIM, so it is one
    line on the screen the operator already has open, beside the promises
    :func:`_promise_lines` prints there for exactly the same reason.

    ABSENT WHEN THERE IS NOTHING TO SAY. Most customers never raise a ticket,
    so on most cards this renders nothing at all — which is what makes it worth
    reading on the cards where it does. A line that is always present is a line
    that stops being read, and the card is long already.

    WHAT IT SAYS, and the two decisions the shape forced:

    * **Several closures** — the COUNT plus the age of the LATEST one, never a
      list. «twice, the last one two days ago» is the whole of what changes the
      operator's next sentence; a list of ticket ids is a screen, and it is the
      screen this is instead of.
    * **A closure long ago** — said in months past
      :data:`_TICKET_MONTHS_AFTER_DAYS`, so an ancient closure reads as history
      rather than as an answer to today. A resolved ticket whose ``resolved_at``
      is NULL — every one closed before the column started being written — says
      so instead of guessing an age.

    ── THE CONSOLE SIDE, WHICH THIS PASS DOES NOT OWN ──────────────────────
    Views are pure: no session, no clock (module docstring). The fact has to
    arrive in the card, and `telegram/console._tenant_card` is the only builder
    of that dict. It is one read there, beside `_promise_facts`:

        closed = session.execute(
            select(func.count(), func.max(SupportEvent.resolved_at))
            .where(SupportEvent.tenant_id == tenant.id,
                   SupportEvent.status.in_(sorted(CLOSED_STATUSES)))
        ).one()
        ...
        "support_tickets": {
            "closed": int(closed[0]),
            "last_closed_days": (
                (now - closed[1]).days if closed[1] is not None else None
            ),
        },

    Until that key is passed this renders nothing, which is the honest state of
    a half nobody has wired — and is why the shape is a dict rather than two
    loose keys: the console owner adds one entry, and an ``open`` count can
    join it later without a third key or a second decision about the line.
    """
    tickets = card.get("support_tickets") or {}
    closed = int(tickets.get("closed") or 0)
    if closed <= 0:
        return []
    days = tickets.get("last_closed_days")
    if days is None:
        when = "تاريخ الإغلاق غير مسجل"
    else:
        days = max(0, int(days))
        if days == 0:
            when = "آخرها اليوم"
        elif days < _TICKET_MONTHS_AFTER_DAYS:
            when = f"آخرها منذ {_ar_digits(days)} يوم"
        else:
            months = days // _TICKET_MONTHS_AFTER_DAYS
            when = f"آخرها منذ {_ar_digits(months)} شهر"
    return [f"🎫 تذاكر دعم أغلقناها: {_ar_digits(closed)} · {when}"]


def render_tenant_card(card: dict[str, Any]) -> tuple[str, Keyboard]:
    """One customer, zero PII: TEN code + plan + states + counters only."""
    code = str(card.get("code"))
    lines = [f"👤 {code}"]
    # Two lines, not one. The subscription status is a Latin token
    # (ACTIVE / PAUSED / EXPIRED …) and the rest of this file goes out of its
    # way to keep TEN codes and dates on their own lines for exactly this
    # reason — a mixed run re-orders in a right-to-left client and the
    # operator reads a scrambled plan and status.
    lines.extend(_fact_lines(
        "الخطة", _plan_marked(card.get("plan_code")),
        card.get("plan_code") or "—",
    ))
    lines.append("الحالة:")
    lines.append(str(card.get("sub_status") or "—"))
    age = card.get("sub_age_days")
    if age is not None:
        # Two defects in one line. The number sat INSIDE an Arabic sentence as
        # a Latin numeral, which the operator's client re-orders — an
        # Arabic-Indic one sits there safely, so the fact keeps its single
        # line. And it went NEGATIVE: the age is computed as
        # (now - created_at).days, so a subscription row created microseconds
        # ahead of the console's clock — or any clock skew between the API
        # container and the host — rendered «عمر الاشتراك: -22 يوم». A
        # negative age reads as corruption and sends the operator hunting a
        # data problem that does not exist, so today is floored at zero and
        # says so in words.
        days = max(0, int(age))
        lines.append(
            "عمر الاشتراك: "
            + ("اليوم" if days == 0 else f"{_ar_digits(days)} يوم")
        )
    journey = card.get("journey_state")
    if not journey:
        lines.append("الرحلة: لم تبدأ")
    else:
        lines.extend(
            _fact_lines("الرحلة", _JOURNEY_AR.get(str(journey)), journey)
        )
    window = card.get("window")
    if window:
        lines.extend(
            _fact_lines("الواتساب", _WINDOW_AR.get(str(window)), window)
        )
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
        f"قراراته: قدّم {_ar_digits(outcomes.get('applied', 0))}"
        f" · تجاهل {_ar_digits(outcomes.get('ignored', 0))}"
    )
    lines.append(
        f"وظائف محجوبة (مكررة): {_ar_digits(card.get('suppressions', 0))}"
    )
    support_min = card.get("support_minutes")
    review_count = card.get("review_count")
    if support_min is not None or review_count is not None:
        lines.append(
            f"⏱ دقائق دعم: {_ar_digits(support_min or 0)}"
            f" · 👁 مراجعات بشرية: {_ar_digits(review_count or 0)}"
        )
    lines.extend(_promise_lines(card))
    # …and, beside them, whether anyone has ever dealt with this customer —
    # the same «is something owed to HIM» question, asked of a table whose
    # closed rows leave the queue on purpose.
    lines.extend(_support_history_lines(card))
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
        [("⏱ +٥ دقائق دعم", f"v1|log|{code}|support5"),
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
    title = _RANGE_AR.get(range_key)
    lines = [f"💰 الأعمال — {title}"] if title else ["💰 الأعمال", str(range_key)]
    lines.extend(plan_counts_lines(data.get("subs_by_plan") or {}))
    revenue = data.get("revenue_sar")
    if revenue is not None:
        lines.append(f"الإيراد: {_ar_digits(revenue)} ريال")
    upgrades = data.get("funnel_upgrades")
    if upgrades is not None:
        lines.append(f"ترقيات القمع (تحليل ← اشتراك): {_ar_digits(upgrades)}")
    # the store page shows a live seat count — the operator sees the same truth
    if data.get("seats_cap") is not None:
        from career.salla.seats import Seats, seats_line_ar

        lines.append(seats_line_ar(Seats(
            cap=int(data["seats_cap"]), taken=int(data.get("seats_taken", 0)),
        )))
    # The ceiling that stops discovery for EVERYONE at once, and the only one
    # the operator could not see: the provider sells a fixed block of searches
    # per month, and it is the number of CAREER PATHS that consumes it — not
    # the number of customers. Both numbers are Arabic-Indic, so the fact and
    # its ceiling are back on ONE line instead of the two they used to take.
    searches = data.get("searches_this_month")
    if searches is not None:
        lines.append(f"بحثات هذا الشهر: {_ar_digits(searches)} من ١٠٠٠٠")
    delivered = data.get("delivered_days")
    if delivered is not None:
        lines.append(f"أيام تسليم ناجحة: {_ar_digits(delivered)}")
    outcomes = data.get("outcomes") or {}
    applied = int(outcomes.get("applied", 0))
    ignored = int(outcomes.get("ignored", 0))
    total_dec = applied + ignored
    rate = (f" ({_ar_digits(round(100 * applied / total_dec))}٪ تقديم)"
            if total_dec else "")
    lines.append(
        f"قرارات العملاء: قدّم {_ar_digits(applied)}"
        f" · تجاهل {_ar_digits(ignored)}{rate}"
    )
    llm = data.get("llm_generations")
    cost = data.get("llm_cost_usd")
    if llm is not None:
        # «كلود» and «دولار», not «Claude» and «$»: the one Latin run on this
        # screen that is a WORD, and a word has an Arabic form — which beats
        # both other cures (a line of its own, or a digit fold that cannot
        # touch letters). The number itself is a count, so it stays inline.
        cost_part = f" · {_ar_digits(cost)} دولار" if cost is not None else ""
        lines.append(f"نداءات كلود: {_ar_digits(llm)}{cost_part}")
    statuses = data.get("message_statuses") or {}
    if statuses:
        read = int(statuses.get("read", 0))
        landed = read + int(statuses.get("delivered", 0))
        sent_only = int(statuses.get("sent", 0))
        failed = int(statuses.get("failed", 0))
        part = f"رسائلنا: قُرئ {_ar_digits(read)} · وصل {_ar_digits(landed)}"
        if sent_only:
            part += f" · أُرسل {_ar_digits(sent_only)}"
        if failed:
            part += f" · فشل {_ar_digits(failed)}"
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
