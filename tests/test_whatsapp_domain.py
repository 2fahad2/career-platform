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
                       "ساعدني"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.SUPPORT, phrase

    def test_resume_folds_its_own_spellings(self) -> None:
        for phrase in ("تشغيل الرسائل", "رجّعني", "رجعني",
                       "استئناف", "start"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.RESUME, phrase


class TestWhatTheFoldCostUs:
    """AUDIT 2026-08-05. Folding is what makes this module work on real
    keyboards, and it is also what erased the two distinctions below. Both
    failures land on a compliance surface."""

    def test_an_opt_out_after_a_negated_clause_is_still_an_opt_out(self) -> None:
        """The veto ran over the whole message as one bag of tokens, so a
        «لا» in the FIRST clause killed the command in the second. These are
        Meta-mandated opt-outs, and they were returning OTHER — the silent
        failure classify_inbound's own docstring claims STOP is biased
        against."""
        for phrase in ("لا تراسلوني، إلغاء الاشتراك", "ما أبغى رسائل، إيقاف",
                       "ما عاد أبيكم. إلغاء الاشتراك",
                       "لا تتصلون علي\nإيقاف الرسائل"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.STOP, phrase

    def test_a_negation_still_vetoes_the_clause_it_belongs_to(self) -> None:
        """The counterweight, and the reason this is a clause rule and not an
        «any keyword wins» rule: one clause, one negation, no command."""
        for phrase in ("ما ابي الغاء الاشتراك", "لا أريد إيقاف الرسائل",
                       "مو قصدي الغاء", "لا احتاج مساعدة"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase

    def test_never_is_not_start(self) -> None:
        """«ابدأ» and «أبدًا» fold to the SAME string, and the resume set was
        checked first — so a customer answering «أبدًا» («never») had their
        opt-out cleared and the messages resumed. RESUME's whole reason to be
        narrow is that it un-silences someone who asked for silence."""
        for phrase in ("أبدًا", "ابدا", "ابدأ"):
            kind, _ = classify_inbound(phrase)
            assert kind is not InboundKind.RESUME, phrase
        # the word we actually teach still works
        assert classify_inbound("تشغيل الرسائل")[0] is InboundKind.RESUME

    def test_a_job_field_is_not_a_call_for_a_human(self) -> None:
        """«خدمة العملاء» was a whole-message SUPPORT phrase. It is one of the
        commonest job fields in the Kingdom and our own gap question — «وش
        أبرز خبرة عملية عندك؟» — invites it, but SUPPORT resolves before any
        onboarding routing, so the answer was discarded and a ticket raised.
        Same reasoning that already keeps «دعم فني» out of the set."""
        for phrase in ("خدمة العملاء", "خدمه العملاء", "خدمة عملاء"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase
        # …while the escape hatch we DO print everywhere is untouched
        for phrase in ("دعم", "الدعم", "مساعدة", "ابي مساعده لو سمحت"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.SUPPORT, phrase


class TestTheClauseSplitterMustNotReadAListAsACommand:
    """AUDIT 2026-08-06. Clause splitting exists for «لا تراسلوني، إلغاء
    الاشتراك», and it was applied to SUPPORT as well as STOP — but a comma is
    not only a clause boundary, it is how a person writes a LIST. So the same
    commit that took «خدمة العملاء» out of the phrase set to stop discarding
    answers re-opened the identical hole through punctuation, and the docstring
    that claimed splitting "can only find a command inside a sentence that
    already contained one outright" was describing something the code did not
    do.
    """

    def test_a_comma_separated_list_of_career_fields_is_not_a_call_for_help(
        self,
    ) -> None:
        """These are answers to our own gap question «وش أبرز خبرة عملية
        عندك؟». The worker resolves SUPPORT before it routes anything into
        onboarding — it writes the ticket, pages the operator, acks and
        returns — so reading one of these as SUPPORT throws the customer's
        answer away, opens a ticket against them, and asks again."""
        for phrase in ("تسويق، دعم، مبيعات", "sales, support, marketing",
                       "أعمل في مجال المبيعات، دعم"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.OTHER, phrase

    def test_the_escape_hatch_is_untouched_by_that_fix(self) -> None:
        """The counterweight. «دعم» is printed in every error message, in the
        onboarding copy and on the store page; SUPPORT giving up clause
        reading may not cost the word itself, wrapped or bare."""
        for phrase in ("دعم", "الدعم", "مساعده", "support", "help",
                       "ابي مساعده لو سمحت", "يا اخوي ساعدني", "ممكن مساعدة"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.SUPPORT, phrase

    def test_the_opt_out_keeps_the_clause_reading_it_was_built_for(
        self,
    ) -> None:
        """The other counterweight, and the reason this is not a revert. A
        missed opt-out is a Meta compliance breach that fails in silence; a
        false SUPPORT ticket destroys a paying customer's answer. The two
        costs point opposite ways, so the two sets no longer share one rule —
        STOP still reads every clause."""
        for phrase in ("لا تراسلوني، إلغاء الاشتراك", "ما أبغى رسائل، إيقاف",
                       "ما عاد أبيكم. إلغاء الاشتراك",
                       "لا تتصلون علي\nإيقاف الرسائل"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.STOP, phrase

    def test_the_clause_marks_a_real_phone_keyboard_produces(self) -> None:
        """The splitter knew a comma, a full stop and a SPACED ascii hyphen.
        A phone keyboard offers an em dash, an ellipsis and a slash, and a
        customer types a colon — so «لا تراسلوني — إلغاء الاشتراك» stayed one
        clause with a negation in it, which is a silently-missed opt-out."""
        for phrase in ("لا تراسلوني — إلغاء الاشتراك",
                       "لا تراسلوني…إلغاء الاشتراك",
                       "لا تراسلوني/إلغاء الاشتراك",
                       "ملاحظة: إلغاء الاشتراك",
                       "لا تراسلوني-إلغاء الاشتراك"):
            kind, _ = classify_inbound(phrase)
            assert kind is InboundKind.STOP, phrase

    def test_what_the_stop_bias_still_does_not_reach(self) -> None:
        """Honesty about the bound, since the docstring now claims it rather
        than the unqualified "biased toward detecting STOP". The reading is a
        CLOSED vocabulary: one word we were never taught and the message is a
        sentence for the conversation to answer. «خلاص إلغاء الاشتراك» is a
        real opt-out and we do not hear it — the same rule that keeps «متى
        ينتهي الاشتراك» from cancelling anything."""
        kind, _ = classify_inbound("خلاص إلغاء الاشتراك")
        assert kind is InboundKind.OTHER


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
