"""The onboarding state machine — pure logic (whitepaper §05).

PAID_UNCLAIMED → PHONE_VERIFIED → CONSENT_PENDING → CV_UPLOAD_PENDING →
CV_PROCESSING → PROFILE_CONFIRMATION → CAREER_PATH_REVIEW →
SEARCH_POLICY_REVIEW → READY_FOR_ACTIVATION → ACTIVE.

Rules, verbatim from the whitepaper:
- resumable at any time — progression is strictly forward, one state at a time
  (the single documented backward edge: a rejected/unreadable CV sends
  CV_PROCESSING back to CV_UPLOAD_PENDING);
- a stall of more than 24 hours makes the journey reminder-eligible, once per
  stall (a new customer interaction re-arms the reminder);
- «دعم» escalates to a human from every state.

No I/O and no clock here — ``now`` is always injected, so every rule is
deterministic and unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

STATES: tuple[str, ...] = (
    "PAID_UNCLAIMED",
    "PHONE_VERIFIED",
    "CONSENT_PENDING",
    "CV_UPLOAD_PENDING",
    "CV_PROCESSING",
    "PROFILE_CONFIRMATION",
    "CAREER_PATH_REVIEW",
    "SEARCH_POLICY_REVIEW",
    "READY_FOR_ACTIVATION",
    "ACTIVE",
)

_INDEX: dict[str, int] = {state: i for i, state in enumerate(STATES)}

#: The only documented backward edge: a failed scan / unreadable file returns
#: the customer to the upload step.
_FAILURE_REGRESSIONS: dict[str, str] = {"CV_PROCESSING": "CV_UPLOAD_PENDING"}

#: Stall threshold for the reminder template (whitepaper §05: «توقف >24 ساعة»).
REMINDER_STALL = timedelta(hours=24)

#: States where a stall reminder makes sense: the customer has a verified
#: WhatsApp channel (past PAID_UNCLAIMED) and the journey is not finished.
_REMINDER_ELIGIBLE: frozenset[str] = frozenset(STATES[1:-1])


class InvalidTransition(Exception):
    """Raised for transitions the whitepaper does not allow."""


def _require_known(state: str) -> None:
    if state not in _INDEX:
        raise InvalidTransition(f"unknown onboarding state: {state!r}")


def state_index(state: str) -> int:
    _require_known(state)
    return _INDEX[state]


def is_complete(state: str) -> bool:
    _require_known(state)
    return state == STATES[-1]


def next_state(state: str) -> str:
    """The single allowed forward step from ``state``."""
    _require_known(state)
    if is_complete(state):
        raise InvalidTransition("ACTIVE is terminal — nothing to advance to")
    return STATES[_INDEX[state] + 1]


def advance(state: str) -> str:
    """Alias of :func:`next_state` — advancing never skips a state."""
    return next_state(state)


def regress_on_failure(state: str) -> str:
    """The documented failure regression for ``state`` (only CV_PROCESSING)."""
    _require_known(state)
    try:
        return _FAILURE_REGRESSIONS[state]
    except KeyError:
        raise InvalidTransition(f"no failure regression from {state!r}") from None


def validate_transition(current: str, target: str) -> None:
    """Allow exactly: the next forward state, or a documented failure
    regression. Anything else (skips, arbitrary backward moves, unknown
    states) raises :class:`InvalidTransition`."""
    _require_known(current)
    _require_known(target)
    if is_complete(current):
        raise InvalidTransition("ACTIVE is terminal")
    if target == STATES[_INDEX[current] + 1]:
        return
    if _FAILURE_REGRESSIONS.get(current) == target:
        return
    raise InvalidTransition(f"transition {current!r} → {target!r} is not allowed")


def is_reminder_due(
    *,
    state: str,
    last_interaction_at: datetime | None,
    last_reminder_at: datetime | None,
    now: datetime,
) -> bool:
    """True when the journey stalled for more than 24h and this stall has not
    been reminded yet. A reminder sent after the last interaction suppresses
    repeats; a newer interaction re-arms it. Never true without an interaction
    timestamp (nothing to measure the stall from), outside eligible states, or
    at exactly the threshold (strictly greater — «أكثر من 24 ساعة»)."""
    _require_known(state)
    if state not in _REMINDER_ELIGIBLE:
        return False
    if last_interaction_at is None:
        return False
    if now - last_interaction_at <= REMINDER_STALL:
        return False
    return not (last_reminder_at is not None and last_reminder_at > last_interaction_at)


def supports_interrupt(state: str) -> bool:
    """«دعم» escalates to a human from every state — no exceptions."""
    _require_known(state)
    return True
