"""Adaptive delivery execution (whatsapp §08).

Given a bundle (generic parts list for now; C7 fills real CV/job refs), either
send it directly (open window) or send the morning template and hold the bundle
until the customer opens the window, then descend. Every outbound message is
logged in delivery_messages for the delivery receipts.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("career.whatsapp")

DELIVERY_PENDING = "PENDING_WINDOW"
DELIVERY_OPENED = "OPENED"
DELIVERY_COMPLETED = "COMPLETED"
DELIVERY_NO_SEND = "NO_SEND"
DELIVERY_PARTIAL = "PARTIAL"      # some job groups failed — honest, never hidden


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


def _send_grouped_bundle(
    session: Session, channel: CustomerChannel, delivery: Delivery,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> tuple[list[str], list[str]]:
    """C7 job bundles (§08): header, then per job its card followed by ITS
    document. A failing card skips its OWN document only; a sent card whose
    document fails marks the JOB failed (a missing CV is never success);
    one job's failure never aborts its siblings. Returns
    (delivered_groups, failed_groups)."""
    delivered: list[str] = []
    failed: list[str] = []

    header = delivery.bundle.get("header")
    if header:
        try:
            mid = whatsapp_client.send_text(channel.phone_e164, header)
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception:  # noqa: BLE001 — header failure ≠ job failures
            logger.warning("bundle header send failed", exc_info=True)

    for entry in delivery.bundle.get("jobs", []):
        group = str(entry.get("group", ""))
        card = entry.get("card") or {}
        document = entry.get("document") or {}
        try:
            mid = whatsapp_client.send_text(channel.phone_e164, card["body"])
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception:  # noqa: BLE001 — card failed → document not attempted
            logger.warning("job card send failed", exc_info=True)
            failed.append(group)
            continue
        try:
            mid = whatsapp_client.send_document(
                channel.phone_e164, document["ref"],
                filename=document.get("filename", "cv.pdf"),
                caption=document.get("caption", ""),
            )
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="document", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception:  # noqa: BLE001 — card without its CV = job FAILED
            logger.warning("job document send failed", exc_info=True)
            failed.append(group)
            continue
        outcome = entry.get("outcome") or {}
        if outcome.get("buttons"):
            # §14 measurement fuel only — the delivery contract (§08) is
            # card+document, so a failed buttons message never fails the job.
            try:
                mid = whatsapp_client.send_interactive(
                    channel.phone_e164, outcome.get("body", ""),
                    [tuple(b) for b in outcome["buttons"]],
                )
                record_out(session, tenant_id=channel.tenant_id,
                           channel_id=channel.id, kind="interactive",
                           wa_message_id=mid, delivery_id=delivery.id, now=now)
            except Exception:  # noqa: BLE001
                logger.warning("outcome buttons send failed", exc_info=True)
        delivered.append(group)
    return delivered, failed


def _dispatch_bundle(
    session: Session, channel: CustomerChannel, delivery: Delivery,
    *, whatsapp_client: WhatsAppClient, now: datetime,
) -> None:
    """Send the held bundle and set the HONEST final status."""
    if delivery.bundle.get("grouped"):
        delivered, failed = _send_grouped_bundle(
            session, channel, delivery, whatsapp_client=whatsapp_client, now=now
        )
        delivery.bundle = {
            **delivery.bundle,
            "results": {"delivered": delivered, "failed": failed},
        }
        delivery.status = DELIVERY_COMPLETED if not failed else DELIVERY_PARTIAL
    else:
        _send_bundle_parts(
            session, channel, delivery, whatsapp_client=whatsapp_client, now=now
        )
        delivery.status = DELIVERY_COMPLETED
    delivery.completed_at = now


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
        _dispatch_bundle(session, channel, delivery,
                         whatsapp_client=whatsapp_client, now=now)
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
    _dispatch_bundle(session, channel, delivery,
                     whatsapp_client=whatsapp_client, now=now)
    return delivery
