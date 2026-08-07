#!/usr/bin/env python3
"""Keep the Salla credential alive BEFORE it dies — never after.

THE INCIDENT THIS EXISTS BECAUSE OF. The access token expired on 2026-07-29
and nobody found out for nine days, until paid orders stopped provisioning.
Nothing was broken: the token simply reached its expiry with no path to renew
it, because `app.store.authorize` — the one moment Salla ever offers a
replacement — was defined and deliberately not consumed. CHANGELOG §29 closes
that decision and adds the fourth condition this file implements: «التجديد قبل
الانتهاء لا بعده، ومن مؤقّت يملك سببه».

WHAT SALLA'S OWN DOCUMENTATION SAYS, and what it does not
(docs.salla.dev/doc-421118 «Authorization», docs.salla.dev/421413m0 «App
Events»; read 2026-08-07):

  * The token endpoint is ``https://accounts.salla.sa/oauth2/token``.
  * The refresh exchange is ``grant_type=refresh_token`` with ``client_id``,
    ``client_secret`` and ``refresh_token``; ``scope`` is optional.
  * The success body is documented verbatim as
    ``{"access_token": ..., "expires_in": 1209599, "refresh_token": ...,
    "scope": ..., "token_type": "bearer"}`` — ``expires_in`` is a DURATION IN
    SECONDS, and 1209599 is fourteen days. The webhook's field is a different
    shape entirely (``expires``, a unix timestamp), so the two must never be
    read by the same code path.
  * **Refresh tokens are single-use.** Every successful exchange issues a new
    one and invalidates the old. Salla states that reusing a refresh token, or
    refreshing twice in parallel, makes its OAuth server invalidate the token,
    revoke the access tokens obtained with it, refuse every later attempt, and
    REQUIRE THE MERCHANT TO REINSTALL THE APPLICATION. That single sentence
    decides most of this file: the lock, the absence of a retry loop, and the
    fact that the only recovery we can ever offer the operator is «reinstall».

  NOT VERIFIED, and therefore not depended on:
  * The exact error body of a rejected refresh. Salla documents the
    consequence, not the payload. ``invalid_grant`` / ``invalid_client`` are
    RFC 6749 §5.2 codes and the endpoint answers in that shape (the token
    prefixes ``ory_at_``/``ory_rt_`` in Salla's own example say it is Ory
    Hydra), but no Salla page states them. So classification is by HTTP
    STATUS — any 4xx is «refused» — and the ``error`` field, when present and
    well-formed, only sharpens the sentence the operator reads.
  * The access-token lifetime is stated inconsistently by Salla itself: the
    Authorization page says two weeks and the documented ``expires_in`` agrees
    (1209599s), while other Salla material says one week. Nothing here assumes
    a lifetime: every decision is made from the expiry the store actually
    holds, and the one place a lifetime has to be guessed (a success body
    whose ``expires_in`` is unusable) takes the SHORTER of the two.
  * Whether Salla rotates the refresh token before or after it answers, and
    therefore what a lost answer means. Hence the ``lost`` reason on
    :class:`RefreshFailed`: it is treated as «the credential may already be
    gone», never as «ask again».

THE DECISIONS, each with the alternative that was rejected.

1. MARGIN — refresh when five days or fewer remain (:data:`REFRESH_MARGIN_DAYS`).
   The timer is daily, so the margin IS the retry budget: five days means six
   attempts (5, 4, 3, 2, 1, 0) before the credential dies, and a single bad
   night — a Salla outage, a DNS blip, a host that was off — costs nothing.
   Rejected: a one- or two-day margin, which survives exactly one failure and
   would have re-created the incident with extra steps. Also rejected: a
   margin of half the token life (7 days), which refreshes twice as often for
   no added safety, and every extra exchange is another chance to lose the
   single-use refresh token in flight.

2. NO RETRY LOOP. A failed attempt waits for tomorrow's timer. The exception
   is a failure that could not possibly have reached Salla (a connection that
   never opened): those are retried twice, in-process, because nothing was
   sent and nothing could have rotated. Rejected: the ordinary backoff-and-
   retry loop, and rejected specifically BECAUSE of the single-use rule — a
   read timeout means the request was delivered and the answer was lost, so
   the server may already have rotated; retrying then presents a token Salla
   considers used, which is the one action that turns «we could not refresh
   today» into «the merchant must reinstall». The margin gives us six honest
   attempts a day apart; that is a better retry policy than seconds apart.

3. A REJECTED REFRESH TOKEN IS UNMISTAKABLE. It is unrecoverable from this
   host — Salla's Easy Mode never shows the token to anyone, so there is
   nothing for a human to paste. The only recovery is Fahad reinstalling the
   app on his store, which re-fires `app.store.authorize` with a fresh
   credential. So a refusal alerts immediately at any margin, says exactly
   that sentence, and exits :data:`EXIT_NEEDS_REINSTALL`.

4. IT NEVER SPEAKS TWICE THE SAME WAY. An alarm that fires flatly every day
   for weeks gets muted, and a muted channel is how nine days pass. So the
   operator hears from THIS script only when the tier changes — margin →
   warning → urgent → dead, or a new kind of failure — and each message
   carries the number of days that is left. The steady daily drumbeat is the
   boot check's job (career.engine.cli), which is the one place a credential
   that is already dead must speak every single morning.

5. FAIL CLOSED ON THE LOCK, FAIL OPEN ON THE ALERT. Two copies of this script
   refreshing at once is the documented way to destroy the installation, so a
   lock that cannot be taken stops the refresh. De-duplication state that
   cannot be read does the opposite and sends anyway — the cost of a wrong
   guess there is a noisy hour, and the cost in the other direction is the
   silence this file exists to end (same reasoning, same shape, as
   scripts/alert_unit_failure.sh).

6. IT SWEEPS THE SUPERSEDED COPY, even on the days it does nothing else. The
   credential store keeps one backup of the secrets file and replaces it on
   every write; what a module that only runs when something is stored cannot
   do is expire that file on a host where nothing is being stored — which is
   the July state exactly. This timer runs daily whatever happens, so it is
   the one that can. Rejected: a second timer for the sweep, which is a second
   thing to notice had stopped; and rejected: doing it only when a refresh
   actually happens, which is the case that already replaces the file.

NO TOKEN VALUE IS EVER PRINTED — §15.13 and CHANGELOG §29's second condition.
That is enforced by the objects and not by the call sites: :class:`NewCredential`
hides its two fields exactly as ``career.salla.tokens.SallaCredentials`` does,
because a generated repr puts a live token in every traceback frame and every
assertion diff no matter how careful the code around it is.
Both halves of a successful exchange are registered with the log scrubber the
instant they are parsed, so even a future careless ``%s`` is redacted rather
than trusted to discipline; the response body is never logged, and the only
part of an error body that can reach the journal is an ``error`` code that
matches a short lowercase shape.

THE PERSIST IS THE POINT. Once Salla has answered 200, the OLD refresh token
is already dead: whatever happens next, the only credential that exists is the
one in memory. So storing it takes precedence over every opinion this script
has about the answer, and a failure to store is a red alert, not an exception
in a log.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from career.logging_filters import install_secret_redaction, register_secret

logger = logging.getLogger("career.salla.token_refresh")

#: Verified 2026-08-07 against docs.salla.dev/doc-421118 («Authorization»).
OAUTH_ENDPOINT = "https://accounts.salla.sa/oauth2/token"

#: Days of remaining life at or below which the credential is renewed.
#: Decision 1 in the module docstring. The boot check in
#: ``career.engine.cli`` reads the same number as ITS warning threshold on
#: purpose: with a daily timer, a boot at 11:00 can only still see five days
#: left if the 09:00 refresh did not happen or did not work, so the threshold
#: is silent on every healthy day and speaks on exactly the days that matter.
REFRESH_MARGIN_DAYS = 5

#: Seconds for the one HTTP call. Long enough for a slow accounts host, short
#: enough that the oneshot cannot sit past its own timer's next firing.
HTTP_TIMEOUT_S = 20.0

#: Salla's two documented access-token lifetimes disagree (one week / two
#: weeks). Used ONLY when a 200 arrives with an unusable ``expires_in``, and
#: the shorter one is taken so an unreadable answer makes us refresh sooner
#: rather than believe a life we were never told.
FALLBACK_LIFETIME_DAYS = 7

EXIT_OK = 0
#: The attempt failed and tomorrow's timer will try again. Non-zero so the
#: unit enters `failed` and OnFailure= puts the operational fact on the phone;
#: the actionable sentence is this script's own alert.
EXIT_TRANSIENT = 1
#: Unrecoverable from this host. Only a reinstall on the store fixes it.
EXIT_NEEDS_REINSTALL = 2
#: We cannot even ask: no token store, no credential, or no app identity.
EXIT_NOT_CONFIGURED = 3

TIER_MARGIN = "margin"
TIER_WARN = "warn"
TIER_URGENT = "urgent"
TIER_DEAD = "dead"

#: Failures that speak the first time they happen no matter how much margin is
#: left, because they either cannot heal on their own or may already have cost
#: us the credential.
LOUD_REASONS = frozenset({"refused", "lost", "unconfigured"})

#: An OAuth error code we are willing to put in the journal: short, lowercase,
#: no separators a token could hide in.
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z_]{1,39}$")

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def ar_num(value: int) -> str:
    """Arabic-Indic digits, and never a Latin minus sign.

    A number in an Arabic line has to be Arabic-Indic or the line arrives
    reversed in the operator's client, and a leading `-` scrambles it even
    when the digits are right (CHANGELOG §28). Callers pass magnitudes and put
    the direction in the words.
    """
    return str(abs(int(value))).translate(_ARABIC_DIGITS)


class RefreshFailed(Exception):
    """One exchange did not produce a credential we can store.

    ``reason`` decides everything the caller does with it:

      ``refused``  Salla said no. Unrecoverable here — reinstall.
      ``unavailable``  We could not ask (nothing was sent). Tomorrow.
      ``lost``  We asked and the answer never arrived, so Salla may already
        have rotated the token underneath us. Not retried, and loud.
    """

    def __init__(self, message: str, *, reason: str, error_code: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.error_code = error_code


@dataclass(frozen=True, repr=False)
class NewCredential:
    """What a successful exchange yields: two LIVE credentials.

    ``repr=False`` and the explicit ``__repr__`` are the same armour
    ``career.salla.tokens.SallaCredentials`` carries, and for the same reason
    (constant 13): a generated repr puts both values in every traceback frame
    of this file, in every ``logger.info("%s", credential)`` a later edit
    adds, and in every pytest assertion diff — and this module's own docstring
    promises that NO TOKEN VALUE IS EVER PRINTED. Nothing rendered it on
    2026-08-07; nothing stopped the next thing from rendering it either, and
    «no caller does it today» is not a property of the object.

    ``tests/test_salla_token_refresh.TestNoDataclassCarriesAToken`` is the
    guard that stops the next such class: it walks every dataclass in this file
    and in the credential module and fails on any that would render a field
    whose name says it holds a secret.
    """

    access_token: str
    refresh_token: str
    expires_at: datetime
    #: True when ``expires_in`` was missing or unusable and
    #: :data:`FALLBACK_LIFETIME_DAYS` was assumed instead.
    expiry_assumed: bool = False

    def __repr__(self) -> str:
        return (
            "NewCredential("
            f"access_token={'set' if self.access_token else 'unset'}, "
            f"refresh_token={'set' if self.refresh_token else 'unset'}, "
            f"expires_at={self.expires_at.isoformat()}, "
            f"expiry_assumed={self.expiry_assumed})"
        )

    __str__ = __repr__


class FormPoster(Protocol):
    """The one network call, injectable so every branch is provable offline."""

    def __call__(
        self, url: str, data: dict[str, str], timeout: float
    ) -> tuple[int, dict[str, Any]]: ...


class AdminChannel(Protocol):
    def send_admin(self, text: str) -> str: ...


# ── the token store (agent T1's `career.salla.tokens`) ──────────────────────
#
# Bound by name at call time rather than imported at module load, for one
# reason that is not tidiness: this script must be able to REFUSE TO EXCHANGE
# when the store is missing. A refresh whose result cannot be persisted is
# strictly worse than no refresh at all — it rotates the single-use token,
# receives the replacement, and drops it, which leaves the installation dead
# with no way back except a reinstall. So the store is resolved and checked in
# full before the first byte goes to Salla.


@dataclass(frozen=True)
class TokenStore:
    current: Callable[[], Any]
    days_left: Callable[[], int | None]
    store_credentials: Callable[..., Any]
    #: Optional, and the only member that is: shredding a superseded copy of
    #: the secrets file is housekeeping, so a store that does not offer it must
    #: still be able to refresh a credential. See :func:`_sweep_backups`.
    prune_backups: Callable[[], bool] | None = None


def load_store(module: ModuleType | None = None) -> TokenStore:
    """Resolve `career.salla.tokens` and prove it can do all three jobs."""
    if module is None:
        from career.salla import tokens

        module = tokens
    missing = [
        name for name in ("current", "days_left", "store_credentials")
        if not callable(getattr(module, name, None))
    ]
    if missing:
        raise RefreshFailed(
            "career.salla.tokens is missing " + ", ".join(missing),
            reason="unconfigured",
        )
    return TokenStore(
        current=module.current,
        days_left=module.days_left,
        store_credentials=module.store_credentials,
        prune_backups=getattr(module, "prune_credential_backups", None),
    )


def _httpx_post(
    url: str, data: dict[str, str], timeout: float
) -> tuple[int, dict[str, Any]]:  # pragma: no cover — the network edge
    """The real call. Transport failures are classified by whether the request
    can possibly have been delivered, because that is the only question that
    decides whether retrying is safe."""
    import httpx

    try:
        response = httpx.post(url, data=data, timeout=timeout)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # The connection never opened: Salla did not see the request, so
        # nothing rotated and asking again is free.
        raise RefreshFailed("could not reach salla", reason="unavailable") from exc
    except httpx.TransportError as exc:
        # Sent, and the answer did not come back. The refresh token may be
        # spent already. Never retried — see decision 2.
        raise RefreshFailed(
            "the answer to the refresh was lost in transit", reason="lost"
        ) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return response.status_code, payload


def exchange(
    *,
    poster: FormPoster,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    now: datetime,
    attempts: int = 3,
) -> NewCredential:
    """POST the documented refresh exchange and read the documented answer.

    ``attempts`` retries ONLY the failure that cannot have been delivered.
    """
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    for attempt in range(1, max(1, attempts) + 1):
        try:
            status, payload = poster(OAUTH_ENDPOINT, data, HTTP_TIMEOUT_S)
        except RefreshFailed as exc:
            if exc.reason == "unavailable" and attempt < attempts:
                logger.warning(
                    "salla unreachable on attempt %d of %d — nothing was sent, "
                    "so asking again is safe", attempt, attempts,
                )
                continue
            raise
        return _read_answer(status, payload, now=now)
    raise RefreshFailed("unreachable", reason="unavailable")  # pragma: no cover


def _read_answer(
    status: int, payload: dict[str, Any], *, now: datetime
) -> NewCredential:
    code = _error_code(payload)
    if status == 200:
        return _credential_from(payload, now=now)
    if 400 <= status < 500:
        # Salla is refusing the credential itself. There is no version of this
        # that a retry improves, and none that a human on this host can fix:
        # Easy Mode never exposes the token to be re-pasted.
        raise RefreshFailed(
            f"salla refused the refresh (HTTP {status})", reason="refused",
            error_code=code,
        )
    raise RefreshFailed(
        f"salla could not answer the refresh (HTTP {status})",
        reason="unavailable", error_code=code,
    )


def _error_code(payload: dict[str, Any]) -> str:
    """The RFC 6749 ``error`` field IF it is a short lowercase code.

    Anything else is discarded rather than logged. The body of a token
    endpoint's response is the last place to relax about what reaches a
    journal — one shape of it carries live credentials.
    """
    raw = payload.get("error")
    if isinstance(raw, str) and _ERROR_CODE_RE.match(raw):
        return raw
    return ""


def _credential_from(payload: dict[str, Any], *, now: datetime) -> NewCredential:
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access or not refresh:
        # A 200 we cannot read is the dangerous shape: Salla has answered, so
        # the old refresh token is spent, and we are holding nothing.
        raise RefreshFailed(
            "salla answered 200 without a usable credential pair", reason="lost"
        )
    # Registered BEFORE anything else can touch them, including the exception
    # paths below.
    register_secret(access)
    register_secret(refresh)

    assumed = False
    try:
        seconds = int(payload["expires_in"])
    except (KeyError, TypeError, ValueError):
        seconds = 0
    if seconds <= 0 or seconds > 400 * 86400:
        seconds = FALLBACK_LIFETIME_DAYS * 86400
        assumed = True
    return NewCredential(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + timedelta(seconds=seconds),
        expiry_assumed=assumed,
    )


# ── how loud, and how often ─────────────────────────────────────────────────


def tier_for(days_left: int | None) -> str:
    """Remaining margin → how bad this is.

    Deliberately coarse and deliberately NOT the same thresholds as the
    refresh itself: the first two days inside the margin are the automation's
    own business (four more attempts are still coming), and telling the
    operator about them would be an alarm that fires while nothing is wrong.
    """
    if days_left is None:
        return TIER_URGENT
    if days_left < 0:
        return TIER_DEAD
    if days_left <= 1:
        return TIER_URGENT
    if days_left <= 3:
        return TIER_WARN
    return TIER_MARGIN


def should_speak(reason: str, tier: str, last_key: str | None) -> bool:
    """Speak on a CHANGE, never on a repetition.

    The whole ladder is bounded by construction: from the first failure inside
    the margin to expiry is at most six days, and this script is silent for
    the first two of them. What the operator can therefore receive is a strict
    escalation — warning, then urgent, then dead — plus one immediate message
    for a failure that cannot heal itself. Ten identical lines a week is what
    trains a person to swipe the channel away; this cannot produce them.
    """
    key = f"{reason}:{tier}"
    if key == last_key:
        return False
    return reason in LOUD_REASONS or tier != TIER_MARGIN


def _state_path() -> Path:
    return Path(os.environ.get(
        "CAREER_SALLA_TOKEN_STATE", "/run/career-salla-token/last-alert"
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
    """A success resets the ladder: the next failure is news again."""
    try:
        path.unlink()
    except OSError:
        pass


# ── the sentences ───────────────────────────────────────────────────────────
#
# Every line is direction-pure — Arabic alone, or a variable name alone. A
# mixed line arrives reversed in the operator's client, and these are read on
# a phone at the moment something is already wrong.


def _runway_line(days_left: int | None) -> str:
    if days_left is None:
        return "لا نعرف كم بقي من عمر الاعتماد"
    if days_left < 0:
        return f"انتهى منذ أيام عددها: {ar_num(days_left)}"
    return f"الأيام المتبقية قبل توقف تزويد الطلبات المدفوعة: {ar_num(days_left)}"


#: The one recovery that exists. Salla's Easy Mode never shows the token to a
#: human, so there is nothing to paste anywhere: reinstalling the app re-fires
#: `app.store.authorize`, and the store consumes it (CHANGELOG §29).
REINSTALL_LINES = (
    "الحل: أعد تثبيت التطبيق على متجرك من لوحة سلة",
    "إعادة التثبيت ترسل اعتمادًا جديدًا ويُحفظ تلقائيًا بدون أي خطوة منك",
)


def alert_text(reason: str, tier: str, days_left: int | None) -> str:
    """The message, chosen by WHY and by HOW MUCH IS LEFT."""
    lines: list[str] = []
    if reason == "refused":
        lines.append("🔴 سلة رفضت تجديد اعتماد المتجر — لا يمكن إصلاحه من الخادم")
        lines.append(_runway_line(days_left))
        lines.extend(REINSTALL_LINES)
    elif reason == "lost":
        lines.append("🔴 انقطع الرد أثناء تجديد اعتماد سلة — قد يكون تبدّل ولم يصلنا")
        lines.append(_runway_line(days_left))
        lines.append("إن فشلت محاولة الغد أيضًا فالاعتماد ضاع، وعندها:")
        lines.extend(REINSTALL_LINES)
    elif reason == "unstorable":
        lines.append("🔴 وصل اعتماد سلة الجديد ولم نستطع حفظه — القديم لم يعد صالحًا")
        lines.extend(REINSTALL_LINES)
    elif reason == "unconfigured":
        lines.append("🔴 التجديد التلقائي لاعتماد سلة معطّل — لا يوجد ما يُجدَّد")
        lines.append(_runway_line(days_left))
        lines.extend(REINSTALL_LINES)
    else:  # unavailable
        if tier == TIER_DEAD:
            lines.append("🔴 اعتماد سلة انتهى والتجديد يفشل — الطلبات المدفوعة تنتظر")
        elif tier == TIER_URGENT:
            lines.append("🔴 تجديد اعتماد سلة يفشل والمهلة توشك أن تنتهي")
        else:
            lines.append("⚠️ تجديد اعتماد سلة لم ينجح — تعاد المحاولة تلقائيًا كل يوم")
        lines.append(_runway_line(days_left))
        lines.append("إن استمر الفشل حتى نفاد المهلة:")
        lines.extend(REINSTALL_LINES)
    return "\n".join(lines)


MISSING_APP_IDENTITY = "\n".join((
    "🔴 هوية تطبيق سلة ناقصة على الخادم — التجديد التلقائي لا يستطيع العمل",
    "المتغيرات الناقصة:",
    "SALLA_CLIENT_ID",
    "SALLA_CLIENT_SECRET",
))


def announce(
    admin: AdminChannel,
    *,
    reason: str,
    days_left: int | None,
    text: str | None = None,
    state: Path | None = None,
) -> bool:
    """Send at most one message, and only when it says something new."""
    path = state or _state_path()
    tier = tier_for(days_left)
    if not should_speak(reason, tier, read_last_key(path)):
        logger.info(
            "not repeating the %s alert at tier %s — nothing has changed",
            reason, tier,
        )
        return False
    try:
        admin.send_admin(text if text is not None else alert_text(reason, tier, days_left))
    except Exception:  # noqa: BLE001 — an unreachable phone never masks the exit code
        logger.warning("could not reach the admin channel", exc_info=True)
        return False
    write_last_key(path, f"{reason}:{tier}")
    return True


# ── the run ─────────────────────────────────────────────────────────────────


class LockUnavailable(RuntimeError):
    """Another refresh holds the lock, or the lock cannot be taken at all."""


def _acquire_lock() -> Any:
    """Single-flight, and FAIL CLOSED.

    Salla invalidates everything when one refresh token is presented twice, so
    two concurrent runs are not a race that costs a duplicate message — they
    are the documented way to force a reinstall. Everything else in this
    project's ops tooling fails open; this one must not.
    """
    path = Path(os.environ.get(
        "CAREER_SALLA_TOKEN_LOCK", "/run/career-salla-token.lock"
    ))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
    except OSError as exc:
        raise LockUnavailable(f"cannot open the refresh lock at {path}") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise LockUnavailable("another refresh is already running") from exc
    return handle


def _app_identity() -> tuple[str, str]:
    """`SALLA_CLIENT_ID` / `SALLA_CLIENT_SECRET` from the process environment.

    Read here and not from ``career.config.Settings``, which does not carry
    them — that file belongs to another owner, and the unit already supplies
    the whole credential file through ``EnvironmentFile=`` exactly as
    career-engine-nightly.service does.
    """
    return (
        os.environ.get("SALLA_CLIENT_ID", "").strip(),
        os.environ.get("SALLA_CLIENT_SECRET", "").strip(),
    )


class _JournalAdmin:
    """Where the message goes when Telegram is not configured."""

    def send_admin(self, text: str) -> str:
        logger.error("ADMIN: %s", text)
        return "journal"


def _admin_for() -> AdminChannel:  # pragma: no cover — thin wiring
    """The operator's channel, chosen the same way ``cli.admin_client_for``
    chooses it — and deliberately NOT by importing that function.

    ``career.engine.cli`` builds an owner-role database engine, and the D20
    ratchet (tests/test_rls_runtime_role.py) is an assertion about which
    production modules can reach the RLS-bypassing role. This job touches no
    database at all; importing the nightly CLI for four lines of wiring would
    quietly add it to that set, which is exactly the kind of drift the ratchet
    exists to catch.
    """
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


def _days_left(store: TokenStore) -> int | None:
    try:
        value = store.days_left()
    except Exception:  # noqa: BLE001 — a blind store must not crash the timer
        logger.error("the token store could not say how long is left", exc_info=True)
        return None
    return None if value is None else int(value)


def _sweep_backups(store: TokenStore) -> None:
    """Give the superseded secrets file a death date that does not depend on
    the next write ever happening.

    ``career.salla.tokens`` replaces its one backup on every write, so the
    generation count is bounded there; what it cannot bound by itself is TIME,
    because it only runs when something is stored. This timer runs daily
    whatever happens, which makes it the right owner — and the wrong thing to
    let fail: a backup that could not be shredded is not a reason to skip a
    credential renewal.
    """
    if store.prune_backups is None:
        return
    try:
        store.prune_backups()
    except Exception:  # noqa: BLE001 — housekeeping never blocks the refresh
        logger.warning("could not sweep the superseded credential backup",
                       exc_info=True)


def run(
    *,
    poster: FormPoster,
    admin: AdminChannel,
    store: TokenStore,
    now: datetime,
    margin_days: int = REFRESH_MARGIN_DAYS,
    state: Path | None = None,
) -> int:
    """One decision and, at most, one exchange. Returns the exit code."""
    # BEFORE the margin check, because the margin check returns early on every
    # healthy day and the file being swept is left by the LAST rotation. A host
    # that stops rotating — which is the state this project was in for nine
    # days in July — is exactly the host whose superseded copy of every secret
    # would otherwise sit at 0600 forever with no owner.
    _sweep_backups(store)
    days_left = _days_left(store)
    if days_left is not None and days_left > margin_days:
        logger.info(
            "salla credential has %d days left — outside the %d day margin, "
            "leaving it alone", days_left, margin_days,
        )
        return EXIT_OK

    try:
        credential = store.current()
    except Exception as exc:  # noqa: BLE001
        logger.error("the token store holds no readable credential: %s", type(exc).__name__)
        credential = None
    refresh_token = str(getattr(credential, "refresh_token", "") or "")
    if not refresh_token:
        logger.error(
            "no stored refresh token — nothing can be renewed from this host"
        )
        announce(admin, reason="unconfigured", days_left=days_left, state=state)
        return EXIT_NOT_CONFIGURED

    client_id, client_secret = _app_identity()
    if not client_id or not client_secret:
        logger.error(
            "SALLA_CLIENT_ID/SALLA_CLIENT_SECRET are not set — the refresh "
            "exchange cannot be made"
        )
        announce(
            admin, reason="unconfigured", days_left=days_left,
            text=MISSING_APP_IDENTITY, state=state,
        )
        return EXIT_NOT_CONFIGURED

    logger.info(
        "salla credential has %s days left — inside the %d day margin, refreshing",
        "unknown" if days_left is None else str(days_left), margin_days,
    )
    try:
        fresh = exchange(
            poster=poster, client_id=client_id, client_secret=client_secret,
            refresh_token=refresh_token, now=now,
        )
    except RefreshFailed as exc:
        return _handle_failure(exc, admin=admin, days_left=days_left, state=state)

    # From here the OLD refresh token is spent. Storing what we hold is the
    # only thing that matters; every other consideration is secondary to it.
    if not _persist(store, fresh, credential):
        announce(admin, reason="unstorable", days_left=days_left, state=state)
        return EXIT_NEEDS_REINSTALL

    if fresh.expiry_assumed:
        logger.warning(
            "salla did not return a usable expires_in — recorded a "
            "conservative %d day life instead", FALLBACK_LIFETIME_DAYS,
        )
    logger.info(
        "salla credential refreshed; it now expires on %s",
        fresh.expires_at.date().isoformat(),
    )
    clear_last_key(state or _state_path())
    return EXIT_OK


def _persist(
    store: TokenStore, fresh: NewCredential, previous: Any,
    *, attempts: int = 3, pause_s: float = 1.0,
) -> bool:
    """Write the rotated pair, and TRY HARDER than anywhere else in this file.

    This is the mirror image of decision 2. Retrying the EXCHANGE can destroy
    the installation, because it re-presents a single-use token to Salla.
    Retrying the WRITE cannot: it touches nothing but our own file, the old
    credential is already spent either way, and the only alternative to
    succeeding here is telling Fahad to reinstall the app. A transient
    `CredentialWriteError` — a full disk, a lock held by the webhook worker
    writing the same file — must not cost him that.
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            outcome = store.store_credentials(
                access_token=fresh.access_token,
                refresh_token=fresh.refresh_token,
                expires_at=fresh.expires_at,
                store_id=getattr(previous, "store_id", None),
            )
        except Exception as exc:  # noqa: BLE001 — the one failure with no way back
            logger.error(
                "attempt %d of %d to store the refreshed salla credential "
                "failed: %s", attempt, attempts, type(exc).__name__,
            )
            if attempt < attempts:
                time.sleep(pause_s)
                continue
            logger.error(
                "the refreshed salla credential could not be stored and the "
                "previous one is already spent", exc_info=True,
            )
            return False
        # The store refuses a credential older than the one it already holds
        # (a redelivered authorize event beside a live refresh timer). It
        # cannot normally happen here — what we just received expires two
        # weeks out and what we replaced expires within five days — so if it
        # DOES, something wrote the file underneath us, and that is worth a
        # line rather than a shrug.
        if str(getattr(outcome, "value", outcome)) == "stale":
            logger.warning(
                "the store kept its own credential and discarded the refreshed "
                "one as older — something else wrote the file during the "
                "exchange"
            )
        return True
    return False  # pragma: no cover — the loop always returns


