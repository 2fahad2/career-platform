"""WhatsApp template categories — what we asked for, what Meta answered.

The category is Meta's answer and not our claim: five approved templates were
moved from UTILITY to MARKETING after approval, and the last section pins what
the code does with a measurement that arrives after this file was written.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

# ── the cheapest APPROVED daily template wins (2 August) ────────────────────


def test_the_utility_template_is_preferred_once_meta_approves_it() -> None:
    """The daily chooser must adopt a better template the moment Meta approves
    it, with no deploy — marketing costs ~4.7× per message in Saudi Arabia
    ($0.0501 vs $0.0107, Meta's live card 2026-08-08) AND can be suppressed
    outright by a recipient's marketing setting (131050) or the per-user
    marketing cap (131049), so a paying customer could miss their own delivery.

    The `approved` argument here is a bare set of names: «approved, category
    unknown». That is the OLD contract, kept working on purpose because
    `engine/cli.py` (not owned by this change) still passes one, and it falls
    back to the hand-ordered preference — a guess. The Mapping form below is
    the one that cannot be wrong.
    """
    from career.whatsapp.templates import (
        DAILY_MARKETING,
        DAILY_SERVICE_UPDATE,
        DAILY_UTILITY,
        SUBSCRIPTION_DAILY_REPORT,
        preferred_daily_template,
    )

    # once approved, it wins
    assert preferred_daily_template(
        {"daily_service_update", "daily_opportunities_utility",
         "daily_opportunities_marketing"}
    ) is DAILY_SERVICE_UPDATE

    # the older names keep the day running while a better one is pending
    assert preferred_daily_template(
        {"daily_opportunities_utility", "daily_opportunities_marketing"}
    ) is DAILY_UTILITY

    # Meta unreachable → the verified-APPROVED, verified-UTILITY name. This was
    # `daily_opportunities_utility` on the reasoning «the only name we have
    # ever seen Meta accept»; that is no longer true, and the old fallback is
    # MARKETING at Meta, so the unattended path used to cost 4.7× and could be
    # suppressed for a customer who has marketing switched off.
    assert preferred_daily_template(None) is SUBSCRIPTION_DAILY_REPORT

    # marketing is the LAST resort, but a last resort is still better than a
    # name Meta never approved: an unapproved template does not send at all,
    # and a day that costs more beats a day that never arrives.
    assert preferred_daily_template(
        {"daily_opportunities_marketing"}) is DAILY_MARKETING


def test_the_chooser_obeys_metas_category_over_our_preference_order() -> None:
    """AUDIT 2026-08-08 — the hand-ordered preference list is a guess about
    categories, and the guess was wrong on three of its four entries. When the
    probe returns categories as well as names, the choice is made on META'S
    answer: any approved candidate Meta calls utility beats every candidate it
    calls marketing, whatever our ordering or our names claim.
    """
    from career.whatsapp.templates import (
        DAILY_MARKETING,
        SUBSCRIPTION_DAILY_REPORT,
        preferred_daily_template,
    )

    # Today's real inventory. Name-order alone would already pick the right
    # one, so the discriminating case is the one below it.
    assert preferred_daily_template({
        "subscription_daily_report": "UTILITY",
        "daily_service_update": "MARKETING",
        "daily_opportunities_utility": "MARKETING",
        "daily_opportunities_marketing": "MARKETING",
    }) is SUBSCRIPTION_DAILY_REPORT

    # The case the old code got wrong: the FIRST-preferred name is marketing at
    # Meta and a later one is utility. Preference order says «take the first»;
    # Meta says otherwise, and Meta is the one billing and delivering.
    assert preferred_daily_template({
        "subscription_daily_report": "MARKETING",
        "daily_opportunities_marketing": "UTILITY",
    }) is DAILY_MARKETING

    # Nothing utility today: the day still runs (a customer who gets nothing is
    # worse than a customer who costs more), but on the preference order.
    assert preferred_daily_template({
        "daily_service_update": "MARKETING",
        "daily_opportunities_marketing": "MARKETING",
    }).name == "daily_service_update"


def test_the_daily_service_update_reads_as_a_transaction_not_an_offer() -> None:
    """Meta classifies from CONTENT. Promotional punctuation or enticement is
    what pushed the previous one into MARKETING."""
    from career.whatsapp.templates import DAILY_SERVICE_UPDATE

    body = DAILY_SERVICE_UPDATE.body
    assert "اشتراكك" in body                    # tied to what they paid for
    for promo in ("!", "🎯", "الآن!", "عرض خاص", "مجانًا"):
        assert promo not in body


def test_the_body_that_survived_review_never_mentions_opportunities() -> None:
    """The empirical lesson from two submissions on the same day: Meta accepted
    `daily_service_update` as UTILITY and later moved it to MARKETING, while
    `subscription_daily_report` still holds UTILITY. The only difference is
    that the surviving one never announces «الفرص» — their classifier reads
    that as promotion regardless of what the customer paid for. Pin it, so a
    future edit does not casually re-add the word and silently multiply the
    per-message cost."""
    from career.whatsapp.templates import (
        DAILY_PREFERENCE,
        SUBSCRIPTION_DAILY_REPORT,
        TemplateCategory,
        observed_category,
    )

    assert DAILY_PREFERENCE[0] is SUBSCRIPTION_DAILY_REPORT
    assert observed_category(SUBSCRIPTION_DAILY_REPORT.name) is (
        TemplateCategory.UTILITY
    )
    assert "فرص" not in SUBSCRIPTION_DAILY_REPORT.body
    assert "اشتراكك" in SUBSCRIPTION_DAILY_REPORT.body


# ── a category is Meta's answer, never our claim (audit 2026-08-08) ──────────


def test_the_name_that_says_utility_is_marketing_at_meta() -> None:
    """`daily_opportunities_utility` is APPROVED as MARKETING. Its name is the
    single clearest example in this repository of a claim nobody verified, and
    a template name is immutable at Meta, so the name cannot be corrected —
    only stripped of authority. Nothing may derive a category from it.
    """
    from career.whatsapp.templates import (
        DAILY_UTILITY,
        TemplateCategory,
        billed_category,
        observed_category,
    )

    assert "utility" in DAILY_UTILITY.name
    assert DAILY_UTILITY.requested_category is TemplateCategory.UTILITY
    assert observed_category(DAILY_UTILITY.name) is TemplateCategory.MARKETING
    assert billed_category(DAILY_UTILITY.name) is TemplateCategory.MARKETING


def test_five_templates_were_re_categorised_by_meta_after_approval() -> None:
    """The measurement, pinned. Every one of these was SUBMITTED as utility,
    ACCEPTED as utility, and carries ``previous_category: UTILITY`` today while
    reporting ``category: MARKETING`` — Meta moved them after approving them,
    without asking. That is why a submitted category can never be treated as a
    live fact, and why :func:`category_divergences` has to run nightly.
    """
    from career.whatsapp.templates import (
        META_CATEGORY_OBSERVED,
        REGISTRY,
        TemplateCategory,
    )

    moved = {
        name for name, live in META_CATEGORY_OBSERVED.items()
        if live is TemplateCategory.MARKETING
        and REGISTRY[name].requested_category is TemplateCategory.UTILITY
    }
    assert moved == {
        "daily_service_update", "daily_opportunities_utility",
        "welcome_activation", "onboarding_reminder", "renewal_reminder",
    }
    # …and exactly one survivor.
    assert [
        name for name, live in META_CATEGORY_OBSERVED.items()
        if live is TemplateCategory.UTILITY
    ] == ["subscription_daily_report"]


def test_every_template_we_actually_send_has_a_measured_category() -> None:
    """A spec with no measurement is a spec whose price and deliverability we
    are guessing at. `zero_day_report` is the one legitimate absence: it has no
    call site anywhere in the tree and does not exist at Meta, so sending it
    would be rejected outright — its absence here is that sentence, checked.
    """
    from career.whatsapp.templates import (
        DAILY_PREFERENCE,
        META_CATEGORY_OBSERVED,
        ONBOARDING_REMINDER,
        RECOVERY,
        REGISTRY,
        RENEWAL_REMINDER,
        WELCOME_ACTIVATION,
        ZERO_DAY_REPORT,
    )

    sendable = {spec.name for spec in DAILY_PREFERENCE} | {
        WELCOME_ACTIVATION.name, ONBOARDING_REMINDER.name,
        RENEWAL_REMINDER.name, RECOVERY.name,
    }
    assert sendable <= set(META_CATEGORY_OBSERVED)
    assert ZERO_DAY_REPORT.name not in META_CATEGORY_OBSERVED
    assert set(REGISTRY) - sendable == {ZERO_DAY_REPORT.name}


def test_an_unmeasured_template_is_billed_at_the_marketing_rate() -> None:
    """Err expensive. A category we have not measured must never make the bill
    look smaller than it is — the same rule `cv/close` already applied to
    template names it did not recognise at all."""
    from career.whatsapp.templates import (
        TemplateCategory,
        billed_category,
        observed_category,
    )

    assert observed_category("zero_day_report") is None
    assert billed_category("zero_day_report") is TemplateCategory.MARKETING
    assert observed_category("never_submitted") is None
    assert billed_category("never_submitted") is TemplateCategory.MARKETING


def test_divergence_from_the_live_account_is_detectable() -> None:
    """The snapshot is only a measurement if something re-measures it. Meta
    re-categorises approved templates silently — five times on this account
    already — so a snapshot with no nightly comparison is the same defect one
    week older."""
    from career.whatsapp.templates import (
        META_CATEGORY_OBSERVED,
        category_divergences,
    )

    live_today = {name: str(cat).upper()
                  for name, cat in META_CATEGORY_OBSERVED.items()}
    assert category_divergences(live_today) == {}

    # Meta's own sample templates live on this account and are not ours.
    assert category_divergences(
        {**live_today, "hello_world": "UTILITY"}
    ) == {}

    # the two failures that must be loud
    moved = {**live_today, "subscription_daily_report": "MARKETING"}
    assert category_divergences(moved) == {
        "subscription_daily_report": ("utility", "marketing")
    }
    gone = {k: v for k, v in live_today.items() if k != "renewal_reminder"}
    assert category_divergences(gone) == {
        "renewal_reminder": ("marketing", "absent")
    }


# ── a measurement newer than the file (2026-08-08, the receiving half) ───────
#
# Meta PUSHES `template_category_update` the moment it moves a template. The
# app was never subscribed to that field — `GET /{app_id}/subscriptions`
# answered `fields: ['messages']` and nothing else — so the five that moved
# from UTILITY to MARKETING were found by a hand audit weeks later. Once the
# owner subscribes, the push is a measurement NEWER than this file, and the
# file cannot write itself. These pin what the code does with it.

@pytest.fixture(autouse=True)
def _no_leaked_observations() -> Iterator[None]:
    """The overlay is module state. A test that records into it and walks away
    would silently re-price another test's WhatsApp bill."""
    from career.whatsapp.templates import forget_live_observations

    forget_live_observations()
    yield
    forget_live_observations()


def test_a_pushed_category_outranks_the_dated_constant() -> None:
    """`META_CATEGORY_OBSERVED` is dated 2026-08-08 and that date is the point.
    A push that arrives afterwards is a newer reading of the same fact, and a
    process that has just been TOLD `welcome_activation` is UTILITY again and
    goes on billing it as MARKETING is preferring a constant to evidence."""
    from datetime import date

    from career.whatsapp.templates import (
        TemplateCategory,
        billed_category,
        observed_category,
        record_observed_category,
    )

    assert observed_category("welcome_activation") is TemplateCategory.MARKETING

    # an appeal succeeded and Meta pushed the reversal
    assert record_observed_category(
        "welcome_activation", "UTILITY",
        observed_on=date(2026, 8, 20), source="webhook",
    )
    assert observed_category("welcome_activation") is TemplateCategory.UTILITY
    assert billed_category("welcome_activation") is TemplateCategory.UTILITY

    # and the daily chooser sees it too — same door, no second reader
    from career.whatsapp.templates import META_CATEGORY_OBSERVED

    # the FILE is untouched: it is not a cache, it is the durable record, and
    # nothing here may make it claim a measurement it did not take
    assert META_CATEGORY_OBSERVED["welcome_activation"] is (
        TemplateCategory.MARKETING
    )


def test_a_divergent_push_names_the_exact_edit_that_fixes_the_file() -> None:
    """«A divergence was detected» is not something anyone can act on at 6am.
    The line has to carry the constant, the value and the date, because the
    only thing that makes the file true again is a human typing them."""
    from datetime import date

    from career.whatsapp.templates import record_observed_category

    logger = logging.getLogger("career.whatsapp")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        record_observed_category(
            "subscription_daily_report", "MARKETING",
            observed_on=date(2026, 8, 20), source="webhook",
        )
    finally:
        logger.removeHandler(handler)

    loud = [r for r in records if r.levelno >= logging.ERROR]
    assert loud, "a divergence the operator never hears about is the old defect"
    msg = loud[0].getMessage()
    assert "subscription_daily_report" in msg
    assert "META_CATEGORY_OBSERVED" in msg
    assert "MARKETING" in msg
    assert "2026-08-20" in msg


def test_the_overlay_never_claims_a_template_we_do_not_send() -> None:
    """The live account carries Meta's own `hello_world` and `jaspers_market_*`
    samples. This module saying anything at all about a template we never send
    is the same unearned claim in a new place, so the overlay is bounded by
    REGISTRY exactly as the file is."""
    from datetime import date

    from career.whatsapp.templates import (
        META_CATEGORY_OBSERVED,
        observed_category,
        record_observed_category,
    )

    assert not record_observed_category(
        "hello_world", "UTILITY", observed_on=date(2026, 8, 20), source="probe",
    )
    assert observed_category("hello_world") is None
    assert "hello_world" not in META_CATEGORY_OBSERVED


def test_a_stale_redelivery_cannot_undo_a_fresher_reading() -> None:
    """Meta redelivers webhooks freely and out of order, and the nightly probe
    runs beside them. A measurement is only allowed to win on its DATE."""
    from datetime import date

    from career.whatsapp.templates import (
        TemplateCategory,
        observed_category,
        record_observed_category,
    )

    record_observed_category("renewal_reminder", "UTILITY",
                             observed_on=date(2026, 9, 1), source="probe")
    # a webhook from three weeks earlier arrives late
    assert not record_observed_category(
        "renewal_reminder", "MARKETING",
        observed_on=date(2026, 8, 10), source="webhook",
    )
    assert observed_category("renewal_reminder") is TemplateCategory.UTILITY

    # and nothing older than the file may speak at all
    assert not record_observed_category(
        "recovery", "UTILITY", observed_on=date(2026, 8, 1), source="webhook",
    )
    assert observed_category("recovery") is TemplateCategory.MARKETING


def test_a_category_word_meta_invents_is_not_guessed_at() -> None:
    """AUTHENTICATION exists at Meta and has no member here. Coercing it into
    one of the two we know would put a fabricated category into the bill."""
    from datetime import date

    from career.whatsapp.templates import (
        TemplateCategory,
        billed_category,
        record_observed_category,
    )

    assert not record_observed_category(
        "recovery", "AUTHENTICATION",
        observed_on=date(2026, 8, 20), source="webhook",
    )
    assert billed_category("recovery") is TemplateCategory.MARKETING


def test_no_spec_can_answer_a_category_from_what_we_merely_requested() -> None:
    """The `TemplateSpec.category` shim resolved to `requested_category` when
    Meta had never been asked — i.e. it could answer «utility» about a template
    nobody had ever measured. That is the exact claim this module exists to
    stop making, and its last two readers (`engine/cli.py`) now go through
    `billed_category`."""
    from career.whatsapp.templates import ZERO_DAY_REPORT, TemplateSpec

    assert not hasattr(TemplateSpec, "category")
    assert ZERO_DAY_REPORT.requested_category is not None
