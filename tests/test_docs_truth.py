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
#:
#: «49» was absent from this tuple until 2026-08-05, and it was the one that
#: mattered: the analysis product was drafted at 49 and APPROVED at 29, so 49
#: is the retired price the repository was still repeating — in the docstring
#: of the extractor a reader takes for the spec, in the rehearsal script whose
#: whole claim is «the door works end to end», and in the generator of the
#: sample report we show buyers. The guard that would have caught it read one
#: file, so it could not have caught it either way; see below.
RETIRED = ("49", "149", "279", "٤٩", "١٤٩", "٢٧٩")


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


# ── the same stale price, everywhere else it can hide ────────────────────────


#: Currency words that turn a bare number into an AMOUNT. `ريال` is a prefix
#: match on purpose so `ريالًا` and `ريالات` are covered too.
_CURRENCY = r"(?:ريال|ر\.س|SAR|﷼)"

#: A retired amount that is not part of a longer number and not the tail of a
#: decimal. Python's `\d` is Unicode-aware, so the one lookbehind excludes
#: «٤٩» inside «٤٤٩» exactly as it excludes «49» inside «449»; the leading
#: `.`/`٫` guard keeps `0.49` and `1.279` out of it.
_RETIRED_AMOUNT = r"(?<![\d.,٫٬])(?:" + "|".join(RETIRED) + r")(?!\d)"

#: How close the currency word has to be. Twelve characters is wide enough for
#: «149.00 SAR» and for the whitepaper's «~149 <small>ريال», and narrow enough
#: that the things a bare 49 legitimately IS — a line number (`flow.py:149` in
#: the July audit), a count, a cap, a token limit — never reach one.
_PRICE_GAP = 12

_STALE_PRICE = re.compile(
    rf"{_RETIRED_AMOUNT}.{{0,{_PRICE_GAP}}}?{_CURRENCY}"
    rf"|{_CURRENCY}.{{0,{_PRICE_GAP}}}?{_RETIRED_AMOUNT}"
)

_SCANNED_SUFFIXES = frozenset({".py", ".md", ".html", ".sh", ".sql", ".txt"})

#: Files that quote a retired price ON PURPOSE, and what makes each one a
#: record rather than an offer. Every one is history: the boot check whose
#: docstring names the exact figure the store sold for twelve days, the
#: whitepaper's original v1.0 anchors (superseded by CHANGELOG-v1.1), the
#: products sheet that keeps the pre-approval draft under a warning header the
#: test above enforces, and two dated audit records. Deleting the number from
#: any of them deletes the reason the guard exists — but the exemption is per
#: FILE and written down, so a new stale price in a file nobody thought about
#: is still caught.
_PRICE_HISTORY: dict[str, str] = {
    "src/career/engine/cli.py":
        "the twelve-day 149.00 SAR incident verify_environment exists for",
    "docs/WHITEPAPER.html":
        "v1.0 indicative anchors — superseded by docs/CHANGELOG-v1.1.md",
    "docs/PRODUCTS-SHEET.md":
        "the pre-approval draft, kept deliberately under its own warning",
    "docs/CLOSURE-AUDIT-2026-07-31.md":
        "a dated audit; quoting the wrong price IS its finding",
    "docs/DEVIATIONS.md":
        "D17, a dated deviation record of what was true on 31 July",
}


def _stale_price_hits() -> list[str]:
    hits: list[str] = []
    for root in ("src", "scripts", "docs"):
        for path in sorted(pathlib.Path(root).rglob("*")):
            if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
                continue
            if path.as_posix() in _PRICE_HISTORY:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if _STALE_PRICE.search(line):
                    hits.append(f"{path.as_posix()}:{number}: {line.strip()[:100]}")
    return hits


def test_no_living_file_quotes_a_retired_price() -> None:
    """The store pages were never the only place a price is written down.

    The guard above read ONE file, and the price it did not know about was 49 —
    the analysis product's draft figure, approved at 29. So a docstring in the
    live extractor told every future reader the funnel costs 49, the rehearsal
    script announced «the 49-SAR door works end to end» while proving the 29-SAR
    one, and the sample-report generator labelled its output with it. None of
    that reaches a customer directly, which is exactly why it survives: it is
    the layer people read to find out what is true, and the next person to wire
    a price reads it and is wrong.

    A number only counts as a price when a currency word is beside it, so the
    line numbers, counts and caps that are legitimately 49 do not trip it.
    """
    hits = _stale_price_hits()
    assert not hits, (
        "a retired price is quoted as an amount in files that are read as "
        "current truth:\n" + "\n".join(hits)
    )


