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


#: Each paid entitlement column, with the Arabic phrases that SELL it on the
#: store pages. Deliberately a mapping and not a blacklist: the test permits a
#: phrase the moment its column is actually read by the product, so
#: implementing the feature un-blocks the copy automatically instead of
#: requiring somebody to remember this file.
_SOLD_ENTITLEMENTS: dict[str, tuple[str, ...]] = {
    "human_review_monthly": ("مراجعة بشرية شهرية", "المراجعة الشهرية"),
    "queue_priority": ("أولوية في الطابور", "أولوية معالجة"),
    "intro_blurb": ("نبذة تمهيدية", "نبذة تقديم"),
    "weekly_report": ("تقرير أسبوعي",),
}


def _read_by_product(column: str) -> bool:
    """Does any module outside the schema itself consult this entitlement?"""
    root = pathlib.Path("src/career")
    for path in root.rglob("*.py"):
        if path.name == "models.py":
            continue          # declaring a column is not reading it
        if re.search(rf"\b{column}\b", path.read_text(encoding="utf-8")):
            return True
    return False


def test_the_store_never_sells_an_entitlement_no_code_reads() -> None:
    """لمّاح+ at 449 sold three things backed by nothing.

    `human_review_monthly`, `queue_priority` and `intro_blurb` existed as
    columns and as sentences on the sales page, and not one line of the
    product ever read them: no reminder, no queue, no blurb. A customer paying
    double received exactly what the 199 customer received, and would have
    discovered it a month later.

    The guard is structural rather than a list of banned words — a column the
    product genuinely reads may be sold freely.
    """
    text = pathlib.Path("docs/STORE-PAGES-AR.md").read_text(encoding="utf-8")
    for column, phrases in _SOLD_ENTITLEMENTS.items():
        if _read_by_product(column):
            continue
        for phrase in phrases:
            assert phrase not in text, (
                f"the store sells «{phrase}» but nothing reads {column} — "
                "either implement it or stop selling it"
            )


def test_no_refund_is_promised_as_automatic() -> None:
    """Nothing in this project ever initiates a refund.

    We receive Salla's refund notification and stop the service; the money
    only moves when a human moves it. «الاسترداد الكامل تلقائي» promised a
    machine that does not exist, and the customer it fails is by definition
    one already unhappy enough to ask for their money back."""
    text = pathlib.Path("docs/STORE-PAGES-AR.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "استرداد" in line or "نستردّ" in line:
            assert "تلقائي" not in line, (
                f"a refund is promised as automatic, and none is: {line!r}"
            )


def test_the_store_quantities_cannot_oversell_the_founding_wave() -> None:
    """Salla stock is PER PRODUCT and the passes are separate products.

    The settings table asked for quantity 30 on one and 15 on the other while
    the page promises «ما نبيع الكرسي رقم ٣١» — thirty-one through forty-five
    were sellable, and nothing in the payment path may refuse them (Constant
    4: a paid order becomes a subscription). The only place this limit can be
    enforced is the store configuration, so the sheet that configures it must
    add up."""
    from career.salla.seats import FOUNDING_SEATS_CAP

    text = pathlib.Path("docs/STORE-PAGES-AR.md").read_text(encoding="utf-8")
    quantities = [
        int(m.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
        for m in re.findall(r"\|\s*كمية «لمّاح\+?»\s*\|\s*\*\*([٠-٩\d]+)\*\*", text)
    ]
    assert len(quantities) == 2, "both pass quantities must be in the table"
    assert sum(quantities) <= FOUNDING_SEATS_CAP, (
        f"the store is configured to sell {sum(quantities)} founding seats "
        f"while the page promises {FOUNDING_SEATS_CAP}"
    )


def _arabic_digits(value: str) -> str:
    table = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    return value.translate(table)


def test_a_stale_container_is_visible_on_the_health_screen() -> None:
    """Committed is not deployed, and nothing used to say so.

    Thirty-two commits — including the fix that made zero-touch activation
    work for a real customer — sat in the repository for four days while the
    container serving both webhooks ran the image built before them. Every
    light on the health screen was green the whole time, honestly: the worker
    and the timers run from the host checkout and really were current. Only
    the baked image was stale, and nothing compared the two.
    """
    from career.telegram.views import render_health

    same, _ = render_health({"deployed_source": ("abc123abc123", "abc123abc123")})
    assert "🟢 مطابق للمستودع" in same

    drifted, _ = render_health({"deployed_source": ("oldoldoldold", "newnewnewnew")})
    assert "🔴" in drifted and "أعد بناء الحاوية" in drifted
    # both hashes named, and each alone on its line — a Latin hash inside an
    # Arabic sentence is scrambled by the operator's client
    assert "oldoldoldold" in drifted.splitlines()
    assert "newnewnewnew" in drifted.splitlines()

    unknown, _ = render_health({})
    assert "⚪" in unknown


def test_the_fingerprint_changes_when_the_source_changes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """It must react to an edit, an addition and a deletion — a hash that only
    covers file contents would miss a renamed or removed module."""
    from career.fingerprint import source_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n", encoding="utf-8")
    first = source_fingerprint(tmp_path)

    assert source_fingerprint(tmp_path) == first, "must be stable"

    (tmp_path / "pkg" / "b.py").write_text("y = 3\n", encoding="utf-8")
    edited = source_fingerprint(tmp_path)
    assert edited != first

    (tmp_path / "pkg" / "c.py").write_text("z = 4\n", encoding="utf-8")
    added = source_fingerprint(tmp_path)
    assert added != edited

    (tmp_path / "pkg" / "c.py").unlink()
    assert source_fingerprint(tmp_path) == edited, "a deletion must show"

    # a stray cache directory must not move it
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("q = 9\n", encoding="utf-8")
    assert source_fingerprint(tmp_path) == edited
