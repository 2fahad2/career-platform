"""Activation §08 — the customer who PAID while our messages were switched off.

THE DEFECT this file exists for. ``_activate_with_token`` has two branches: a
brand-new channel, and a channel that already exists (the §04 funnel→لمّاح
upgrade deliberately reuses it, and so does its mirror, a subscriber buying the
29-riyal analysis). The existing-channel branch refreshed ``verified_at``,
``opt_in_at``, ``last_inbound_at`` and ``subscription_id`` — and left
``opt_out_at`` exactly where it was. A customer who had once sent «إيقاف» and
then bought therefore ended up ACTIVE, welcomed, billed, and permanently
silent:

    channel.opt_out_at is not None
      → window.window_state(...)          → WindowState.OPTED_OUT
      → adaptive.plan_delivery(...)       → DeliveryAction.SKIP_OPTED_OUT
      → delivery.deliver_adaptive         → status DELIVERY_NO_SEND
      → cv.daily_run._run_tenant          → close.close_skipped_opted_out
      → tenant_day_states                 → "SKIPPED_OPTED_OUT"
      → engine.cli.HONEST_DAY_STATES      → exit code 0, no alert fires

Every night, forever, and the night's verdict is «healthy» — because
SKIPPED_OPTED_OUT is an honest state when the customer chose it. Here he did
not choose it for THIS purchase; he chose it before paying, and nobody ever
told him the two were connected. :func:`test_the_night_stays_quiet_about_him`
pins that chain so the fix cannot be mistaken for a cure of it: the chain is
correct, the customer's silence is real, and what was missing was that anyone
be TOLD.

THE FIX IS NOT TO CLEAR THE COLUMN. An opt-out is a standing compliance
instruction, not a preference that money overrides — and clearing it here is
precisely the write ``test_promises`` now forbids structurally (every write to
``opt_out_at`` must sit beside a classified `stop`/`resume` inbound row,
because ``promises/guarantee._silence_inside`` reads the customer's silence out
of ``inbound_messages``). Satisfying that ratchet from activation would mean
manufacturing a `resume` row for a message the customer never sent: a forged
consent, in the one table the refund decision is read from.
:func:`test_the_standing_instruction_survives_the_purchase` holds that line.

So: he is TOLD (one word restores him, and it is the word the parser really
accepts), and the operator is TOLD (an open ticket on the tickets screen plus
one page) — once, not once per retry.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.engine import cli
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp import activation_flow as flow
from career.whatsapp.adaptive import DeliveryAction, plan_delivery
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.inbound import InboundKind, classify_inbound
from career.whatsapp.window import WindowState, window_state
from career.whatsapp.worker import _handle_message

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9]")


def _probe_phone() -> str:
    """A real Salla order ALWAYS carries the buyer's mobile — the zero-touch
    claim keys on it. An order without one describes a shape the world never
    sends."""
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _provision(owner: Session, *, product: str, plan: str, price: str) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", product, Decimal(price), "SAR",
                             customer_phone=_probe_phone())
    })
    result = provision_order(
        owner, order_id, salla_client=client,
        product_catalog={product: plan},
        expected_pricing={product: (Decimal(price), "SAR")},
    )
    assert result.activation_token is not None
    return result.activation_token


def _provision_funnel(owner: Session) -> str:
    return _provision(owner, product="prod_cv", plan="cv_analysis", price="29")


def _provision_subscription(owner: Session) -> str:
    return _provision(owner, product="prod_basic", plan="basic", price="149")


def _activate(owner: Session, token: str, phone: str, wa, admin, *, minutes: int = 0):
    return flow.activate(
        owner, token=token, from_phone=phone, display_name=None,
        now=NOW + timedelta(minutes=minutes), whatsapp_client=wa, admin_client=admin,
    )


def _say_stop(owner: Session, phone: str, wa, admin, *, minutes: int) -> None:
    """The opt-out through the REAL worker — so ``opt_out_at`` and the
    classified `stop` row are written by the one authority that may write
    them, and this test never touches the column it is arguing about.

    ``from`` carries the phone EXACTLY as the channel holds it, because
    ``worker._channel_for_phone`` compares it exactly (``phone_e164 ==
    phone``) while ``activation_flow._channel_for_phone`` and
    ``worker._event_ten_codes`` both go through ``phone_variants``. Nothing
    here depends on that divergence and nothing here hides it — it is reported
    to the worker's owner."""
    _handle_message(
        owner,
        {"id": f"wamid.{uuid.uuid4().hex}", "from": phone,
         "type": "text", "text": {"body": "إيقاف الرسائل"}},
        whatsapp_client=wa, admin_client=admin, now=NOW + timedelta(minutes=minutes),
    )


