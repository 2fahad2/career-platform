"""Activation: link a paid order to the WhatsApp number that claims it (§08).

The customer sends "تفعيل <token>". We verify the token (hash lookup, not
expired, not used), then create/link the customer_channel (phone ↔ tenant),
mark the token used, and move the subscription PAID_UNCLAIMED → ONBOARDING.
Runs as the owner role (looks up across tenants, creates channels). Failures
send a customer-facing Arabic reply and a PII-free admin alert.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import (
    ActivationToken,
    CustomerChannel,
    Subscription,
    SupportEvent,
    Tenant,
)
from career.salla import subscriptions as sub_states
from career.support import MUTING_STATUSES, OPEN
from career.telegram import messages as admin_msg
from career.telegram.admin import TelegramAdminClient
from career.tokens import hash_token
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.delivery import record_out
from career.whatsapp.phones import phone_variants

logger = logging.getLogger("career.whatsapp")

_WELCOME = "تم التفعيل ✅ أهلًا بك! لنبدأ إعداد خدمتك."
_INVALID = "لم نتعرّف على رمز التفعيل. تأكّد من الرابط في صفحة الشكر بعد الدفع."
_EXPIRED = "انتهت صلاحية رمز التفعيل. أرسل: دعم — وسنعيد إرساله."
_USED = "رمز التفعيل مستخدم مسبقًا. إن واجهت مشكلة أرسل: دعم"
_CONFLICT = "هذا الرقم مرتبط بحساب آخر. أرسل: دعم"

#: ── the buyer who is still silenced (audit, 7 August) ───────────────────────
#: The existing-channel branch below refreshes four columns and deliberately
#: does NOT refresh ``opt_out_at``. That is correct — and on its own it was a
#: customer who paid for nothing:
#:
#:   opt_out_at set → window_state → OPTED_OUT → plan_delivery → SKIP_OPTED_OUT
#:   → deliver_adaptive writes DELIVERY_NO_SEND → daily_run closes the day
#:   SKIPPED_OPTED_OUT → engine.cli calls that an HONEST state → exit code 0.
#:
#: Every link is right and the sum is a paying, ACTIVE customer who receives
#: nothing, every night, with nobody told. The §04 upgrade path walks straight
#: into it: it reuses the existing channel ON PURPOSE, so a funnel customer
#: who once sent «إيقاف» and then buys لمّاح is exactly this case.
#:
#: THAT PATH IS THE RARE ONE (audit D2.1). Activation only runs on a first
#: purchase, the §04 upgrade or a re-buy; the customer who silences us without
#: cancelling his BILLING never comes back through here at all — Salla simply
#: charges him every 28 days. That route is `salla/provisioning`'s RENEWED
#: branch, and it raises this same ticket through :func:`ticket_if_silenced`
#: below rather than deciding any of it a second time.
#:
#: WHAT WE DO NOT DO: clear the column. An opt-out is a standing compliance
#: instruction, not a preference that a payment overrides, and clearing it
#: here would also have to forge the `resume` row that
#: `tests/test_promises.py` structurally requires beside every write to it
#: (`promises/guarantee._silence_inside` reads the customer's silence out of
#: `inbound_messages`, because the column remembers only the last flip). A
#: resume he never sent is a false fact in the table a refund is decided from.
#: So he keeps his silence, and instead: he is told, and the operator is told.
_WELCOME_OPTED_OUT = (
    "تم التفعيل ✅ اشتراكك شغّال من الآن\n"
    "بس رسائلنا موقوفة عندك بطلب سابق منك، فما راح توصلك الفرص\n"
    "وما نقدر نشغّلها من طرفنا — ترجع بكلمة وحدة، أرسلها لحالها:\n"
    "تشغيل الرسائل"
)
#: `support_events.kind` (String(32)). NOT `support_request`: nobody asked for
#: support, and putting this under that label would file it as a customer
#: request the operator can answer with a reply — this one he cannot answer at
#: all, only chase.
OPTED_OUT_TICKET_KIND = "paid_while_opted_out"
#: The alert's first line, kept a constant so a counter can key on it instead
#: of on a phrase (whatsapp/worker also pages the operator about opt-outs).
_OPTED_OUT_ALERT_HEAD = "⛔ دفع وهو موقف الرسائل — ما راح يستلم شي"
_OPTED_OUT_ALERT_TAIL = (
    "فعّلنا اشتراكه وقلنا له يرسل «تشغيل الرسائل»\n"
    "الإيقاف تعليمة منه وما نلغيها نيابة عنه — التذكرة مفتوحة في شاشة التذاكر"
)
#: The RENEWAL tail (§16 — adversarial review D2.1). Every sentence of the tail
#: above is FALSE on a renewal: nothing was activated, and above all we did NOT
#: tell him. A renewal is not a reply to anything he sent — it is a charge from
#: the storefront — so the one justification that let the activation welcome
#: through to a silenced number does not exist here, and he is sent nothing at
#: all. Which leaves the operator holding the entire fact, and the fact is
#: about money: it is arriving monthly against a service that cannot be
#: delivered, and only he can decide what happens to it.
RENEWAL_OPTED_OUT_TAIL = (
    "تجديد جديد على حساب موقف الرسائل — ما أرسلنا له شي وما نقدر نرسل\n"
    "التجديد ما هو ردّ على رسالة منه، فأي إرسال له مخالف لتعليمته\n"
    "الفلوس وصلت والخدمة ما راح توصل — قرار الاسترجاع أو الإيقاف قرارك\n"
    "التذكرة مفتوحة في شاشة التذاكر"
)


def _opted_out_alert(ten_code: str, tail: str = _OPTED_OUT_ALERT_TAIL) -> str:
    """TEN code alone on its own line (§15.13 + the bidi rule): an Arabic line
    with a Latin token in it arrives scrambled on the operator's client."""
    return f"{_OPTED_OUT_ALERT_HEAD}\n{ten_code}\n{tail}"


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
    #: The customer activated while his channel is STILL silenced (§08 opt-out
    #: standing). Carried out of here because the caller is the one that runs
    #: the handover — ``worker._finish_activation`` starts the funnel or the
    #: onboarding immediately after, and both then talk at length to someone
    #: who asked us not to, burying the one line that tells him why nothing
    #: will arrive. Nothing in the tree reads this yet; it is the fact the
    #: worker's owner needs in order to decide, not a decision taken for him.
    opted_out: bool = False


