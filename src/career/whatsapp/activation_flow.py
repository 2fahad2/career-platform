"""Activation: link a paid order to the WhatsApp number that claims it (§08).

The customer sends "تفعيل <token>". We verify the token (hash lookup, not
expired, not used), then create/link the customer_channel (phone ↔ tenant),
mark the token used, and move the subscription PAID_UNCLAIMED → ONBOARDING.
Runs as the owner role (looks up across tenants, creates channels). Failures
send a customer-facing Arabic reply and a PII-free admin alert.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import ActivationToken, CustomerChannel, Subscription, Tenant
from career.salla import subscriptions as sub_states
from career.telegram import messages as admin_msg
from career.telegram.admin import TelegramAdminClient
from career.tokens import hash_token
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.delivery import record_out

_WELCOME = "تم التفعيل ✅ أهلًا بك! لنبدأ إعداد خدمتك."
_INVALID = "لم نتعرّف على رمز التفعيل. تأكّد من الرابط في صفحة الشكر بعد الدفع."
_EXPIRED = "انتهت صلاحية رمز التفعيل. أرسل: دعم — وسنعيد إرساله."
_USED = "رمز التفعيل مستخدم مسبقًا. إن واجهت مشكلة أرسل: دعم"
_CONFLICT = "هذا الرقم مرتبط بحساب آخر. أرسل: دعم"


class ActivationStatus(StrEnum):
    ACTIVATED = "activated"
    ALREADY_LINKED = "already_linked"
    INVALID_TOKEN = "invalid_token"  # noqa: S105 — status enum, not a secret
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ActivationResult:
    status: ActivationStatus
    tenant_id: str | None = None
    channel_id: str | None = None
    subscription_id: str | None = None


def _channel_for_phone(session: Session, phone: str) -> CustomerChannel | None:
    return session.execute(
        select(CustomerChannel).where(
            CustomerChannel.provider == "whatsapp",
            CustomerChannel.phone_e164 == phone,
        )
    ).scalar_one_or_none()


def _is_funnel_only_tenant(owner_session: Session, tenant_id: uuid.UUID) -> bool:
    """True when the tenant's every subscription is the cv_analysis product —
    the §04 upgrade-inheritance precondition."""
    plans = owner_session.execute(
        select(Subscription.plan_code).where(Subscription.tenant_id == tenant_id)
    ).scalars().all()
    return bool(plans) and all(p == "cv_analysis" for p in plans)


def activate(
    owner_session: Session,
    *,
    token: str,
    from_phone: str,
    display_name: str | None,
    now: datetime,
    whatsapp_client: WhatsAppClient,
    admin_client: TelegramAdminClient,
) -> ActivationResult:
    tok = owner_session.execute(
        select(ActivationToken).where(ActivationToken.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if tok is None:
        whatsapp_client.send_text(from_phone, _INVALID)
        admin_client.send_admin(admin_msg.activation_failed("(unknown)", "invalid_token"))
        return ActivationResult(ActivationStatus.INVALID_TOKEN)

    ten_code = owner_session.get(Tenant, tok.tenant_id).code  # type: ignore[union-attr]
    existing = _channel_for_phone(owner_session, from_phone)

    # Phone already belongs to a different customer. ONE documented exception
    # (§04 inheritance): a funnel-only customer upgrading — their extracted
    # facts live on the EXISTING tenant, so the new purchase re-links to it.
    # The freshly provisioned shell tenant stays empty and harmless: nothing
    # is deleted, ever.
    if existing is not None and existing.tenant_id != tok.tenant_id:
        if _is_funnel_only_tenant(owner_session, existing.tenant_id):
            shell_code = ten_code
            moved = owner_session.get(Subscription, tok.subscription_id)
            if moved is not None:
                moved.tenant_id = existing.tenant_id
            tok.tenant_id = existing.tenant_id
            ten_code = owner_session.get(  # type: ignore[union-attr]
                Tenant, existing.tenant_id
            ).code
            admin_client.send_admin(
                f"↻ ترقية من قمع التحليل: {shell_code} → {ten_code}"
            )
        else:
            whatsapp_client.send_text(from_phone, _CONFLICT)
            admin_client.send_admin(
                admin_msg.activation_failed(ten_code, "phone_conflict")
            )
            return ActivationResult(ActivationStatus.CONFLICT)

    if tok.expires_at < now:
        whatsapp_client.send_text(from_phone, _EXPIRED)
        admin_client.send_admin(admin_msg.activation_failed(ten_code, "expired"))
        return ActivationResult(ActivationStatus.EXPIRED)

    if tok.used_at is not None:
        # Already used: if this same phone/tenant is already linked, it's an
        # idempotent re-send — just refresh the window.
        if existing is not None and existing.tenant_id == tok.tenant_id:
            existing.last_inbound_at = now
            owner_session.commit()
            return ActivationResult(
                ActivationStatus.ALREADY_LINKED, tenant_id=str(tok.tenant_id),
                channel_id=str(existing.id), subscription_id=str(tok.subscription_id),
            )
        whatsapp_client.send_text(from_phone, _USED)
        admin_client.send_admin(admin_msg.activation_failed(ten_code, "token_reused"))
        return ActivationResult(ActivationStatus.ALREADY_USED)

    # Happy path — create or refresh the channel, consume the token, advance sub.
    if existing is None:
        channel = CustomerChannel(
            id=uuid.uuid4(), tenant_id=tok.tenant_id, subscription_id=tok.subscription_id,
            provider="whatsapp", phone_e164=from_phone, display_name=display_name,
            verified_at=now, opt_in_at=now, last_inbound_at=now,
        )
        owner_session.add(channel)
        owner_session.flush()
    else:
        channel = existing
        channel.verified_at = now
        channel.opt_in_at = channel.opt_in_at or now
        channel.last_inbound_at = now
        channel.subscription_id = tok.subscription_id

    tok.used_at = now

    sub = owner_session.get(Subscription, tok.subscription_id)
    if sub is not None and sub.status == sub_states.PAID_UNCLAIMED:
        sub_states.transition(
            owner_session, sub, sub_states.ONBOARDING,
            event_type="activated", salla_order_id=sub.salla_order_id,
        )

    mid = whatsapp_client.send_text(from_phone, _WELCOME)
    record_out(owner_session, tenant_id=channel.tenant_id, channel_id=channel.id,
               kind="text", wa_message_id=mid, now=now)
    owner_session.commit()
    return ActivationResult(
        ActivationStatus.ACTIVATED, tenant_id=str(tok.tenant_id),
        channel_id=str(channel.id), subscription_id=str(tok.subscription_id),
    )
