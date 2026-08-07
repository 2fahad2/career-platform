#!/usr/bin/env python3
"""Watch the credential the whole product SPEAKS through — and renew nothing.

WHY THIS FILE EXISTS. The Salla token expired on 2026-07-29 and the first
symptom anybody saw was paid orders provisioning nothing, nine days later. The
cadence audit then found that the Meta credential — the one that DELIVERS to
everyone who has already paid — had no scheduled observer at all: the boot
check asked whether ``WHATSAPP_ACCESS_TOKEN`` was a non-empty string, which is
a question about a file, not about a credential.

WHAT `/debug_token` ACTUALLY SAYS ABOUT OUR TOKEN. Read live, read-only, on
2026-08-08 (nothing here reproduces the value, the number or any id):

    type                     SYSTEM_USER
    expires_at               0            ← never
    data_access_expires_at   0            ← never
    is_valid                 true
    scopes                   whatsapp_business_management,
                             whatsapp_business_messaging,
                             manage_app_solution,
                             whatsapp_business_manage_events, public_profile

**SO THERE IS NOTHING TO RENEW, AND THIS SCRIPT RENEWS NOTHING.** Meta issues
several kinds of token and they are not interchangeable: a short-lived user
token (~1 hour), a long-lived user token (~60 days, refreshable through
``fb_exchange_token``), an app token, and a System User token, which is the
only one that can be issued with expiry «Never». ``expires_at: 0`` is Meta's
own spelling of «never», and it is what we hold. A System User token has NO
refresh endpoint — the exchange that renews a 60-day user token answers an
error for it — so a refresher written for this credential would be a scheduled
job whose only possible outcome is failure, and a permanently red unit is a
unit the operator stops reading. The «roughly two weeks» in the project notes
was TRUE of the temporary token pasted in on 17 July, and is not true of what
is configured now.

``data_access_expires_at: 0`` closes the other clock. That timestamp is
Meta's Platform-Terms data-access window (the 90-day one), and it can run out
while a token is still ``is_valid: true`` — a token that authenticates and
then cannot read what it was granted. Ours is 0, so it does not apply. It is
asserted anyway, because a token that reports «no expiry» WITH a data-access
clock set is the signature of a long-lived USER token, not a System User one,
and that swap is exactly the thing this file exists to notice.

WHAT CAN STILL TAKE IT AWAY. «Permanent» is a statement about the clock, not
about the credential. Every one of these is instantaneous and announced to
nobody:

  * it is revoked — a human clicks it away in Business Settings, the System
    User is deleted or demoted, or the app secret is reset;
  * the app is disabled or restricted, or its WhatsApp product is removed;
  * a SCOPE is withdrawn. ``is_valid`` stays true and every send starts coming
    back refused, so a check that reads only ``is_valid`` certifies a total
    outage as healthy;
  * the phone number or its WABA is UNASSIGNED from the System User. The token
    is valid, scoped and useless — and ``/debug_token`` cannot see this at all,
    which is why there is a second, equally read-only GET;
  * somebody replaces it with a temporary token. Then a countdown is back, and
    on this credential a countdown is a REGRESSION rather than a schedule.

THE DECISIONS, each with what it rejected.

1. LIVE STATE, NEVER A CONFIGURED CONSTANT. Salla's watch read
   ``SALLA_TOKEN_EXPIRES_AT`` — a date a human typed once — which is why it
   could be confidently wrong for nine days. Every number here comes back from
   Meta on the run that uses it. Rejected: a `WHATSAPP_TOKEN_EXPIRES_AT`
   variable, which is the same bug with a new name.

2. DAILY, IN HIS WORKING HOURS, BEFORE THE DELIVERY RUN. There is no margin to
   divide into a retry budget here — a permanent token has no runway, so the
   only quantity a schedule can move is TIME-TO-DETECTION. The recovery is
   entirely manual (generate a token in Business Settings, paste, restart), so
   a cadence that fires when nobody can act buys nothing. 08:30 Riyadh is the
   start of the operator's day and two and a half hours ahead of the 11:00
   delivery run, which is enough to fix it before the night it would ruin.
   Rejected: hourly — the outage it detects is already loud within one nightly
   run (`cli.exit_code_for` returns 3 and `OnFailure=` pages), so twenty-four
   pages a day would buy hours of notice on a fix nobody can apply at 03:00.
   Rejected: weekly — a week of unclaimed paid orders is the incident again.

3. IT RETRIES THE GET, WHERE `scripts/refresh_salla_token.py` REFUSES TO. That
   script must never repeat its call: Salla's refresh tokens are single-use and
   a second exchange revokes the installation. This is a read-only GET —
   repeating it rotates nothing and costs nothing — so a blip is retried
   in-process instead of becoming a page.

4. AN UNREADABLE ANSWER IS NOT AN ALARM. «We could not reach Meta» says
   nothing about the credential, and alarming on it would put Meta's uptime on
   the operator's phone at night with no action attached — which is precisely
   how the channel that must carry «the token is revoked» gets muted. The
   transient fact still reaches him, once and deduplicated, through a non-zero
   exit and the unit's ``OnFailure=``.

5. IT NEVER SPEAKS TWICE THE SAME WAY. Same rule as the Salla refresher and
   the same mechanism: a stamp holds the last thing said, and the operator
   hears from THIS script only when the verdict or the tier changes. The daily
   drumbeat for a credential that is already dead belongs to the boot check
   (`career.engine.cli.report_environment`), which is the one place that must
   repeat itself every single morning.

6. IT NEVER SENDS A WHATSAPP MESSAGE AND NEVER POSTs TO META. A watchdog that
   proves the channel by using it is a watchdog that can bill us, can wake a
   customer, and can trip Meta's own quality controls. Two GETs, and the
   verdict is read off them.

NO TOKEN VALUE IS EVER PRINTED (§15.13). ``TokenHealth`` carries no secret at
all — not the token, not the phone number, not an id — so even a traceback
frame or an assertion diff is safe, and the token is registered with the log
scrubber before the first call regardless.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Protocol

from career.logging_filters import install_secret_redaction, register_secret
from career.whatsapp.client import (
    TokenHealth,
    TokenVerdict,
    inspect_token,
    token_problems,
)

logger = logging.getLogger("career.whatsapp.token_watch")

#: Days of remaining life at or below which a DATED token becomes urgent.
#:
#: Deliberately NOT `provisioning.TOKEN_EXPIRY_WARN_DAYS` (5), even though the
#: two look alike. That number is the Salla refresher's own margin and means
#: «six automated attempts left»; here nothing is attempting anything, so the
#: only thing the number can measure is how long a HUMAN needs to notice a
#: message, reach Meta Business Settings, generate a token, paste it and
#: restart. A week covers a weekend and a trip. Sharing Salla's constant would
#: have tied a human's response time to an OAuth server's retry budget.
HANDOVER_DAYS = 7

EXIT_OK = 0
#: Meta could not be asked. Non-zero so the unit enters `failed` and
#: `OnFailure=` states the operational fact once — but no Telegram message
#: from this script, per decision 4.
EXIT_UNREADABLE = 1
#: The credential is dead, degraded, or on a clock. Only a human fixes it.
EXIT_NEEDS_NEW_TOKEN = 2
#: Nothing configured to check.
EXIT_NOT_CONFIGURED = 3

TIER_OK = "ok"
TIER_MARGIN = "margin"
TIER_WARN = "warn"
TIER_URGENT = "urgent"
TIER_DEAD = "dead"

REASON_OK = "ok"
REASON_INVALID = "invalid"
REASON_SCOPES = "scopes"
REASON_UNADDRESSABLE = "unaddressable"
REASON_TEMPORARY = "temporary"
REASON_UNCONFIGURED = "unconfigured"
REASON_UNREADABLE = "unreadable"

#: Findings that speak the first time they are seen, whatever the tier —
#: because none of them heals on its own and every one of them is already
#: costing a customer something.
LOUD_REASONS = frozenset({
    REASON_INVALID, REASON_SCOPES, REASON_UNADDRESSABLE, REASON_TEMPORARY,
    REASON_UNCONFIGURED,
})

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def ar_num(value: int) -> str:
    """Arabic-Indic digits, and never a Latin minus sign.

    Identical to `scripts/refresh_salla_token.ar_num` and duplicated rather
    than imported on purpose: importing it would make this unit's ability to
    speak depend on the Salla refresher still existing, and these two jobs
    have no other relationship. Callers pass magnitudes and put the direction
    in the words (CHANGELOG §28).
    """
    return str(abs(int(value))).translate(_ARABIC_DIGITS)


class AdminChannel(Protocol):
    def send_admin(self, text: str) -> str: ...


# ── the verdict ─────────────────────────────────────────────────────────────


def reason_for(health: TokenHealth) -> str:
    """One finding, worst first.

    Ordered by what it costs the customer right now: a refused token stops
    everything, a missing scope stops everything one layer down, an
    unassigned number stops everything one layer below that, and a dated token
    stops nothing yet.
    """
    if health.verdict is TokenVerdict.UNCONFIGURED:
        return REASON_UNCONFIGURED
    if health.verdict is TokenVerdict.UNREADABLE:
        return REASON_UNREADABLE
    if health.verdict is TokenVerdict.INVALID:
        return REASON_INVALID
    if health.missing_scopes:
        return REASON_SCOPES
    if health.number_addressable is False:
        return REASON_UNADDRESSABLE
    if not health.never_expires or not health.data_access_never_expires:
        return REASON_TEMPORARY
    return REASON_OK


def tier_for(reason: str, days_left: int | None) -> str:
    """How bad, so that a repeat can be told from an escalation.

    Everything that is not a countdown is URGENT by construction: a revoked
    token, a withdrawn scope and an unassigned number are all total outages
    already in progress, and there is no smaller version of them to grade.
    """
    if reason == REASON_OK:
        return TIER_OK
    if reason in (REASON_INVALID, REASON_SCOPES, REASON_UNADDRESSABLE,
                  REASON_UNCONFIGURED):
        return TIER_URGENT
    if days_left is None:
        return TIER_URGENT
    if days_left < 0:
        return TIER_DEAD
    if days_left <= 1:
        return TIER_URGENT
    if days_left <= HANDOVER_DAYS:
        return TIER_WARN
    return TIER_MARGIN


def should_speak(reason: str, tier: str, last_key: str | None) -> bool:
    """Speak on a CHANGE, never on a repetition — `refresh_salla_token`'s rule.

    The one addition is that TIER_MARGIN speaks here when the reason is loud.
    Salla's margin rung is silent because its refresher is actively working
    inside it and four more attempts are still coming; nothing is working on
    this credential's behalf, so «a temporary token appeared, with a month of
    runway» has to be said the day it appears or it is said the day it dies.
    """
    if reason in (REASON_OK, REASON_UNREADABLE):
        return False
    key = f"{reason}:{tier}"
    if key == last_key:
        return False
    return reason in LOUD_REASONS or tier != TIER_MARGIN


def _state_path() -> Path:
    return Path(os.environ.get(
        "CAREER_WHATSAPP_TOKEN_STATE", "/run/career-whatsapp-token/last-alert"
    ))


def read_last_key(path: Path) -> str | None:
    """FAILS OPEN: an unreadable stamp means «say it», never «stay quiet»."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_last_key(path: Path, key: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}")
        tmp.write_text(key, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("could not record the alert stamp at %s", path)


def clear_last_key(path: Path) -> None:
    """A healthy run resets the ladder: the next finding is news again."""
    try:
        path.unlink()
    except OSError:
        pass


# ── the sentences ───────────────────────────────────────────────────────────
#
# Every line is direction-pure: Arabic alone, or a variable name alone, never
# both — and Latin DIGITS count as Latin, which is what `ar_num` is for. These
# are read on a phone at the moment something is already wrong.

#: The only recovery that exists, and it is entirely manual. There is no
#: refresh endpoint for a System User token, so no server-side step can be
#: offered here the way one could be for a 60-day user token.
NEW_TOKEN_LINES = (
    "الحل: من إعدادات أعمال ميتا، افتح مستخدم النظام وأنشئ رمزًا جديدًا",
    "اختر التطبيق وصلاحيتي واتساب، واضبط انتهاء الصلاحية على «أبدًا»",
    "ثم ضع الرمز الجديد في ملف الإعدادات وأعد تشغيل الخدمة",
)


def _runway_line(days_left: int | None) -> str:
    if days_left is None:
        return "لا نعرف كم بقي من عمر الرمز الحالي"
    if days_left < 0:
        return f"انتهى منذ أيام عددها: {ar_num(days_left)}"
    return f"الأيام المتبقية قبل توقف التسليم: {ar_num(days_left)}"


def alert_text(reason: str, tier: str, health: TokenHealth) -> str:
    """The message, chosen by WHAT IS WRONG and by HOW MUCH TIME IS LEFT."""
    lines: list[str] = []
    if reason == REASON_INVALID:
        lines.append("🔴 ميتا ترفض رمز واتساب — لا شيء يصل أي عميل الآن")
        lines.append("لا السيرة اليومية ولا رسالة التفعيل ولا تنبيه التجديد")
        lines.extend(NEW_TOKEN_LINES)
    elif reason == REASON_SCOPES:
        lines.append("🔴 رمز واتساب فقد صلاحية الإرسال — كل رسالة سترفضها ميتا")
        lines.append("الرمز نفسه ما زال مقبولًا، والصلاحية هي الناقصة")
        lines.extend(NEW_TOKEN_LINES)
    elif reason == REASON_UNADDRESSABLE:
        lines.append("🔴 الرمز سليم لكنه لم يعد يملك رقم الإرسال")
        lines.append("أعد ربط الرقم بحساب واتساب للأعمال في إعدادات أعمال ميتا")
        lines.append("لا داعي لإنشاء رمز جديد — المشكلة في ربط الرقم وحده")
    elif reason == REASON_UNCONFIGURED:
        lines.append("🔴 لا يوجد رمز واتساب مضبوط — التسليم اليومي لا يعمل")
        lines.extend(NEW_TOKEN_LINES)
    else:  # REASON_TEMPORARY — the countdown that should not exist
        if tier == TIER_DEAD:
            lines.append("🔴 رمز واتساب انتهى — التسليم متوقف عن كل العملاء")
        elif tier == TIER_URGENT:
            lines.append("🔴 رمز واتساب يوشك أن ينتهي والتسليم سيتوقف")
        elif tier == TIER_WARN:
            lines.append("⚠️ رمز واتساب مؤقت وقارب على الانتهاء")
        else:
            lines.append("⚠️ رمز واتساب الحالي مؤقت وليس دائمًا")
        lines.append("لا يوجد أي تجديد تلقائي لهذا الرمز — التجديد يدوي بالكامل")
        lines.append(_runway_line(health.days_left))
        lines.extend(NEW_TOKEN_LINES)
    return "\n".join(lines)


def announce(
    admin: AdminChannel, *, reason: str, health: TokenHealth,
    state: Path | None = None,
) -> bool:
    """Send at most one message, and only when it says something new."""
    path = state or _state_path()
    tier = tier_for(reason, health.days_left)
    if not should_speak(reason, tier, read_last_key(path)):
        logger.info("not repeating the %s alert at tier %s — nothing changed",
                    reason, tier)
        return False
    try:
        admin.send_admin(alert_text(reason, tier, health))
    except Exception:  # noqa: BLE001 — an unreachable phone never masks the exit
        logger.warning("could not reach the admin channel", exc_info=True)
        return False
    write_last_key(path, f"{reason}:{tier}")
    return True


# ── the run ─────────────────────────────────────────────────────────────────


class _JournalAdmin:
    """Where the message goes when Telegram is not configured."""

    def send_admin(self, text: str) -> str:
        logger.error("ADMIN: %s", text)
        return "journal"


def _admin_for() -> AdminChannel:  # pragma: no cover — thin wiring
    """The operator's channel, chosen the way ``cli.admin_client_for`` chooses
    it and deliberately not by importing it — see the same note in
    `scripts/refresh_salla_token.py`: that function builds an owner-role
    database engine, and this job touches no database at all."""
    from career.config import get_settings

    settings = get_settings()
    if settings.telegram_admin_bot_token and settings.telegram_admin_chat_id:
        from career.telegram.admin import HttpTelegramAdminClient

        client: AdminChannel = HttpTelegramAdminClient(
            settings.telegram_admin_bot_token,
            settings.telegram_admin_chat_id,
        )
        return client
    return _JournalAdmin()


EXIT_FOR_REASON = {
    REASON_OK: EXIT_OK,
    REASON_UNREADABLE: EXIT_UNREADABLE,
    REASON_UNCONFIGURED: EXIT_NOT_CONFIGURED,
    REASON_INVALID: EXIT_NEEDS_NEW_TOKEN,
    REASON_SCOPES: EXIT_NEEDS_NEW_TOKEN,
    REASON_UNADDRESSABLE: EXIT_NEEDS_NEW_TOKEN,
}


def exit_code_for(reason: str, tier: str) -> int:
    """The unit's verdict as one number.

    A TEMPORARY token with real runway exits 0 on purpose. The finding is
    already on the operator's phone (once) and in the boot check's daily line;
    a unit that sits in `failed` for the fifty-five days such a token still has
    is a red light that means «yes, we know», and the next red light after it
    means nothing at all. It goes non-zero once the runway is inside the week a
    human needs.
    """
    if reason == REASON_TEMPORARY:
        return EXIT_OK if tier == TIER_MARGIN else EXIT_NEEDS_NEW_TOKEN
    return EXIT_FOR_REASON.get(reason, EXIT_NEEDS_NEW_TOKEN)


def run(
    *, health: TokenHealth, admin: AdminChannel, state: Path | None = None,
) -> int:
    """Everything after the two GETs: decide, speak once, return a number."""
    reason = reason_for(health)
    tier = tier_for(reason, health.days_left)

    for problem in token_problems(health, HANDOVER_DAYS):
        # The same English sentence the boot check writes, from the same
        # ladder (`career.whatsapp.client.token_problems` — NOT via
        # `engine.cli`, which would drag this database-free watchdog across
        # the D20 owner-role ratchet), so the journal and the phone can never
        # disagree. ERROR is the level the operator's harvester forwards.
        logger.error("WHATSAPP TOKEN %s: %s", problem.key, problem.english)

    if reason == REASON_UNREADABLE:
        # Not an alarm and not a verdict — see decision 4. The non-zero exit
        # is the whole signal.
        logger.error("could not read the WhatsApp credential from Meta — "
                     "the token was NOT judged either way")
    elif reason == REASON_OK:
        logger.info("whatsapp credential: type %s, no expiry, scopes present, "
                    "sending number addressable", health.token_type or "?")
        clear_last_key(state or _state_path())
    else:
        announce(admin, reason=reason, health=health, state=state)
    return exit_code_for(reason, tier)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_whatsapp_token",
        description="Ask Meta whether the delivery credential is still one.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="print the verdict and exit — never alerts, never writes the "
             "stamp. Safe to run by hand on a live host.",
    )
    return parser


def main(  # pragma: no cover — composition; every branch is tested via run()
    argv: list[str] | None = None,
    *,
    health: TokenHealth | None = None,
    admin: AdminChannel | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO)
    install_secret_redaction()
    args = build_parser().parse_args(argv)

    if health is None:
        from career.config import get_settings

        settings = get_settings()
        token = settings.whatsapp_access_token
        # get_settings() already registers it; repeated here because this file
        # promises it in its own docstring and that promise must not depend on
        # a module somebody else owns.
        register_secret(token)
        health = inspect_token(token, settings.whatsapp_phone_number_id)

    reason = reason_for(health)
    if args.check:
        runway = "never" if health.never_expires else str(health.days_left)
        print(  # noqa: T201 — the point of --check
            f"verdict: {health.verdict} | type: {health.token_type or '?'} | "
            f"expires in days: {runway} | missing scopes: "
            f"{','.join(health.missing_scopes) or 'none'} | number "
            f"addressable: {health.number_addressable} | finding: {reason}"
        )
        return exit_code_for(reason, tier_for(reason, health.days_left))

    return run(health=health, admin=admin if admin is not None else _admin_for())


if __name__ == "__main__":
    sys.exit(main())