def test_the_price_history_exemptions_still_earn_their_place() -> None:
    """An allowlist nobody re-checks becomes permission.

    Each exempted file is exempt because it still CARRIES the old price on
    purpose. The day one of them stops, the entry stops being a stated reason
    and becomes a hole in the guard — so it has to be deleted, and this says
    so rather than waiting for the hole to be used.
    """
    stale = [
        path for path in _PRICE_HISTORY
        if not _STALE_PRICE.search(
            pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        )
    ]
    assert not stale, (
        f"these files no longer quote a retired price at all: {stale} — remove "
        "them from _PRICE_HISTORY so the guard covers them again"
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
    # Found by a later sweep, all of the same class as the three above and all
    # missed because the map was written from the three we already knew:
    "support_sla_hours": ("دعم خلال ٢٤ ساعة",),
    "banned_companies": ("الشركات اللي ما تبي سيرتك توصلها",),
    "cover_letter": ("خطاب تقديم", "خطاب التغطية"),
}


#: Columns the grep below finds and must NOT count as implemented, with the
#: reason. The heuristic — «mentioned outside models.py» — is right for most
#: entitlements and wrong for these, and a guard that quietly says yes is worse
#: than no guard: `banned_companies` is written as a hardcoded empty dict at
#: policy.py and copied by the privacy export, so it is «mentioned» twice and
#: consulted never. Nothing asks the customer for it and the gate never
#: receives it. Delete an entry here the day the column genuinely does
#: something.
_MENTIONED_BUT_INERT: frozenset[str] = frozenset({"banned_companies"})


def _read_by_product(column: str) -> bool:
    """Does any module outside the schema itself consult this entitlement?"""
    if column in _MENTIONED_BUT_INERT:
        return False
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
    # The first version of this guard looked only for «استرداد»/«نستردّ» and
    # passed while the product page still said «فلوسك ترجع كاملة تلقائيًا» —
    # the same false promise, one synonym away. A guard narrower than the
    # language it polices is a guard that reports success.
    money_words = ("استرداد", "نستردّ", "ترجع", "نرجّع", "يرجع", "المبلغ")
    for line in text.splitlines():
        if any(word in line for word in money_words):
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


def test_an_unscanned_upload_is_visible_on_the_health_screen() -> None:
    """The scanner was an interface with nothing behind it, and said so nowhere.

    For the whole live period the worker injected a stand-in whose scan()
    returned None — the value that means «clean» — so every CV passed, every
    cv_uploads row read `clean`, and this screen carried no line about uploads
    at all. Three states now, each its own colour, and the absent one has to
    say in words that files are passing unscanned: an undisclosed posture is
    the thing being fixed, so a vague amber word would reproduce it.
    """
    from career.onboarding.upload import (
        HEALTH_KEY,
        SCANNER_ABSENT,
        SCANNER_READY,
        SCANNER_UNREACHABLE,
        ScannerHealth,
    )
    from career.telegram.views import render_health

    ready, _ = render_health({HEALTH_KEY: ScannerHealth(SCANNER_READY, "clamd")})
    assert "فاحص الملفات: 🟢 يعمل ويجيب" in ready.splitlines()

    absent, _ = render_health(
        {HEALTH_KEY: ScannerHealth(SCANNER_ABSENT, "none", "no_engine_configured")}
    )
    assert "🟠" in absent and "الملفات تمر بلا فحص" in absent
    # the slug is Latin and must never be folded into an Arabic sentence
    assert "no_engine_configured" not in absent

    down, _ = render_health(
        {HEALTH_KEY: ScannerHealth(SCANNER_UNREACHABLE, "clamd", "engine_timeout")}
    )
    assert "فاحص الملفات: 🔴 مركّب ولا يستجيب" in down.splitlines()
    assert "engine_timeout" in down.splitlines()

    # a probe that could not answer is «unknown», never green
    unknown, _ = render_health({})
    assert "فاحص الملفات: ⚪ غير معروف" in unknown.splitlines()


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


