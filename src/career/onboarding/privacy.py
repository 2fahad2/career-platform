"""Standing privacy commands (whitepaper §12) — rights as conversation actions.

Every right runs through ``privacy_requests`` with a DECLARED deadline, so the
promise («تنفَّذ بمهلة معلنة») is a recorded fact, not a hope. Deletion is the
sharp edge: the personal tables are actually deleted — profile, facts, claims,
assessments, policies, uploads, channels and message logs — while the
regulatory exceptions survive by design (§12): financial records
(subscriptions + their events), the consent history, audit events, the
privacy-request trail itself, and the PII-free tenant skeleton (TEN-####).
Object-storage bytes cannot be deleted inside a DB transaction, so the report
returns the exact keys to purge — the caller deletes them via StorageAdapter
right after commit.

Pause suspends the service without extending the period (§05: وقف مؤقت لا
يمدد) — the period end stays where it was.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from career.db.models import (
    CareerPathAssessment,
    ConsentEvent,
    CustomerChannel,
    CustomerProfile,
    CvUpload,
    Delivery,
    DeliveryMessage,
    Document,
    ForbiddenClaim,
    FunnelSession,
    InboundMessage,
    OnboardingSession,
    PrivacyRequest,
    ProfileFact,
    SearchPolicy,
    Subscription,
)
from career.salla import subscriptions as sub_states
from career.salla.subscriptions import transition
from career.storage import StorageAdapter, tenant_key

#: Declared fulfillment deadlines per request kind (days) — the «مهلة معلنة».
DEADLINES_DAYS: dict[str, int] = {
    "export": 7,
    "delete": 30,
    "pause": 0,
    "status": 0,
    "stop_messages": 0,
    "withdraw_consent": 0,
}


class UnknownRequestKind(Exception):
    """A privacy-request kind outside the documented set."""


class RequestNotFound(Exception):
    """No open request of the expected kind with this id for this tenant."""


def open_request(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    now: datetime,
    source_inbound_message_id: uuid.UUID | None = None,
) -> PrivacyRequest:
    if kind not in DEADLINES_DAYS:
        raise UnknownRequestKind(kind)
    request = PrivacyRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        status="received",
        source_inbound_message_id=source_inbound_message_id,
        requested_at=now,
        deadline_at=now + timedelta(days=DEADLINES_DAYS[kind]),
    )
    session.add(request)
    session.flush()
    return request


def _get_open_request(
    session: Session, tenant_id: uuid.UUID, request_id: uuid.UUID, kind: str
) -> PrivacyRequest:
    request = session.execute(
        select(PrivacyRequest).where(
            PrivacyRequest.tenant_id == tenant_id,
            PrivacyRequest.id == request_id,
            PrivacyRequest.kind == kind,
            PrivacyRequest.status == "received",
        )
    ).scalar_one_or_none()
    if request is None:
        raise RequestNotFound(f"no open {kind} request {request_id}")
    return request


# ── export ───────────────────────────────────────────────────────────────────


def export_bundle(session: Session, *, tenant_id: uuid.UUID) -> dict[str, object]:
    """Everything personal we hold about this tenant, as one JSON document."""
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == tenant_id)
    ).scalars().first()
    facts = session.execute(
        select(ProfileFact).where(ProfileFact.tenant_id == tenant_id)
        .order_by(ProfileFact.created_at)
    ).scalars().all()
    consent_rows = session.execute(
        select(ConsentEvent).where(ConsentEvent.tenant_id == tenant_id)
        .order_by(ConsentEvent.seq)
    ).scalars().all()
    policies = session.execute(
        select(SearchPolicy).where(SearchPolicy.tenant_id == tenant_id)
        .order_by(SearchPolicy.version)
    ).scalars().all()
    assessments = session.execute(
        select(CareerPathAssessment).where(CareerPathAssessment.tenant_id == tenant_id)
        .order_by(CareerPathAssessment.created_at)
    ).scalars().all()
    # the live row, not whichever came back first: an upgraded customer's
    # data export used to name «cv_analysis» while they paid for a pass
    from career.salla.renewal import current_subscription

    subscription = current_subscription(session, tenant_id)
    funnels = session.execute(
        select(FunnelSession).where(FunnelSession.tenant_id == tenant_id)
        .order_by(FunnelSession.created_at)
    ).scalars().all()

    return {
        "profile": {
            "cv_full_name": profile.cv_full_name if profile else None,
            "city": profile.city if profile else None,
            "current_title": profile.current_title if profile else None,
            "years_experience": profile.years_experience if profile else None,
            "expected_salary_sar": (
                str(profile.expected_salary_sar)
                if profile and profile.expected_salary_sar is not None
                else None
            ),
            "remote_preference": profile.remote_preference if profile else None,
            "requested_path": profile.requested_path if profile else None,
            # audit fix: migration-0009 contact fields were omitted (§12
            # 'everything personal we hold')
            "email": profile.email if profile else None,
            "linkedin_url": profile.linkedin_url if profile else None,
            "region": profile.region if profile else None,
        },
        "funnel_analyses": [
            {"state": fn.state, "report": fn.report} for fn in funnels
        ],
        "facts": [
            {"category": f.category, "status": f.status, "payload": f.payload}
            for f in facts
        ],
        "consents": [
            {"purpose": c.purpose, "action": c.action, "occurred_at": str(c.occurred_at)}
            for c in consent_rows
        ],
        "search_policies": [
            {"version": p.version, "status": p.status,
             "approved_paths": p.approved_paths, "cities": p.cities}
            for p in policies
        ],
        "assessments": [
            {"requested_path": x.requested_path, "fit_score": x.fit_score,
             "status": x.status}
            for x in assessments
        ],
        "subscription": {
            "plan_code": subscription.plan_code if subscription else None,
            "status": subscription.status if subscription else None,
        },
    }


def fulfill_export(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    storage: StorageAdapter,
    now: datetime,
) -> str:
    """Write the bundle to storage and mark the request fulfilled."""
    request = _get_open_request(session, tenant_id, request_id, "export")
    bundle = export_bundle(session, tenant_id=tenant_id)
    key = tenant_key(str(tenant_id), "exports", f"{request_id}.json")
    storage.put(
        key,
        json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json",
    )
    request.status = "fulfilled"
    request.fulfilled_at = now
    request.details = {"storage_key": key}
    session.flush()
    return key


# ── deletion ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeletionReport:
    deleted: dict[str, int]
    retained: tuple[str, ...]
    storage_keys_to_purge: tuple[str, ...]


#: Deleted in FK-safe order. Everything here is personal by content (§12).
_PERSONAL_DELETION_ORDER: tuple[type, ...] = (
    ForbiddenClaim,
    ProfileFact,
    CustomerProfile,
    CareerPathAssessment,
    SearchPolicy,
    CvUpload,
    Document,
    FunnelSession,          # the analysis report JSON + funnel context (§12)
    OnboardingSession,
    DeliveryMessage,
    Delivery,
    InboundMessage,
    CustomerChannel,
)

#: Survive deletion by design (§12) — regulatory/security retention.
RETAINED_TABLES: tuple[str, ...] = (
    "subscriptions", "subscription_events", "consent_events", "audit_events",
    "privacy_requests", "tenants",
)


def execute_deletion(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    now: datetime,
    storage: StorageAdapter | None = None,
) -> DeletionReport:
    """Delete the personal tables for this tenant, honoring the regulatory
    exceptions, and report the object-storage keys the caller must purge."""
    request = _get_open_request(session, tenant_id, request_id, "delete")

    # Every object under the tenant prefix is deleted — uploads, tailored
    # CVs, AND funnel report PDFs (audit fix: the DB-tracked keys missed the
    # funnel reports, which are stored under a timestamped key with no row).
    keys: list[str] = []
    if storage is not None:
        keys = list(storage.list_keys(tenant_key(str(tenant_id))))
    else:  # fallback: the two DB-tracked kinds when no storage is injected
        keys = [
            k for k in session.execute(
                select(Document.storage_key).where(
                    Document.tenant_id == tenant_id)
            ).scalars()
        ]
        keys += [
            k for k in session.execute(
                select(CvUpload.extracted_text_storage_key).where(
                    CvUpload.tenant_id == tenant_id,
                    CvUpload.extracted_text_storage_key.is_not(None),
                )
            ).scalars() if k
        ]

    deleted: dict[str, int] = {}
    for model in _PERSONAL_DELETION_ORDER:
        result = session.execute(
            delete(model).where(model.tenant_id == tenant_id)  # type: ignore[attr-defined]
        )
        deleted[model.__tablename__] = result.rowcount or 0  # type: ignore[attr-defined]

    request.status = "fulfilled"
    request.fulfilled_at = now
    request.details = {"deleted": deleted, "storage_keys": keys}
    session.flush()
    return DeletionReport(
        deleted=deleted,
        retained=RETAINED_TABLES,
        storage_keys_to_purge=tuple(keys),
    )


# ── pause / resume (لا يمدد) ─────────────────────────────────────────────────


def _subscription(session: Session, tenant_id: uuid.UUID) -> Subscription:
    """THE live subscription. Since renewals (§16) a tenant holds one row per
    order, so «whichever row came back first» would have shown a renewing
    customer their old expired period — and pause/resume would have acted on
    it. The order is explicit in career.salla.renewal."""
    from career.salla.renewal import current_subscription

    subscription = current_subscription(session, tenant_id)
    if subscription is None:
        raise RequestNotFound("subscription")
    return subscription


def pause_subscription(session: Session, *, tenant_id: uuid.UUID) -> Subscription:
    """Suspend delivery WITHOUT touching the period end (§05: لا يمدد)."""
    subscription = _subscription(session, tenant_id)
    return transition(
        session, subscription, sub_states.PAUSED, event_type="customer_pause"
    )


def resume_subscription(session: Session, *, tenant_id: uuid.UUID) -> Subscription:
    subscription = _subscription(session, tenant_id)
    # audit fix: resume is only meaningful FROM paused. During onboarding the
    # unconditional transition jumped ONBOARDING→ACTIVE, corrupting the
    # subscription and permanently blocking activation.
    if subscription.status != sub_states.PAUSED:
        return subscription
    return transition(
        session, subscription, sub_states.ACTIVE, event_type="customer_resume"
    )


# ── subscription status: an honest Arabic one-liner ──────────────────────────

_STATUS_AR = {
    "ACTIVE": "نشط",
    "PAUSED": "موقوف مؤقتًا",
    "ONBOARDING": "قيد الإعداد",
    "GRACE": "في مهلة السماح",
    "EXPIRED": "منتهي",
    "CANCELED": "ملغى",
    "PAID_UNCLAIMED": "مدفوع بانتظار التفعيل",
    "REFUNDED": "مسترد",
    "SUSPENDED": "معلّق",
    "CHARGEBACK": "متنازع عليه",
}


#: A status reply that says «منتهي» and stops is a dead end — the customer has
#: no way to come back from inside the conversation. §16: the store link is
#: printed on its own line (Fahad's client scrambles mixed-direction lines).
_RENEW_CTA_STATES: frozenset[str] = frozenset({"GRACE", "EXPIRED", "CANCELED"})
_RENEW_CTA_DAYS = 3


def _renew_cta(store_url: str | None) -> str:
    url = (store_url or "").strip()
    if not url:
        return "\nللتجديد أرسل: دعم"
    return "\nتقدر تجدد من هنا:\n" + url


def subscription_status_summary(
    session: Session, *, tenant_id: uuid.UUID, now: datetime,
    store_url: str | None = None,
) -> str:
    subscription = _subscription(session, tenant_id)
    label = _STATUS_AR.get(subscription.status, subscription.status)
    if subscription.current_period_end is not None:
        days_left = max(0, (subscription.current_period_end - now).days)
        line = f"اشتراكك: {label} — باقي {days_left} يومًا على نهاية الفترة الحالية."
        if (subscription.status in _RENEW_CTA_STATES
                or days_left <= _RENEW_CTA_DAYS):
            line += _renew_cta(store_url)
        return line
    line = f"اشتراكك: {label}."
    if subscription.status in _RENEW_CTA_STATES:
        line += _renew_cta(store_url)
    return line
