"""Documents that would mislead the owner at the moment he acts.

Not style checking. These two files are what gets PASTED into the store and
into the environment, and a stale number in them is a customer who pays and is
refused by the triple match — the highest-cost failure the system has.
"""

from __future__ import annotations

import pathlib
import re

APPROVED = {"29": "تقييم لمّاح", "199": "لمّاح", "449": "لمّاح+"}
#: Prices from the pre-approval draft. Retired from sale on 2 August.
RETIRED = ("149", "279", "١٤٩", "٢٧٩")


def test_the_store_pages_carry_only_approved_prices() -> None:
    """docs/STORE-PAGES-AR.md is pasted verbatim into Salla. A retired price
    surviving here is a store selling at an amount the environment does not
    expect, and the triple match refuses every such order."""
    text = pathlib.Path("docs/STORE-PAGES-AR.md").read_text(encoding="utf-8")
    for price in RETIRED:
        assert not re.search(rf"(?<!\d){price}(?!\d)\s*(ريال|ر\b)", text), (
            f"the store pages still quote the retired price {price}"
        )
    for price in APPROVED:
        assert price in text or _arabic_digits(price) in text, (
            f"the approved price {price} is missing from the store pages"
        )


def test_the_products_sheet_says_which_pricing_is_live() -> None:
    """The sheet keeps the pre-approval draft on purpose — it carries the
    arguments the decision was built on, and deleting it would hide the WHY.
    But it then contains two contradictory price sets, so it must say
    unmistakably which one is real, before either of them is read."""
    text = pathlib.Path("docs/PRODUCTS-SHEET.md").read_text(encoding="utf-8")
    head = text[:1200]
    assert "اقرأ هذا أولًا" in head, "the warning must come before any price"
    assert "١٩٩" in head or "199" in head
    assert "لا شيء منها ساري" in head


def _arabic_digits(value: str) -> str:
    table = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    return value.translate(table)
