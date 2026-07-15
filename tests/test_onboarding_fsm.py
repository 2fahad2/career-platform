"""Onboarding FSM acceptance tests (whitepaper §05) — written before the code.

The journey is a linear 10-state machine, resumable at any time; a >24h stall
makes it reminder-eligible (one reminder per stall); «دعم» interrupts from every
state. Pure logic: time is always injected, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from career.onboarding import fsm

T0 = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


# ── the ten states, in order (whitepaper §05 verbatim) ───────────────────────


def test_states_are_the_documented_ten_in_order() -> None:
    assert fsm.STATES == (
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


def test_happy_path_advances_through_all_states() -> None:
    state = fsm.STATES[0]
    visited = [state]
    while not fsm.is_complete(state):
        state = fsm.advance(state)
        visited.append(state)
    assert visited == list(fsm.STATES)


def test_advance_never_skips_states() -> None:
    for i, state in enumerate(fsm.STATES[:-1]):
        assert fsm.advance(state) == fsm.STATES[i + 1]


def test_active_is_terminal() -> None:
    assert fsm.is_complete("ACTIVE")
    with pytest.raises(fsm.InvalidTransition):
        fsm.advance("ACTIVE")


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(fsm.InvalidTransition):
        fsm.advance("SHIPPED")
    with pytest.raises(fsm.InvalidTransition):
        fsm.validate_transition("SHIPPED", "ACTIVE")


# ── transition validation: forward-next or a documented failure regression ───


def test_validate_transition_accepts_forward_next_only() -> None:
    fsm.validate_transition("CONSENT_PENDING", "CV_UPLOAD_PENDING")
    with pytest.raises(fsm.InvalidTransition):
        fsm.validate_transition("CONSENT_PENDING", "PROFILE_CONFIRMATION")  # skip
    with pytest.raises(fsm.InvalidTransition):
        fsm.validate_transition("PROFILE_CONFIRMATION", "CONSENT_PENDING")  # backward


def test_rejected_cv_regresses_processing_to_upload() -> None:
    """A failed security scan / unreadable file sends the customer back to
    upload — the only documented backward edge."""
    fsm.validate_transition("CV_PROCESSING", "CV_UPLOAD_PENDING")
    assert fsm.regress_on_failure("CV_PROCESSING") == "CV_UPLOAD_PENDING"
    with pytest.raises(fsm.InvalidTransition):
        fsm.regress_on_failure("CAREER_PATH_REVIEW")


# ── reminder eligibility (>24h stall, once per stall, injected time) ─────────


def test_stall_over_24h_is_reminder_due() -> None:
    assert fsm.is_reminder_due(
        state="CV_UPLOAD_PENDING",
        last_interaction_at=T0,
        last_reminder_at=None,
        now=T0 + timedelta(hours=24, minutes=1),
    )


def test_stall_under_24h_is_not_due() -> None:
    assert not fsm.is_reminder_due(
        state="CV_UPLOAD_PENDING",
        last_interaction_at=T0,
        last_reminder_at=None,
        now=T0 + timedelta(hours=23, minutes=59),
    )


def test_exactly_24h_is_not_due_yet() -> None:
    assert not fsm.is_reminder_due(
        state="CV_UPLOAD_PENDING",
        last_interaction_at=T0,
        last_reminder_at=None,
        now=T0 + timedelta(hours=24),
    )


def test_reminder_already_sent_for_this_stall_is_not_repeated() -> None:
    """One reminder per stall: a reminder newer than the last interaction
    suppresses further reminders until the customer interacts again."""
    assert not fsm.is_reminder_due(
        state="CV_UPLOAD_PENDING",
        last_interaction_at=T0,
        last_reminder_at=T0 + timedelta(hours=25),
        now=T0 + timedelta(hours=49),
    )


def test_new_interaction_after_reminder_rearms_it() -> None:
    reminded = T0 + timedelta(hours=25)
    interacted_again = T0 + timedelta(hours=30)
    assert fsm.is_reminder_due(
        state="CV_UPLOAD_PENDING",
        last_interaction_at=interacted_again,
        last_reminder_at=reminded,
        now=interacted_again + timedelta(hours=24, minutes=1),
    )


def test_active_and_unclaimed_states_are_never_reminder_eligible() -> None:
    """ACTIVE has nothing to remind about; PAID_UNCLAIMED has no verified
    WhatsApp channel to send a reminder to (the claim deadline is a separate
    email/Salla flow, not this template)."""
    for state in ("ACTIVE", "PAID_UNCLAIMED"):
        assert not fsm.is_reminder_due(
            state=state,
            last_interaction_at=T0,
            last_reminder_at=None,
            now=T0 + timedelta(days=3),
        )


def test_no_interaction_timestamp_is_never_due() -> None:
    assert not fsm.is_reminder_due(
        state="CV_UPLOAD_PENDING",
        last_interaction_at=None,
        last_reminder_at=None,
        now=T0 + timedelta(days=2),
    )


# ── «دعم» interrupts from every state (whitepaper §05) ───────────────────────


def test_support_interrupts_every_state() -> None:
    for state in fsm.STATES:
        assert fsm.supports_interrupt(state)