def _channel_for_phone(session: Session, phone: str) -> CustomerChannel | None:
    """Tolerant of the ``+``. Meta delivers ``from`` WITHOUT it while the rest
    of the system writes numbers with it, so an exact comparison silently
    fails to find a channel that is right there (see career.whatsapp.phones).
    Oldest first — the first binding of a number is the proven one."""
    return session.execute(
        select(CustomerChannel).where(
            CustomerChannel.provider == "whatsapp",
            CustomerChannel.phone_e164.in_(phone_variants(phone)),
        ).order_by(CustomerChannel.created_at, CustomerChannel.id)
    ).scalars().first()


def _is_funnel_only_tenant(owner_session: Session, tenant_id: uuid.UUID) -> bool:
    """True when the tenant's every subscription is the cv_analysis product —
    the §04 upgrade-inheritance precondition."""
    plans = owner_session.execute(
        select(Subscription.plan_code).where(Subscription.tenant_id == tenant_id)
    ).scalars().all()
    return bool(plans) and all(p == "cv_analysis" for p in plans)


def _is_funnel_purchase(
    owner_session: Session, subscription_id: uuid.UUID | None
) -> bool:
    """True when the NEW order being activated is the one-shot analysis."""
    if subscription_id is None:
        return False
    sub = owner_session.get(Subscription, subscription_id)
    return sub is not None and sub.plan_code == "cv_analysis"


