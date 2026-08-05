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


class TestSpellingsThatUsedToBeMissed:
    """P0-15: the sets were byte-exact, so a customer's own keyboard decided
    whether an opt-out or a call for help was heard at all."""

    def test_the_opt_out_spellings_a_real_keyboard_produces(self) -> None:
        """LIVE MISS: «الغاء الإشتراك» — a hamza on the alef of الاشتراك —
        was not read as an opt-out. Meta requires us to honor it, and it was
        failing in silence, which is the worst shape a compliance bug takes."""
        for phrase in (
            "الغاء الإشتراك", "إلغاء الإشتراك", "إلغاء الاشتراك",
            "الغاء الاشتراك", "إلغَاء الاشتراك", "ايقاف الرسائل",
            "إيقاف الرسائل", "الغاء", "إلغاء",
        ):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.STOP, phrase

    def test_the_help_spellings_a_real_keyboard_produces(self) -> None:
        """LIVE MISS: «مساعده» (taa-marbuta typed as haa) reached nobody, and
        «الدعم» — the word with the article a customer naturally types — was
        not the escape hatch «دعم» is promised to be everywhere."""
        for phrase in ("مساعده", "مساعدة", "المساعدة", "الدعم", "دعم",
                       "ساعدني", "خدمة العملاء"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.SUPPORT, phrase

    def test_resume_folds_its_own_spellings(self) -> None:
        for phrase in ("تشغيل الرسائل", "ابدأ", "ابدا", "رجّعني", "رجعني",
                       "استئناف", "start"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.RESUME, phrase


class TestCommandsInsideARequest:
    """STOP and SUPPORT tolerate the words a Saudi customer wraps a request
    in; RESUME deliberately does not (a false resume re-opens messaging to
    someone who asked for silence)."""

    def test_a_polite_opt_out_is_still_an_opt_out(self) -> None:
        for phrase in ("ابي الغاء الاشتراك", "الغاء الاشتراك لو سمحت",
                       "أبغى إيقاف الرسائل من فضلك", "أوقفوا الرسائل"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.STOP, phrase

    def test_a_polite_call_for_help_is_still_a_call_for_help(self) -> None:
        for phrase in ("ابي مساعده", "محتاج دعم لو سمحت", "ممكن مساعدة",
                       "يا اخوي ساعدني"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.SUPPORT, phrase

    def test_resume_stays_narrow_and_is_not_guessed(self) -> None:
        """A missed resume costs one retyped word — the opt-out confirmation
        prints «تشغيل الرسائل» verbatim. A guessed one starts messaging a
        customer who is on record asking us to stop."""
        for phrase in ("ابغى ارجع", "ودي ترجع الرسائل", "شغل"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase


class TestTrapsTheseCommandsMustNotFallInto:
    def test_a_negation_kills_the_command_outright(self) -> None:
        """«ما أبي إلغاء الاشتراك» is a customer REFUSING to cancel. Reading
        the keyword there would put the opposite of their words in their
        mouth — the same rule the consent gate uses one layer down."""
        for phrase in ("ما ابي الغاء الاشتراك", "لا أريد إيقاف الرسائل",
                       "مو قصدي الغاء", "لا احتاج مساعدة", "ما ابغى دعم"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase

    def test_one_unknown_word_makes_it_a_sentence_not_a_command(self) -> None:
        for phrase in ("أريد إيقاف الرسائل غدًا وليس الآن",
                       "متى ينتهي الاشتراك", "ابي الغاء الموعد بكرة",
                       "دعم فني", "مساعد إداري"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase

    def test_the_pause_command_is_never_read_as_an_opt_out(self) -> None:
        """§05's «وقف مؤقت» / «إيقاف مؤقت» pause the SUBSCRIPTION, and the
        worker resolves STOP before it ever reaches the privacy commands — so
        a pause read as an opt-out would silence the customer instead."""
        for phrase in ("وقف مؤقت", "إيقاف مؤقت", "ايقاف مؤقت"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase

    def test_the_subscription_nouns_alone_are_never_a_command(self) -> None:
        """«حالة اشتراكي» and «تجديد الاشتراك» (an approved template button)
        share their noun with the opt-out and must never share its fate."""
        for phrase in ("حالة اشتراكي", "تجديد الاشتراك", "الاشتراك",
                       "الرسائل", "لو سمحت"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase

    def test_folding_never_touches_the_activation_token(self) -> None:
        """The fold lowercases and drops punctuation; the token carries case,
        «-» and «_». Extraction must keep reading the RAW text."""
        token = "AbC-dEf_123456789012345"
        kind, extracted = classify_inbound(f"تفعيل {token}")
        assert kind is InboundKind.ACTIVATION
        assert extracted == token

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
