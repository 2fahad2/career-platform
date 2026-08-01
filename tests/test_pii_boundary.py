"""§15.8 at every model boundary — the promise printed on the store page.

«كل نداء يمر بعد تجريد اسمك وجوالك وبريدك» is published to customers and to
the regulator. This file exists because two of the six Claude call sites had
no gate at all, and one of them was reachable with the customer's raw WhatsApp
text: a free-text onboarding answer is stored verbatim as an experience title,
which the tailoring chain JSON-dumps straight into the ranking prompt.
"""

from __future__ import annotations

import pathlib
import re

from career.cv.generate import PiiGuardedLlm
from career.onboarding.extraction import PiiLeak


class _Recorder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "{}"


def test_the_customer_name_never_reaches_the_model() -> None:
    inner = _Recorder()
    guarded = PiiGuardedLlm(inner, known_name="Fahad Almulhim")

    guarded.complete("Rank these: Fahad Almulhim, branch manager")

    assert inner.prompts, "the call must still go through"
    assert "Fahad" not in inner.prompts[0]
    assert "Almulhim" not in inner.prompts[0]


def test_an_email_or_phone_in_the_bank_never_reaches_the_model() -> None:
    inner = _Recorder()
    guarded = PiiGuardedLlm(inner)

    guarded.complete("contact me on fahad@example.com or 0501234567")

    sent = inner.prompts[0]
    assert "fahad@example.com" not in sent
    assert "0501234567" not in sent


def test_a_leak_that_survives_stripping_is_not_sent_at_all() -> None:
    """Fail closed. The chain degrades to deterministic rules, so the
    customer still gets a CV — we lose a model call, not the promise."""

    class _Hostile(_Recorder):
        pass

    inner = _Hostile()
    guarded = PiiGuardedLlm(inner, known_name="Fahad")

    class _NoStrip:
        text = "Fahad stayed in the text"

    import career.onboarding.extraction as extraction

    original = extraction.strip_pii
    extraction.strip_pii = lambda text, **kw: _NoStrip()   # simulate a miss
    try:
        raised = False
        try:
            guarded.complete("Fahad")
        except PiiLeak:
            raised = True
    finally:
        extraction.strip_pii = original

    assert raised, "a surviving leak must raise, not be sent"
    assert not inner.prompts, "nothing may go on the wire after a leak"


def test_every_model_call_site_sits_behind_a_gate() -> None:
    """The structural guard: a NEW `.messages.create(` added without a PII
    gate in its module fails here, the way the two ungated sites would have.
    """
    src = pathlib.Path("src/career")
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        if ".messages.create(" not in body:
            continue
        gated = ("strip_pii" in body or "assert_no_pii" in body
                 or "PiiGuardedLlm" in body)
        if not gated:
            offenders.append(str(path))
    assert offenders == [], (
        "these modules call a model with no PII gate in sight: "
        + ", ".join(offenders)
    )


def test_the_tailoring_chain_installs_the_gate_itself() -> None:
    """Wrapping the client — not patching call sites — is what makes a future
    prompt safe by construction. Pin that it is still wired that way."""
    body = pathlib.Path("src/career/cv/generate.py").read_text(encoding="utf-8")
    assert re.search(r"llm\s*=\s*PiiGuardedLlm\(llm", body)
