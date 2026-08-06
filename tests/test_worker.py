"""Constant 11 on the LIVE path — the worker never trusts the payload (DB).

`career/worker/worker.py` used to hold this guarantee: a queue message claimed
a tenant, and the worker re-verified that claim from the database before doing
any work. That module had no caller and never did (DEVIATIONS D24), so the
guarantee it carried lived only in its own tests, which is the most convincing
way for an invariant to be absent.

The live path answers the same constant, and answers it more strongly: there
is no tenant claim to verify. Every entry point RESOLVES the tenant from the
database — a phone number against `customer_channels`, a Salla order against
`app.tenant_for_salla_order`, tonight's list against
`app.tenant_ids_with_status` — so a payload field naming another tenant is not
rejected, it is never read. That is a stronger property than a check, and a
quieter one: nothing fails loudly if someone later starts trusting a payload
field, which is exactly why it is asserted here.

These tests attack the WhatsApp worker, because that is the one entry point
whose payload is written by an outside party on every request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.salla.client import FakeSallaClient, SallaOrder
from career.salla.provisioning import provision_order
from career.telegram.admin import FakeTelegramAdminClient
from career.whatsapp.client import FakeWhatsAppClient
from career.whatsapp.worker import process_pending_whatsapp

NOW = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
CATALOG = {"prod_pro": "professional"}


def _phone() -> str:
    return f"+96650{uuid.uuid4().int % 10_000_000:07d}"


def _provision_token(owner_session: Session) -> str:
    order_id = f"ORD-{uuid.uuid4()}"
    client = FakeSallaClient({
        order_id: SallaOrder(order_id, "paid", "prod_pro", Decimal("279.00"),
                             "SAR", customer_phone=_phone()),
    })
    pricing = {k: (Decimal("279.00"), "SAR") for k in CATALOG}
    token = provision_order(
        owner_session, order_id, salla_client=client,
        product_catalog=CATALOG, expected_pricing=pricing,
    ).activation_token
    assert token is not None
    return token


def _insert_event(owner_session: Session, payload: dict[str, Any]) -> None:
    owner_session.add(WebhookEvent(
        id=uuid.uuid4(), provider="whatsapp", event_type="messages",
        event_fingerprint=f"wa:{uuid.uuid4()}", signature_valid=True,
        payload=payload, processing_status="received",
    ))
    owner_session.commit()


def _payload(messages: list[dict[str, Any]], **forged: Any) -> dict[str, Any]:
    """A Meta webhook body with a tenant claim smuggled into it at every level
    an implementation might reach for. Meta sends none of these; that is the
    point — they are what an attacker (or a careless refactor) would add."""
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "messages": messages, "statuses": [],
    }
    value.update(forged)
    return {"entry": [{"changes": [{"value": value}], **forged}], **forged}


def _text_msg(wamid: str, phone: str, body: str, **forged: Any) -> dict[str, Any]:
    msg = {"id": wamid, "from": phone, "type": "text", "text": {"body": body}}
    msg.update(forged)
    return msg


def _run(owner_session: Session) -> dict[str, int]:
    return process_pending_whatsapp(
        owner_session, whatsapp_client=FakeWhatsAppClient(),
        admin_client=FakeTelegramAdminClient(), now=NOW,
    )


def _activate(owner_session: Session) -> tuple[str, str]:
    """Take a real buyer through activation and return (phone, tenant_id)."""
    token = _provision_token(owner_session)
    phone = _phone()
    _insert_event(owner_session, _payload(
        [_text_msg(f"wamid-{uuid.uuid4()}", phone, f"تفعيل {token}")]))
    _run(owner_session)
    tenant_id = owner_session.execute(
        text("SELECT tenant_id::text FROM customer_channels WHERE phone_e164 = :p"),
        {"p": phone},
    ).scalar_one()
    return phone, str(tenant_id)


def test_the_tenant_comes_from_the_phone_not_from_the_payload(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """Two paying customers. A message arrives from A's phone carrying B's
    tenant id in five different fields. Everything the worker writes must
    belong to A."""
    phone_a, tenant_a = _activate(owner_session)
    _phone_b, tenant_b = _activate(owner_session)
    assert tenant_a != tenant_b

    def rows_for(tenant_id: str) -> int:
        with Session(owner_engine) as s:
            return s.execute(
                text("SELECT count(*) FROM inbound_messages"
                     " WHERE tenant_id = :t"), {"t": tenant_id},
            ).scalar_one()

    before_b = rows_for(tenant_b)      # B's own activation message
    wamid = f"wamid-{uuid.uuid4()}"
    _insert_event(owner_session, _payload(
        [_text_msg(wamid, phone_a, "السلام عليكم",
                   tenant_id=tenant_b, metadata={"tenant_id": tenant_b})],
        tenant_id=tenant_b,
    ))
    assert _run(owner_session)["messages"] == 1

    with Session(owner_engine) as s:
        owner = s.execute(
            text("SELECT tenant_id::text FROM inbound_messages"
                 " WHERE wa_message_id = :w"), {"w": wamid},
        ).scalar_one()
    assert owner == tenant_a, "the payload's tenant claim was believed"
    assert rows_for(tenant_b) == before_b, (
        "tenant B gained a row from tenant A's message"
    )


def test_an_unknown_phone_is_attributed_to_nobody(
    owner_session: Session, owner_engine: Engine, clean_billing: None
) -> None:
    """The other half, and the one a check-based worker gets wrong more often:
    when there is no tenant to resolve, the answer is «nothing is written»,
    not «use what the payload said». A forged claim here is the cheapest
    possible attack — no purchase, no activation, just a POST."""
    _phone_b, tenant_b = _activate(owner_session)

    wamid = f"wamid-{uuid.uuid4()}"
    _insert_event(owner_session, _payload(
        [_text_msg(wamid, _phone(), "ابدأ", tenant_id=tenant_b)],
        tenant_id=tenant_b,
    ))
    _run(owner_session)

    with Session(owner_engine) as s:
        recorded = s.execute(
            text("SELECT count(*) FROM inbound_messages WHERE wa_message_id = :w"),
            {"w": wamid},
        ).scalar_one()
        for table in ("inbound_messages", "customer_channels"):
            assert s.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": tenant_b},
            ).scalar_one() <= 1, table          # 1 = B's own activation row
    assert recorded == 0


def test_no_live_entry_point_reads_a_tenant_out_of_a_payload() -> None:
    """A ratchet, not a proof — and the reason the two tests above are not
    enough on their own. They demonstrate today's behaviour; this one fails
    the day someone adds `payload["tenant_id"]` to a live routing path, which
    is the change that would silently undo them.
    """
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "career"
    entry_points = (
        src / "whatsapp" / "worker.py",
        src / "salla" / "provisioning.py",
        src / "webhooks" / "intake.py",
        src / "cv" / "daily_run.py",
    )
    # a tenant id lifted out of any dict-ish thing whose name is the request
    pattern = re.compile(
        r"(payload|body|value|message|event|entry|data)\s*"
        r"(\.get\(\s*[\"']tenant_id|\[\s*[\"']tenant_id)"
    )
    for path in entry_points:
        offenders = [
            f"{path.name}:{n}: {line.strip()}"
            for n, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1)
            if pattern.search(line)
        ]
        assert not offenders, (
            "Constant 11: a live entry point is taking a tenant id from the "
            f"request instead of resolving it from the database — {offenders}"
        )
