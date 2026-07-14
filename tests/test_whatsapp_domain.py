"""Pure C4 domain logic — window, classification, adaptive planner, activation.

No DB, no network. `now` is injected everywhere so window behavior is
deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from career.tokens import hash_token, new_activation_token
from career.whatsapp.activation import activation_link, activation_message
from career.whatsapp.adaptive import DeliveryAction, plan_delivery
from career.whatsapp.inbound import (
    InboundKind,
    classify_inbound,
    extract_activation_token,
    normalize,
)
from career.whatsapp.window import WindowState, window_state

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


class TestWindow:
    def test_opted_out_overrides_everything(self) -> None:
        assert window_state(
            last_inbound_at=NOW, opt_out_at=NOW, now=NOW
        ) is WindowState.OPTED_OUT

    def test_no_inbound_is_closed(self) -> None:
        assert window_state(
            last_inbound_at=None, opt_out_at=None, now=NOW
        ) is WindowState.CLOSED

    def test_open_within_24h(self) -> None:
        last = NOW - timedelta(hours=23, minutes=59)
        assert window_state(last_inbound_at=last, opt_out_at=None, now=NOW) is WindowState.OPEN

    def test_closed_after_24h(self) -> None:
        last = NOW - timedelta(hours=24, minutes=1)
        assert window_state(last_inbound_at=last, opt_out_at=None, now=NOW) is WindowState.CLOSED

    def test_boundary_exactly_24h_is_closed(self) -> None:
        last = NOW - timedelta(hours=24)
        assert window_state(last_inbound_at=last, opt_out_at=None, now=NOW) is WindowState.CLOSED


class TestClassification:
    def test_activation_with_token(self) -> None:
        token = new_activation_token()
        kind, extracted = classify_inbound(f"تفعيل {token}")
        assert kind is InboundKind.ACTIVATION
        assert extracted == token

    def test_activation_english_keyword(self) -> None:
        token = new_activation_token()
        kind, extracted = classify_inbound(f"activate {token}")
        assert kind is InboundKind.ACTIVATION
        assert extracted == token

    def test_activation_keyword_without_token_is_not_activation(self) -> None:
        kind, extracted = classify_inbound("تفعيل")
        assert kind is InboundKind.OTHER
        assert extracted is None

    def test_stop_exact_phrases(self) -> None:
        for phrase in ("STOP", "إيقاف الرسائل", "الغاء الاشتراك", "unsubscribe", "توقف"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.STOP, phrase

    def test_stop_is_exact_not_contains(self) -> None:
        # A message merely mentioning the word is NOT a STOP command.
        kind, _ = classify_inbound("أريد إيقاف الرسائل غدًا وليس الآن")
        assert kind is InboundKind.OTHER

    def test_support_phrases(self) -> None:
        for phrase in ("دعم", "support", "مساعدة"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.SUPPORT, phrase

    def test_other(self) -> None:
        assert classify_inbound("نعم أوافق")[0] is InboundKind.OTHER
        assert classify_inbound("")[0] is InboundKind.OTHER
        assert classify_inbound(None)[0] is InboundKind.OTHER

    def test_normalize_collapses_whitespace(self) -> None:
        assert normalize("  دعم   ") == "دعم"

    def test_extract_rejects_short_or_bad(self) -> None:
        assert extract_activation_token("تفعيل short") is None
        assert extract_activation_token("random long-enough-string-here-xyz") is None


class TestAdaptivePlanner:
    def test_open_sends_direct(self) -> None:
        assert plan_delivery(WindowState.OPEN) is DeliveryAction.SEND_DIRECT

    def test_closed_sends_template_then_waits(self) -> None:
        assert plan_delivery(WindowState.CLOSED) is DeliveryAction.SEND_TEMPLATE_THEN_WAIT

    def test_opted_out_skips(self) -> None:
        assert plan_delivery(WindowState.OPTED_OUT) is DeliveryAction.SKIP_OPTED_OUT


class TestActivationLinkAndToken:
    def test_activation_message_shape(self) -> None:
        assert activation_message("abc123") == "تفعيل abc123"

    def test_link_uses_number_without_plus_and_encodes_text(self) -> None:
        link = activation_link("+966500000000", "TOK123456")
        assert link.startswith("https://wa.me/966500000000?text=")
        assert "TOK123456" in link
        assert "+" not in link.split("?")[0]  # number has no leading +

    def test_hash_is_stable_and_matches_authority(self) -> None:
        raw = new_activation_token()
        assert hash_token(raw) == hash_token(raw)
        assert len(hash_token(raw)) == 64
