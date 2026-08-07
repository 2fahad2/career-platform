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

The fold is not free, and the 2026-08-05 audit is where the bill arrived: it
erases the punctuation a sentence is built from, and it collapses «ابدأ» into
«أبدًا». Both cost a compliance surface — see :func:`_clauses` and
``_RESUME_PHRASES``. Anything read on a clause boundary is now split off the
RAW message, before folding.

Folding alone is not the whole fix, though, because it only equates spellings
of the SAME words. The three sets are also matched with different generosity,
and deliberately so — see :func:`classify_inbound`, which carries the reasoning
for each one. The one rule they share is agent-D's discipline from the consent
gate one layer down: whole tokens, a negation vetoes the command it scopes,
and a single word we do not know means "this is a sentence, not a command".

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
#: It used to be imported from onboarding.achievement_render, and that import
#: was acyclic but not free: achievement_render reaches into career.cv.generate
#: at module level, so this module — documented on its first line as PURE —
#: pulled in career.cv.*, career.db.models and all of SQLAlchemy in order to
#: fold a string. The fold now lives in career.arabic, a leaf that imports
#: nothing from career at all, so the word "pure" above is true again.
from career.arabic import normalize_ar


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
#:
#: «ابدأ» is pointedly ABSENT (audit 2026-08-05). The fold that makes this
#: module work at all also makes «ابدأ» and «أبدًا» the same string, "ابدا" —
#: the resume word and the strongest negation in Arabic are indistinguishable
#: here, and the set was matched first, so a customer answering «أبدًا» —
#: «never» — had their opt-out CLEARED and the messages started again. That is
#: the exact compliance surface RESUME is supposed to protect, breached from
#: the inside. When two readings collide and only one of them can be right,
#: this set is the one that gives way: the opt-out confirmation prints
#: «تشغيل الرسائل» verbatim, so nothing is lost but a word we never taught.
_RESUME_PHRASES = _folded({
    "start", "unstop", "resume",
    "تشغيل الرسائل", "شغل الرسائل", "رجّعني", "استئناف",
    "استئناف الرسائل", "عودة", "ارجعوا الرسائل", "رجعوا الرسائل",
})