# ── the whitepaper vs the changelog that supersedes it ───────────────────────
#
# CLAUDE.md names WHITEPAPER.html the FIRST source of truth and
# CHANGELOG-v1.1.md the second, «تَفوق ما يخالفها» — the changelog wins on any
# conflict. That governance rule had no enforcement at all: nothing in this
# suite read the whitepaper, so it drifted for a month into contradicting the
# shipped product in four places at once (a three-tier catalogue at retired
# prices, an entitlements table selling four columns no code reads, a renewal
# that «extends 30 days» while the code writes one row per order, and a C9 exit
# criterion requiring three plans the approved catalogue reduced to two).
#
# The fix is NOT to rewrite the whitepaper — its retired numbers are the
# argument the later decision was built on, and deleting them hides the WHY.
# It is to mark each contradicting point in place, pointing at the entry that
# overrides it. This section is what makes the marker mandatory: a superseded
# passage that loses its marker, or cites an entry that does not exist, or
# survives a rewrite of the entry it depends on, fails here.

_WHITEPAPER = pathlib.Path("docs/WHITEPAPER.html")
_CHANGELOG = pathlib.Path("docs/CHANGELOG-v1.1.md")

#: A superseded-by banner. Matched on the CSS class rather than on wording:
#: the class is what makes it visible to a reader as «this is no longer true»,
#: and a marker nobody can see is not a marker.
_SUP_OPEN = re.compile(r"<(div|span) class=\"sup\">")
_ANY_TAG = re.compile(r"</?(div|span)\b[^>]*>")


def _sup_blocks(html: str) -> list[str]:
    """Every superseded-by banner, whole.

    A non-greedy regex to the first `</div>` was the obvious way to do this and
    it silently truncated the §04 marker at the inline price list it contains —
    so the test read a marker that had lost its own citation and reported the
    whitepaper as unmarked. Tag depth is counted instead: these blocks nest
    one level and the count is what says where the banner ends.
    """
    blocks: list[str] = []
    for opening in _SUP_OPEN.finditer(html):
        depth = 0
        for tag in _ANY_TAG.finditer(html, opening.start()):
            depth += -1 if tag.group(0).startswith("</") else 1
            if depth == 0:
                blocks.append(html[opening.start():tag.end()])
                break
    return blocks

#: Each passage of the whitepaper the changelog overrides:
#:   section id → (a literal fragment proving the passage is still there,
#:                 the changelog entry number that governs it,
#:                 what the reader would otherwise act on)
#:
#: The fragment is asserted PRESENT on purpose. Without it the test would pass
#: by deletion — and deleting a superseded decision is exactly what the
#: governance rule forbids, because the history is the reason.
_SUPERSEDED: dict[str, tuple[str, str, str]] = {
    "s4": ("~149", "19",
           "a three-tier catalogue at 149/279 — retired on 2 August"),
    "s5": ("الدفع يمدد 30 يومًا", "16",
           "renewal described as extending one row; the code writes one row "
           "per Salla order and retires the previous one"),
    "s13": ("باقات الاشتراك الثلاث", "19",
            "a C9 exit criterion that can never be met — the third plan is "
            "deliberately retired from sale"),
}


def _sections() -> dict[str, str]:
    """The whitepaper split by its own section ids."""
    text = _WHITEPAPER.read_text(encoding="utf-8")
    parts = re.split(r'<section id="(s\d+)">', text)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _changelog_entry(number: str) -> str | None:
    """The body of one numbered changelog entry, or None if there is none."""
    text = _CHANGELOG.read_text(encoding="utf-8")
    match = re.search(rf"^{number}\. \*\*(.*?)(?=^\d+\. \*\*|\Z)",
                      text, re.DOTALL | re.MULTILINE)
    return match.group(0) if match else None


