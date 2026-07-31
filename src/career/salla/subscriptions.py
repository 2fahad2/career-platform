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

_ALLOWED: dict[str, frozenset[str]] = {
    PENDING_PAYMENT: frozenset({PAID_UNCLAIMED, CANCELED, EXPIRED}),
    PAID_UNCLAIMED: frozenset({ONBOARDING, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED, EXPIRED}),
    ONBOARDING: frozenset({ACTIVE, PAUSED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED, EXPIRED}),
    ACTIVE: frozenset({PAUSED, GRACE, EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED}),
    PAUSED: frozenset({ACTIVE, EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED}),
    GRACE: frozenset({ACTIVE, EXPIRED, CANCELED, REFUNDED, CHARGEBACK, SUSPENDED}),
    # A refund/cancel/chargeback can arrive long after a period closed —
    # renewals (§16) retire the superseded row to EXPIRED while its order
    # stays refundable for weeks. Refusing the transition threw
    # InvalidTransition out of the webhook worker and jammed the whole queue.
    EXPIRED: frozenset({ACTIVE, CANCELED, REFUNDED, CHARGEBACK}),
    CANCELED: frozenset(),
    REFUNDED: frozenset(),
    CHARGEBACK: frozenset(),
    SUSPENDED: frozenset({ACTIVE, CANCELED}),
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
