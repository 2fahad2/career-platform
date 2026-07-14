"""Subscription state machine (pure transition rules)."""

from __future__ import annotations

from career.salla import subscriptions as st


def test_all_eleven_states_defined() -> None:
    assert len(st.ALL_STATES) == 11


def test_provisioning_target_and_activation_path() -> None:
    assert st.can_transition(st.PENDING_PAYMENT, st.PAID_UNCLAIMED)
    assert st.can_transition(st.PAID_UNCLAIMED, st.ONBOARDING)
    assert st.can_transition(st.ONBOARDING, st.ACTIVE)


def test_service_off_reachable_from_live_states() -> None:
    for live in (st.PAID_UNCLAIMED, st.ONBOARDING, st.ACTIVE, st.PAUSED, st.GRACE):
        assert st.can_transition(live, st.REFUNDED)
        assert st.can_transition(live, st.CANCELED)
        assert st.can_transition(live, st.CHARGEBACK)


def test_terminal_states_have_no_exits() -> None:
    for terminal in st.TERMINAL_STATES:
        assert all(not st.can_transition(terminal, other) for other in st.ALL_STATES)


def test_illegal_transition_examples() -> None:
    assert not st.can_transition(st.ACTIVE, st.PENDING_PAYMENT)
    assert not st.can_transition(st.REFUNDED, st.ACTIVE)
    assert st.can_transition(st.EXPIRED, st.ACTIVE)  # renewal reactivation is allowed


def test_order_event_mapping() -> None:
    assert st._ORDER_EVENT_TO_STATE["order.refunded"] == st.REFUNDED
    assert st._ORDER_EVENT_TO_STATE["order.cancelled"] == st.CANCELED
    assert st._ORDER_EVENT_TO_STATE["order.chargeback"] == st.CHARGEBACK
