"""Subscription time lifecycle (whitepaper §05 «التجديد والنهايات»).

The paper's exact schedule, implemented as one idempotent daily sweep:
reminders on day 27 and 29 (3 and 1 days before the period end) via the
approved renewal template → period end → GRACE for 48 hours → EXPIRED →
a recovery message 7 days later. Refund/cancel/chargeback are separate
(webhook-driven, immediate). Runs as the owner role BEFORE the nightly
engine so a just-expired customer never enters tonight's query families.

Idempotency: every send is guarded by a subscription_event row — a sweep
that runs twice (or a restart mid-sweep) never double-sends.

Three judgement calls this module now makes explicitly:

* **A PAUSED subscription is swept exactly like an ACTIVE one.** §05 is
  explicit that a pause does not extend the period, so the paid days drain
  while the customer is quiet. Selecting only (ACTIVE, GRACE, EXPIRED) made
  PAUSED terminal in practice: no reminder, no grace, no expiry, no recovery
  — the customer was simply never asked to renew and never billed again,
  their founding seat was held for life, and the 90-day retention sweep
  (which skips a «live» tenant) could never fire. The transition is made
  LEGAL in the state machine rather than forced here.
* **A row retired by a renewal is never nudged.** The renewal marker is now
  written unconditionally (renewal.close_previous), and the check below has
  a second, independent leg: a tenant who holds a live row with a later
  period never hears «نفتقدك» about an older one, however that older row
  came to be retired.
* **A renewal nudge always carries a way to act.** With no storefront URL
  configured the link used to be a silent no-op, so the reminder told a
  customer to renew with nowhere to go; it now degrades to the escape hatch
  the rest of the product prints, and the operator is told the link is
  missing instead of finding out from a customer.
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


#: States that prove the tenant is a customer RIGHT NOW, whatever some older
#: row of theirs still says.
_LIVE_NOW: frozenset[str] = frozenset({
    sub_states.PAID_UNCLAIMED, sub_states.ONBOARDING, sub_states.ACTIVE,
    sub_states.PAUSED, sub_states.GRACE, sub_states.SUSPENDED,
})


def _superseded_by_renewal(session: Session, sub: Subscription) -> bool:
    """Was this row retired by a renewal (§16)?

    The retired row keeps its old period_end, so without this check the sweep
    would walk an ACTIVE paying customer through their PREVIOUS period all
    over again: a «we miss you» recovery template plus a renew link, about
    nine days after every single renewal, forever.

    Two legs, deliberately. The marker event is the precise answer, but it was
    only ever written when the previous row could still legally move — a
    customer who lapsed to EXPIRED and then came back got NO marker, so the
    sweep sent the recovery template to someone who had paid hours earlier.
    The second leg asks the question that actually matters — «does this human
    hold a live subscription today?» — and is independent of how the old row
    was retired (merged prepayment, operator action, a marker that failed to
    write). A newer live row always wins over an older dead one.
    """
    if _event_exists(session, sub.id, "renewed"):
        return True
    if sub.current_period_end is None:
        return False
    # Strictly LATER, not merely «another live row»: a tenant can legitimately
    # hold a live row with no period at all (a prepayment waiting for
    # activation), and treating that as a supersession would silence the
    # reminders on the row the customer is actually living on.
    newer = session.execute(
        select(Subscription.id).where(
            Subscription.tenant_id == sub.tenant_id,
            Subscription.id != sub.id,
            Subscription.status.in_(sorted(_LIVE_NOW)),
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end > sub.current_period_end,
        )
    ).first()
    return newer is not None


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

#: The same escape hatch «حالة اشتراكي» prints when no storefront is
#: configured (onboarding.privacy._renew_cta) — one wording for one promise.
#: «دعم» is routed: it acks the customer and pages the operator.
_RENEW_NO_LINK_AR = "للتجديد أرسل: دعم"


def _send_renew_link(
    whatsapp_client: Any, phone: str | None, store_url: str | None
) -> bool:
    """§16 — the approved templates carry a renewal button but no URL, so for
    ten days the reminder pointed nowhere. The link follows as a plain text:
    it lands whenever the 24h window is open, and costs nothing when it is
    not. The URL sits on its own line (mixed-direction lines scramble in the
    customer's client).

    Returns True when a real link went out. With SALLA_STORE_URL unset — which
    is its state on every machine today — this used to return silently, so the
    whole nudge was «renew» with no how. It now falls back to the routed «دعم»
    escape hatch, and the caller tells the operator the link is missing.
    """
    if whatsapp_client is None or not phone:
        return False
    url = (store_url or "").strip()
    body = f"{_RENEW_LINK_AR}\n{url}" if url else _RENEW_NO_LINK_AR
    try:
        whatsapp_client.send_text(phone, body)
    except Exception:  # noqa: BLE001 — messaging never blocks the lifecycle
        logger.warning("renew link send failed", exc_info=True)
        return False
    return bool(url)


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
              "unclaimed_reminded": 0, "unclaimed_expired": 0,
              # nudges that went out with no purchase link behind them
              "nudged_without_link": 0}

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

    # PAUSED belongs here with ACTIVE. Its clock is running (§05: a pause does
    # not extend the period), so its period ends on the same day and must end
    # the same way. Left out, it was the one state no clock could ever leave.
    subs = session.execute(
        select(Subscription).where(
            Subscription.status.in_(
                (sub_states.ACTIVE, sub_states.PAUSED,
                 sub_states.GRACE, sub_states.EXPIRED)
            ),
            Subscription.current_period_end.is_not(None),
            Subscription.plan_code.not_in(_TIMED_PLANS_EXCLUDED),
        )
    ).scalars().all()

    for sub in subs:
        period_end = sub.current_period_end
        if period_end is None:  # filtered above — belt and braces
            continue
        if _superseded_by_renewal(session, sub):
            continue

        if sub.status in (sub_states.ACTIVE, sub_states.PAUSED):
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
                    if not _send_renew_link(
                        whatsapp_client, grace_phone, store_url
                    ):
                        counts["nudged_without_link"] += 1
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
                            if not _send_renew_link(
                                whatsapp_client, due_phone, store_url
                            ):
                                counts["nudged_without_link"] += 1
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
                    if not _send_renew_link(
                        whatsapp_client, rec_phone, store_url
                    ):
                        counts["nudged_without_link"] += 1

    if counts["nudged_without_link"]:
        # The nightly caller (engine/cli.py) passes no admin_client today, so
        # the journal is the floor: this must be visible even with no channel.
        logger.warning(
            "renewal nudges sent with no store url configured: %d",
            counts["nudged_without_link"],
        )
    if admin_client is not None and any(counts.values()):
        line = (
            "⏳ دورة الاشتراكات: "
            f"تذكير {counts['reminded']} · سماح {counts['graced']} · "
            f"انتهى {counts['expired']} · استرجاع {counts['recovered']}"
        )
        if counts["nudged_without_link"]:
            # The operator hears it the night it happens, not from a customer
            # who tapped «جدّد» and found nothing behind it.
            line += (
                "\n⚠️ رابط الشراء غير مضبوط في الإعدادات — "
                f"{counts['nudged_without_link']} تذكير خرج بلا رابط"
            )
        try:
            admin_client.send_admin(line)
        except Exception:  # noqa: BLE001
            logger.warning("lifecycle admin note failed", exc_info=True)
    return counts
