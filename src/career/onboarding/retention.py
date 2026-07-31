"""The 90-day retention promise, with an owner at last (§12).

Three published places tell the customer the same thing: their data is kept in
full while they are subscribed, for 90 days after it ends, and then
professionally deleted — the consent text they tick before paying, §12 of the
whitepaper, and the privacy page on the store. Nothing ran it. It was a
commitment with no scheduled job behind it, and the first one falls due about
four months after the first sale, which is exactly the kind of deadline a
launch pushes out of sight.

This is that job. It is deliberately conservative:

* **Only a customer who is really gone.** The clock starts at the END of their
  last period, and a tenant with any live or recoverable subscription is
  skipped entirely — a lapsed customer who renews on day 89 keeps everything.
* **The same deletion the customer's own «حذف بياناتي» performs**, through the
  same authority, so the regulatory exceptions (financial records, the consent
  history, the audit trail, the PII-free tenant skeleton) survive by exactly
  the same rule rather than a second interpretation of it.
* **A recorded request either way.** Every run opens a privacy_request and
  fulfils it, so the deletion has the same auditable trail as a customer-
  initiated one, and «what happened to my data» has an answer with a date.
* **Once.** A tenant already emptied is not re-deleted; the sweep is safe to
  run every night forever.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import CustomerProfile, PrivacyRequest, Subscription
from career.onboarding import privacy
from career.salla import subscriptions as sub_states
from career.storage import StorageAdapter

logger = logging.getLogger("career.retention")

#: §12 verbatim: «كامل أثناء الاشتراك، 90 يومًا بعده، ثم حذف مهني».
RETENTION_DAYS_AFTER_END = 90

#: A tenant holding any of these is still our customer — never swept.
_LIVE_STATES: frozenset[str] = frozenset({
    sub_states.PENDING_PAYMENT, sub_states.PAID_UNCLAIMED,
    sub_states.ONBOARDING, sub_states.ACTIVE, sub_states.PAUSED,
    sub_states.GRACE, sub_states.SUSPENDED,
})

#: The marker written on the request so a swept tenant is never swept twice.
SWEEP_MARKER = "retention_sweep_90d"


def due_tenants(session: Session, *, now: datetime) -> list[uuid.UUID]:
    """Tenants whose retention window closed. Owner session — spans tenants."""
    cutoff = now - timedelta(days=RETENTION_DAYS_AFTER_END)

    live = select(Subscription.tenant_id).where(
        Subscription.status.in_(sorted(_LIVE_STATES))
    )
    already = select(PrivacyRequest.tenant_id).where(
        PrivacyRequest.kind == "delete",
        PrivacyRequest.status == "fulfilled",
        PrivacyRequest.details["source"].astext == SWEEP_MARKER,
    )
    # The clock runs from the LAST period end the tenant ever had. A tenant
    # with no period at all (paid, never activated, then expired) has nothing
    # personal worth sweeping and no honest date to sweep from, so max() being
    # NULL excludes it.
    rows = session.execute(
        select(Subscription.tenant_id)
        .where(
            Subscription.tenant_id.not_in(live),
            Subscription.tenant_id.not_in(already),
        )
        .group_by(Subscription.tenant_id)
        .having(func.max(Subscription.current_period_end) < cutoff)
    ).scalars().all()
    return list(rows)


def sweep_retention(
    session: Session,
    *,
    now: datetime,
    storage: StorageAdapter | None = None,
    admin_client: object | None = None,
) -> dict[str, int]:
    """One idempotent pass. Returns honest counters for the admin summary."""
    counts = {"swept": 0, "skipped_empty": 0, "storage_keys": 0}

    for tenant_id in due_tenants(session, now=now):
        # Nothing personal left (an earlier customer-initiated deletion, or a
        # tenant that never got past payment) — record nothing, delete nothing.
        has_profile = session.execute(
            select(CustomerProfile.tenant_id).where(
                CustomerProfile.tenant_id == tenant_id
            )
        ).first()
        if has_profile is None:
            counts["skipped_empty"] += 1
            continue

        request = privacy.open_request(
            session, tenant_id=tenant_id, kind="delete", now=now,
        )
        report = privacy.execute_deletion(
            session, tenant_id=tenant_id, request_id=request.id,
            now=now, storage=storage,
        )
        # mark the request so the tenant is never swept a second time
        request.details = {**(request.details or {}), "source": SWEEP_MARKER}
        counts["swept"] += 1
        counts["storage_keys"] += len(report.storage_keys_to_purge)

        if storage is not None:
            for key in report.storage_keys_to_purge:
                try:
                    storage.delete(key)
                except Exception:  # noqa: BLE001 — a stuck object must not
                    # abort the sweep; the DB deletion already committed the
                    # promise and the key stays listed in the request details.
                    logger.warning("retention: storage key purge failed",
                                   exc_info=True)
        session.flush()
        # TEN codes only in the admin channel (§15.13) — and not even that per
        # tenant: a per-customer deletion notice is noise, the total is signal.

    if admin_client is not None and counts["swept"]:
        try:
            admin_client.send_admin(  # type: ignore[attr-defined]
                "🧹 كنس الاحتفاظ: حُذفت بيانات "
                f"{counts['swept']} عميل انتهى اشتراكهم قبل أكثر من "
                f"{RETENTION_DAYS_AFTER_END} يومًا "
                "(السجلات المالية وسجل الموافقات محفوظة نظاميًا)"
            )
        except Exception:  # noqa: BLE001 — notifying never blocks the promise
            logger.warning("retention sweep notice failed", exc_info=True)
    return counts
