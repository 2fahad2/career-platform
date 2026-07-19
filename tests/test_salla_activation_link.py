"""The §09 activation deep link: wa.me format + round-trip through the
inbound classifier (the pre-filled text must be recognized as activation)."""

from __future__ import annotations

from career.salla.activation_link import activation_message, build_activation_link
from career.whatsapp.inbound import InboundKind, classify_inbound


def test_link_is_digits_only_wa_me_with_prefilled_activation() -> None:
    token = "abc123def456ghi789jkl"          # ≥20 chars per the classifier
    link = build_activation_link(
        whatsapp_number_e164="+1 555-159-4303", token=token
    )
    assert link.startswith("https://wa.me/15551594303?text=")
    assert "+" not in link.split("?")[0]      # number is digits only


def test_prefilled_text_round_trips_through_the_classifier() -> None:
    token = "abc123def456ghi789jkl"
    kind, extracted = classify_inbound(activation_message(token))
    assert kind is InboundKind.ACTIVATION
    assert extracted == token
