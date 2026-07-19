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
from career.whatsapp.templates import RECOVERY, RENEWAL_REMINDER

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


def sweep_subscription_lifecycle(
    session: Session,
    *,
    now: datetime,
    whatsapp_client: Any = None,
    admin_client: Any = None,
) -> dict[str, int]:
    """One idempotent pass over every timed subscription. Returns honest
    counters for the admin summary."""
    counts = {"reminded": 0, "graced": 0, "expired": 0, "recovered": 0}

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
                if _send_template(
                    whatsapp_client, _channel_phone(session, sub.tenant_id),
                    RENEWAL_REMINDER,
                ):
                    _mark(session, sub, "renewal_reminder_grace")
            else:
                days_left = (period_end - now).days
                for mark_day in REMINDER_DAYS_BEFORE:
                    if days_left < mark_day and not _event_exists(
                        session, sub.id, f"renewal_reminder_d{mark_day}"
                    ):
                        if _send_template(
                            whatsapp_client,
                            _channel_phone(session, sub.tenant_id),
                            RENEWAL_REMINDER,
                        ):
                            _mark(session, sub, f"renewal_reminder_d{mark_day}")
                            counts["reminded"] += 1
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
                if _send_template(
                    whatsapp_client, _channel_phone(session, sub.tenant_id),
                    RECOVERY,
                ):
                    _mark(session, sub, "recovery_sent")
                    counts["recovered"] += 1

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
