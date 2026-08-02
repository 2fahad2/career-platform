"""The activation deep link (whitepaper §09).

After a paid order provisions, the buyer must be able to start on WhatsApp
with a single tap: the thank-you page shows a button opening WhatsApp with
the activation message pre-filled («تفعيل <token>»). The inbound classifier
recognizes exactly that phrasing, so the link closes the purchase→activation
loop without the customer ever copying a code by hand.
"""

from __future__ import annotations

from urllib.parse import quote

#: The pre-filled message — MUST match whatsapp/inbound.py's _ACTIVATION_RE.
ACTIVATION_PREFIX = "تفعيل"


def activation_message(token: str) -> str:
    return f"{ACTIVATION_PREFIX} {token}"


def build_activation_link(*, whatsapp_number_e164: str, token: str) -> str:
    """A wa.me deep link that pre-fills the activation message. The number is
    digits-only per wa.me (a leading '+' or spaces break the link)."""
    digits = "".join(ch for ch in whatsapp_number_e164 if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(activation_message(token))}"


def normalize_order_phone(raw: str | None) -> str | None:
    """Salla order phones arrive as ``+9665…``, ``9665…``, ``05…`` or
    ``00966…`` — normalize to ``+E164`` or None when unusable. Conservative:
    anything that doesn't look like a full international or Saudi local
    number is dropped (the token path still covers those buyers)."""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("05") and len(digits) == 10:   # Saudi local mobile
        digits = "966" + digits[1:]
    elif digits.startswith("5") and len(digits) == 9:
        # A bare Saudi mobile with neither the country code nor the leading
        # zero — the shape Salla's `mobile` field carries when its country
        # code sits in `mobile_code`. Safe to assume Saudi here: this product
        # sells only in Saudi Arabia and every plan is priced in riyals.
        digits = "966" + digits
    if len(digits) < 11 or len(digits) > 15:
        return None
    return f"+{digits}"
