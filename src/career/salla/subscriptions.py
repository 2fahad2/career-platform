"""Subscription state machine (whitepaper §05).

The eleven states and the legal transitions between them. Refund / cancel /
chargeback move a live subscription to a service-off state immediately. Every
transition appends a subscription_event (audit trail).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from career.db.models import Subscription, SubscriptionEvent

# ── The eleven states ────────────────────────────────────────────────────────
PENDING_PAYMENT = "PENDING_PAYMENT"
PAID_UNCLAIMED = "PAID_UNCLAIMED"
ONBOARDING = "ONBOARDING"
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"
GRACE = "GRACE"
EXPIRED = "EXPIRED"
CANCELED = "CANCELED"
REFUNDED = "REFUNDED"
CHARGEBACK = "CHARGEBACK"
SUSPENDED = "SUSPENDED"

ALL_STATES = frozenset({
    PENDING_PAYMENT, PAID_UNCLAIMED, ONBOARDING, ACTIVE, PAUSED, GRACE,
    EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED,
})

# Service-off terminal states (a refund/cancel/chargeback lands here).
TERMINAL_STATES = frozenset({CANCELED, REFUNDED, CHARGEBACK})

# A rule, not a table of accidents: **a money reversal beats every internal
# state.** Refund / cancel / chargeback are facts about the payment, and no
# state of ours may refuse one — a subscription that cannot record «the money
# went back» keeps serving a customer who was refunded, and the webhook that
# said so lands in the poison quarantine. So every non-terminal state below
# reaches CANCELED, REFUNDED and CHARGEBACK. (PENDING_PAYMENT and SUSPENDED
# were the two that did not, and both are reachable with real money behind
# them: a captured payment reversed before we ever saw it paid, and a disputed
# account parked for review by the renewal path.)
_ALLOWED: dict[str, frozenset[str]] = {
    PENDING_PAYMENT: frozenset({PAID_UNCLAIMED, CANCELED, REFUNDED, CHARGEBACK, EXPIRED}),
    PAID_UNCLAIMED: frozenset({ONBOARDING, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED, EXPIRED}),
    ONBOARDING: frozenset({ACTIVE, PAUSED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED, EXPIRED}),
    ACTIVE: frozenset({PAUSED, GRACE, EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED}),
    # PAUSED -> GRACE is legal, deliberately, not an accident of the sweep:
    # §05 says a pause does NOT extend the period («الوقف المؤقت لا يمدد
    # المدة»), so a paused period ENDS on exactly the same day an active one
    # would. It therefore has to end the same way — grace, renewal reminder,
    # expiry — otherwise PAUSED is terminal in practice: no clock ever left
    # it, so a customer who paused once was never reminded, never expired,
    # never billed again, and their PII outlived the published 90 days.
    # Entering GRACE resumes no service (families.py enrols ACTIVE only).
    PAUSED: frozenset({ACTIVE, GRACE, EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED}),
    GRACE: frozenset({ACTIVE, EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED}),
    # A refund/cancel/chargeback can arrive long after a period closed —
    # renewals (§16) retire the superseded row to EXPIRED while its order
    # stays refundable for weeks. Refusing the transition threw
    # InvalidTransition out of the webhook worker and jammed the whole queue.
    EXPIRED: frozenset({ACTIVE, CANCELED, REFUNDED, CHARGEBACK}),
    CANCELED: frozenset(),
    REFUNDED: frozenset(),
    CHARGEBACK: frozenset(),
    SUSPENDED: frozenset({ACTIVE, CANCELED, REFUNDED, CHARGEBACK}),
}

# Salla order lifecycle event → the target service-off state.
_ORDER_EVENT_TO_STATE = {
    "order.refunded": REFUNDED,
    "order.cancelled": CANCELED,
    "order.canceled": CANCELED,
    "order.chargeback": CHARGEBACK,
}


class InvalidTransition(Exception):
    pass


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in _ALLOWED.get(from_status, frozenset())


def transition(
    session: Session,
    subscription: Subscription,
    to_status: str,
    *,
    event_type: str,
    salla_order_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> Subscription:
    """Apply a validated state transition and record a subscription_event."""
    if to_status not in ALL_STATES:
        raise InvalidTransition(f"unknown target state: {to_status}")
    from_status = subscription.status
    if from_status == to_status:
        raise InvalidTransition(f"no-op transition from {from_status}")
    if not can_transition(from_status, to_status):
        raise InvalidTransition(f"illegal transition {from_status} -> {to_status}")

    subscription.status = to_status
    subscription.updated_at = func.now()
    session.add(
        SubscriptionEvent(
            id=uuid.uuid4(),
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            salla_order_id=salla_order_id,
            details=details or {},
        )
    )
    session.flush()
    return subscription


def apply_order_lifecycle(
    session: Session,
    subscription: Subscription,
    salla_event_type: str,
    *,
    salla_order_id: str | None = None,
) -> Subscription:
    """Map a Salla order lifecycle webhook to a state change. Refund/cancel/
    chargeback suspend service immediately. A subscription already in a terminal
    state is left unchanged (idempotent)."""
    target = _ORDER_EVENT_TO_STATE.get(salla_event_type)
    if target is None:
        raise InvalidTransition(f"unhandled order event: {salla_event_type}")
    if subscription.status in TERMINAL_STATES:
        return subscription  # already off — idempotent
    return transition(
        session, subscription, target,
        event_type=salla_event_type, salla_order_id=salla_order_id,
    )
