"""Inbound message classification (whatsapp §08) — pure.

Classifies a customer's message into ACTIVATION / STOP / SUPPORT / OTHER and
extracts the activation token. STOP and SUPPORT match the message EXACTLY (after
normalization) — never a loose "contains" — so ordinary messages mentioning a
word are not misread as commands. Activation requires the explicit keyword +
token so a random string is never treated as a token.
"""

from __future__ import annotations

import re
from enum import StrEnum


class InboundKind(StrEnum):
    ACTIVATION = "activation"
    STOP = "stop"
    RESUME = "resume"
    SUPPORT = "support"
    OTHER = "other"


# Opt-out phrases (Meta requires honoring STOP). Exact match after normalization.
_STOP_PHRASES = frozenset({
    "stop", "unsubscribe",
    "إيقاف", "ايقاف", "إيقاف الرسائل", "ايقاف الرسائل",
    "الغاء", "إلغاء", "الغاء الاشتراك", "إلغاء الاشتراك", "توقف",
})
#: The way BACK. Nothing anywhere cleared ``opt_out_at`` — a customer who
#: stopped messages was silenced permanently while their subscription (and
#: their billing) carried on, and the confirmation told them to send «دعم»,
#: which does not clear it either. Meta's own convention is STOP/START, so
#: both the English and the Arabic a Saudi customer would actually type are
#: accepted, plus «استئناف» because our privacy copy already teaches it.
_RESUME_PHRASES = frozenset({
    "start", "unstop", "resume",
    "تشغيل الرسائل", "شغل الرسائل", "ابدأ", "ابدا", "رجعني", "استئناف",
    "استئناف الرسائل", "عودة", "رجّعني",
})

_SUPPORT_PHRASES = frozenset({"دعم", "support", "مساعدة", "help"})

# Activation deep link prefills "تفعيل <token>" (or "activate <token>").
_ACTIVATION_RE = re.compile(r"^(?:تفعيل|activate)\s+([A-Za-z0-9_\-]{20,})$", re.IGNORECASE)


def normalize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.strip().split())


def extract_activation_token(text: str | None) -> str | None:
    m = _ACTIVATION_RE.match(normalize(text))
    return m.group(1) if m else None


def classify_inbound(text: str | None) -> tuple[InboundKind, str | None]:
    norm = normalize(text)
    if not norm:
        return (InboundKind.OTHER, None)
    token = extract_activation_token(norm)
    if token is not None:
        return (InboundKind.ACTIVATION, token)
    low = norm.lower()
    if low in _STOP_PHRASES:
        return (InboundKind.STOP, None)
    if low in _RESUME_PHRASES:
        return (InboundKind.RESUME, None)
    if low in _SUPPORT_PHRASES:
        return (InboundKind.SUPPORT, None)
    return (InboundKind.OTHER, None)