def _raise_opted_out_ticket(
    owner_session: Session,
    *,
    channel: CustomerChannel,
    ten_code: str,
    admin_client: TelegramAdminClient | None,
    now: datetime,
    tail: str = _OPTED_OUT_ALERT_TAIL,
) -> bool:
    """Put the undeliverable purchase on a SCREEN, once. True when this call is
    the one that raised it (a deduplicated repeat returns False and says
    nothing).

    ``support_events`` is reused rather than anything new: it is the table the
    console's tickets screen already reads and already lets the operator
    close, so this ticket ages, queues and resolves beside «دعم» and the funnel
    stall with no new surface to remember. What was rejected:

    * a log line only — the defect is that nobody finds out, and a log is
      exactly the place nobody looks;
    * an admin message only — the alert scrolls away in minutes and there is
      no record afterwards that the customer is still silent;
    * a new table or a new console screen — a second inbox is a second thing
      to forget, and this ticket needs precisely what a ticket needs;
    * ``kind="support_request"`` — see the constant: the operator would read
      it as a customer asking for help and answer with a reply the customer
      cannot receive.

    DEDUPLICATED on a MUTING ticket for this CHANNEL, so a retried activation
    and the upgrade re-run do not grow the queue. Keyed on the channel and not
    the tenant because the §04 upgrade MOVES the subscription to another
    tenant — the channel is the one identity that survives it. Not keyed on
    «any ticket ever»: a customer who is still silent and pays AGAIN is a fact
    the operator has to hear a second time.

    HOW LONG THE DEDUPE HOLDS — corrected, because it drifted by one word.
    This used to say «once the operator has closed this one», and closing is
    no longer the only way out: since `telegram.console.
    release_forgotten_tickets` the sweep moves an unclosed ticket OPEN →
    RELEASED after 48 hours, and RELEASED is not in ``MUTING_STATUSES``. So
    the true bound is «until it is closed OR released», i.e. at most 48 hours
    — which is the invariant that sweep exists to state (NO TICKET MUTES
    ANYTHING FOR LONGER THAN THIS) and it applies to this dedupe exactly as it
    applies to the other three. The code already read the set rather than the
    word, so nothing here changes; the sentence was the only thing that was
    still describing the old world.

    ``created_at`` is written explicitly (the column has a server default) so
    the age the tickets screen prints comes from the same clock the rest of
    activation uses.

    ``tail`` is the only thing a second caller may vary: ``salla/provisioning``
    raises this same ticket on a RENEWAL, where the activation tail's «we told
    him» is untrue. The kind, the dedupe key, the ordering and the head line
    are deliberately NOT parameters — a second wording of the same fact is a
    second thing for the operator to learn and for a counter to miss.
    """
    already_open = owner_session.execute(
        select(SupportEvent.id).where(
            SupportEvent.channel_id == channel.id,
            SupportEvent.kind == OPTED_OUT_TICKET_KIND,
            SupportEvent.status.in_(sorted(MUTING_STATUSES)),
        ).limit(1)
    ).first()
    if already_open is not None:
        return False
    owner_session.add(SupportEvent(
        id=uuid.uuid4(), tenant_id=channel.tenant_id, channel_id=channel.id,
        kind=OPTED_OUT_TICKET_KIND, status=OPEN, created_at=now,
    ))
    owner_session.flush()
    # The ticket is the record; the page is the courtesy. Same ordering, and
    # the same reason, as the opt-out itself in whatsapp/worker: a Telegram
    # 429 must not take the activation down with it.
    if admin_client is not None:
        try:
            admin_client.send_admin(_opted_out_alert(ten_code, tail))
        except Exception:  # noqa: BLE001 — the ticket is already written
            logger.warning("opted-out purchase alert failed", exc_info=True)
    return True


def ticket_if_silenced(
    owner_session: Session,
    *,
    phone: str,
    ten_code: str,
    admin_client: TelegramAdminClient | None,
    now: datetime,
    tail: str,
) -> bool:
    """«Is the number behind this money switched off? Then put it on a screen.»

    THE ENTRY POINT FOR MONEY THAT DID NOT COME THROUGH ACTIVATION, and it
    lives here rather than in ``salla/provisioning`` on purpose: everything
    this decision is made of — the ticket kind, the channel-keyed dedupe, the
    alert head a counter keys on, the refusal to clear ``opt_out_at`` — is
    already here and must have exactly one owner. The renewal path would
    otherwise have grown a second, slightly different answer to the same
    question, which is how the two halves drift apart.

    The channel lookup is ``_channel_for_phone``, the SAME tolerant lookup
    activation uses (Meta delivers ``from`` without the ``+``, Salla stores it
    with one) — not a fresh comparison that would fail to find a channel that
    is right there. ``False`` when there is no channel yet or it is not
    silenced: nothing to raise, and nothing said.

    Writes inside the CALLER's transaction and does not commit — the caller
    owns whether the money and the ticket land together.
    """
    channel = _channel_for_phone(owner_session, phone)
    if channel is None or channel.opt_out_at is None:
        return False
    return _raise_opted_out_ticket(
        owner_session, channel=channel, ten_code=ten_code,
        admin_client=admin_client, now=now, tail=tail,
    )


