

# ── the cheapest APPROVED daily template wins (2 August) ────────────────────


def test_the_utility_template_is_preferred_once_meta_approves_it() -> None:
    """`daily_opportunities_utility` was approved under the MARKETING category
    despite its name, and Meta refuses to re-categorise an approved template
    («You cannot update an approved template category»). A UTILITY-shaped
    replacement was submitted; the run must adopt it the moment it lands, with
    no deploy — marketing costs roughly three times as much per message AND is
    subject to per-user marketing limits, so a paying customer could miss
    their own delivery."""
    from career.whatsapp.templates import (
        DAILY_MARKETING,
        DAILY_SERVICE_UPDATE,
        DAILY_UTILITY,
        TemplateCategory,
        preferred_daily_template,
    )

    assert DAILY_SERVICE_UPDATE.category is TemplateCategory.UTILITY

    # once approved, it wins
    assert preferred_daily_template(
        {"daily_service_update", "daily_opportunities_utility",
         "daily_opportunities_marketing"}
    ) is DAILY_SERVICE_UPDATE

    # still pending → the long-standing one keeps the day running
    assert preferred_daily_template(
        {"daily_opportunities_utility", "daily_opportunities_marketing"}
    ) is DAILY_UTILITY

    # Meta unreachable → never gamble the day on an unverified name
    assert preferred_daily_template(None) is DAILY_UTILITY

    # marketing is the LAST resort, but a last resort is still better than a
    # name Meta never approved: an unapproved template does not send at all,
    # and a day that costs more beats a day that never arrives.
    assert preferred_daily_template(
        {"daily_opportunities_marketing"}) is DAILY_MARKETING
    assert DAILY_MARKETING.category is TemplateCategory.MARKETING


def test_the_daily_service_update_reads_as_a_transaction_not_an_offer() -> None:
    """Meta classifies from CONTENT. Promotional punctuation or enticement is
    what pushed the previous one into MARKETING."""
    from career.whatsapp.templates import DAILY_SERVICE_UPDATE

    body = DAILY_SERVICE_UPDATE.body
    assert "اشتراكك" in body                    # tied to what they paid for
    for promo in ("!", "🎯", "الآن!", "عرض خاص", "مجانًا"):
        assert promo not in body