def _channel(owner: Session, phone: str):
    return owner.execute(
        sql_text("SELECT id, tenant_id, opt_out_at, last_inbound_at"
                 " FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).one()


def _tickets(owner: Session, channel_id) -> list:
    """EVERY ticket on the channel, deliberately unfiltered by kind: the
    ordinary-case tests below have to be runnable against the code as it was
    BEFORE this change, and a helper that names the new constant could not be."""
    return owner.execute(
        sql_text("SELECT kind, status, tenant_id FROM support_events"
                 " WHERE channel_id = :c"),
        {"c": str(channel_id)},
    ).all()


def _bodies(wa: FakeWhatsAppClient) -> list[str]:
    return [m.body or "" for m in wa.sent if m.kind == "text"]


def _opted_out_pages(admin: FakeTelegramAdminClient) -> list[str]:
    """Matched on the alert's own first line rather than a phrase, so the
    count cannot quietly start including somebody else's message about
    opt-outs (whatsapp/worker pages the operator on «إيقاف» too)."""
    return [
        m for m in admin.messages
        if m.splitlines()[:1] == [flow._OPTED_OUT_ALERT_HEAD]
    ]


def _assert_direction_pure(text: str) -> None:
    """§13/Fahad's client scrambles a line that mixes scripts. Every line is
    Arabic or Latin, never both."""
    for line in text.splitlines():
        assert not (_ARABIC.search(line) and _LATIN_OR_DIGIT.search(line)), (
            f"mixed-direction line: {line!r}"
        )


# ── the ordinary case, which must not move at all ───────────────────────────


def test_the_customer_who_never_opted_out_sees_exactly_what_he_saw_before(
    owner_session: Session, clean_billing: None
) -> None:
    """The whole risk of this change is here: 99 activations in 100 are a
    fresh channel, and they must be byte-identical to the old behaviour — one
    text, the old welcome, no ticket, and NOTHING extra on the admin channel."""
    token = _provision_subscription(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()

    result = _activate(owner_session, token, phone, wa, admin)

    assert result.status is flow.ActivationStatus.ACTIVATED
    assert _bodies(wa) == [flow._WELCOME]        # the same one text, verbatim
    assert admin.messages == []                  # the operator hears nothing
    channel = _channel(owner_session, phone)
    assert channel.opt_out_at is None
    assert _tickets(owner_session, channel.id) == []


def test_an_upgrade_by_a_customer_who_never_opted_out_is_untouched_too(
    owner_session: Session, clean_billing: None
) -> None:
    """The OTHER ordinary case, and the one that shares the changed branch:
    the §04 upgrade re-links an existing channel. A customer who never said
    «إيقاف» must still get the plain welcome and raise no ticket."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)

    upgrade_token = _provision_subscription(owner_session)
    result = _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)

    assert result.status is flow.ActivationStatus.ACTIVATED
    assert _bodies(wa) == [flow._WELCOME, flow._WELCOME]
    assert all("ترقية" in m for m in admin.messages)   # the upgrade note only
    channel = _channel(owner_session, phone)
    assert _tickets(owner_session, channel.id) == []


# ── the chain: why nobody ever found out ────────────────────────────────────


def test_the_night_stays_quiet_about_him(
    owner_session: Session, clean_billing: None
) -> None:
    """Not a fix — a pin. After the upgrade the channel is silenced, and every
    link downstream behaves correctly all the way to an exit code of 0."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)

    upgrade_token = _provision_subscription(owner_session)
    result = _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)
    assert result.status is flow.ActivationStatus.ACTIVATED

    channel = _channel(owner_session, phone)
    state = window_state(
        last_inbound_at=channel.last_inbound_at, opt_out_at=channel.opt_out_at,
        now=NOW + timedelta(hours=25),
    )
    assert state is WindowState.OPTED_OUT
    assert plan_delivery(state) is DeliveryAction.SKIP_OPTED_OUT
    # …and the day that closes on that skip is one of the states the runner
    # calls healthy, so nothing anywhere raises its voice.
    assert "SKIPPED_OPTED_OUT" in cli.HONEST_DAY_STATES
    assert "SKIPPED_OPTED_OUT" not in cli.FAILED_DAY_STATES
    assert cli.exit_code_for("completed", ["SKIPPED_OPTED_OUT"]) == 0


# ── what the customer is told ───────────────────────────────────────────────


def test_the_paying_silenced_customer_is_told_and_given_the_way_back(
    owner_session: Session, clean_billing: None
) -> None:
    """He just paid and is about to receive nothing. The welcome he gets says
    so, and the word it teaches is a word the parser really accepts — the copy
    and the classifier are asserted against each other, not trusted to agree."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    before = len(wa.sent)

    upgrade_token = _provision_subscription(owner_session)
    _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)

    said = [m.body or "" for m in wa.sent[before:] if m.kind == "text"]
    assert said == [flow._WELCOME_OPTED_OUT]     # ONE message, not two
    assert flow._WELCOME not in said             # and never the false one
    _assert_direction_pure(flow._WELCOME_OPTED_OUT)
    # The last line is the word, alone — RESUME only matches a whole message.
    word = flow._WELCOME_OPTED_OUT.splitlines()[-1]
    kind, _token = classify_inbound(word)
    assert kind is InboundKind.RESUME, (
        f"the copy teaches {word!r}, which whatsapp.inbound does not read as a "
        "resume — the customer would type it and stay silenced"
    )
    # No human is promised anywhere in it.
    assert "نتواصل" not in flow._WELCOME_OPTED_OUT
    assert "نكلمك" not in flow._WELCOME_OPTED_OUT


def test_the_standing_instruction_survives_the_purchase(
    owner_session: Session, clean_billing: None
) -> None:
    """The write we refused. Activation must NOT clear ``opt_out_at`` and must
    NOT forge the `resume` row that clearing it would require: the customer's
    own history in ``inbound_messages`` is what the start guarantee reads, and
    a resume he never sent is a false fact in the table a refund is decided
    from."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    silenced_at = _channel(owner_session, phone).opt_out_at

    upgrade_token = _provision_subscription(owner_session)
    _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)

    channel = _channel(owner_session, phone)
    assert channel.opt_out_at == silenced_at     # untouched, to the microsecond
    resumes = owner_session.execute(
        sql_text("SELECT count(*) FROM inbound_messages WHERE channel_id = :c"
                 " AND classification = 'resume'"),
        {"c": str(channel.id)},
    ).scalar_one()
    assert resumes == 0


# ── what the operator is told ───────────────────────────────────────────────


def test_the_operator_gets_a_ticket_and_a_page_that_names_no_phone(
    owner_session: Session, clean_billing: None
) -> None:
    """Money already taken against a service that cannot be delivered belongs
    on a screen, not only in a log: an OPEN row in ``support_events``, which is
    what the console's tickets screen reads. The page beside it carries the TEN
    code and nothing else (§15.13)."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    assert _opted_out_pages(admin) == []

    upgrade_token = _provision_subscription(owner_session)
    result = _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)

    channel = _channel(owner_session, phone)
    tickets = _tickets(owner_session, channel.id)
    assert len(tickets) == 1
    assert tickets[0].kind == flow.OPTED_OUT_TICKET_KIND
    assert tickets[0].status == "open"           # the screen filters on this
    assert str(tickets[0].tenant_id) == str(channel.tenant_id) == result.tenant_id

    paged = _opted_out_pages(admin)
    assert len(paged) == 1
    code = owner_session.execute(
        sql_text("SELECT code FROM tenants WHERE id = :t"), {"t": result.tenant_id},
    ).scalar_one()
    assert code in paged[0]
    _assert_direction_pure(paged[0])
    for message in admin.messages:               # §15.13, the whole channel
        assert phone not in message
        assert phone.lstrip("+") not in message


# ── idempotency: retries must not repeat either half ────────────────────────


def test_a_retried_activation_repeats_neither_the_notice_nor_the_ticket(
    owner_session: Session, clean_billing: None
) -> None:
    """The same token typed twice (a customer who taps the link again, or the
    worker replaying an inbound) is ALREADY_LINKED: it says nothing new and
    raises nothing new."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    upgrade_token = _provision_subscription(owner_session)
    _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)
    sent, paged = len(wa.sent), len(admin.messages)

    again = _activate(owner_session, upgrade_token, phone, wa, admin, minutes=61)

    assert again.status is flow.ActivationStatus.ALREADY_LINKED
    assert len(wa.sent) == sent                  # the customer hears nothing
    assert len(admin.messages) == paged          # the operator is not re-paged
    assert len(_tickets(owner_session, _channel(owner_session, phone).id)) == 1