def test_every_superseded_passage_carries_a_visible_marker() -> None:
    """A contradiction between the whitepaper and the changelog fails HERE.

    Nothing guarded the whitepaper at all before this, which is how the first
    source of truth came to describe a product we do not sell. Each entry below
    is a passage that is still correct as HISTORY and wrong as INSTRUCTION, and
    the only thing separating those two readings is the marker.
    """
    sections = _sections()
    for section_id, (fragment, entry, why) in _SUPERSEDED.items():
        body = sections.get(section_id)
        assert body is not None, f"the whitepaper no longer has §{section_id}"
        assert fragment in body, (
            f"§{section_id} no longer contains {fragment!r}. If the passage was "
            f"REWRITTEN, that is a whitepaper decision and needs its own "
            f"`docs: whitepaper vX.Y` commit; if it was DELETED, the history "
            f"that explains «{why}» went with it."
        )
        markers = _sup_blocks(body)
        assert markers, (
            f"§{section_id} contradicts the changelog ({why}) and carries no "
            "superseded-by marker — a reader acts on it as current truth"
        )
        assert any(f"§{entry}" in marker for marker in markers), (
            f"§{section_id}'s marker does not name CHANGELOG-v1.1.md §{entry}, "
            "which is the entry that actually overrides it"
        )


def test_every_marker_points_at_a_changelog_entry_that_exists() -> None:
    """A citation to nothing is worse than no citation: it reads as governance
    while resolving to a section number a later edit renumbered away."""
    for _, (_, entry, _) in _SUPERSEDED.items():
        assert _changelog_entry(entry) is not None, (
            f"the whitepaper defers to CHANGELOG-v1.1.md §{entry} and no such "
            "numbered entry exists"
        )


def test_the_marked_catalogue_quotes_the_prices_actually_on_sale() -> None:
    """The §04 marker is what a reader sees INSTEAD of the retired tiers, so it
    has to carry the live catalogue — otherwise it says «this is wrong» and
    leaves the reader with nowhere to go, which is how the products sheet came
    to hold two price sets with nothing declaring which was real."""
    section = _sections()["s4"]
    marker = "\n".join(_sup_blocks(section))
    for price, product in APPROVED.items():
        assert price in marker, (
            f"the live price of {product} ({price}) is missing from the §04 "
            "superseded-by marker"
        )


def test_the_whitepaper_never_sells_an_entitlement_no_code_reads() -> None:
    """The same structural rule the store pages get, for the document the store
    pages are WRITTEN FROM.

    The 449 tier's entitlements table survived unmarked for a month after the
    store copy was corrected: `human_review_monthly`, `queue_priority`,
    `intro_blurb` and `cover_letter` are columns nothing reads, and this is the
    file a future author would have rebuilt the sales page from. Naming the
    column inside a marker is enough — that is precisely the reader being told
    it is not sold — and implementing the feature lifts the requirement
    automatically, exactly as it does for the store pages.
    """
    text = _WHITEPAPER.read_text(encoding="utf-8")
    marked = "\n".join(_sup_blocks(text))
    for column in _SOLD_ENTITLEMENTS:
        if _read_by_product(column) or column not in text:
            continue
        assert column in marked, (
            f"the whitepaper sells {column} and nothing outside models.py "
            "reads it — mark the row superseded or implement the feature"
        )


def test_the_whitepaper_states_one_version_number() -> None:
    """The tab said v1.1 while the header and the footer said 1.2.

    The v1.2 entry itself ends «الرقم يُدار في مكان واحد من الآن» — written
    after the header and footer had drifted apart the previous time. They had,
    and the `<title>` was the place nobody looked, because it is the one copy
    of the number that never appears on the page.
    """
    text = _WHITEPAPER.read_text(encoding="utf-8")
    # the three places the page states its OWN version: the tab, the header
    # eyebrow and the footer. The dated list in §17 is history and is not one
    # of them — every past number legitimately appears there.
    places = {
        "<title>": re.search(r"<title>[^<]*·\s*v([12]\.\d)</title>", text),
        'class="eyebrow"': re.search(r'class="eyebrow">[^<]*النسخة ([12]\.\d)', text),
        "<footer>": re.search(r"<footer[^>]*>\s*[^<]*النسخة ([12]\.\d)", text),
    }
    missing = [where for where, found in places.items() if found is None]
    assert not missing, f"the whitepaper states no version in: {missing}"
    declared = {where: found.group(1) for where, found in places.items()
                if found is not None}
    assert len(set(declared.values())) == 1, (
        f"the whitepaper declares more than one current version: {declared}"
    )