#: Whole messages that are unmistakably a request for a human, including the
#: multi-word ones no token rule should try to assemble.
#: «الدعم الفني» is deliberately NOT here, and neither is «دعم فني»: it is a
#: career field our own onboarding asks about, and a customer answering the
#: path question with their own profession must not be escalated instead of
#: heard. «دعم» alone already reaches a human.
#:
#: AUDIT 2026-08-05: «خدمة العملاء» was here, and it is the same mistake the
#: line above was written to avoid — only worse, because customer service is
#: one of the commonest job fields in the Kingdom and our own gap question
#: («وش أبرز خبرة عملية عندك؟») invites exactly that answer. SUPPORT is
#: resolved before any onboarding routing, so the answer was thrown away, a
#: ticket was raised against a customer who had asked for nothing, and the
#: question came back. Nothing is lost by removing it: «دعم» is the escape
#: hatch every error message, the onboarding copy and the store page print,
#: and «خدمة العملاء» is printed nowhere as a way to reach us.
_SUPPORT_PHRASES = _folded({
    "دعم", "support", "مساعدة", "help",
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
#:
#: «don't» is gone (audit 2026-08-05): the fold turns an apostrophe into a
#: space, so the entry was the two-word string "don t" and could never equal a
#: token — a dead line that read as coverage. «dont» carries the spelling, and
#: an English contraction folds to «don» + «t», two words this vocabulary does
#: not know, which drops the message to OTHER by the unknown-word rule anyway.
_NEGATION_TOKENS = _folded({
    "لا", "ما", "مو", "مب", "مش", "ماني", "أبد", "أبدًا", "لست", "بدون",
    "no", "not", "never", "dont",
})
#: A command is SHORT. Past this it is a sentence with a condition or a
#: question in it, and those are answered by the conversation, not obeyed.
_COMMAND_MAX_WORDS = 5

#: Where one clause ends and the next begins. This split happens on the RAW
#: message, before the fold — ``normalize_ar`` turns every one of these marks
#: into a space, so by the time we have tokens the sentence structure is gone
#: and «لا تراسلوني، إلغاء الاشتراك» is an indistinguishable bag of words.
#:
#: AUDIT 2026-08-06: the first version knew a comma, a full stop and a spaced
#: ASCII hyphen, and nothing else — so «لا تراسلوني — إلغاء الاشتراك» (an em
#: dash, which is what a phone keyboard offers), «ملاحظة: إلغاء الاشتراك» and
#: «لا تراسلوني/إلغاء الاشتراك» were all still one clause with a negation in
#: it, i.e. still a silently-missed opt-out. The colon, the slash, the
#: ellipsis and the dash family are added here, and the dash no longer needs
#: surrounding spaces. Widening this set can only ever ADD a STOP reading
#: (SUPPORT no longer splits at all, and RESUME never did), which is the
#: direction the compliance surface wants.
_CLAUSE_SPLIT = re.compile(r"[،؛,;:.!؟?/…\n\r]+|\s*[-–—]+\s*")

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


def _clauses(text: str) -> list[str]:
    """One message split into the clauses it is actually made of, each folded.

    AUDIT 2026-08-05, and the reason this exists. The negation veto was
    applied to the whole message as one bag of tokens, so a negation ANYWHERE
    killed the command — and the two most natural ways to opt out in Arabic
    open with one:

        «لا تراسلوني، إلغاء الاشتراك»   → OTHER
        «ما أبغى رسائل، إيقاف»          → OTHER

    Both of those are Meta-mandated opt-outs failing in silence, which the
    docstring of :func:`classify_inbound` claims is the one outcome STOP is
    biased against. The bug was not the veto; it was reading a negation that
    scopes ONE clause as if it scoped the message. «ما أبي إلغاء الاشتراك» —
    a customer refusing to cancel — is a single clause and must still be
    vetoed, and it still is: a clause is only obeyed when the clause ITSELF
    is a whole command with no negation in it.

    AUDIT 2026-08-06 — what this function does NOT do, and the claim that
    used to stand here. It said: "splitting can therefore only find a command
    inside a sentence that already contained one outright". That is false, and
    it was false the day it was written. A comma does not only separate
    clauses; it separates LIST ITEMS, and «تسويق، دعم، مبيعات» — three career
    fields, an ordinary answer to our own gap question «وش أبرز خبرة عملية
    عندك؟» — contains no command outright and yet has «دعم» sitting alone as
    an item. Split, each item is a bare word, and a bare word from the
    vocabulary is a whole command. That re-opened, through a comma, the exact
    hole the same commit closed by taking «خدمة العملاء» out of
    ``_SUPPORT_PHRASES``: the answer discarded, a ticket raised against a
    customer who asked for nothing, the question asked again.

    The reason the fix is NOT here is that the two callers do not want the
    same thing. Weakening the split (demanding a "substantial" clause) would
    break «ما أبغى رسائل، إيقاف», where the matched clause is one bare word
    and the message is a real opt-out; the list SHAPE cannot be recognised
    either, since «أعمل في مجال المبيعات، دعم» is not list-shaped at all. So
    the split stays exactly as generous as it is, and the choice of who reads
    clauses moved to :func:`classify_inbound` — STOP does, SUPPORT does not.
    """
    return [
        folded for folded in
        (normalize_ar(part) for part in _CLAUSE_SPLIT.split(text))
        if folded
    ]


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
    negation drops it back to OTHER, where the conversation answers it. Since
    2026-08-05 that bias is real rather than asserted — the reading is per
    CLAUSE (:func:`_clauses`), because a whole-message negation veto turned
    «لا تراسلوني، إلغاء الاشتراك» into OTHER and this paragraph described a
    generosity the code did not have. The bias is bounded, and 2026-08-06 is
    where the bound got written down instead of glossed over: it holds inside
    a closed vocabulary and a known set of clause marks, so «خلاص إلغاء
    الاشتراك» is OTHER (one unknown word) and always will be, by the same
    rule that keeps «متى ينتهي الاشتراك» from cancelling anything. What we
    can honestly claim is narrower than "biased toward detecting STOP": no
    punctuation a customer types between a negation and an opt-out will hide
    the opt-out, and no wrapper of known courtesy words will either. A
    customer who says it in words we were never taught is not heard, and the
    operator is not told — that residue is real and is not fixed here.

    RESUME — bias toward NOT detecting it, the one set that is deliberately
    narrower than its neighbours: the WHOLE message, never a clause, and no
    request wrapping. A false resume clears ``opt_out_at`` and starts sending
    again to someone on record as having asked for silence: the SAME
    compliance surface STOP protects, from the other side, and the customer
    has no reason to expect it. The miss costs nothing to speak of, because
    the opt-out confirmation prints «تشغيل الرسائل» on its own line — we tell
    them the exact word, so an exact match is enough, and a customer who types
    something else stays silenced for one more message rather than being
    un-silenced by a guess. That is also why «ابدأ» left the set: after the
    fold it IS «أبدًا», and a guess that reads «never» as «start» is this
    error in its worst form.

    SUPPORT — bias toward detecting it, but over the WHOLE message only,
    never a clause. «دعم» is the escape hatch printed in every error message,
    in the onboarding copy and on the store page; a customer reaching for it
    and getting the generic flow is the worst experience this product can
    produce, and it is the exact live miss «مساعده» caused. So the word still
    arrives wrapped in courtesy («أبي مساعدة لو سمحت») and still counts. The
    accepted cost is that a bare «دعم» typed as an ANSWER (it is a real
    career field) escalates instead of being recorded — one ticket against a
    customer who can at least reach a human.

    AUDIT 2026-08-06 — why SUPPORT does not share STOP's clause rule, and the
    sentence above that used to read "most generously of the three". Clause
    splitting was applied to both sets, and a comma is how a person writes a
    LIST: «تسويق، دعم، مبيعات» and «أعمل في مجال المبيعات، دعم» became
    SUPPORT, when both are answers to our own gap question. The worker
    resolves SUPPORT before it routes anything into onboarding, so that
    answer is thrown away, a ticket is opened against the customer, and the
    question comes back — the same incident «خدمة العملاء» was removed to
    prevent, arriving through a comma instead of a phrase list.

    The costs point in opposite directions, so the two sets must not share
    one rule. A missed opt-out is a compliance breach that nobody sees; a
    false opt-out is loud, reversible in one word and paged — so STOP reads
    every clause and accepts the false positives that come with it. A missed
    SUPPORT costs a customer one retyped word, since «دعم» on its own line
    still reaches a human and every error message prints it; a false SUPPORT
    destroys a paying customer's answer. So SUPPORT gives up the clause
    reading entirely. Nothing that clause splitting was built for is lost:
    it was built for «لا تراسلوني، إلغاء الاشتراك», and that is STOP.

    OTHER — and 2026-08-07, where the لمّاح+ direct line was wired WITHOUT
    touching anything above. The 449 tier sells «تواصل مباشر معي … اكتب لي
    وقت ما تحتاج»: not a word, ANY message. Two things follow, and both point
    away from this function.

    There is no new keyword, and there will not be one. A keyword would sell
    the higher tier a spelling instead of access, and this file already paid
    twice for reading ordinary words as commands — «مساعده» (a customer asking
    for a human, heard by nobody) and «تسويق، دعم، مبيعات» (a career-field
    answer read as a support ticket, the answer discarded and a ticket raised
    against someone who asked for nothing). Nothing here can tell those apart
    from a request, because nothing here knows what was ASKED.

    And the tier test cannot live here at all: this module is pure — it
    imports the Arabic fold and nothing else from `career`, deliberately (see
    the ``normalize_ar`` note above) — so it has no session, no subscription
    row, and no way to know who is paying. «Which tier is this customer on» is
    a fact about a database row, not about a string, and reading it here would
    also make every future edit to these sets an edit to a billing rule.

    So OTHER still means OTHER, and the escalation lives where the
    subscription is known and where every other consumer of the message has
    already declined it: `promises.career_session.escalate_direct_message`,
    called from the worker's LAST branch. That ordering is the guarantee an
    onboarding answer, a gate reply or a document is never re-read as «a
    message for the operator».

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
    # the RAW text, never ``norm``: normalize() collapses newlines, and a line
    # break is a clause boundary a customer uses as readily as a comma.
    clauses = _clauses(text or "")
    if any(
        clause in _STOP_PHRASES or _is_command(
            clause.split(), core=_STOP_VERBS,
            filler=_STOP_OBJECTS | _REQUEST_FILLER,
        )
        for clause in clauses
    ):
        return (InboundKind.STOP, None)
    if folded in _RESUME_PHRASES:
        return (InboundKind.RESUME, None)
    # the WHOLE message, never a clause: a comma is how a customer writes a
    # list of career fields, and «تسويق، دعم، مبيعات» is an answer, not a
    # request for a human. See the SUPPORT paragraph above.
    if folded in _SUPPORT_PHRASES or _is_command(
        folded.split(), core=_SUPPORT_CORE, filler=_REQUEST_FILLER,
    ):
        return (InboundKind.SUPPORT, None)
    return (InboundKind.OTHER, None)
