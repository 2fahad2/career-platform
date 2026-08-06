"""Arabic text folding — the one authority, and a LEAF.

Everything that compares what a Saudi customer typed against something we
wrote sits on this file: consent classification (a PDPL artifact), the
Meta-mandated STOP/RESUME opt-out reading, the standing privacy commands, the
greeting and courtesy tests, and the name matching that keeps a customer's
name out of every model prompt. «اوافق» and «أوافق» are the same word to the
person typing them, and until they are the same string to us, a gate is only
as good as one spelling — which is exactly how «مساعده» reached nobody and
«الغاء الإشتراك» was not read as an opt-out.

WHY IT LIVES HERE, at the root of the package, importing nothing from
``career`` at all. The fold used to live in ``onboarding/achievement_render``,
a module that reaches into ``career.cv.generate`` at import time. The bill for
that arrived three times:

* ``onboarding/extraction`` — the leaf every model boundary imports — could
  not import the fold without closing a cycle, so it kept its own copy, with
  a comment explaining the cycle instead of sharing the code. Two copies of a
  compliance primitive is one copy too many: a fix applied to either one is a
  silent divergence in the other.
* ``whatsapp/inbound``, documented as a pure module, transitively pulled in
  ``career.cv.*``, ``career.db.models`` and all of SQLAlchemy in order to fold
  a string.
* Two separate agents had to reason about the cycle rather than about the work
  they were doing.

A primitive this load-bearing must never fail to load, and the cheapest way to
guarantee that is to give it nothing that CAN fail: ``re`` and nothing else.
``tests/test_arabic.py`` fails the build if this module ever grows an import
from ``career.*`` — that guard is the whole point of the move and is not a
formality.

Behaviour is frozen. The move that created this file was proven byte-identical
over a corpus of every Arabic code point, every combining mark, both digit
sets, emoji, punctuation, Latin and mixed strings, and every classification
outcome downstream of it was compared before and after. Any change here is a
change to consent, opt-out and PDPL matching, and belongs in a commit that
says so.
"""

from __future__ import annotations

import re

__all__ = ["AR_DIACRITICS", "AR_FOLD", "AR_MARKS", "fold_token", "normalize_ar"]

#: Arabic combining marks: harakat, the superscript alef, tatweel, and the
#: quranic marks. «فُلان» and «فلان» are the same name to every human and to
#: no substring matcher, and a mark is not ``\w``, which is precisely how a
#: diacritised name used to escape the tokeniser.
#:
#: Exported as a character-class BODY (not a compiled pattern) because
#: extraction.py builds its own "letter" class out of it: an Arabic letter
#: wearing a damma must count as part of one word.
AR_MARKS = "\u064B-\u0652\u0670\u0640\u06D6-\u06ED"
AR_DIACRITICS = re.compile(f"[{AR_MARKS}]")

#: The seat/shape folds. A hamza seat is a spelling choice, not a different
#: letter — most Saudi Android keyboards will not produce «أ» at all — and
#: taa-marbuta/haa and alef-maqsura/yaa are typed interchangeably by everyone.
AR_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي",
})

#: For :func:`normalize_ar`, which works on a whole message and must keep the
#: word boundaries: punctuation and emoji become spaces so the caller can read
#: whole TOKENS. Callers depend on that — a substring reading of a consent
#: gate is how «لا أوافق» would grant a consent.
_NON_WORD_KEEP_SPACE = re.compile(r"[^\w\s]", re.UNICODE)
#: For :func:`fold_token`, which works on ONE word that has already been cut
#: out of the text: here whitespace is not a boundary to preserve, it is
#: noise, so everything non-word is removed outright rather than spaced.
_NON_WORD = re.compile(r"[^\w]", re.UNICODE)


def normalize_ar(text: str | None) -> str:
    r"""Fold a whole message to a comparable form.

    Marks and tatweel go, hamza seats and taa-marbuta and alef-maqsura fold,
    punctuation and emoji become spaces, Latin lowercases, runs of whitespace
    collapse. «مضبوط ✅», «مضبوط» and «مضبووط» all arrive here from the same
    customer meaning the same thing.

    Arabic-Indic digits SURVIVE (they are ``\w``): the achievement grounding
    guard reads numbers out of the customer's own answer, and folding «٤٠» away
    would unground the one fact it exists to check.

    ``None`` and non-strings fold to ``""`` rather than raising — every caller
    is on a message path where a missing body is an ordinary event (a sticker
    carries no text), and a classifier that raises there turns a photo into a
    500.
    """
    body = AR_DIACRITICS.sub("", str(text or ""))
    body = body.translate(AR_FOLD)
    body = _NON_WORD_KEEP_SPACE.sub(" ", body)
    return " ".join(body.lower().split())


def fold_token(token: str) -> str:
    """The comparison key for ONE token: marks dropped, hamza/yaa/taa folded,
    punctuation removed entirely, lowercased.

    «الفلانى» and «الفلاني» collapse; so do "Al-Fulani" and "alfulani". This is
    the name matcher's key, and it removes rather than spaces its punctuation
    on purpose — the caller has already decided where this word starts and
    ends, so an internal hyphen inside "Al-Fulani" must not split it into two
    keys that match nothing.
    """
    body = AR_DIACRITICS.sub("", token).translate(AR_FOLD)
    return _NON_WORD.sub("", body).lower()
