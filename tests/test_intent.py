"""F-INTENT §15 — understanding what the customer WANTS.

The owner's correction: «يصير يعرف وش قصده مو ميزة أو إنجاز». These tests pin
the two properties that matter: the model's answer is honoured when it is
confident and valid, and EVERY other path lands on the deterministic verdict
rather than on a guess or a crash.
"""

from __future__ import annotations

from typing import Any

from career.onboarding import intent as it


class _Classifier:
    def __init__(self, verdict: Any) -> None:
        self._verdict = verdict
        self.calls: list[tuple[str, str | None]] = []

    def classify(self, reply: str, *, draft: str | None) -> dict[str, Any]:
        self.calls.append((reply, draft))
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


def _resolve(verdict: Any, *, body: str = "ابدع",
             deterministic: str = it.ANSWER) -> tuple[str, str]:
    return it.resolve_intent(
        body, draft="Improved reporting.", classifier=_Classifier(verdict),
        deterministic=deterministic,
    )


def test_confident_model_verdict_wins() -> None:
    assert _resolve({"intent": "revise", "topic": "other",
                     "confidence": 0.9})[0] == it.REVISE


def test_off_topic_carries_its_topic() -> None:
    intent, topic = _resolve({"intent": "off_topic",
                              "topic": "subscription_status",
                              "confidence": 0.95})
    assert intent == it.OFF_TOPIC and topic == "subscription_status"


def test_unknown_topic_is_neutralised() -> None:
    _, topic = _resolve({"intent": "off_topic", "topic": "launch_missiles",
                         "confidence": 0.9})
    assert topic == "other"


def test_low_confidence_defers_to_the_deterministic_verdict() -> None:
    assert _resolve({"intent": "revise", "topic": "other", "confidence": 0.2},
                    deterministic=it.ANSWER)[0] == it.ANSWER


def test_unknown_intent_defers() -> None:
    assert _resolve({"intent": "hallucinated", "topic": "other",
                     "confidence": 0.99},
                    deterministic=it.REVISE)[0] == it.REVISE


def test_malformed_verdicts_defer_without_crashing() -> None:
    for verdict in ({}, {"intent": None}, {"intent": "revise"},
                    {"intent": "revise", "confidence": "high"},
                    RuntimeError("classifier down")):
        assert _resolve(verdict, deterministic=it.ANSWER)[0] == it.ANSWER, verdict


def test_no_classifier_uses_the_deterministic_verdict() -> None:
    assert it.resolve_intent("ابدع", draft=None, classifier=None,
                             deterministic=it.REVISE) == (it.REVISE, "other")


def test_the_draft_is_shown_to_the_classifier() -> None:
    c = _Classifier({"intent": "revise", "topic": "other", "confidence": 0.9})
    it.resolve_intent("ابدع", draft="Improved reporting.", classifier=c,
                      deterministic=it.ANSWER)
    assert c.calls == [("ابدع", "Improved reporting.")]


def test_off_topic_topics_map_to_standing_commands() -> None:
    assert it.command_for_topic("subscription_status") == "حالة اشتراكي"
    assert it.command_for_topic("pause") == "وقف مؤقت"
    assert it.command_for_topic("delete") == "حذف بياناتي"
    assert it.command_for_topic("human") is None      # escalation, not a command
    assert it.command_for_topic("other") is None


def test_every_mapped_command_is_a_real_privacy_command() -> None:
    """A typo here would silently route a customer's request nowhere."""
    from career.onboarding.orchestrator import _PRIVACY_COMMANDS

    for command in it.TOPIC_TO_COMMAND.values():
        assert command in _PRIVACY_COMMANDS, command