def activate_by_order_phone(
    owner_session: Session,
    *,
    from_phone: str,
    display_name: str | None,
    now: datetime,
    whatsapp_client: WhatsAppClient,
    admin_client: TelegramAdminClient,
) -> ActivationResult | None:
    """CHANGELOG §11 zero-touch claim: an inbound from the exact phone on a
    PAID_UNCLAIMED order proves ownership — activate without a typed token.
    Returns None when no claimable subscription matches (caller falls back
    to the generic help); token expiry is deliberately NOT checked here (the
    proof is the reply, not the token's freshness)."""
    # The ``+`` boundary, which had never been crossed on this path: Salla
    # phones are stored normalized WITH a leading ``+`` while Meta delivers
    # ``from`` WITHOUT one, so this exact comparison could not match a real
    # customer even once. The welcome template tells the buyer «ردّ بأي رسالة
    # للمتابعة» — that instructed reply landed on the generic «send your
    # activation code» help, for a code we only ever send to the operator.
    sub = owner_session.execute(
        select(Subscription).where(
            Subscription.order_phone_e164.in_(phone_variants(from_phone)),
            Subscription.status == sub_states.PAID_UNCLAIMED,
        ).order_by(Subscription.created_at)
    ).scalars().first()
    if sub is None:
        return None
    tok = owner_session.execute(
        select(ActivationToken).where(
            ActivationToken.subscription_id == sub.id,
            ActivationToken.used_at.is_(None),
        )
    ).scalar_one_or_none()
    if tok is None:
        return None
    return _activate_with_token(
        owner_session, tok=tok, from_phone=from_phone,
        display_name=display_name, now=now, check_expiry=False,
        whatsapp_client=whatsapp_client, admin_client=admin_client,
    )


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
    return _activate_with_token(
        owner_session, tok=tok, from_phone=from_phone,
        display_name=display_name, now=now, check_expiry=True,
        whatsapp_client=whatsapp_client, admin_client=admin_client,
    )


