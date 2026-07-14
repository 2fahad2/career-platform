"""Adaptive delivery execution (whatsapp §08).

Given a bundle (generic parts list for now; C7 fills real CV/job refs), either
send it directly (open window) or send the morning template and hold the bundle
until the customer opens the window, then descend. Every outbound message is
logged in delivery_messages for the delivery receipts.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, Delivery, DeliveryMessage
from career.whatsapp.adaptive import DeliveryAction, plan_delivery
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.templates import TemplateSpec
from career.whatsapp.window import window_state

DELIVERY_PENDING = "PENDING_WINDOW"
DELIVERY_OPENED = "OPENED"
DELIVERY_COMPLETED = "COMPLETED"
DELIVERY_NO_SEND = "NO_SEND"


def record_out(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    channel_id: uuid.UUID,
    kind: str,
    wa_message_id: str,
    template_name: str | None = None,
    delivery_id: uuid.UUID | None = None,
    now: datetime | None = None,
    status: str = "sent",
) -> DeliveryMessage:
    dm = DeliveryMessage(
        id=uuid.uuid4(), tenant_id=tenant_id, channel_id=channel_id,
        delivery_id=delivery_id, wa_message_id=wa_message_id, kind=kind,
        template_name=template_name, status=status, status_updated_at=now,
    )
    session.add(dm)
    return dm


def _send_bundle_parts(
    session: Session, channel: CustomerChannel, delivery: Delivery,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> None:
    for part in delivery.bundle.get("parts", []):
        if part.get("kind") == "text":
            mid = whatsapp_client.send_text(channel.phone_e164, part["body"])
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id, now=now)
        elif part.get("kind") == "document":
            mid = whatsapp_client.send_document(
                channel.phone_e164, part["ref"],
                filename=part.get("filename", "cv.pdf"), caption=part.get("caption", ""),
            )
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="document", wa_message_id=mid, delivery_id=delivery.id, now=now)


def deliver_adaptive(
    session: Session, channel: CustomerChannel, bundle: dict[str, Any],
    *, run_date: date, whatsapp_client: WhatsAppClient, daily_template: TemplateSpec,
    now: datetime,
) -> Delivery:
    window = window_state(
        last_inbound_at=channel.last_inbound_at, opt_out_at=channel.opt_out_at, now=now
    )
    action = plan_delivery(window)
    delivery = Delivery(
        id=uuid.uuid4(), tenant_id=channel.tenant_id, channel_id=channel.id,
        run_date=run_date, status=DELIVERY_NO_SEND,
        window_state_at_start=window.value, bundle=bundle,
    )
    session.add(delivery)
    session.flush()

    if action is DeliveryAction.SKIP_OPTED_OUT:
        delivery.status = DELIVERY_NO_SEND
    elif action is DeliveryAction.SEND_DIRECT:
        _send_bundle_parts(session, channel, delivery, whatsapp_client=whatsapp_client, now=now)
        delivery.status = DELIVERY_COMPLETED
        delivery.completed_at = now
    else:  # SEND_TEMPLATE_THEN_WAIT
        mid = whatsapp_client.send_template(
            channel.phone_e164, daily_template.name, daily_template.language,
            buttons=daily_template.buttons,
        )
        delivery.template_message_id = mid
        record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                   kind="template", wa_message_id=mid,
                   template_name=daily_template.name, delivery_id=delivery.id, now=now)
        delivery.status = DELIVERY_PENDING
    return delivery


def descend_pending_delivery(
    session: Session, channel: CustomerChannel,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> Delivery | None:
    """When the customer opens the window, send the held bundle."""
    delivery = session.execute(
        select(Delivery)
        .where(Delivery.channel_id == channel.id, Delivery.status == DELIVERY_PENDING)
        .order_by(Delivery.created_at)
    ).scalars().first()
    if delivery is None:
        return None
    delivery.opened_at = now
    _send_bundle_parts(session, channel, delivery, whatsapp_client=whatsapp_client, now=now)
    delivery.status = DELIVERY_COMPLETED
    delivery.completed_at = now
    return delivery
