"""Activation deep link (whatsapp §08).

The Salla thank-you page shows a button that opens WhatsApp to OUR number with a
prefilled message "تفعيل <token>". When the customer sends it, the inbound worker
proves the number belongs to the paying order (links order ↔ number ↔ customer).
"""

from __future__ import annotations

from urllib.parse import quote

ACTIVATION_PREFIX = "تفعيل"


def activation_message(token: str) -> str:
    return f"{ACTIVATION_PREFIX} {token}"


def activation_link(our_number_e164: str, token: str) -> str:
    """wa.me deep link opening a chat to our WABA number with the token prefilled."""
    number = our_number_e164.lstrip("+")
    return f"https://wa.me/{number}?text={quote(activation_message(token))}"