def _activate_with_token(
    owner_session: Session,
    *,
    tok: ActivationToken,
    from_phone: str,
    display_name: str | None,
    now: datetime,
    check_expiry: bool,
    whatsapp_client: WhatsAppClient,
    admin_client: TelegramAdminClient,
) -> ActivationResult:

    ten_code = owner_session.get(Tenant, tok.tenant_id).code  # type: ignore[union-attr]
    existing = _channel_for_phone(owner_session, from_phone)

    # Phone already belongs to a different customer. ONE documented exception
    # (§04 inheritance): a funnel-only customer upgrading — their extracted
    # facts live on the EXISTING tenant, so the new purchase re-links to it.
    # DETECTED here, but MUTATED only after every token validity check passes
    # (audit fix A: the worker commits after activate — an early-return path
    # must leave zero side effects). The shell tenant stays empty and
    # harmless: nothing is deleted, ever.
    inherit = False
    if existing is not None and existing.tenant_id != tok.tenant_id:
        if _is_funnel_only_tenant(owner_session, existing.tenant_id):
            inherit = True
        elif _is_funnel_purchase(owner_session, tok.subscription_id):
            # The mirror case (closure audit, 31 July): a PASS customer buys
            # the 29-SAR analysis. It provisioned a shell tenant, so typing
            # the token answered «هذا الرقم مرتبط بحساب آخر» — a customer
            # blocked from a product they had just paid for. One human is one
            # tenant in both directions; the analysis rides along on theirs.
            inherit = True
        else:
            whatsapp_client.send_text(from_phone, _CONFLICT)
            admin_client.send_admin(
                admin_msg.activation_failed(ten_code, "phone_conflict")
            )
            return ActivationResult(ActivationStatus.CONFLICT)

    if check_expiry and tok.expires_at < now:
        whatsapp_client.send_text(from_phone, _EXPIRED)
        admin_client.send_admin(admin_msg.activation_failed(ten_code, "expired"))
        return ActivationResult(ActivationStatus.EXPIRED)

    if tok.used_at is not None:
        # Already used: if this same phone/tenant is already linked, it's an
        # idempotent re-send — just refresh the window.
        if existing is not None and existing.tenant_id == tok.tenant_id:
            existing.last_inbound_at = now
            silenced = existing.opt_out_at is not None
            if silenced:
                # AUDIT D2.4. This branch used to claim it was «the RETRY of an
                # activation that already spoke and already ticketed» and act on
                # that claim by doing nothing. It is only true when the opt-out
                # came FIRST. When it came AFTER — an ordinary activation, then
                # «إيقاف الرسائل» a day later, then this link typed again or
                # re-issued because nothing is arriving — no ticket has ever
                # existed for this channel, and this is the system looking
                # straight at a paying customer's silence and saying nothing.
                # The dedupe makes the true retry a no-op, so the claim no
                # longer has to be true for the code to be right.
                _raise_opted_out_ticket(
                    owner_session, channel=existing, ten_code=ten_code,
                    admin_client=admin_client, now=now,
                )
            owner_session.commit()
            return ActivationResult(
                ActivationStatus.ALREADY_LINKED, tenant_id=str(tok.tenant_id),
                channel_id=str(existing.id), subscription_id=str(tok.subscription_id),
                # The OPERATOR is told (once); the customer is not re-announced
                # to. He was welcomed by the activation this repeats, and if he
                # went quiet afterwards it is because he asked to.
                opted_out=silenced,
            )
        whatsapp_client.send_text(from_phone, _USED)
        admin_client.send_admin(admin_msg.activation_failed(ten_code, "token_reused"))
        return ActivationResult(ActivationStatus.ALREADY_USED)

    # Happy path — every validity check passed; NOW mutations may begin.
    if inherit and existing is not None:
        shell_code = ten_code
        moved = owner_session.get(Subscription, tok.subscription_id)
        if moved is not None:
            moved.tenant_id = existing.tenant_id
        tok.tenant_id = existing.tenant_id
        ten_code = owner_session.get(  # type: ignore[union-attr]
            Tenant, existing.tenant_id
        ).code
        # Both codes on a line of their own (§16): a line mixing Arabic with
        # Latin reverses in the operator's client, and «TEN-0007 → TEN-0002»
        # is a line whose whole meaning is the ORDER of its two halves.
        admin_client.send_admin(
            "↻ ترقية من قمع التحليل\n"
            f"{shell_code} → {ten_code}"
        )

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
        # ``opt_out_at`` is POINTEDLY not touched here — see the note beside
        # _WELCOME_OPTED_OUT. A payment does not revoke a compliance
        # instruction; it obliges us to say what the instruction now costs.

    tok.used_at = now

    sub = owner_session.get(Subscription, tok.subscription_id)
    if sub is not None and sub.status == sub_states.PAID_UNCLAIMED:
        sub_states.transition(
            owner_session, sub, sub_states.ONBOARDING,
            event_type="activated", salla_order_id=sub.salla_order_id,
        )

    # ONE welcome either way — the silenced buyer gets a different sentence,
    # never a second message. A new channel can never be silenced, so the
    # ordinary activation is bit-for-bit what it was.
    #
    # Sending at all to a silenced number is a considered call: this is a
    # DIRECT REPLY to a message he just sent us (the typed token, or the
    # inbound that claimed his order), about a purchase he just made, and it
    # is the only way he can ever learn why nothing arrives. It also opens no
    # new surface — this send already happened here, unconditionally, before
    # this change; only its words are new. Nothing proactive follows it: the
    # nightly run still refuses him, correctly, until he says the word.
    silenced = channel.opt_out_at is not None
    mid = whatsapp_client.send_text(
        from_phone, _WELCOME_OPTED_OUT if silenced else _WELCOME
    )
    record_out(owner_session, tenant_id=channel.tenant_id, channel_id=channel.id,
               kind="text", wa_message_id=mid, now=now)
    if silenced:
        _raise_opted_out_ticket(
            owner_session, channel=channel, ten_code=ten_code,
            admin_client=admin_client, now=now,
        )
    owner_session.commit()
    return ActivationResult(
        ActivationStatus.ACTIVATED, tenant_id=str(tok.tenant_id),
        channel_id=str(channel.id), subscription_id=str(tok.subscription_id),
        opted_out=silenced,
    )
