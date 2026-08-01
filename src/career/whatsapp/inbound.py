"""Inbound message classification (whatsapp §08) — pure.

Classifies a customer's message into ACTIVATION / STOP / SUPPORT / OTHER and
extracts the activation token. STOP and SUPPORT match the message EXACTLY (after
normalization) — never a loose "contains" — so ordinary messages mentioning a
word are not misread as commands. Activation requires the explicit keyword +
token so a random string is never treated as a token.

It also answers the question the worker must ask before routing anything into
the conversation: did the customer actually SAY something we can read? A voice
note, a photo, a sticker or a location carries no text at all, and treating the
resulting empty string as an answer both discards what they meant and writes a
blank «customer-confirmed» fact into the achievement bank (§15.5).
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


def has_readable_text(text: str | None) -> bool:
    """True only when the customer really wrote something we can read.

    ``worker._text_of`` yields None for every media type, and a body of
    whitespace is not an answer either — routing either one into the
    conversation as "" is how a sticker became an empty CUSTOMER_CONFIRMED
    experience row.
    """
    return bool(normalize(text))


#: A full, grammatical Arabic acknowledgement per message type. A single
#: template with a «{noun}» slot cannot work: «ملصق» and «موقع» are masculine,
#: «صورة» and «رسالة صوتية» are not, and a customer reading a broken sentence
#: learns that nobody is really on the other side. Arabic only, no Latin and
#: no digits — these lines ship inside Arabic messages (§16 direction purity).
_MEDIA_ACK: dict[str, str] = {
    "audio": "وصلتني رسالتك الصوتية 👌",
    "voice": "وصلتني رسالتك الصوتية 👌",
    "ptt": "وصلتني رسالتك الصوتية 👌",
    "image": "وصلتني الصورة 👌",
    "video": "وصلني المقطع 👌",
    "sticker": "وصلني الملصق 👌",
    "location": "وصلني الموقع 👌",
    "contacts": "وصلتني جهة الاتصال 👌",
    "document": "وصلني الملف 👌",
    "reaction": "وصلني تفاعلك 👌",
    "order": "وصلني طلبك 👌",
}
#: Meta sends `unsupported` for anything the Cloud API cannot forward (polls,
#: view-once, …) and invents new types without asking us — the default keeps a
#: brand-new type answered instead of silent.
_MEDIA_ACK_DEFAULT = "وصلتني رسالتك 👌"


def media_ack(message_type: str | None) -> str:
    """The opening line for a message we received but cannot read."""
    return _MEDIA_ACK.get((message_type or "").strip().lower(), _MEDIA_ACK_DEFAULT)


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
