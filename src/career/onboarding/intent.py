"""What does this customer WANT? (F-INTENT §15)

The old question was narrow and slightly insulting: «is this text an
achievement?» — so a customer giving feedback got told we could not find an
achievement in it. The owner's correction: understand the intent, then serve
it. This module answers the wider question.

Design, in the order the checks run:

1. **Deterministic first**, for the unambiguous cases — button ids and the
   literal labels. Free, instant, works with no network, and the customer's
   most common replies never wait on a model.
2. **The model** for everything else, because Saudi colloquial has more ways
   to say «make it stronger» than any keyword list will ever hold. It returns
   ONE label from a closed set — it never writes customer-facing text and
   never decides what enters the achievement bank (constants 5 and 9 stay
   intact, and there is no prompt-injection payoff in returning a label).
3. **The deterministic classifier as the fallback**, if the model is down.
   Degradation is graceful and silent to the customer.

Unknown or unparseable → ``ANSWER``, the safe default: it can waste one panel
run, but it can never swallow a real achievement as something else.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from career.onboarding.achievement_render import normalize_ar

logger = logging.getLogger("career.enrichment")

# ── the closed set ───────────────────────────────────────────────────────────

ANSWER = "answer"                # a description of what they did
REVISE = "revise"                # feedback on the draft («ابدع», «اقصرها»)
AFFIRM = "affirm"                # warmth/agreement, NOT consent (constant 5)
DISPUTE = "dispute"             # «هذا غلط», the draft misstates something
QUESTION = "question"            # they asked us something
STOP = "stop"                    # skip this / stop asking
OFF_TOPIC = "off_topic"          # subscription, billing, pause, human…

INTENTS: frozenset[str] = frozenset(
    {ANSWER, REVISE, AFFIRM, DISPUTE, QUESTION, STOP, OFF_TOPIC}
)

#: Off-topic asks we can actually serve, so the classifier's ``topic`` can be
#: routed rather than guessed at. Mirrors the standing privacy commands plus
#: the human-escalation path (§05/§12).
OFF_TOPIC_TOPICS: frozenset[str] = frozenset({
    "subscription_status", "pause", "resume", "export", "delete",
    "human", "billing", "other",
})


class IntentClassifier(Protocol):
    def classify(self, reply: str, *, draft: str | None) -> dict[str, Any]:
        """{intent, topic, confidence} — a label, never customer-facing text."""
        ...


# ── the model boundary (one structured call, PII-free by caller contract) ────

_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "A Saudi job-seeker is in a WhatsApp conversation about ONE line of their "
    "CV. We may have shown them a draft of that line. Your only job is to say "
    "what their reply WANTS. Return exactly one intent:\n"
    "- answer: they are describing what they did in that job (the content we "
    "asked for), even partially or messily.\n"
    "- revise: they are commenting on OUR draft and want it changed — "
    "stronger, shorter, simpler, different wording, more detail. Any Saudi "
    "phrasing counts, including terse reactions like «ابدع» or «حلوة بس».\n"
    "- affirm: agreement or warmth with no new content («تمام», «ايه صح»).\n"
    "- dispute: they say the draft is factually wrong or claims too much.\n"
    "- question: they are asking US something.\n"
    "- stop: they want to skip this line or stop being asked.\n"
    "- off_topic: the reply is about something else entirely — subscription "
    "status, pausing messages, billing, deleting data, or wanting a human.\n"
    "For off_topic also set topic to one of: subscription_status, pause, "
    "resume, export, delete, human, billing, other. Otherwise topic is "
    "\"other\". Set confidence between 0 and 1. Never write a message to the "
    "customer; return the classification only."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "topic", "confidence"],
    "properties": {
        "intent": {"type": "string"},
        "topic": {"type": "string"},
        "confidence": {"type": "number"},
    },
}


class AnthropicIntentClassifier:  # pragma: no cover — live boundary
    def __init__(self, api_key: str | None = None, client: Any | None = None,
                 model: str = _MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model

    def classify(self, reply: str, *, draft: str | None) -> dict[str, Any]:
        import json

        prompt = f"Their reply:\n{reply}"
        if draft:
            prompt = f"The draft we showed them:\n{draft}\n\n{prompt}"
        response = self._client.messages.create(
            model=self._model, max_tokens=1024,
            thinking={"type": "adaptive"}, system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason != "end_turn":
            logger.warning("intent classifier did not finish: %s",
                           response.stop_reason)
            return {}
        for block in response.content:
            text = getattr(block, "text", None)
            if getattr(block, "type", "") == "text" and isinstance(text, str):
                try:
                    return dict(json.loads(text))
                except ValueError:
                    logger.warning("intent classifier returned unparseable JSON")
                    return {}
        return {}


# ── the resolver the conversation calls ─────────────────────────────────────


def resolve_intent(
    body: str,
    *,
    draft: str | None,
    classifier: IntentClassifier | None,
    deterministic: str,
) -> tuple[str, str]:
    """(intent, topic). ``deterministic`` is the keyword classifier's verdict,
    used when the model is unavailable or unhelpful — never overridden by a
    low-confidence guess."""
    if classifier is None:
        return deterministic, "other"
    try:
        verdict = classifier.classify(body, draft=draft)
    except Exception:  # noqa: BLE001 — a down classifier must not block anyone
        logger.warning("intent classification failed", exc_info=True)
        return deterministic, "other"

    intent = str(verdict.get("intent") or "").strip().lower()
    topic = str(verdict.get("topic") or "other").strip().lower()
    try:
        confidence = float(verdict.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    if intent not in INTENTS or confidence < 0.5:
        logger.info("intent fell back to deterministic (%s, conf=%.2f)",
                    intent or "?", confidence)
        return deterministic, "other"
    if topic not in OFF_TOPIC_TOPICS:
        topic = "other"
    return intent, topic


#: Off-topic topic → the standing privacy command that already serves it, so
#: the conversation delegates instead of reimplementing (§05/§12).
TOPIC_TO_COMMAND: dict[str, str] = {
    "subscription_status": "حالة اشتراكي",
    "pause": "وقف مؤقت",
    "resume": "استئناف",
    "export": "تصدير بياناتي",
    "delete": "حذف بياناتي",
}


def command_for_topic(topic: str) -> str | None:
    return TOPIC_TO_COMMAND.get(normalize_ar(topic) or topic)
