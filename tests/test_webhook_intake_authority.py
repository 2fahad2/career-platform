"""Two things `career.webhooks.intake` says about its rows, made true.

Both defects had the same shape — a name that promised more than the code
delivered — and both had grown a reader that ACTS on the promise, which is what
turned them from tidiness into risk.

* ``signature_valid`` was a column named as a verdict and written as the
  literal ``True``. What made it mean anything was that both existing callers
  verified first; `salla.provisioning._verified` now reads that column before
  it will let a payload deliver a live merchant credential to the credential
  store (§29).
* ``credential_is_still_consumable`` deferred the §12/§25 retention redaction
  of an unconsumed `app.store.authorize` body «while the credential in it can
  still be used», and measured that entirely from ``data.expires`` — a number
  Salla sends us. A body claiming the year 2100 was consumable for
  seventy-four years and never redacted.

The rest of the retention rule lives in
`tests/test_salla_tokens.py::TestTheRefusedCredentialSurvives`, and its first
test was written against the unbounded behaviour; the class docstring there
says so. This file holds the bound.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.webhooks import intake
from tests.test_salla_tokens import FAKE_ACCESS, authorize_payload

_INTAKE_SOURCE = pathlib.Path(intake.__file__)

#: A signed body that claims a credential alive until the year 2100. This is
#: the reviewer's own repro value and it is a real unix epoch, not a sentinel:
#: 4102444800 is 2100-01-01T00:00:00Z.
YEAR_2100 = 4102444800


def _authorize_event(
    *,
    age_days: float = 0.0,
    expires: object = None,
    status: str = "received",
    signature_valid: bool = True,
) -> WebhookEvent:
    """A detached row — every reader under test is pure, and no DB is needed
    to ask it a question."""
    return WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="app.store.authorize",
        event_fingerprint=f"test:{uuid.uuid4()}",
        signature_valid=signature_valid,
        payload=authorize_payload(expires=expires),
        processing_status=status,
        received_at=datetime.now(UTC) - timedelta(days=age_days),
    )


# ── the verdict that was a constant ─────────────────────────────────────────


def test_the_verdict_cannot_be_omitted() -> None:
    """The whole fix in one assertion: there is no default to inherit.

    Before this, `persist_deduped_event` took no such argument at all and
    wrote ``signature_valid=True`` into the INSERT unconditionally. A future
    intake that forgot to verify got a row marked verified for free. Now the
    call does not compile without an answer.
    """
    parameter = inspect.signature(intake.persist_deduped_event).parameters[
        "signature_valid"
    ]
    assert parameter.default is inspect.Parameter.empty, (
        "a default here re-creates the defect: the verdict becomes something a "
        "caller can inherit rather than something it has to know"
    )
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_insert_writes_the_answer_it_was_GIVEN() -> None:
    """Read out of the source, because «the column is no longer a literal» is
    a property of the INSERT and not of anything the function returns.

    A regression here looks like ``signature_valid=True`` reappearing in
    `.values(...)` while the argument stays in the signature, unused — which
    would pass every behavioural test in this file.
    """
    tree = ast.parse(_INTAKE_SOURCE.read_text(encoding="utf-8"))
    persist = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "persist_deduped_event"
    )
    written = [
        keyword.value for keyword in
        (kw for call in ast.walk(persist)
         if isinstance(call, ast.Call) for kw in call.keywords)
        if keyword.arg == "signature_valid"
    ]
    assert written, "the INSERT no longer sets signature_valid at all"
    for value in written:
        assert isinstance(value, ast.Name) and value.id == "signature_valid", (
            "the column is being written from a literal again — that is the "
            "defect, not a shorthand for it"
        )


def test_an_intake_that_forgets_to_verify_gets_no_row(
    owner_session: Session, clean_billing: None
) -> None:
    """The third intake, written by somebody who has not read the module.

    Before the fix this exact call — every argument the old signature took,
    and no verification anywhere — inserted a row that
    `provisioning._verified` would then accept as signed.
    """
    before = owner_session.execute(select(WebhookEvent.id)).scalars().all()
    with pytest.raises(TypeError):
        intake.persist_deduped_event(  # type: ignore[call-arg]
            owner_session, provider="salla",
            event_type="app.store.authorize",
            fingerprint=f"test:{uuid.uuid4()}",
            payload=authorize_payload(),
        )
    owner_session.rollback()
    after = owner_session.execute(select(WebhookEvent.id)).scalars().all()
    assert len(after) == len(before), "an unverified body reached the table"


def test_an_unverified_body_is_refused_rather_than_recorded(
    owner_session: Session, clean_billing: None
) -> None:
    """The other half of «the wrong thing is impossible»: a caller that DID
    verify, got «no», and persisted anyway.

    It is refused, not stored-as-false, because both endpoints already drop a
    forged body without a write (so a forged request cannot drive unbounded
    inserts) — and because the invariant the two readers of this column lean
    on is «every row in this table was verified», which a False row would end.
    """
    fingerprint = f"test:{uuid.uuid4()}"
    with pytest.raises(intake.UnverifiedWebhook):
        intake.persist_deduped_event(
            owner_session, provider="salla",
            event_type="app.store.authorize",
            fingerprint=fingerprint, payload=authorize_payload(),
            signature_valid=False,
        )
    owner_session.rollback()
    assert owner_session.execute(
        select(WebhookEvent.id).where(
            WebhookEvent.event_fingerprint == fingerprint
        )
    ).first() is None


def test_the_refusal_carries_no_payload(owner_session: Session) -> None:
    """§15.13. The body it is refusing is by definition one nobody
    authenticated, and it is the likeliest thing in the process to be hostile
    — so it does not travel into an exception message that ends up in a log."""
    secret = "0501234567"
    with pytest.raises(intake.UnverifiedWebhook) as raised:
        intake.persist_deduped_event(
            owner_session, provider="whatsapp", event_type="messages",
            fingerprint=f"test:{uuid.uuid4()}",
            payload={"data": {"customer": {"mobile": secret}}},
            signature_valid=False,
        )
    assert secret not in str(raised.value)


def test_a_verified_body_is_recorded_as_verified(
    owner_session: Session, clean_billing: None
) -> None:
    """The ordinary path still works, and the column still says True — the
    difference is that it now says True because a caller answered, not because
    the INSERT could not say anything else."""
    fingerprint = f"test:{uuid.uuid4()}"
    event_id = intake.persist_deduped_event(
        owner_session, provider="salla", event_type="app.store.authorize",
        fingerprint=fingerprint, payload=authorize_payload(),
        signature_valid=True,
    )
    assert event_id is not None
    stored = owner_session.execute(
        select(WebhookEvent).where(WebhookEvent.id == uuid.UUID(event_id))
    ).scalar_one()
    assert stored.signature_valid is True


def test_both_live_intakes_still_hand_over_their_answer() -> None:
    """The two callers named in the module docstring. A signature change that
    left one of them on the old shape would be a TypeError in production and
    green here — unless the call is read."""
    for module in ("career.salla.webhook", "career.whatsapp.webhook"):
        source = pathlib.Path(
            __import__(module, fromlist=["_"]).__file__ or ""
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "persist_deduped_event"
        ]
        assert calls, f"{module} no longer persists through the one authority"
        for call in calls:
            assert "signature_valid" in {kw.arg for kw in call.keywords}, module


# ── the retention deadline that was somebody else's number ──────────────────


def test_a_year_2100_expiry_is_not_consumable_forever() -> None:
    """THE DEFECT, at the value the reviewer verified it with.

    Before the ceiling, this row answered True — and would have gone on
    answering True until the year 2100, so `prune_webhook_payloads` deferred
    it every single night and the body was never redacted.
    """
    forever = _authorize_event(age_days=40, expires=YEAR_2100)
    assert intake.credential_is_still_consumable(
        forever, now=datetime.now(UTC)
    ) is False


def test_a_year_2100_body_is_redacted_on_the_ordinary_schedule(
    owner_session: Session, clean_billing: None
) -> None:
    """And the sweep actually does it — the promise is about the body, so the
    proof has to be that the body is gone.

    It is counted as `expired_credentials` and not merely `redacted`: what
    happened is «a store's install was never connected and now cannot be»,
    which is a different sentence to the operator from «an old body expired».
    """
    event = _authorize_event(
        age_days=intake.RAW_PAYLOAD_RETENTION_DAYS + 1, expires=YEAR_2100
    )
    owner_session.add(event)
    owner_session.commit()

    counts = intake.prune_webhook_payloads(owner_session, now=datetime.now(UTC))
    owner_session.commit()
    owner_session.expire_all()

    stored = owner_session.execute(
        select(WebhookEvent).where(WebhookEvent.id == event.id)
    ).scalar_one()
    assert FAKE_ACCESS not in str(stored.payload)
    assert stored.payload["_redacted"] is True
    assert stored.payload_redacted_at is not None
    assert counts["expired_credentials"] >= 1
    assert counts["deferred_credentials"] == 0
    # the row itself survives — it is the idempotency record (§09)
    assert stored.event_fingerprint is not None


def test_the_ceiling_is_measured_from_the_clock_we_own() -> None:
    """`received_at` is written by our database when the body lands;
    `data.expires` is written by Salla. The same impossible payload is
    consumable while the row is young and is not once the ceiling has passed,
    which is what «bounded» means — the answer moves on OUR clock."""
    now = datetime.now(UTC)
    young = _authorize_event(age_days=3, expires=YEAR_2100)
    old = _authorize_event(
        age_days=intake._AUTHORIZE_MAX_LIFE_DAYS + 1, expires=YEAR_2100
    )
    assert intake.credential_is_still_consumable(young, now=now) is True
    assert intake.credential_is_still_consumable(old, now=now) is False


def test_the_payload_can_only_ever_shorten_the_answer() -> None:
    """The ceiling is a `min`, never a replacement. A credential Salla says
    dies tomorrow dies tomorrow — the bound is an upper one, so it can never
    extend a body's life, only cap it. That is also the §12/§25 argument: a
    change that can only shorten how long a raw body survives cannot weaken a
    retention promise."""
    now = datetime.now(UTC)
    for expires in (
        YEAR_2100,                                   # far beyond the ceiling
        int((now + timedelta(days=1)).timestamp()),  # tomorrow
        int((now - timedelta(days=1)).timestamp()),  # already dead
        None,                                        # a live Salla default
        "not-a-timestamp",                           # unreadable
    ):
        event = _authorize_event(age_days=2, expires=expires)
        ceiling = intake._as_utc(event.received_at) + timedelta(
            days=intake._AUTHORIZE_MAX_LIFE_DAYS
        )
        assert intake._authorize_usable_until(
            event, event.payload["data"]
        ) <= ceiling


def test_an_absent_expiry_still_falls_back_to_the_same_number() -> None:
    """The constant was already the fallback for a payload with no readable
    `data.expires`; it is now also the ceiling for one that has it. Same
    number, same reason — the longest life Salla has ever published — so
    there is no second figure to keep in step."""
    payload = authorize_payload()
    del payload["data"]["expires"]
    event = _authorize_event(age_days=2)
    event.payload = payload
    assert intake._authorize_usable_until(event, payload["data"]) == (
        intake._as_utc(event.received_at)
        + timedelta(days=intake._AUTHORIZE_MAX_LIFE_DAYS)
    )


def test_an_honest_salla_payload_is_unaffected() -> None:
    """What a real delivery looks like: fourteen days, from today. The
    ceiling is the same fourteen days from `received_at`, so nothing about the
    live path changes — which is the point. The only bodies the bound touches
    are the ones claiming a life Salla does not issue."""
    now = datetime.now(UTC)
    fresh = _authorize_event(
        age_days=0, expires=int((now + timedelta(days=14)).timestamp())
    )
    assert intake.credential_is_still_consumable(fresh, now=now) is True
    dead = _authorize_event(
        age_days=15, expires=int((now - timedelta(days=1)).timestamp())
    )
    assert intake.credential_is_still_consumable(dead, now=now) is False


def test_the_ceiling_never_reaches_a_body_with_a_customer_in_it(
    owner_session: Session, clean_billing: None
) -> None:
    """§25, restated against the CHANGED code. The deferral and its ceiling
    are keyed on the event type, so neither can touch a body carrying a name,
    a mobile or an email — a customer body expires on the ordinary clock, and
    the only thing this fix could do to it is nothing."""
    order = WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="order.created",
        event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
        payload={"event": "order.created",
                 "data": {"customer": {"mobile": "0501234567"},
                          "access_token": FAKE_ACCESS,
                          "expires": YEAR_2100}},
        processing_status="received",
        received_at=datetime.now(UTC) - timedelta(
            days=intake.RAW_PAYLOAD_RETENTION_DAYS + 1
        ),
    )
    owner_session.add(order)
    owner_session.commit()
    assert intake.credential_is_still_consumable(
        order, now=datetime.now(UTC)
    ) is False

    counts = intake.prune_webhook_payloads(owner_session, now=datetime.now(UTC))
    owner_session.commit()
    owner_session.expire_all()

    stored = owner_session.execute(
        select(WebhookEvent).where(WebhookEvent.id == order.id)
    ).scalar_one()
    assert "0501234567" not in str(stored.payload)
    assert counts["deferred_credentials"] == 0
    assert intake.RAW_PAYLOAD_RETENTION_DAYS == 30      # §12 window untouched