def test_a_second_purchase_in_the_same_silence_raises_no_second_ticket(
    owner_session: Session, clean_billing: None
) -> None:
    """A different retry shape: the §04 mirror — a subscriber buying the
    analysis — runs activation AGAIN on the same silenced channel. He is told
    about the purchase he just made (it is his welcome, and the true one), but
    the operator's queue does not grow a duplicate of a ticket he already has
    open."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    upgrade_token = _provision_subscription(owner_session)
    _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)
    assert len(_opted_out_pages(admin)) == 1

    second_analysis = _provision_funnel(owner_session)
    result = _activate(owner_session, second_analysis, phone, wa, admin, minutes=120)

    assert result.status is flow.ActivationStatus.ACTIVATED
    channel = _channel(owner_session, phone)
    assert len(_tickets(owner_session, channel.id)) == 1
    assert len(_opted_out_pages(admin)) == 1
    assert _bodies(wa)[-1] == flow._WELCOME_OPTED_OUT


def test_a_new_silence_after_the_ticket_was_closed_pages_again(
    owner_session: Session, clean_billing: None
) -> None:
    """The other direction, so the dedup cannot become a mute button: once the
    operator has closed the ticket, a customer who is STILL silent and pays
    again is a fact he has to hear a second time."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    _activate(owner_session, funnel_token, phone, wa, admin)
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    upgrade_token = _provision_subscription(owner_session)
    _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)
    channel = _channel(owner_session, phone)
    owner_session.execute(
        sql_text("UPDATE support_events SET status = 'resolved', resolved_at = now()"
                 " WHERE channel_id = :c AND kind = :k"),
        {"c": str(channel.id), "k": flow.OPTED_OUT_TICKET_KIND},
    )
    owner_session.commit()

    second_analysis = _provision_funnel(owner_session)
    _activate(owner_session, second_analysis, phone, wa, admin, minutes=120)

    tickets = _tickets(owner_session, channel.id)
    assert len(tickets) == 2
    assert sorted(t.status for t in tickets) == ["open", "resolved"]


