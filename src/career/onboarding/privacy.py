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

One table is neither deleted nor kept whole. `webhook_events` holds the
provider's body verbatim — the buyer's name, mobile and email from Salla, the
customer's phone, profile name and typed text from Meta — and had no tenant
column at all until 0024, so it was in no list here: not deleted, not
exported, not pruned, and carried in the nightly backup for months. It is now
REDACTED on request rather than deleted, because the row is the idempotency
record and a forgotten fingerprint would let a replayed webhook provision the
same customer twice (career.webhooks.intake owns that argument, and the
thirty-day expiry that catches the bodies this path cannot attribute).

Pause suspends the service without extending the period (§05: وقف مؤقت لا
يمدد) — the period end stays where it was.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

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
    WebhookEvent,
)
from career.salla import subscriptions as sub_states
from career.salla.subscriptions import transition
from career.storage import StorageAdapter, tenant_key
from career.webhooks import intake

logger = logging.getLogger("career.onboarding")

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

    def _all(model: Any, order: Any) -> list[Any]:
        return list(session.execute(
            select(model).where(model.tenant_id == tenant_id).order_by(order)
        ).scalars().all())

    channels = _all(CustomerChannel, CustomerChannel.created_at)
    uploads = _all(CvUpload, CvUpload.created_at)
    documents = _all(Document, Document.created_at)
    forbidden = _all(ForbiddenClaim, ForbiddenClaim.created_at)
    deliveries = _all(Delivery, Delivery.run_date)
    outbound = _all(DeliveryMessage, DeliveryMessage.created_at)
    inbound = _all(InboundMessage, InboundMessage.received_at)
    funnels = session.execute(
        select(FunnelSession).where(FunnelSession.tenant_id == tenant_id)
        .order_by(FunnelSession.created_at)
    ).scalars().all()
    # 0024: the raw provider bodies that are about this customer. They were
    # outside every part of §12 until the subject link existed — no tenant
    # column meant no export, no deletion and no expiry.
    webhooks = session.execute(
        select(WebhookEvent).where(WebhookEvent.subject_tenant_id == tenant_id)
        .order_by(WebhookEvent.received_at)
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
             "approved_paths": p.approved_paths, "cities": p.cities,
             # the privacy page names these to the customer by name —
             # «راتبك المستهدف» and «الشركات اللي ما تبي سيرتك توصلها»
             "min_salary_sar": (
                 str(p.min_salary_sar) if p.min_salary_sar is not None else None
             ),
             "unknown_salary_policy": p.unknown_salary_policy,
             "remote_policy": p.remote_policy,
             "sectors_preferred": p.sectors_preferred,
             "sectors_avoided": p.sectors_avoided,
             "banned_companies": p.banned_companies,
             "daily_job_limit": p.daily_job_limit}
            for p in policies
        ],
        # §12 grants a copy of everything personal we hold, and the privacy
        # page names «سجل تفاعلك مع الفرص المرسلة» explicitly. The yardstick
        # is the deletion list: anything personal enough to DELETE is personal
        # enough to HAND OVER, and seven of those tables were missing — the
        # customer got no record of which jobs we sent, which they marked
        # applied, which CVs were generated in their name, or what they
        # uploaded, while the request was still marked fulfilled.
        "channels": [
            {"provider": c.provider, "phone_e164": c.phone_e164,
             "display_name": c.display_name,
             "opted_out_at": str(c.opt_out_at) if c.opt_out_at else None,
             "last_inbound_at": (
                 str(c.last_inbound_at) if c.last_inbound_at else None
             ),
             "created_at": str(c.created_at)}
            for c in channels
        ],
        "cv_uploads": [
            {"filename": u.original_filename, "status": u.status,
             "mime_detected": u.mime_detected, "size_bytes": u.size_bytes,
             "page_count": u.page_count, "scan_status": u.scan_status,
             "created_at": str(u.created_at)}
            for u in uploads
        ],
        "generated_cvs": [
            {"storage_key": d.storage_key, "content_type": d.content_type,
             "size_bytes": d.size_bytes, "status": d.status,
             "created_at": str(d.created_at)}
            for d in documents
        ],
        "forbidden_claims": [
            {"claim": fc.claim, "source": fc.source,
             "created_at": str(fc.created_at)}
            for fc in forbidden
        ],
        "delivery_days": [
            {"run_date": str(dv.run_date), "status": dv.status,
             "window_at_start": dv.window_state_at_start,
             "opened_at": str(dv.opened_at) if dv.opened_at else None,
             "completed_at": (
                 str(dv.completed_at) if dv.completed_at else None
             ),
             "results": (dv.bundle or {}).get("results")}
            for dv in deliveries
        ],
        "messages_we_sent": [
            {"kind": dm.kind, "template": dm.template_name,
             "status": dm.status, "created_at": str(dm.created_at)}
            for dm in outbound
        ],
        "messages_you_sent": [
            {"classification": im.classification,
             "message_type": im.message_type, "text": im.text_body,
             "received_at": str(im.received_at)}
            for im in inbound
        ],
        "assessments": [
            {"requested_path": x.requested_path, "fit_score": x.fit_score,
             "status": x.status}
            for x in assessments
        ],
        # The FACT of each provider event, never the body. §12 grants a copy
        # of everything personal we hold about this customer — and a single
        # Meta POST can carry two customers' messages, so handing one of them
        # the raw body would answer their right by breaking the other's. What
        # they typed is already exported in full under `messages_you_sent`,
        # which is the parsed, per-customer copy of the same words; this
        # section tells them the wire record exists and when it expires.
        "provider_events": [
            {"provider": w.provider, "event_type": w.event_type,
             "received_at": str(w.received_at),
             "processing_status": w.processing_status,
             "raw_body_still_held": w.payload_redacted_at is None,
             "raw_body_erased_at": (
                 str(w.payload_redacted_at) if w.payload_redacted_at else None
             )}
            for w in webhooks
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
    #: Tables where the ROW survives and its personal content does not. Only
    #: webhook_events so far: the row is the idempotency record and losing a
    #: fingerprint would let a replayed Salla webhook provision the same
    #: customer twice, so the body goes and the fingerprint stays. Defaulted
    #: because it arrived after the callers did (0024).
    redacted: dict[str, int] = field(default_factory=dict)


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

#: Survive as a row, not as content. `webhook_events` is the only member and
#: the reason is the same one that keeps subscriptions: the row carries an
#: obligation that outlives the data in it. Here the obligation is idempotency
#: — `event_fingerprint` is what makes a replayed Salla webhook a no-op — so
#: the body is replaced by a PII-free skeleton and the fingerprint stays.
REDACTED_TABLES: tuple[str, ...] = ("webhook_events",)


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

    # The raw provider bodies. Redacted rather than deleted — the row is the
    # idempotency record (webhooks.intake carries the argument) — and reaching
    # only the events that resolved to exactly one tenant. A body that named
    # two customers at once, or none we could recognise, is not erased here;
    # it expires on RAW_PAYLOAD_RETENTION_DAYS instead. That gap is reported
    # rather than papered over: attributing an ambiguous batch to whoever
    # asked first would erase the other customer's record on this one's
    # request, which is a worse answer than «thirty days».
    redacted = {
        "webhook_events": intake.redact_for_tenant(
            session, tenant_id=tenant_id, now=now
        )
    }

    request.status = "fulfilled"
    request.fulfilled_at = now
    request.details = {
        "deleted": deleted, "redacted": redacted, "storage_keys": keys,
    }
    session.flush()
    return DeletionReport(
        deleted=deleted,
        retained=RETAINED_TABLES,
        storage_keys_to_purge=tuple(keys),
        redacted=redacted,
    )


# ── pause / resume (لا يمدد) ─────────────────────────────────────────────────


def _live_subscription(
    session: Session, tenant_id: uuid.UUID
) -> Subscription | None:
    """THE live subscription, or None when this tenant holds no row at all.

    Since renewals (§16) a tenant holds one row per order, so «whichever row
    came back first» would have shown a renewing customer their old expired
    period — and pause/resume would have acted on it. The order is explicit in
    career.salla.renewal.

    Absence is a FACT, not an error. This used to raise RequestNotFound — a
    privacy-REQUEST exception borrowed to mean «no subscription» — and every
    caller inherited the raise, including «حالة اشتراكي», the single most
    advertised command in the product: a tenant with no live row (the shells a
    §04 upgrade leaves behind, an order still being provisioned) asked the one
    question we tell them to ask and their whole turn died with no reply.
    """
    from career.salla.renewal import current_subscription

    return current_subscription(session, tenant_id)


#: Pausing is meaningful only from a state that is actually running — and that
#: set is the state machine's to define, not ours. Restating it by hand is
#: what put GRACE in here while `_ALLOWED` had no GRACE→PAUSED edge, turning
#: the documented «وقف مؤقت» into an InvalidTransition in the customer's face
#: (career.salla.subscriptions.PAUSABLE_STATES carries the full incident).
_PAUSABLE: frozenset[str] = sub_states.PAUSABLE_STATES


class ActionOutcome(StrEnum):
    """What a pause/resume actually DID — never inferred, always returned."""

    CHANGED = "changed"
    ALREADY = "already"
    NOT_ELIGIBLE = "not_eligible"
    NO_SUBSCRIPTION = "no_subscription"


@dataclass(frozen=True)
class SubscriptionActionResult:
    """The answer to «what happened?», in a shape a caller cannot ignore.

    Both actions used to return the subscription row UNCHANGED when they
    declined, so a refusal and a success were the same value and every caller
    had to re-read the status to tell them apart. One caller did not, and the
    watchtower printed «✅ استأنفنا الخدمة للعميل» over an account that was
    never resumed. The row is still here for the callers that want it, and
    `status` is the status as it stands after the call, but the fact of the
    matter now travels with it and reads the same to everyone.
    """

    outcome: ActionOutcome
    status: str | None
    subscription: Subscription | None

    @property
    def changed(self) -> bool:
        return self.outcome is ActionOutcome.CHANGED


def pause_subscription(
    session: Session, *, tenant_id: uuid.UUID
) -> SubscriptionActionResult:
    """Suspend delivery WITHOUT touching the period end (§05: لا يمدد).

    Idempotent, and never an exception. The unguarded transition raised
    InvalidTransition straight into the WhatsApp worker for anyone already
    paused, expired or cancelled — a customer typing «وقف مؤقت» a second time,
    or after their period ended, crashed their own turn and was answered with
    nothing at all. Sending a command twice is not an error, and a customer
    must never be able to break the conversation by repeating themselves.
    """
    subscription = _live_subscription(session, tenant_id)
    if subscription is None:
        return SubscriptionActionResult(
            ActionOutcome.NO_SUBSCRIPTION, None, None
        )
    status = str(subscription.status)
    if status == sub_states.PAUSED:
        return SubscriptionActionResult(
            ActionOutcome.ALREADY, status, subscription
        )
    if status not in _PAUSABLE:
        return SubscriptionActionResult(
            ActionOutcome.NOT_ELIGIBLE, status, subscription
        )
    transition(
        session, subscription, sub_states.PAUSED, event_type="customer_pause"
    )
    return SubscriptionActionResult(
        ActionOutcome.CHANGED, str(subscription.status), subscription
    )


def resume_subscription(
    session: Session, *, tenant_id: uuid.UUID
) -> SubscriptionActionResult:
    """Put a paused customer back in service — and say whether it happened."""
    subscription = _live_subscription(session, tenant_id)
    if subscription is None:
        return SubscriptionActionResult(
            ActionOutcome.NO_SUBSCRIPTION, None, None
        )
    status = str(subscription.status)
    if status == sub_states.ACTIVE:
        return SubscriptionActionResult(
            ActionOutcome.ALREADY, status, subscription
        )
    # audit fix: resume is only meaningful FROM paused. During onboarding the
    # unconditional transition jumped ONBOARDING→ACTIVE, corrupting the
    # subscription and permanently blocking activation.
    if status != sub_states.PAUSED or not sub_states.can_transition(
        status, sub_states.ACTIVE
    ):
        return SubscriptionActionResult(
            ActionOutcome.NOT_ELIGIBLE, status, subscription
        )
    transition(
        session, subscription, sub_states.ACTIVE, event_type="customer_resume"
    )
    return SubscriptionActionResult(
        ActionOutcome.CHANGED, str(subscription.status), subscription
    )


# ── subscription status: an honest Arabic one-liner ──────────────────────────

_STATUS_AR = {
    # PENDING_PAYMENT was the hole: an unmapped status fell through to the raw
    # Latin token, so «حالة اشتراكي» answered a real customer with an Arabic
    # sentence carrying a Latin word in the middle of it — a line Fahad's
    # client scrambles on delivery (§16) and that means nothing to a reader
    # either way. Every one of the eleven states of §05 is named here.
    "PENDING_PAYMENT": "بانتظار الدفع",
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


#: No live subscription row to speak for. The customer asked the question we
#: print in every message; «I have nothing to tell you» is a poor answer but it
#: is an answer, and the alternative here was a RequestNotFound that killed
#: their whole turn — no reply, inbound rolled back, event marked failed.
_NO_SUBSCRIPTION_AR = (
    "ما لقيت اشتراكًا مسجلًا على رقمك من هنا\n"
    "إذا كنت دفعت للتو فعطني دقائق وأعد إرسال: حالة اشتراكي\n"
    "وإذا ما تغير شي أرسل: دعم — وأحد من الفريق يتابعها معك"
)

#: A status we have no Arabic name for. The operator hears about it in the
#: journal; the customer hears a sentence in their own language plus the one
#: step that always works.
_UNKNOWN_STATUS_AR = (
    "اشتراكك: حالته غير واضحة عندي الحين\n"
    "أرسل: دعم — وأحد من الفريق يتابعها معك"
)


def subscription_status_summary(
    session: Session, *, tenant_id: uuid.UUID, now: datetime,
    store_url: str | None = None,
) -> str:
    subscription = _live_subscription(session, tenant_id)
    if subscription is None:
        return _NO_SUBSCRIPTION_AR
    label = _STATUS_AR.get(subscription.status)
    if label is None:
        # never leak the raw state name into an Arabic line (§16) — and never
        # go quiet about it either: an unnamed status is our bug, not theirs.
        logger.warning(
            "subscription status has no Arabic label: %s", subscription.status
        )
        return _UNKNOWN_STATUS_AR
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
