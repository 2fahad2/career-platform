"""Renewal — the second payment continues, it does not start over (§16).

Before this module the project had no renewal at all. Every paid order built a
whole new customer from nothing: a second tenant for the same human, a second
founding seat, and an activation token that died unclaimed after seven days —
and if the customer typed that token we answered «هذا الرقم مرتبط بحساب آخر».
The day-27 and day-29 renewal reminders walked them into exactly that wall.

The fix belongs in the provisioning layer, decided BEFORE anything is created:
the order's phone is the same phone we already serve, so this is the same
customer. Their profile, achievement bank, search policy and conversation are
untouched; only their days grow.

Two decisions worth stating plainly:

* **One subscription row per ORDER**, on the existing tenant — not an extension
  of the old row. Refunds match an order by ``salla_order_id``; a renewal with
  no row of its own would be a refund with nothing to refund (D18).
* **Days stack, never burn.** Renewing a week early moves the new period's
  start to the old period's end, so nobody is punished for paying early.

Ownership needs no second proof: the phone is already bound to a channel that
was activated by token once (§11). A disputed or suspended account is the one
exception — it is attached to the same tenant but left for a human, because
silently restoring service to a chargeback is not ours to decide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, Subscription
from career.salla import subscriptions as sub_states
from career.whatsapp.phones import phone_variants

#: Pass plans only. The 49-SAR analysis is a one-shot product with no period,
#: and a funnel-only tenant upgrading is a different path entirely (§04
#: inheritance, handled at activation) — neither is a renewal.
RENEWABLE_PLANS: frozenset[str] = frozenset({"basic", "professional", "executive"})

#: A prior subscription in any of these states proves «this human is already
#: our customer». EXPIRED and CANCELED are deliberately included: someone
#: coming back months later is a renewal, not a stranger — and refusing them
#: would recreate the very dead end this module exists to remove.
_PRIOR_STATES: frozenset[str] = frozenset({
    sub_states.ONBOARDING, sub_states.ACTIVE, sub_states.PAUSED,
    sub_states.GRACE, sub_states.EXPIRED, sub_states.CANCELED,
    sub_states.REFUNDED,
})

#: Accounts we re-link but never auto-restore — the operator decides.
_NEEDS_REVIEW: frozenset[str] = frozenset({
    sub_states.CHARGEBACK, sub_states.SUSPENDED,
})

#: The paid period, mirroring policy._SUBSCRIPTION_DAYS.
SUBSCRIPTION_DAYS = 30

#: Which subscription is "the live one" when a tenant holds several. Lower
#: sorts first. Deliberately explicit rather than «whichever row comes back».
_LIVENESS: dict[str, int] = {
    sub_states.ACTIVE: 0,
    sub_states.ONBOARDING: 1,
    sub_states.PAUSED: 2,
    sub_states.GRACE: 3,
    sub_states.PAID_UNCLAIMED: 4,
    sub_states.PENDING_PAYMENT: 5,
    sub_states.EXPIRED: 6,
    sub_states.SUSPENDED: 7,
    sub_states.CHARGEBACK: 8,
    sub_states.CANCELED: 9,
    sub_states.REFUNDED: 10,
}


def _sort_key(sub: Subscription) -> tuple[int, int, float, float]:
    """Pass plans first, then liveness, then the newest period.

    The plan rank leads deliberately. A tenant can hold a one-shot 29-SAR
    analysis row alongside a real pass (the §04 upgrade path leaves both, by
    design), and «the customer's subscription» is always the pass — the
    analysis is a product they bought once, not the service they are on.
    Getting this order wrong is what let an upgraded customer's search policy
    be built from the analysis row's entitlements (daily_job_limit = 0).
    """
    period_end = sub.current_period_end
    created = sub.created_at
    return (
        0 if sub.plan_code in RENEWABLE_PLANS else 1,
        _LIVENESS.get(sub.status, 99),
        -(period_end.timestamp() if period_end is not None else 0.0),
        -(created.timestamp() if created is not None else 0.0),
    )


def tenant_subscriptions(session: Session, tenant_id: uuid.UUID) -> list[Subscription]:
    """A tenant's subscriptions, liveness-ordered (most current first)."""
    rows = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalars().all()
    return sorted(rows, key=_sort_key)


def current_subscription(
    session: Session, tenant_id: uuid.UUID
) -> Subscription | None:
    """THE subscription for this customer right now — the one every status
    reply, pause and resume must act on. Before renewals existed this was
    «the first row we happened to get back», which a renewing customer would
    have seen as their old expired period."""
    rows = tenant_subscriptions(session, tenant_id)
    return rows[0] if rows else None