def test_the_result_carries_the_silence_to_the_caller(
    owner_session: Session, clean_billing: None
) -> None:
    """The half this module cannot do itself. ``worker._finish_activation``
    runs straight into the funnel or the onboarding, which then hold a long
    conversation with a customer who asked for silence — and bury the one line
    that explains why nothing will arrive. That decision is the worker's; the
    FACT is ours, so it leaves here on the result instead of being rediscovered
    with another query."""
    funnel_token = _provision_funnel(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    plain = _activate(owner_session, funnel_token, phone, wa, admin)
    assert plain.opted_out is False              # the ordinary case says so

    _say_stop(owner_session, phone, wa, admin, minutes=30)
    upgrade_token = _provision_subscription(owner_session)
    silenced = _activate(owner_session, upgrade_token, phone, wa, admin, minutes=60)
    assert silenced.opted_out is True

    retry = _activate(owner_session, upgrade_token, phone, wa, admin, minutes=61)
    assert retry.status is flow.ActivationStatus.ALREADY_LINKED
    assert retry.opted_out is True               # still true, still silent


def test_the_ticket_kind_fits_the_column_and_is_new(
    owner_session: Session, clean_billing: None
) -> None:
    """``support_events.kind`` is ``String(32)`` and the console keys its
    Arabic labels off these literals — a kind that collides with an existing
    one would put this ticket under somebody else's sentence."""
    from career.funnel.flow import CONSENT_STUCK_KIND

    assert len(flow.OPTED_OUT_TICKET_KIND) <= 32
    assert flow.OPTED_OUT_TICKET_KIND not in {"support_request", CONSENT_STUCK_KIND}


# ── D2.4: the silence that arrived AFTER the activation ─────────────────────


def test_a_link_re_issued_after_he_went_quiet_still_raises_the_ticket(
    owner_session: Session, clean_billing: None
) -> None:
    """The ALREADY_LINKED early return used to claim it was «the RETRY of an
    activation that already spoke and already ticketed». That is only true when
    the opt-out came FIRST. Here it came after: the activation was an ordinary
    one, the customer silenced us the next day, and then he taps his
    thank-you-page link again — which is exactly what a customer does when
    nothing is arriving, and the only door he has left
    (``issue_activation_link`` refuses an account that is already activated).
    It reported opted_out=True and raised nothing at all: the one moment the
    system looks straight at the silenced channel of a paying customer and
    says nothing about it."""
    token = _provision_subscription(owner_session)
    phone = _phone()
    wa, admin = FakeWhatsAppClient(), FakeTelegramAdminClient()
    first = _activate(owner_session, token, phone, wa, admin)
    assert first.status is flow.ActivationStatus.ACTIVATED
    assert first.opted_out is False               # nothing to raise, yet
    _say_stop(owner_session, phone, wa, admin, minutes=30)
    channel = _channel(owner_session, phone)
    assert _tickets(owner_session, channel.id) == []
    sent = len(wa.sent)

    again = _activate(owner_session, token, phone, wa, admin, minutes=60)

    assert again.status is flow.ActivationStatus.ALREADY_LINKED
    assert again.opted_out is True
    tickets = _tickets(owner_session, channel.id)
    assert [(t.kind, t.status) for t in tickets] == [
        (flow.OPTED_OUT_TICKET_KIND, "open")
    ], f"the re-issued link saw the silence and raised nothing: {tickets}"
    paged = _opted_out_pages(admin)
    assert len(paged) == 1
    _assert_direction_pure(paged[0])
    # He is NOT re-announced to: he was welcomed once, and he is the one who
    # asked for quiet afterwards. The operator carries this from here.
    assert len(wa.sent) == sent


def test_the_funnel_upgrade_notice_does_not_reverse_for_the_operator() -> None:
    """«↻ ترقية من قمع التحليل: TEN-0007 → TEN-0002» was one Arabic line
    holding two Latin codes and an arrow between them — and the arrow's whole
    meaning is the ORDER of its two halves, which is exactly what a reversed
    line destroys. Green counterpart to the tree-wide bidi guard, which is red
    for other owners' files; see the same test in `test_whatsapp_worker`.
    """
    from tests.test_alert_direction_purity import SLOT, verdict

    hits = verdict().get("src/career/whatsapp/activation_flow.py", [])
    assert not hits, "mixed-direction operator line(s) — " + " ; ".join(
        f"L{n}: {line.replace(SLOT, '{…}')!r}" for n, line in hits
    )
