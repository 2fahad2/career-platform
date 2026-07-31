"""Subscription time lifecycle (whitepaper §05 «التجديد والنهايات»).

The paper's exact schedule, implemented as one idempotent daily sweep:
reminders on day 27 and 29 (3 and 1 days before the period end) via the
approved renewal template → period end → GRACE for 48 hours → EXPIRED →
a recovery message 7 days later. Refund/cancel/chargeback are separate
(webhook-driven, immediate). Runs as the owner role BEFORE the nightly
engine so a just-expired customer never enters tonight's query families.

Idempotency: every send is guarded by a subscription_event row — a sweep
that runs twice (or a restart mid-sweep) never double-sends.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import CustomerChannel, Subscription, SubscriptionEvent
from career.salla import subscriptions as sub_states
from career.whatsapp.templates import (
    RECOVERY,
    RENEWAL_REMINDER,
    WELCOME_ACTIVATION,
)

logger = logging.getLogger("career.salla")

#: §05 verbatim: GRACE 48 ساعة, تذكير يوم 27 و29, رسالة استرجاع بعد 7 أيام.
GRACE_HOURS = 48
REMINDER_DAYS_BEFORE = (3, 1)
RECOVERY_DAYS_AFTER = 7

#: Lifecycle plans only — the one-shot cv_analysis product has no period.
_TIMED_PLANS_EXCLUDED = ("cv_analysis",)


def _event_exists(session: Session, subscription_id: uuid.UUID, event_type: str) -> bool:
    return session.execute(
        select(SubscriptionEvent.id).where(
            SubscriptionEvent.subscription_id == subscription_id,
            SubscriptionEvent.event_type == event_type,
        )
    ).first() is not None


def _mark(session: Session, sub: Subscription, event_type: str) -> None:
    session.add(SubscriptionEvent(
        id=uuid.uuid4(), tenant_id=sub.tenant_id, subscription_id=sub.id,
        event_type=event_type, from_status=sub.status, to_status=sub.status,
    ))
    session.flush()


def _channel_phone(session: Session, tenant_id: uuid.UUID) -> str | None:
    channel = session.execute(
        select(CustomerChannel).where(
            CustomerChannel.tenant_id == tenant_id,
            CustomerChannel.opt_out_at.is_(None),
        )
    ).scalars().first()
    return channel.phone_e164 if channel else None


def _send_template(
    whatsapp_client: Any, phone: str | None, template: Any
) -> bool:
    if whatsapp_client is None or not phone:
        return False
    try:
        whatsapp_client.send_template(phone, template.name, template.language)
        return True
    except Exception:  # noqa: BLE001 — messaging never blocks the lifecycle
        logger.warning("lifecycle template send failed", exc_info=True)
        return False


_RENEW_LINK_AR = "تقدر تجدد من هنا:"


def _send_renew_link(
    whatsapp_client: Any, phone: str | None, store_url: str | None
) -> None:
    """§16 — the approved templates carry a «تجديد الاشتراك» button but no
    URL, so for ten days the reminder pointed nowhere. The link follows as a
    plain text: it lands whenever the 24h window is open, and costs nothing
    when it is not. The URL sits on its own line (mixed-direction lines
    scramble in the customer's client)."""
    url = (store_url or "").strip()
    if whatsapp_client is None or not phone or not url:
        return
    try:
        whatsapp_client.send_text(phone, f"{_RENEW_LINK_AR}\n{url}")
    except Exception:  # noqa: BLE001 — messaging never blocks the lifecycle
        logger.warning("renew link send failed", exc_info=True)


def sweep_subscription_lifecycle(
    session: Session,
    *,
    now: datetime,
    whatsapp_client: Any = None,
    admin_client: Any = None,
    store_url: str | None = None,
) -> dict[str, int]:
    """One idempotent pass over every timed subscription. Returns honest
    counters for the admin summary. ``store_url`` (§16) makes every renewal
    nudge actionable — without it the customer is told to renew with nowhere
    to go."""
    counts = {"reminded": 0, "graced": 0, "expired": 0, "recovered": 0,
              "unclaimed_reminded": 0, "unclaimed_expired": 0}

    # AUDIT ك-20: the §05 claim deadline was approved policy with a checker
    # nobody called — a buyer who never activates kept a clockless
    # PAID_UNCLAIMED subscription forever, with no reminder. Now: one nudge
    # at day 5, honest expiry at day 7 (claim_deadline_passed authority).
    from career.onboarding.policy import (
        DEFAULT_CLAIM_DEADLINE_DAYS,
        claim_deadline_passed,
    )

    unclaimed = session.execute(
        select(Subscription).where(
            Subscription.status == sub_states.PAID_UNCLAIMED
        )
    ).scalars().all()
    for sub in unclaimed:
        paid_at = sub.created_at
        if paid_at is None:
            continue
        if claim_deadline_passed(paid_at, now=now):
            sub_states.transition(
                session, sub, sub_states.EXPIRED,
                event_type="claim_deadline_elapsed",
                salla_order_id=sub.salla_order_id,
            )
            counts["unclaimed_expired"] += 1
        elif (
            now > paid_at + timedelta(days=DEFAULT_CLAIM_DEADLINE_DAYS - 2)
            and not _event_exists(session, sub.id, "claim_reminder")
        ):
            phone = sub.order_phone_e164 or _channel_phone(session, sub.tenant_id)
            if _send_template(whatsapp_client, phone, WELCOME_ACTIVATION):
                _mark(session, sub, "claim_reminder")
                counts["unclaimed_reminded"] += 1

    subs = session.execute(
        select(Subscription).where(
            Subscription.status.in_(
                (sub_states.ACTIVE, sub_states.GRACE, sub_states.EXPIRED)
            ),
            Subscription.current_period_end.is_not(None),
            Subscription.plan_code.not_in(_TIMED_PLANS_EXCLUDED),
        )
    ).scalars().all()

    for sub in subs:
        period_end = sub.current_period_end
        if period_end is None:  # filtered above — belt and braces
            continue

        if sub.status == sub_states.ACTIVE:
            if now >= period_end:
                sub_states.transition(
                    session, sub, sub_states.GRACE,
                    event_type="period_ended",
                    salla_order_id=sub.salla_order_id,
                )
                counts["graced"] += 1
                grace_phone = _channel_phone(session, sub.tenant_id)
                if _send_template(
                    whatsapp_client, grace_phone, RENEWAL_REMINDER,
                ):
                    _mark(session, sub, "renewal_reminder_grace")
                    _send_renew_link(whatsapp_client, grace_phone, store_url)
            else:
                days_left = (period_end - now).days
                for mark_day in REMINDER_DAYS_BEFORE:
                    if days_left < mark_day and not _event_exists(
                        session, sub.id, f"renewal_reminder_d{mark_day}"
                    ):
                        due_phone = _channel_phone(session, sub.tenant_id)
                        if _send_template(
                            whatsapp_client, due_phone, RENEWAL_REMINDER,
                        ):
                            _mark(session, sub, f"renewal_reminder_d{mark_day}")
                            counts["reminded"] += 1
                            _send_renew_link(
                                whatsapp_client, due_phone, store_url
                            )
                        break  # one reminder per sweep at most

        elif sub.status == sub_states.GRACE:
            if now >= period_end + timedelta(hours=GRACE_HOURS):
                sub_states.transition(
                    session, sub, sub_states.EXPIRED,
                    event_type="grace_elapsed",
                    salla_order_id=sub.salla_order_id,
                )
                counts["expired"] += 1

        elif sub.status == sub_states.EXPIRED:
            recovery_at = (
                period_end + timedelta(hours=GRACE_HOURS)
                + timedelta(days=RECOVERY_DAYS_AFTER)
            )
            if now >= recovery_at and not _event_exists(
                session, sub.id, "recovery_sent"
            ):
                rec_phone = _channel_phone(session, sub.tenant_id)
                if _send_template(whatsapp_client, rec_phone, RECOVERY):
                    _mark(session, sub, "recovery_sent")
                    counts["recovered"] += 1
                    _send_renew_link(whatsapp_client, rec_phone, store_url)

    if admin_client is not None and any(counts.values()):
        try:
            admin_client.send_admin(
                "⏳ دورة الاشتراكات: "
                f"تذكير {counts['reminded']} · سماح {counts['graced']} · "
                f"انتهى {counts['expired']} · استرجاع {counts['recovered']}"
            )
        except Exception:  # noqa: BLE001
            logger.warning("lifecycle admin note failed", exc_info=True)
    return counts