@dataclass(frozen=True)
class RenewalTarget:
    """An existing customer identified behind a new paid order."""

    tenant_id: uuid.UUID
    previous_id: uuid.UUID
    previous_status: str
    period_start: datetime
    period_end: datetime
    needs_review: bool

    @property
    def new_status(self) -> str:
        """A paused customer stays paused — they asked for quiet, and paying
        does not revoke that. A disputed account waits for a human.

        SUSPENDED, deliberately, for the review case: PAID_UNCLAIMED would
        have been swept by the claim deadline — a welcome template at day 5
        to an activated customer, and the paid renewal silently EXPIRED at
        day 7 with no alert. SUSPENDED is untouched by every sweep and the
        operator can move it straight to ACTIVE.
        """
        if self.needs_review:
            return sub_states.SUSPENDED
        if self.previous_status == sub_states.PAUSED:
            return sub_states.PAUSED
        return sub_states.ACTIVE


def find_renewal(
    session: Session, *, order_phone: str | None, plan_code: str, now: datetime
) -> RenewalTarget | None:
    """Is this paid order a renewal? Owner-session lookup — it spans tenants.

    Returns None for anything that is not unambiguously the same customer
    buying the same kind of pass again; the caller then provisions normally.
    """
    if plan_code not in RENEWABLE_PLANS:
        return None
    variants = phone_variants(order_phone)
    if not variants:
        return None
    # Deterministic and provider-scoped. Two spellings of one number can
    # legitimately coexist on different tenants (the unique index is on the
    # exact string), and an arbitrary pick would extend the WRONG customer's
    # service with this customer's money. Oldest channel wins — the first
    # binding of that number is the one that was proven by token.
    channels = session.execute(
        select(CustomerChannel).where(
            CustomerChannel.phone_e164.in_(variants),
            CustomerChannel.provider == "whatsapp",
        ).order_by(CustomerChannel.created_at, CustomerChannel.id)
    ).scalars().all()
    if not channels:
        return None
    channel = channels[0]

    prior = [
        sub for sub in tenant_subscriptions(session, channel.tenant_id)
        if sub.plan_code in RENEWABLE_PLANS
    ]
    if not prior:
        return None
    live = prior[0]
    if live.status not in _PRIOR_STATES and live.status not in _NEEDS_REVIEW:
        return None

    # Days stack: a customer renewing early keeps every day they paid for.
    period_end = live.current_period_end
    start = period_end if period_end is not None and period_end > now else now
    return RenewalTarget(
        tenant_id=channel.tenant_id,
        previous_id=live.id,
        previous_status=live.status,
        period_start=start,
        period_end=start + timedelta(days=SUBSCRIPTION_DAYS),
        needs_review=live.status in _NEEDS_REVIEW,
    )


def resync_policy_limit(
    session: Session, *, tenant_id: uuid.UUID, plan_code: str
) -> bool:
    """Re-snapshot the active search policy's daily limit for a new plan.

    Entitlements are snapshotted onto ``search_policies`` at onboarding and
    never re-read, so a customer who renewed on a BIGGER pass kept the old
    plan's daily limit — paying more for exactly the same service (and a
    downgrade kept the richer one). Returns True when something changed.
    """
    from career.db.models import PlanEntitlement, SearchPolicy

    limit = session.execute(
        select(PlanEntitlement.daily_job_limit).where(
            PlanEntitlement.plan_code == plan_code
        )
    ).scalars().first()
    if limit is None:
        return False
    policy = session.execute(
        select(SearchPolicy).where(
            SearchPolicy.tenant_id == tenant_id,
            SearchPolicy.status == "active",
        )
    ).scalars().first()
    if policy is None or policy.daily_job_limit == limit:
        return False
    policy.daily_job_limit = limit
    session.flush()
    return True


def close_previous(
    session: Session, target: RenewalTarget, *, salla_order_id: str
) -> None:
    """Retire the superseded subscription so exactly one row is live.

    Silent no-op when the previous row is already in a state that cannot or
    need not move (EXPIRED, refunded, disputed) — the trail stays honest
    either way, and a renewal must never fail because of bookkeeping.
    """
    if target.needs_review:
        return
    previous = session.get(Subscription, target.previous_id)
    if previous is None:
        return
    if not sub_states.can_transition(previous.status, sub_states.EXPIRED):
        return
    sub_states.transition(
        session, previous, sub_states.EXPIRED,
        event_type="renewed", salla_order_id=salla_order_id,
    )


#: What the renewing customer reads. No token, no steps — that is the point.
#: The announcer reads the persisted row (the honest source after commit) and
#: appends the date, so these stay pure text.
RENEWED_CUSTOMER_AR = (
    "تم تجديد اشتراكك ✅\n"
    "كل شي عندك مكانه — ملفك وسيرتك وإعداداتك ما تغيرت، وبنكمل عادي\n"
    "اشتراكك مستمر إلى"
)

#: §05 is explicit that a pause does NOT extend the period — the clock keeps
#: running. Promising «أيامك محفوظة» would be a promise the system does not
#: keep, so the paused renewal states the real end date and nudges them to
#: resume.
RENEWED_PAUSED_AR = (
    "تم تجديد اشتراكك ✅\n"
    "لكنه لا يزال موقوفًا مؤقتًا بطلبك، والمدة تمشي وأنت موقوف\n"
    "أرسل: استئناف — عشان تستفيد من أيامك، وهي تنتهي في"
)
