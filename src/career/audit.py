"""Central audit writer.

One place records audit events, so the PII/secret discipline is enforced in a
single spot: details are passed through the secret-redaction sanitizer before
storage (defense in depth — details are already PII-free by contract). Writes
happen inside the caller's tenant-bound transaction; RLS confines each row to
its tenant, and the app role has INSERT/SELECT only (append-only).

WHAT MAY BE WRITTEN HERE, AND WHY THE VOCABULARY IS CLOSED
----------------------------------------------------------
``audit_events`` is one of the six tables `onboarding.privacy.RETAINED_TABLES`
keeps through a customer's «حذف بياناتي» — we tell a customer we delete their
data and then keep this. That is only defensible while every row in it is a
security, money or privacy event that a regulator or the customer themselves
could ask us to produce. A general-purpose event log would make the retention
exception a lie, and would make the table useless besides: an audit log of
everything is an audit log of nothing, because nobody reads it.

So the action vocabulary is a closed registry with a declared category rather
than a free string. Adding an action is a deliberate act that forces the
question «is this security, money, or privacy?» — and if the answer is «it is
operational telemetry», the answer is the journal, not this table.

The registry is derived from what the code actually emits, never from what it
might one day emit: an action registered with no caller is the same vacuum in
a different shape. Six events are live.

SECURITY:

* ``activation_link_issued`` — salla.provisioning.issue_activation_link. The
  link IS the credential for a paid subscription; issuing one on demand is an
  operator action against a customer's account.
* ``cv_pair_quarantined`` — cv.publish. A customer-facing artifact failed the
  §7.1 acceptance authority and was removed from the live path (Constant 6).

PRIVACY — ``onboarding/privacy.py``. These two closed the sharpest gap this
table had: the retention exemption above says `audit_events` survives a
customer's «حذف بياناتي» BY DESIGN, and the deletion itself was the one event
nobody wrote. An exemption whose justification is a trail that does not exist
is not an exemption, it is data we simply kept.

* ``privacy_deletion_executed`` — which tables lost how many rows, and how many
  storage objects were purged. Counts, never contents.
* ``privacy_export_fulfilled`` — a complete copy of everything personal we hold
  was materialised into object storage. That is a disclosure, and a disclosure
  with no record is indistinguishable from a leak.

MONEY — ``salla/provisioning.py`` (the renewal half runs through the same
function, ``salla/renewal.py`` only decides WHOSE it is):

* ``subscription_provisioned`` — Constant 4 says a subscription is created only
  on ``payment = paid`` after a re-verification against the Salla API, and
  nothing anywhere recorded that the re-check had actually happened for a given
  order. It does now, with the order id, the plan and the amount that was
  matched.
* ``subscription_refunded`` — the money went back and service stopped. Written
  for every reversal (refund, cancellation, chargeback), with the event name in
  the details, because from the customer's side they are one fact: they are no
  longer paying and we are no longer serving them.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import AuditEvent
from career.logging_filters import sanitize_secret_value


class AuditCategory(StrEnum):
    """The only three reasons a row may exist in ``audit_events``."""

    SECURITY = "security"
    MONEY = "money"
    PRIVACY = "privacy"


#: A live credential for a paid subscription was minted for the operator.
ACTION_ACTIVATION_LINK_ISSUED = "activation_link_issued"
#: A published CV pair failed binding validation and left the live prefix.
ACTION_CV_PAIR_QUARANTINED = "cv_pair_quarantined"
#: §12 «حذف بياناتي» was executed — the personal tables for one tenant are gone.
ACTION_PRIVACY_DELETION_EXECUTED = "privacy_deletion_executed"
#: §12 «بياناتي» was fulfilled — a full personal copy left for object storage.
ACTION_PRIVACY_EXPORT_FULFILLED = "privacy_export_fulfilled"
#: A paid Salla order was re-verified against the API and became a subscription.
ACTION_SUBSCRIPTION_PROVISIONED = "subscription_provisioned"
#: A reversal (refund / cancellation / chargeback) stopped a paid subscription.
ACTION_SUBSCRIPTION_REFUNDED = "subscription_refunded"

#: action → category. A write with an action outside this map is refused.
AUDIT_ACTIONS: dict[str, AuditCategory] = {
    ACTION_ACTIVATION_LINK_ISSUED: AuditCategory.SECURITY,
    ACTION_CV_PAIR_QUARANTINED: AuditCategory.SECURITY,
    ACTION_PRIVACY_DELETION_EXECUTED: AuditCategory.PRIVACY,
    ACTION_PRIVACY_EXPORT_FULFILLED: AuditCategory.PRIVACY,
    ACTION_SUBSCRIPTION_PROVISIONED: AuditCategory.MONEY,
    ACTION_SUBSCRIPTION_REFUNDED: AuditCategory.MONEY,
}


class UnregisteredAuditAction(ValueError):
    """The action is not in :data:`AUDIT_ACTIONS`.

    A programming error, not a data error — the action is always a literal at
    the call site — so it raises where it is written rather than being written
    and discovered later by whoever tries to read the table.
    """


def record_audit(
    session: Session,
    *,
    tenant_id: str | uuid.UUID,
    actor: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    correlation_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event in the CURRENT transaction. ``tenant_id`` must match
    the session's tenant context or RLS (WITH CHECK) rejects the write.

    ``action`` must be registered in :data:`AUDIT_ACTIONS` — see the module
    docstring for why the vocabulary is closed.
    """
    if action not in AUDIT_ACTIONS:
        raise UnregisteredAuditAction(
            f"{action!r} is not a registered audit action. audit_events "
            "survives a customer's deletion request by design, so it may hold "
            "only security, money and privacy events — register the action in "
            "career.audit.AUDIT_ACTIONS with its category, or send this to the "
            f"journal instead. Registered: {sorted(AUDIT_ACTIONS)}"
        )
    safe_details = sanitize_secret_value(details or {})
    event = AuditEvent(
        tenant_id=uuid.UUID(str(tenant_id)),
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=uuid.UUID(str(resource_id)) if resource_id is not None else None,
        correlation_id=correlation_id,
        details=safe_details,
    )
    session.add(event)
    session.flush()
    return event


def recent_audit(session: Session, *, limit: int = 100) -> list[AuditEvent]:
    """Return the current tenant's most recent audit events (RLS-confined)."""
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())
