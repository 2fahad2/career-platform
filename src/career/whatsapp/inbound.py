"""Inbound message classification (whatsapp §08) — pure.

Classifies a customer's message into ACTIVATION / STOP / SUPPORT / OTHER and
extracts the activation token. Matching is on whole TOKENS of a closed
vocabulary — never a loose "contains" — so ordinary messages mentioning a word
are not misread as commands. Activation requires the explicit keyword + token
so a random string is never treated as a token.

AUDIT 2026-08 (P0-15). Every phrase here used to be compared against
``normalize``, which collapses whitespace and lowercases and does nothing else,
so the sets were byte-exact lists of Arabic spellings. Two live misses came out
of that, both on words the customer was TOLD to send: «مساعده» (the taa-marbuta
typed as haa, which is what most Saudi keyboards produce) reached nobody, and
«الغاء الإشتراك» — the hamza seat on الاشتراك — was not read as the Meta-
mandated opt-out. The first is a customer asking for a human and getting a
robot; the second is a compliance breach that fails in silence. Both are fixed
by folding the phrase sets and the message through the SAME normaliser the
conversation layers already trust (``normalize_ar``: diacritics, tatweel,
hamza seats, taa-marbuta, alef maqsura, punctuation and emoji).

Folding alone is not the whole fix, though, because it only equates spellings
of the SAME words. The three sets are also matched with different generosity,
and deliberately so — see :func:`classify_inbound`, which carries the reasoning
for each one. The one rule they share is agent-D's discipline from the consent
gate one layer down: whole tokens, a negation vetoes the command outright, and
a single word we do not know means "this is a sentence, not a command".

It also answers the question the worker must ask before routing anything into
the conversation: did the customer actually SAY something we can read? A voice
note, a photo, a sticker or a location carries no text at all, and treating the
resulting empty string as an answer both discards what they meant and writes a
blank «customer-confirmed» fact into the achievement bank (§15.5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

#: The one Arabic normaliser in the codebase, imported rather than copied.
#: Checked for a cycle before wiring it: nothing on its import chain
#: (career.cv.close / generate / validate → db.models, engine.ranking) reaches
#: back into career.whatsapp, and the only importers of THIS module are the
#: worker and a comment in salla.activation_link — so the edge is one-way.
#: extraction.py duplicates the same fold on purpose and says why (it is the
#: leaf every model boundary imports); this module is not in that position.
from career.onboarding.achievement_render import normalize_ar


class InboundKind(StrEnum):
    ACTIVATION = "activation"
    STOP = "stop"
    RESUME = "resume"
    SUPPORT = "support"
    OTHER = "other"


def _folded(phrases: Iterable[str]) -> frozenset[str]:
    """Fold a set of literal phrases once, at import, into the form the
    incoming message is folded into. Spellings that differ only by a hamza
    seat, a taa-marbuta or a diacritic collapse into one entry — which is why
    the sets below list WORDS, and never the same word twice."""
    return frozenset(normalize_ar(p) for p in phrases)


#: The canonical opt-outs, matched as a WHOLE message and deliberately not
#: subject to the token machinery below. Every one of them would also pass it
#: today — that is the point: Meta's own «stop», and the two Arabic words our
#: copy teaches, must keep working even if a later edit to the vocabulary
#: sets, the negation list or the word cap gets something wrong. A compliance
#: surface should not depend on a list staying correct.
_STOP_PHRASES = _folded({
    "stop", "unsubscribe",
    "إيقاف", "إيقاف الرسائل", "إلغاء", "إلغاء الاشتراك", "توقف",
})
#: The verbs that MEAN it. A message is only ever read as an opt-out when it
#: carries one of these — «حالة اشتراكي» and «تجديد الاشتراك» share a noun
#: with the opt-out and must never share its fate.
#:
#: «وقف» is pointedly ABSENT, and so is «مؤقت» below: «وقف مؤقت» / «إيقاف
#: مؤقت» is the standing PAUSE command (§05), the worker resolves STOP before
#: it ever reaches the privacy commands, and a pause read as an opt-out would
#: silence the customer instead of pausing their subscription.
_STOP_VERBS = _folded({
    "إيقاف", "أوقف", "أوقفوا", "وقفوا", "إلغاء", "ألغوا", "ألغي", "توقف",
    "stop", "unsubscribe",
})
#: What the verb may act on. Nouns alone are never a command.
_STOP_OBJECTS = _folded({
    "الرسائل", "رسائلكم", "الرسايل", "الاشتراك", "اشتراكي",
    "الخدمة", "خدمتكم", "الإرسال",
})
#: The way BACK. Nothing anywhere cleared ``opt_out_at`` — a customer who
#: stopped messages was silenced permanently while their subscription (and
#: their billing) carried on, and the confirmation told them to send «دعم»,
#: which does not clear it either. Meta's own convention is STOP/START, so
#: both the English and the Arabic a Saudi customer would actually type are
#: accepted, plus «استئناف» because our privacy copy already teaches it.
_RESUME_PHRASES = _folded({
    "start", "unstop", "resume",
    "تشغيل الرسائل", "شغل الرسائل", "ابدأ", "رجّعني", "استئناف",
    "استئناف الرسائل", "عودة", "ارجعوا الرسائل", "رجعوا الرسائل",
})

#: Whole messages that are unmistakably a request for a human, including the
#: multi-word ones no token rule should try to assemble.
#: «الدعم الفني» is deliberately NOT here, and neither is «دعم فني»: it is a
#: career field our own onboarding asks about, and a customer answering the
#: path question with their own profession must not be escalated instead of
#: heard. «دعم» alone already reaches a human.
_SUPPORT_PHRASES = _folded({
    "دعم", "support", "مساعدة", "help", "خدمة العملاء",
})
#: The words that ASK for a human. «الدعم» is listed beside «دعم» because the
#: fold does not strip a definite article — and «دعم» is the escape hatch
#: printed in every error message and on the store page, so the article a
#: customer naturally types must not be the difference between a human and
#: silence. «مساعد» is deliberately not here: it is a job title («مساعد
#: إداري») our own onboarding asks for, and it is a different token.
_SUPPORT_CORE = _folded({
    "دعم", "الدعم", "مساعدة", "المساعدة", "ساعدني", "ساعدوني", "مساعدتكم",
    "support", "help",
})
#: Words that may keep a request company without changing what is asked: the
#: verb of asking and plain courtesy. Nothing here can turn a sentence into a
#: command on its own — a match still needs a core word.
_REQUEST_FILLER = _folded({
    "أبي", "أبغى", "أريد", "بغيت", "محتاج", "أحتاج", "بحاجة", "ممكن",
    "أبيك", "أبيكم", "لو", "سمحت", "سمحتم", "من", "فضلك", "فضلكم", "رجاء",
    "الرجاء", "ياليت", "يا", "أخوي", "أختي", "شكرا", "الله", "يعطيك",
    "العافية", "please", "plz",
})
#: A negation VETOES the command, exactly as it vetoes a consent one layer
#: down. In practice it only ever fires on a message that pairs a negation
#: with the command's own verb — «ما أبي إلغاء الاشتراك», «لا أحتاج مساعدة» —
#: which is the one case where reading the keyword would put the opposite of
#: their words into their mouth.
_NEGATION_TOKENS = _folded({
    "لا", "ما", "مو", "مب", "مش", "ماني", "أبد", "أبدًا", "لست", "بدون",
    "no", "not", "never", "dont", "don't",
})
#: A command is SHORT. Past this it is a sentence with a condition or a
#: question in it, and those are answered by the conversation, not obeyed.
_COMMAND_MAX_WORDS = 5

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


def _is_command(
    tokens: list[str], *, core: frozenset[str], filler: frozenset[str]
) -> bool:
    """True when the message is that command and NOTHING else.

    Three guards, all of them the consent gate's (orchestrator, agent D), and
    each one closes a trap this classifier used to have or would have gained:

    * whole TOKENS, never a substring — «مساعد إداري» is a job title, not a
      call for help, and «لا أوافق» taught us what substrings do at a gate;
    * a negation vetoes outright — a customer saying they do NOT want to
      cancel must never be cancelled by the word they used to refuse;
    * one unknown word and this is a sentence, not a command — «أريد إيقاف
      الرسائل غدًا وليس الآن» is a plan, and obeying it today is wrong twice.
    """
    if not tokens or len(tokens) > _COMMAND_MAX_WORDS:
        return False
    unique = set(tokens)
    if unique & _NEGATION_TOKENS:
        return False
    if not unique & core:
        return False            # courtesy with no command in it
    return unique <= (core | filler)


def classify_inbound(text: str | None) -> tuple[InboundKind, str | None]:
    """Read one inbound message. Returns (kind, activation token or None).

    The three command sets are matched with three DIFFERENT generosities,
    because the cost of being wrong points a different way in each.

    STOP — bias toward DETECTING it. A missed opt-out is a Meta compliance
    breach that fails in silence: we keep messaging someone who told us to
    stop, and nothing anywhere reports it. A false opt-out silences a paying
    customer who did not ask, which is bad but LOUD — the worker's
    confirmation tells them what just happened, «تشغيل الرسائل» brings them
    straight back, and the operator is paged with the TEN code. So the verb
    may arrive wrapped in a request («أبي إلغاء الاشتراك لو سمحت») and still
    counts, bounded by the closed vocabulary above: an unknown word or a
    negation drops it back to OTHER, where the conversation answers it.

    RESUME — bias toward NOT detecting it, the one set that is deliberately
    narrower than its neighbours. A false resume clears ``opt_out_at`` and
    starts sending again to someone on record as having asked for silence:
    the SAME compliance surface STOP protects, from the other side, and the
    customer has no reason to expect it. The miss costs nothing to speak of,
    because the opt-out confirmation prints «تشغيل الرسائل» on its own line —
    we tell them the exact word, so an exact match is enough, and a customer
    who types something else stays silenced for one more message rather than
    being un-silenced by a guess.

    SUPPORT — bias toward DETECTING it, most generously of the three. «دعم»
    is the escape hatch printed in every error message, in the onboarding
    copy and on the store page; a customer reaching for it and getting the
    generic flow is the worst experience this product can produce, and it is
    the exact live miss «مساعده» caused. The false positive is nearly free:
    an operator glances at one ticket, and the customer is told a human is
    coming. The accepted cost is that a bare «دعم» typed as an ANSWER (it is
    a real career field) escalates instead of being recorded — one ticket
    against a customer who cannot reach a human, which is not a close call.

    Activation is extracted from the whitespace-normalised text and never the
    folded one: the token is case-sensitive and carries «-» and «_», all of
    which the Arabic fold would destroy.
    """
    norm = normalize(text)
    if not norm:
        return (InboundKind.OTHER, None)
    token = extract_activation_token(norm)
    if token is not None:
        return (InboundKind.ACTIVATION, token)
    folded = normalize_ar(norm)
    tokens = folded.split()
    if folded in _STOP_PHRASES or _is_command(
        tokens, core=_STOP_VERBS, filler=_STOP_OBJECTS | _REQUEST_FILLER
    ):
        return (InboundKind.STOP, None)
    if folded in _RESUME_PHRASES:
        return (InboundKind.RESUME, None)
    if folded in _SUPPORT_PHRASES or _is_command(
        tokens, core=_SUPPORT_CORE, filler=_REQUEST_FILLER
    ):
        return (InboundKind.SUPPORT, None)
    return (InboundKind.OTHER, None)
