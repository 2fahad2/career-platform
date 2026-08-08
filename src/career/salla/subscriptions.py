"""Subscription state machine (whitepaper §05).

The eleven states and the legal transitions between them. Refund / cancel /
chargeback move a live subscription to a service-off state immediately. Every
transition appends a subscription_event (audit trail).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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

#: The states a pause may legally leave, DERIVED from the table above and
#: never restated anywhere else.
#:
#: onboarding.privacy kept a hand-written copy of this set and the two drifted:
#: it counted GRACE as pausable while no GRACE→PAUSED edge was ever added here,
#: so `transition` raised InvalidTransition straight through the standing
#: privacy command into the WhatsApp worker. The customer whose paid period had
#: just ended — exactly the person most likely to type «وقف مؤقت» — got their
#: inbound row rolled back, their event marked failed, and no reply at all.
#: GRACE stays out on the product's own terms (§05: the period has ALREADY
#: ended there, the 48 hours are a countdown to expiry, and a pause «لا يمدد
#: المدة» so it cannot buy a single one of them back), but the point of
#: publishing the set is that nobody has to remember that: whoever wants to
#: know whether a state can be paused asks the machine.
PAUSABLE_STATES: frozenset[str] = frozenset(
    state for state, allowed in _ALLOWED.items() if PAUSED in allowed
)

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
    now: datetime | None = None,
) -> Subscription:
    """Map a Salla order lifecycle webhook to a state change. Refund/cancel/
    chargeback suspend service immediately. A subscription already in a terminal
    state is left unchanged (idempotent).

    THE FOUNDER PRICE LAPSES HERE, and here is the point. «انقطعت أكثر؟ الكرسي
    ينفتح لغيرك، وترجع بسعر يومها» is one sentence with two consequences: the
    seat opens (`seats._HOLDING_STATES` already reads these states) and the
    locked price ends with it. Only the first was implemented, so a refunded —
    or charged-back, never-activated — customer kept a standing right to the
    founding price forever: `price_lock._continuous` measures from
    ``current_period_end`` and a buyer who never activated has none, so the
    lock could not even age out on its own.

    It is called from THIS function rather than from provisioning's reversal
    paths because a rule a writer must remember to call is a rule the next
    writer will not call — the same reason `cv.close._record_day_state` is the
    single writer of a day state. Every route to a terminal state passes
    through here, including the ones that do not exist yet.
    """
    target = _ORDER_EVENT_TO_STATE.get(salla_event_type)
    if target is None:
        raise InvalidTransition(f"unhandled order event: {salla_event_type}")
    if subscription.status in TERMINAL_STATES:
        return subscription  # already off — idempotent, and no re-stamp
    updated = transition(
        session, subscription, target,
        event_type=salla_event_type, salla_order_id=salla_order_id,
    )
    # AFTER the transition, never before: the lock lapses only once this
    # customer holds no live row on the plan, and that question is answered
    # from the status this call just wrote.
    from career.promises.price_lock import lapse_for_terminal

    lapse_for_terminal(
        session, tenant_id=updated.tenant_id, plan_code=updated.plan_code,
        now=now or datetime.now(UTC), reason=salla_event_type,
    )
    return updated
