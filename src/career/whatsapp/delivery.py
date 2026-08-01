"""Adaptive delivery execution (whatsapp §08).

Given a bundle (generic parts list for now; C7 fills real CV/job refs), either
send it directly (open window) or send the morning template and hold the bundle
until the customer opens the window, then descend. Every outbound message is
logged in delivery_messages for the delivery receipts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, Delivery, DeliveryMessage
from career.whatsapp.adaptive import DeliveryAction, plan_delivery
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.templates import TemplateSpec
from career.whatsapp.window import WindowState, window_state

logger = logging.getLogger("career.whatsapp")

DELIVERY_PENDING = "PENDING_WINDOW"
DELIVERY_OPENED = "OPENED"
DELIVERY_COMPLETED = "COMPLETED"
DELIVERY_NO_SEND = "NO_SEND"
DELIVERY_PARTIAL = "PARTIAL"      # some job groups failed — honest, never hidden

#: How many times one held bundle may be re-attempted before we stop trying
#: and close it honestly. A customer who taps and gets nothing deserves the
#: next message to try again — but not forever, and not on every keystroke.
MAX_DISPATCH_ATTEMPTS = 3

#: The honest outcomes of an operator «إعادة إرسال». A refusal is NOT a
#: failed attempt: only :data:`RESEND_SENT` ever touches the attempt budget.
RESEND_SENT = "sent"
RESEND_NO_BUNDLE = "no_bundle"
RESEND_OPTED_OUT = "opted_out"
RESEND_WINDOW_CLOSED = "window_closed"


@dataclass(frozen=True)
class ResendResult:
    """What the resend did — never just «a Delivery or None».

    The caller has to tell the operator the truth, and «there is nothing to
    resend» and «we refuse to try because the window is shut» are different
    truths with different next steps. ``delivery`` is the bundle we looked at
    (present even on a refusal, so the caller can report on it).
    """

    outcome: str
    delivery: Delivery | None = None

    @property
    def status(self) -> str | None:
        return None if self.delivery is None else str(self.delivery.status)

    @property
    def delivered_groups(self) -> list[str]:
        if self.delivery is None:
            return []
        results = self.delivery.bundle.get("results") or {}
        return [str(g) for g in (results.get("delivered") or [])]


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
        except Exception as exc:  # noqa: BLE001 — header failure ≠ job failures
            logger.error("bundle header send failed: %s", exc, exc_info=True)

    for entry in delivery.bundle.get("jobs", []):
        group = str(entry.get("group", ""))
        card = entry.get("card") or {}
        document = entry.get("document") or {}
        try:
            mid = whatsapp_client.send_text(channel.phone_e164, card["body"])
            record_out(session, tenant_id=channel.tenant_id, channel_id=channel.id,
                       kind="text", wa_message_id=mid, delivery_id=delivery.id,
                       now=now)
        except Exception as exc:  # noqa: BLE001 — card failed → document not attempted
            logger.error("job card send failed: %s", exc, exc_info=True)
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
        except Exception as exc:  # noqa: BLE001 — card without its CV = job FAILED
            logger.error("job document send failed: %s", exc, exc_info=True)
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
    *, whatsapp_client: WhatsAppClient, now: datetime, retry_ok: bool = False,
) -> None:
    """Send the held bundle and set the HONEST final status.

    A tap that delivers NOTHING no longer consumes the bundle. The old code
    stamped COMPLETED/PARTIAL unconditionally after the send loop, so when
    every document failed the delivery left PENDING_WINDOW anyway — and both
    the descend query and the stale sweep filter on PENDING_WINDOW, which
    made the failure terminal and unreachable. The customer's one chance was
    spent on an attempt that sent them nothing (live: 27 July, zero delivered
    and no trace). Now a total failure stays claimable, up to
    MAX_DISPATCH_ATTEMPTS, so the next inbound — or the operator's resend —
    tries again.

    ``retry_ok`` is True only for a HELD bundle (the descend and resend
    paths). On the open-window direct path there was no template and no tap
    to protect, and the day must still close with an honest state (§15.12),
    so a total failure stays terminal exactly as before.
    """
    attempts = int(delivery.bundle.get("attempts", 0)) + 1
    if delivery.bundle.get("grouped"):
        delivered, failed = _send_grouped_bundle(
            session, channel, delivery, whatsapp_client=whatsapp_client, now=now
        )
        delivery.bundle = {
            **delivery.bundle,
            "attempts": attempts,
            "results": {"delivered": delivered, "failed": failed},
        }
        if delivered:
            delivery.status = (
                DELIVERY_COMPLETED if not failed else DELIVERY_PARTIAL
            )
        elif retry_ok and attempts < MAX_DISPATCH_ATTEMPTS:
            logger.error(
                "held bundle delivered nothing on attempt %d — staying "
                "claimable for a retry", attempts,
            )
            delivery.status = DELIVERY_PENDING
            return          # completed_at stays NULL: nothing completed
        else:
            logger.error(
                "held bundle delivered nothing after %d attempts — closing "
                "it honestly", attempts,
            )
            delivery.status = DELIVERY_PARTIAL
    else:
        _send_bundle_parts(
            session, channel, delivery, whatsapp_client=whatsapp_client, now=now
        )
        delivery.bundle = {**delivery.bundle, "attempts": attempts}
        delivery.status = DELIVERY_COMPLETED
    delivery.completed_at = now


def close_day_from_terminal_delivery(
    session: Session, delivery: Delivery, *, now: datetime
) -> None:
    """§15.12 + §15.3: a delivery that reached a TERMINAL status closes its
    tenant's day and writes the suppression for what actually landed.

    AUDIT (constant 12): this used to live only at the two callers that
    happened to remember it (the nightly orchestrator and the inbound descend
    in whatsapp/worker). The operator's resend was a third caller and it
    remembered nothing — so a resend that LANDED left ``tenant_day_states``
    with no row for that run_date at all (permanently: the row is no longer
    PENDING_WINDOW, so neither the descend query nor the stale sweep can ever
    reach it again), wrote no suppression (the same posting passed the gate
    again the next night and the identical cached PDF was re-sent), and
    skipped the day's cost rollup. It now lives INSIDE the dispatch module,
    next to the status it depends on, so no future caller can forget it.
    """
    if delivery.status not in (DELIVERY_COMPLETED, DELIVERY_PARTIAL):
        return
    # local imports: career.cv.daily_run imports this module at module scope
    from career.cv import close as close_mod
    from career.cv.daily_run import close_from_delivery

    closed = close_from_delivery(session, delivery=delivery, now=now)
    if closed is None:  # pragma: no cover — guarded by the status check above
        return
    try:
        close_mod.rollup_costs(
            session, tenant_id=delivery.tenant_id, day=closed.run_date
        )
    except Exception:  # noqa: BLE001 — accounting never blocks the close
        logger.warning("resend cost rollup failed", exc_info=True)


def resend_pending_delivery(
    session: Session, *, tenant_id: uuid.UUID,
    whatsapp_client: WhatsAppClient, now: datetime,
) -> ResendResult:
    """The operator's «إعادة إرسال»: re-attempt today's held bundle.

    Until now a delivery that failed after the tap was terminal — there was
    no retry path anywhere, for the customer OR the operator.

    Two audit fixes are load-bearing here:

    * A held bundle is held precisely because the 24h window was CLOSED. A
      free-form re-dispatch into a closed window is rejected by Meta for
      EVERY job, yet the old code counted the attempt anyway — so three taps
      on the most common held-bundle state (waiting for the morning tap) made
      the bundle terminal and unclaimable, and a customer who tapped the
      template later that day received nothing. The window is now checked
      BEFORE the attempt counter moves, and a refusal costs nothing.
    * A resend that lands is a delivery like any other: it closes the day and
      writes suppression (:func:`close_day_from_terminal_delivery`).
    """
    delivery = session.execute(
        select(Delivery)
        .where(Delivery.tenant_id == tenant_id,
               Delivery.status == DELIVERY_PENDING)
        .order_by(Delivery.created_at)
    ).scalars().first()
    if delivery is None:
        return ResendResult(RESEND_NO_BUNDLE)
    channel = session.get(CustomerChannel, delivery.channel_id)
    if channel is None:
        return ResendResult(RESEND_NO_BUNDLE)
    if channel.opt_out_at is not None:
        return ResendResult(RESEND_OPTED_OUT, delivery)
    window = window_state(
        last_inbound_at=channel.last_inbound_at,
        opt_out_at=channel.opt_out_at, now=now,
    )
    if window is not WindowState.OPEN:
        # No send is possible, so no attempt is spent and nothing is stamped:
        # the bundle stays exactly as claimable as it was.
        return ResendResult(RESEND_WINDOW_CLOSED, delivery)
    delivery.opened_at = delivery.opened_at or now
    _dispatch_bundle(session, channel, delivery,
                     whatsapp_client=whatsapp_client, now=now, retry_ok=True)
    close_day_from_terminal_delivery(session, delivery, now=now)
    return ResendResult(RESEND_SENT, delivery)


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
                     whatsapp_client=whatsapp_client, now=now, retry_ok=True)
    return delivery