def _handle_failure(
    exc: RefreshFailed, *, admin: AdminChannel, days_left: int | None,
    state: Path | None,
) -> int:
    detail = f" ({exc.error_code})" if exc.error_code else ""
    logger.error("salla refresh failed: %s%s", exc, detail)
    announce(admin, reason=exc.reason, days_left=days_left, state=state)
    if exc.reason == "refused":
        return EXIT_NEEDS_REINSTALL
    return EXIT_TRANSIENT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="refresh_salla_token",
        description="Renew the Salla credential before it expires (CHANGELOG §29).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="say what would happen and exit — never calls Salla, never "
             "writes. Safe to run by hand on a live host.",
    )
    return parser


def main(  # pragma: no cover — composition; every branch is tested via run()
    argv: list[str] | None = None,
    *,
    poster: FormPoster | None = None,
    admin: AdminChannel | None = None,
    store: TokenStore | None = None,
    now: datetime | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO)
    install_secret_redaction()
    args = build_parser().parse_args(argv)

    try:
        resolved_store = store if store is not None else load_store()
    except RefreshFailed as exc:
        logger.error("no salla token store on this host: %s", exc)
        if not args.check:
            announce(
                admin if admin is not None else _admin_for(),
                reason="unconfigured", days_left=None,
            )
        return EXIT_NOT_CONFIGURED

    if args.check:
        days_left = _days_left(resolved_store)
        margin = REFRESH_MARGIN_DAYS
        verdict = (
            "would refresh now" if days_left is None or days_left <= margin
            else "nothing to do"
        )
        print(f"days left: {days_left} | margin: {margin} | {verdict}")  # noqa: T201
        return EXIT_OK

    try:
        lock = _acquire_lock()
    except LockUnavailable as exc:
        # Not an alert: either a sibling run is doing the work right now, or
        # the host cannot lock — and the second case still reaches the phone,
        # via the non-zero exit and the unit's OnFailure=.
        logger.error("refusing to refresh without the single-flight lock: %s", exc)
        return EXIT_TRANSIENT
    try:
        return run(
            poster=poster if poster is not None else _httpx_post,
            admin=admin if admin is not None else _admin_for(),
            store=resolved_store,
            now=now or datetime.now(UTC),
        )
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
