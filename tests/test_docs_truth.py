"""Documents that would mislead the owner at the moment he acts.

Not style checking. These two files are what gets PASTED into the store and
into the environment, and a stale number in them is a customer who pays and is
refused by the triple match — the highest-cost failure the system has.
"""

from __future__ import annotations

import ast
import functools
import pathlib
import re
from collections.abc import Iterator

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


#: Each paid entitlement column, with the Arabic phrases that SELL it — on the
#: store pages AND in the whitepaper those pages are written from. Deliberately
#: a mapping and not a blacklist: the test permits a phrase the moment its
#: column is actually read by a module the product LOADS, so implementing the
#: feature un-blocks
#: the copy automatically instead of requiring somebody to remember this file.
#:
#: The COLUMN side of this map is no longer hand-maintained — see
#: `test_the_map_covers_every_paid_entitlement_column`, which derives the set
#: from `plan_entitlements` itself. Only the phrases are written by hand, and
#: they have to carry every wording the two documents actually use: the store
#: page says «٢٤» in Arabic-Indic digits and the whitepaper says «24» in Latin
#: ones, and a guard narrower than the language it polices reports success.
_SOLD_ENTITLEMENTS: dict[str, tuple[str, ...]] = {
    # «مراجعتك البشرية» ALONE is not a sale: §11 lists the owner reading CVs
    # during the alpha as a hallucination MITIGATION, and §14 prices the waves
    # by how many he can read. What the 449 card sells is the recurring one,
    # so the phrase has to carry what makes it recurring and contractual.
    "human_review_monthly": ("مراجعة بشرية شهرية", "المراجعة الشهرية",
                             "مراجعتك البشرية للـMaster CV"),
    "queue_priority": ("أولوية في الطابور", "أولوية معالجة"),
    "intro_blurb": ("نبذة تمهيدية", "نبذة تقديم"),
    "weekly_report": ("تقرير أسبوعي",),
    # Found by a later sweep, all of the same class as the three above and all
    # missed because the map was written from the three we already knew:
    "support_sla_hours": ("دعم خلال ٢٤ ساعة", "دعم خلال 24 ساعة",
                          "دعم خلال 48 ساعة"),
    "banned_companies": ("الشركات اللي ما تبي سيرتك توصلها",),
    "cover_letter": ("خطاب تقديم", "خطاب التغطية"),
    # Latin «15» on the whitepaper's card and Arabic-Indic «١٥» in the banner
    # that supersedes it — both wordings, because the guard has to police the
    # language the documents are actually written in (DEVIATIONS D26 / §04).
    "seats_cap": ("١٥ مقعدًا فقط", "15 مقعدًا فقط"),
}


#: Columns that ARE read by the letter of the rule below and must still not
#: count as implemented, with the reason. `banned_companies` is written as a
#: hardcoded empty dict in `policy.py` (a write, which the reader below already
#: dismisses) and then genuinely read off the row by the privacy export — a
#: real read that consults nothing, because nothing ever asks the customer for
#: the list and the gate never receives it. Copying a column into an export is
#: not a feature. Delete an entry here the day the column genuinely does
#: something.
#:
#: `seats_cap` is here for a different and sharper reason, and it is the one
#: shape no amount of reachability can catch: a literal NAME COLLISION. The
#: two loads the reader sees are `"seats_cap": seats.cap` in the console and
#: `data.get("seats_cap")` in the view that renders it — a dictionary key
#: carrying `FOUNDING_SEATS_CAP`, the shared founding-seat pool, which has
#: nothing to do with the per-plan column. `grep -rn "PlanEntitlement.seats_cap"
#: src/` is empty and has been since migration 0004. Both halves of the guard
#: opened on that spelling (DEVIATIONS D26 / whitepaper §04): one excused the
#: column from the sold-phrase map, the other excused it from needing a marker.
#: A reader that answers «yes» to a matching STRING is measuring spelling, and
#: the only honest answer to a collision is a written-down exemption.
_MENTIONED_BUT_INERT: frozenset[str] = frozenset({"banned_companies", "seats_cap"})


# ── read, or merely mentioned? ───────────────────────────────────────────────
#
# This asked `re.search(rf"\b{column}\b", line)` over every line of every
# module, skipping only imports. That is a MENTION detector, and an adversarial
# review walked it three ways in a minute: a comment naming `cover_letter`, a
# `return intro_blurb` sitting after a `raise`, a column name inside an error
# string. Every one of them flipped the verdict to «implemented» and re-opened
# the sales copy for a feature that does not exist. A guard that says yes to a
# comment is worse than no guard at all, because the copy then ships with its
# blessing.
#
# So the question is put to the syntax tree instead, and answered by what a
# running interpreter could actually TAKE:
#
#   plan.cover_letter               an attribute load off a row or off the
#                                   mapped class — how a real entitlement is read
#   row["daily_job_limit"]          a mapping load
#   row.get("daily_job_limit")      the same, via the method the views use
#   getattr(row, "daily_job_limit") the same, spelled dynamically
#
# The examples are a LIVE column on purpose: what the tree cannot tell you is
# whether the dictionary being subscripted is the entitlement row at all, and
# that is exactly how `seats_cap` walked through — see `_MENTIONED_BUT_INERT`.
#
# and by what it could not: assignment targets and keyword arguments (writes),
# docstrings, comments, error messages, f-strings (prose), and imports. A bare
# NAME is deliberately not a read either — that is what keeps the
# `telegram/weekly_report.py` collision harmless, structurally this time: one
# `from career.telegram import weekly_report` used to tell this guard the
# feature was implemented, and now `weekly_report.build(...)` parses as an
# attribute named `build` on a name nobody inspects.

_UNREACHABLE_AFTER = (ast.Return, ast.Raise, ast.Continue, ast.Break)

#: Conditions whose value is settled before the program starts.
#: `TYPE_CHECKING` is False at runtime by definition, so a block guarded by it
#: is text for a type checker and nothing an interpreter ever enters — and it
#: is the cheapest way to put a read into a module that really is loaded.
_NEVER_TRUE_NAMES = frozenset({"TYPE_CHECKING"})


def _is_never_true(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return not test.value                      # `if False:`, `while 0:`
    if isinstance(test, ast.Name):
        return test.id in _NEVER_TRUE_NAMES
    if isinstance(test, ast.Attribute):
        return test.attr in _NEVER_TRUE_NAMES      # `typing.TYPE_CHECKING`
    return False


def _ends_its_block(statement: ast.stmt) -> bool:
    """Can anything AFTER this statement, in the same block, still run?

    `return`/`raise`/`continue`/`break` are the obvious four. The other two
    shapes an adversarial review used are a process that is already gone —
    `sys.exit(...)` / `os._exit(...)` — and `assert False`, which raises on
    every interpreter this project runs on.
    """
    if isinstance(statement, _UNREACHABLE_AFTER):
        return True
    if isinstance(statement, ast.Assert):
        return _is_never_true(statement.test)
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        function = statement.value.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            return (function.value.id, function.attr) in {
                ("sys", "exit"), ("os", "_exit"), ("os", "abort"),
            }
    return False


def _live_blocks(node: ast.AST) -> Iterator[list[ast.stmt]]:
    """The statement blocks hanging off one node that can be entered at all.

    `cases` is here because `ast.match_case` is neither a statement nor an
    expression: a `match` walked by `body`/`orelse`/`finalbody` alone shows
    NOTHING of itself, so every case body was invisible — a whole statement
    form the guard could not see. Zero `match` statements in `src/career`
    today, which is precisely why it had to be fixed before there is one.
    """
    dead = {"body"} if (isinstance(node, (ast.If, ast.While))
                        and _is_never_true(node.test)) else frozenset[str]()
    for field in ("body", "orelse", "finalbody"):
        if field in dead:
            continue
        block = getattr(node, field, None)
        if isinstance(block, list):
            yield block
    for handler in getattr(node, "handlers", ()) or ():
        yield from _live_blocks(handler)
    for case in getattr(node, "cases", ()) or ():
        yield case.body


def _live_statements(node: ast.AST) -> Iterator[ast.stmt]:
    """Every statement under `node` that a running interpreter could reach.

    The cheap half of reachability, and only that: each block stops at its
    first terminator, and a block whose condition is decided at parse time is
    never entered. It does NOT know whether anything ever CALLS the function a
    statement lives in — see `_read_by_reachable_module` for what that leaves
    open, said in words rather than implied away.
    """
    for block in _live_blocks(node):
        for statement in block:
            if not isinstance(statement, ast.stmt):
                continue
            yield statement
            yield from _live_statements(statement)
            if _ends_its_block(statement):
                break


def _evaluated_by(statement: ast.stmt) -> Iterator[ast.AST]:
    """The nodes one statement evaluates in its own right.

    Its expression children, plus the two places an evaluation hides in a node
    that is NOT an `ast.expr` and was therefore invisible:

      def f(x=row.cover_letter)   `ast.arguments`. A default is evaluated once,
                                  when the `def` runs — a real read. Type
                                  ANNOTATIONS are deliberately excluded: this
                                  repository is `from __future__ import
                                  annotations` throughout, so they are strings
                                  that never evaluate, and counting one would
                                  be an error in the expensive direction.

      case _ if row.cover_letter: `ast.match_case`. The guard, and the value of
                                  a `case row.cover_letter:` pattern, are real
                                  loads. The case BODIES arrive by the other
                                  route, through `_live_statements`.
    """
    for _, value in ast.iter_fields(statement):
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, (ast.expr, ast.keyword,
                                 ast.withitem, ast.comprehension)):
                yield item
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = statement.args
        yield from (default
                    for default in [*arguments.defaults, *arguments.kw_defaults]
                    if default is not None)
    if isinstance(statement, ast.Match):
        for case in statement.cases:
            yield case.pattern
            if case.guard is not None:
                yield case.guard


def _live_expressions(tree: ast.Module) -> Iterator[ast.expr]:
    """Every expression a reachable statement evaluates.

    Walking the expression children of each live statement — rather than
    `ast.walk` over the whole module — is what keeps dead code dead: neither a
    Python expression nor a `match` pattern can contain a statement, so nothing
    yielded here can re-enter a block `_live_statements` already cut off.
    """
    for statement in _live_statements(tree):
        for item in _evaluated_by(statement):
            yield from (node for node in ast.walk(item)
                        if isinstance(node, ast.expr))


def _is_literal(node: ast.expr | None, text: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == text


def _expression_reads(node: ast.expr, column: str) -> bool:
    """Would evaluating this one expression take the column's VALUE?"""
    if isinstance(node, ast.Attribute):
        # Store/Del contexts are writes; only a load is a read.
        return node.attr == column and isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Subscript):
        return isinstance(node.ctx, ast.Load) and _is_literal(node.slice, column)
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == "get" and node.args:
            return _is_literal(node.args[0], column)
        if (isinstance(function, ast.Name) and function.id == "getattr"
                and len(node.args) >= 2):
            return _is_literal(node.args[1], column)
    return False


# ── which modules the product actually LOADS ─────────────────────────────────
#
# The reader below used to walk every `.py` under `src/career` with no notion
# of imports at all, and called the result «read by the product». An
# adversarial review put fifteen lines in a NEW module — one function, called
# by nobody, in a file imported by nobody — and `intro_blurb`, `cover_letter`
# and `weekly_report` all read as implemented, which re-opens the sales copy
# for three features that do not exist, with the whole file green. It did not
# even need `if TYPE_CHECKING:`; an ordinary orphan was enough.
#
# So the claim is made true at MODULE granularity: a file may vouch for a
# column only if starting one of the real processes would import it. What that
# still does not prove is written on `_read_by_reachable_module` in words.

_SRC = pathlib.Path("src")
_PACKAGE = _SRC / "career"

#: The processes, and why each one is a root. `scripts/run_*.py` is globbed
#: rather than listed because that is how the three systemd units name them
#: (career-worker → run_worker_loop, career-admin-bot → run_admin_bot,
#: career-engine-nightly → run_nightly), and a fourth runner added tomorrow
#: must not be silently missing from the graph.
#:
#: This mapping is the one place a module can be declared reachable by hand,
#: which makes it the place to look first if this guard is ever walked around
#: again: an entry here is a claim that a PROCESS starts at that file, and it
#: is three lines long on purpose.
_ENTRYPOINTS: dict[str, str] = {
    "src/career/main.py":
        "the ASGI app — both webhooks are served out of it",
    "src/career/engine/cli.py":
        "the CLI: verify-environment, the drills, the boot checks",
    "src/career/onboarding/extract_worker.py":
        "a process in its own right — upload.py spawns `python -m "
        "career.onboarding.extract_worker` for every uploaded file, so no "
        "import edge anywhere in the repository points at it",
}


@functools.cache
def _parsed(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@functools.cache
def _module_file(dotted: str) -> pathlib.Path | None:
    """`career.cv.deliver` → the file that runs when it is imported."""
    base = _SRC.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Every dotted name that importing this file would load.

    Read off `_live_statements`, not `ast.walk`, and that is the whole point:
    an import inside a function still counts (it loads the moment the function
    runs), while `if TYPE_CHECKING: from career.x import y` does not — that
    module is never loaded by anything, so it cannot vouch for a column. The
    same rule as the reads themselves, applied to the edges.
    """
    names: set[str] = set()
    for statement in _live_statements(_parsed(path)):
        if isinstance(statement, ast.Import):
            names.update(alias.name for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            if statement.level or not statement.module:
                continue                  # no relative imports in this package
            names.add(statement.module)
            # `from career.telegram import weekly_report` — the name may be a
            # submodule rather than an attribute, and only the filesystem knows.
            names.update(f"{statement.module}.{alias.name}"
                         for alias in statement.names)
    return names


@functools.cache
def _reachable_modules() -> frozenset[pathlib.Path]:
    """Every file under `src/` that starting an entrypoint would load."""
    stack = [pathlib.Path(name) for name in _ENTRYPOINTS]
    stack += sorted(pathlib.Path("scripts").glob("run_*.py"))
    seen: set[pathlib.Path] = set()
    while stack:
        path = stack.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for dotted in _imported_modules(path):
            parts = dotted.split(".")
            if parts[0] != "career":
                continue
            # importing `career.cv.deliver` runs `career/__init__.py` and
            # `career/cv/__init__.py` on the way, so every prefix is loaded too
            for depth in range(1, len(parts) + 1):
                loaded = _module_file(".".join(parts[:depth]))
                if loaded is not None:
                    stack.append(loaded)
    return frozenset(seen)


def _read_by_reachable_module(column: str) -> bool:
    """Does a module the product actually LOADS take this entitlement's value?

    Named for what it computes. The previous name, `_read_by_product`, claimed
    one thing more than it delivered, and sales copy is released on the
    strength of that claim.

    WHAT IT PROVES: some module on the import graph of a real entrypoint
    evaluates a load of this column, in a statement an interpreter can reach.

    WHAT IT DOES NOT: that the FUNCTION holding that statement is ever CALLED.
    A dead function inside a live module still counts, and that is the one
    residual hole. Closing it needs a call graph, and a call graph over this
    codebase would be wrong in the direction that matters: handlers reached by
    framework dispatch, callbacks passed as values, `Deps` objects assembled at
    runtime would all read as uncalled, legitimate modules would start failing,
    and a guard that cries wolf gets switched off — which is worse than a guard
    that is honest about its own limit. So the limit is stated here and
    narrowed elsewhere: `test_every_module_is_reached_from_an_entrypoint`
    makes an orphan FILE fail loudly instead of quietly vouching.

    Wrong in the safe direction otherwise, by construction: a read this cannot
    see — `getattr(row, name)` with a computed `name`, an ORM helper that
    reflects the column list — returns False, which blocks the sales copy until
    a human looks. The expensive mistake is the other one.
    """
    if column in _MENTIONED_BUT_INERT:
        return False
    for path in sorted(_reachable_modules()):
        if path.name == "models.py" or not path.is_relative_to(_PACKAGE):
            continue          # declaring a column is not reading it
        if any(_expression_reads(node, column)
               for node in _live_expressions(_parsed(path))):
            return True
    return False


def test_the_declared_entrypoints_are_real() -> None:
    """The graph is only as wide as its roots.

    Rename `main.py` and forget the entry here and every module downstream of
    it silently stops counting — which fails safe (more copy blocked, not
    less) but fails CONFUSINGLY, three tests away from the cause. Say it here
    instead.
    """
    missing = sorted(path for path in _ENTRYPOINTS
                     if not pathlib.Path(path).is_file())
    assert not missing, (
        f"these declared entrypoints do not exist: {missing} — the import "
        "graph is rooted at them, so a stale entry quietly shrinks it"
    )
    assert sorted(pathlib.Path("scripts").glob("run_*.py")), (
        "no scripts/run_*.py at all, and three systemd units start one each — "
        "if the runners were renamed, this glob is now rooting the graph at "
        "nothing"
    )


def test_every_module_is_reached_from_an_entrypoint() -> None:
    """An orphan module is how the reader above was walked around.

    Fifteen lines in a file nobody imports made three unbuilt entitlements
    read as implemented, and the sales copy for all three legal again.
    `_read_by_reachable_module` no longer believes such a file — but merely
    ignoring it leaves it sitting in the tree looking like product code, so it
    is named here too: a module under `src/career` that no entrypoint reaches
    is either dead code to delete or a process to declare in `_ENTRYPOINTS`
    with the reason it is one.
    """
    orphans = sorted(
        path.as_posix()
        for path in set(_PACKAGE.rglob("*.py")) - _reachable_modules()
    )
    assert not orphans, (
        f"nothing the product starts would import these modules: {orphans} — "
        "delete them, wire them, or (if one is a process of its own) declare "
        "it in _ENTRYPOINTS. An unimported module under src/ reads as shipped "
        "code and can vouch for nothing"
    )


def test_a_mention_is_not_a_read() -> None:
    """The guard's own guard, written from the walk-arounds that worked.

    Each source below «names» the column the way some earlier version of this
    reader accepted, and none of them is an implementation. They are asserted
    here rather than described, because the next rewrite has to keep refusing
    every one of them — and because the list only ever grows by somebody
    finding a new way through.
    """
    mentions = (
        "# TODO: one day honour cover_letter here\n",
        '"""The cover_letter entitlement, documented and unbuilt."""\n',
        'def f():\n    raise NotImplementedError("cover_letter is not built")\n',
        "def f():\n    raise NotImplementedError\n    return cover_letter\n",
        "def f():\n    return 1\n    x = row.cover_letter\n",
        "from career.telegram import cover_letter\n\ncover_letter.build()\n",
        "row.cover_letter = None\n",
        "make_plan(cover_letter=False)\n",
        # decided before the interpreter starts: a block that never opens
        "if False:\n    value = row.cover_letter\n",
        "if TYPE_CHECKING:\n    value = row.cover_letter\n",
        "if typing.TYPE_CHECKING:\n    value = row.cover_letter\n",
        "while False:\n    value = row.cover_letter\n",
        # a process that is already gone
        "def f():\n    sys.exit(2)\n    value = row.cover_letter\n",
        "def f():\n    os._exit(2)\n    value = row.cover_letter\n",
        "def f():\n    assert False\n    value = row.cover_letter\n",
    )
    for source in mentions:
        tree = ast.parse(source)
        assert not any(_expression_reads(node, "cover_letter")
                       for node in _live_expressions(tree)), source

    reads = (
        "value = plan.cover_letter\n",
        'value = row["cover_letter"]\n',
        'value = row.get("cover_letter")\n',
        'value = getattr(row, "cover_letter")\n',
        "if plan.cover_letter:\n    send()\n",
        "def f():\n    return plan.cover_letter\n",
        # the two shapes that were invisible: a `match` case, and a parameter
        # default. Neither occurs in src/career today, and both would have
        # read as «nothing implements this» — the direction that blocks copy
        # for a feature that IS built, which is how a guard loses its owner.
        "match plan.plan_code:\n    case _:\n        value = row.cover_letter\n",
        "match value:\n    case _ if plan.cover_letter:\n        send()\n",
        "def f(value=plan.cover_letter):\n    return value\n",
        "def f(*, value=plan.cover_letter):\n    return value\n",
        "f = lambda value=plan.cover_letter: value\n",
    )
    for source in reads:
        tree = ast.parse(source)
        assert any(_expression_reads(node, "cover_letter")
                   for node in _live_expressions(tree)), source


def test_the_store_never_sells_an_entitlement_no_code_reads() -> None:
    """لمّاح+ at 449 sold three things backed by nothing.

    `human_review_monthly`, `queue_priority` and `intro_blurb` existed as
    columns and as sentences on the sales page, and not one line of the
    product ever read them: no reminder, no queue, no blurb. A customer paying
    double received exactly what the 199 customer received, and would have
    discovered it a month later.

    WHAT THIS COVERS: the exact wordings below, wherever they appear in the
    store sheet, for as long as no module the product loads reads their column.

    WHAT IT DOES NOT: any other wording. The phrase side of the map is a
    blocklist and a blocklist of a natural language can never be complete —
    «رسالة تعريفية» sells `intro_blurb`, «معالجتك تسبق غيرك في الطابور» sells
    `queue_priority`, and neither is written below. An adversarial review put
    all three unbacked promises back on the page in reworded Arabic and this
    test returned green. The answer to that is NOT a longer list: it is
    `test_every_promise_on_a_plan_card_names_what_backs_it`, which reads the
    document instead of the vocabulary, so a sentence nobody anticipated fails
    by DEFAULT. This test remains because it also polices the whitepaper and
    because a known wording is worth naming where it can be seen.

    A column the product genuinely reads may be sold freely, in any words.
    """
    text = pathlib.Path("docs/STORE-PAGES-AR.md").read_text(encoding="utf-8")
    for column, phrases in _SOLD_ENTITLEMENTS.items():
        if _read_by_reachable_module(column):
            continue
        for phrase in phrases:
            assert phrase not in text, (
                f"the store sells «{phrase}» and no module the product loads "
                f"reads {column} — "
                "either implement it or stop selling it"
            )


def test_the_map_covers_every_paid_entitlement_column() -> None:
    """The map was written from the entitlements somebody already suspected.

    That is why it took three passes to find them all: `human_review_monthly`,
    `queue_priority` and `intro_blurb` were caught by the pre-launch check;
    `weekly_report`, `support_sla_hours` and `cover_letter` were caught two
    days later by the guard the first pass produced — every one of them the
    same class of defect, and every one of them missed because a human listed
    what a human remembered.

    So the column side stops being a list and becomes a DERIVATION: every
    column of `plan_entitlements` is either read by a loaded module or carries the
    phrases that sell it. A column added to the plan table tomorrow and wired
    to nothing fails here, before it can be sold.

    `plan_code` is the key, not a feature. `banned_companies` legitimately has
    no row here — it is sold like an entitlement but lives on `search_policies`,
    which is why the map is a superset of this table rather than equal to it.

    Presence of the KEY was once the whole check, and that was a hole with a
    one-character exploit: `"cover_letter": ()` satisfied it while policing
    nothing at all, so the wording that sells an unbuilt feature could be
    restored verbatim under a green suite. An entry with no phrases is not an
    entry — it is the column silently opted out.
    """
    from career.db.models import PlanEntitlement

    columns = {
        column.name for column in PlanEntitlement.__table__.columns
        if column.name != "plan_code"
    }
    unaccounted = sorted(
        column for column in columns
        if column not in _SOLD_ENTITLEMENTS and not _read_by_reachable_module(column)
    )
    assert not unaccounted, (
        f"these plan_entitlements columns are neither read by any module the "
        f"product loads nor "
        f"mapped to the wording that sells them: {unaccounted} — a paid column "
        "with no reader and no entry here is a promise nothing can catch"
    )

    disarmed = sorted(
        column for column, phrases in _SOLD_ENTITLEMENTS.items()
        if not _read_by_reachable_module(column)
        and not [phrase for phrase in phrases if phrase.strip()]
    )
    assert not disarmed, (
        f"these columns are in the map with no wording to police: {disarmed} — "
        "an empty phrase tuple keeps the key and drops the guard. Either write "
        "the phrases that sell the column or delete the entry, which fails the "
        "coverage check above and puts the decision in front of a human"
    )


# ── the promises, counted from the PAGE ──────────────────────────────────────
#
# Everything above polices the store sheet with a vocabulary, and a vocabulary
# is only ever as complete as the person who wrote it. `_SOLD_ENTITLEMENTS`
# knows «نبذة تمهيدية»; it does not know «رسالة تعريفية», and the two sell the
# same unbuilt column. Add a phrase and the review finds a synonym; that race
# has one ending.
#
# So the direction is reversed. The register below is keyed by the plan, and
# the test asserts a BIJECTION with what §6 of the store sheet actually says:
# every line on a product page is registered, and every registered row still
# matches a line. A promise nobody anticipated does not have to be recognised
# to be caught — it is caught by not being here, which is what «unknown fails
# safe» means and what a blocklist can never do.
#
# Each row also names what MAKES IT TRUE, and the test resolves that name: the
# file must exist and must define that symbol. Not proof the sentence is kept —
# no test can be — but it turns «somebody wrote a promise» into «somebody
# wrote a promise and had to point at the thing that keeps it».
#
# EVERY LINE, and a WHOLE line. Both words were bought with an exploit:
#
#   * The rows used to be FRAGMENTS, matched with `in`. So a registered bullet
#     could be EXTENDED and stay green — the review appended «…ونقدّم عنك على
#     البوابة نيابةً عنك… ونضمن لك مقابلة خلال ٣٠ يومًا» to a signed bullet,
#     which promises automated applying (Constant 1, the one thing this
#     project may never do) plus an interview guarantee, and the guard said
#     nothing because the fragment it knew was still in there. A row is now the
#     whole line, compared after formatting is stripped: a comma or a bold
#     marker moving is still not a new promise, and an added clause is.
#
#   * The lines used to be `line.startswith("- ")`. So an indented `  - `, a
#     `* ` bullet, a `**bold**` line and a plain prose paragraph were all
#     invisible — and that was not hypothetical: the strongest quantitative
#     claims on the live page were never bullets at all («حتى فرصتين كل يوم»,
#     «شهر كامل … أربع وأربعين سيرة مفصّلة», «يُنفَّذ فور الشراء»). Every
#     non-blank line of a card is now a line to account for, whatever shape it
#     came in.
#
# WHAT THIS STILL CANNOT DO: judge the Arabic. A row says a human read that
# sentence and named what keeps it; nothing here checks that the named symbol
# does what the sentence says, and nothing here can tell a promise from a
# turn of phrase — which is why `_NO_CLAIM` exists and why it is policed by
# the one property a quantitative promise cannot hide from, a digit.

#: The one backer that is not code. The 449 card was deliberately rebuilt
#: around promises a HUMAN keeps (CHANGELOG-v1.1 §23 / PROGRESS, 6 August): the
#: alternative was building a monthly-reminder machine nobody would run and a
#: queue nobody would re-order. Written down as a distinct value so it can be
#: counted — the day this list grows, the plan is drifting back toward selling
#: intentions, and that is visible here before it is visible to a customer.
_OPERATOR = "OPERATOR"

#: A promise kept by code that must NEVER EXIST. «وما نقدّم باسمك لأي وظيفة»
#: is Constant 1, and the only thing that keeps it is that no Playwright, no
#: form filler and no portal upload is ever written. Naming a symbol for it
#: would be a lie in the accountable direction — there is nothing to point at,
#: and pointing at something would suggest a mechanism exists that could break.
_ABSENCE = "ABSENCE"

#: A line that promises the customer NOTHING: an argument, a diagnosis of the
#: problem, a sentence about who the product is for. Registered rather than
#: skipped, because «this line is not a promise» is a judgement a human makes,
#: and a judgement nobody wrote down is a judgement nobody can re-check. This
#: is the softest value in the file and the place a promise could be hidden,
#: so `test_a_line_that_claims_nothing_carries_no_quantity` holds it to the
#: one thing prose cannot fake.
_NO_CLAIM = "NO_CLAIM"

#: The three values above are not `path::symbol` and must not be resolved.
_NOT_CODE = frozenset({_OPERATOR, _ABSENCE, _NO_CLAIM})

#: plan price → ((the line, exactly as the page says it, what backs it), …)
#:
#: Whole lines, compared after `_normalised` removes bullet markers, emphasis
#: and punctuation: moving a comma or bolding a different word is not a new
#: promise and must not fail, but adding a clause is a new promise and must.
#: A NEW line matches no row at all, which is the point.
_BACKED_PROMISES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "29": (
        ("**العنوان:** تقييم سيرتك الذاتية — تقرير صريح خلال دقائق",
         ("src/career/funnel/flow.py::handle_funnel_document",
          "src/career/funnel/report.py::render_report_pdf")),
        ("أرسل سيرتك، ويوصلك تقرير في صفحة واحدة يقول لك بالمكشوف:",
         ("src/career/funnel/report.py::render_report_pdf",)),
        ("- **درجة عامة وأربعة محاور**: وضوح المسار، قوة الإنجازات، الملاءمة "
         "للمسار المطلوب، وجودة القراءة الآلية",
         ("src/career/funnel/evaluation.py::Report",)),
        ("- **حتى خمس ملاحظات** على ما يضعّف سيرتك فعلًا — «هذا يقول واجباتك "
         "ولا يقول إنجازك». سيرة قوية تاخذ ملاحظات أقل، وهذا في صالحك",
         ("src/career/funnel/evaluation.py::_notes",)),
        ("- **مقارنة بين مسارين**: المسار اللي تطلبه، ومسار بديل قد يكون أقرب "
         "لك مما تظن",
         ("src/career/funnel/evaluation.py::PathVerdict",
          "src/career/onboarding/paths.py::score_path")),
        ("- **ثلاث خطوات** تنفّذها اليوم",
         ("src/career/funnel/evaluation.py::evaluate",)),
        # two halves: the run starts at the paid order, and the refund is moved
        # by a human in Salla — see `test_no_refund_is_promised_as_automatic`.
        ("يُنفَّذ فور الشراء. ما وصلك التقرير؟ راسلنا على واتساب وترجع لك "
         "فلوسك كاملة عبر سلة.",
         ("src/career/salla/provisioning.py::provision_order", _OPERATOR)),
    ),
    "199": (
        ("**العنوان:** حتى فرصتين كل يوم، ومع كل وحدة سيرة مكتوبة لها",
         ("src/career/engine/run.py::run_nightly",
          "src/career/cv/generate.py::tailor_cv")),
        ("المشكلة مو إنك ما تلقى وظايف. المشكلة إنك ترسل نفس السيرة لخمسين "
         "وظيفة، وأنظمة الفرز تقارن سيرتك بالوصف الوظيفي — فتُرفض قبل ما "
         "يشوفك إنسان.",
         (_NO_CLAIM,)),
        ("تفصيل سيرة لكل فرصة يحتاج ساعة. ما أحد يسويها.",
         (_NO_CLAIM,)),
        ("**لمّاح يسويها لك كل يوم — عند توفر فرصة تعدّي فلترك:**",
         ("src/career/cv/daily_run.py::run_daily_delivery",
          "src/career/engine/gate.py::evaluate")),
        ("- نبحث لك يوميًا بمعاييرك أنت: مسارك، مدينتك، وراتبك المستهدف",
         ("src/career/engine/families.py::derive_query_families",
          "src/career/engine/gate.py::evaluate")),
        ("- كل فرصة تعدّي فلترك، نكتب لها **سيرة إنجليزية مفصّلة لها بالذات** "
         "في صفحة واحدة",
         ("src/career/cv/generate.py::tailor_cv",)),
        ("- توصلك على واتساب من الأحد إلى الخميس، ومعها سبب اختيارنا لها",
         ("src/career/cv/deliver.py::build_daily_bundle",)),
        # two promises in one line: the buttons, and «not shown again for a
        # month» — which is the suppression ledger, not a wish.
        ("- تضغط «قدّمت» أو «ما ناسبتني»، وأي فرصة وصلتك ما تتكرر عليك لمدة شهر",
         ("src/career/cv/deliver.py::parse_outcome_button",
          "src/career/engine/ranking.py::filter_suppressed")),
        ("- وبعد أسبوعين من كل «قدّمت» نسألك سؤالًا واحدًا: وش صار؟ — عشان "
         "نعرف أي الفرص تستاهل وقتك فعلًا",
         ("src/career/cv/outcome_followup.py::sweep_outcome_questions",)),
        # the sourcing promise is code; «وما نقدّم باسمك لأي وظيفة» is the
        # absence of code, and says so rather than borrowing a symbol.
        ("**بالمكشوف:** كل سطر في سيرتك مصدره كلامك أنت المؤكد منك بالحرف. ما "
         "نخترع إنجازًا ما صار، وما نقدّم باسمك لأي وظيفة — التقديم بيدك.",
         ("src/career/cv/generate.py::contains_invented_content",
          "src/career/cv/publish.py::validate_cv_binding", _ABSENCE)),
        ("شهر كامل من هذا يوصل إلى أربع وأربعين سيرة مفصّلة — والرقم يعتمد "
         "على ما يتوفر فعلًا من فرص تعدّي فلترك. كاتب السيرة يأخذ من ١٥٠ إلى "
         "٥٠٠ ريالًا للسيرة **الواحدة**.",
         ("src/career/cv/daily_run.py::MonthlyCapBudget",)),
    ),
    "449": (
        # «ومعه رقمي أنا» is the same direct line the bullet below sells, in
        # four words, so it gets the same backer for the same reason.
        ("**العنوان:** كل لمّاح، ومعه رقمي أنا",
         ("src/career/onboarding/policy.py::build_draft_policy",
          "src/career/promises/career_session.py::escalate_direct_message",
          "src/career/telegram/console.py::_run_reply")),
        ("كل اللي في لمّاح، وزيادة:",
         ("src/career/onboarding/policy.py::build_draft_policy",)),
        # RE-POINTED 2026-08-07. This row registered `_run_reply` + `_OPERATOR`
        # and that was the truth AT REGISTRATION: the reply box existed, and
        # NOTHING carried an ordinary message to it. A لمّاح+ customer who
        # wrote «عرض وصلك وتبي رأي» was classified OTHER, answered by a robot,
        # and reached no human at all — so the inbound half of «اكتب لي وقت ما
        # تحتاج» rested on the operator happening to open a conversation.
        # `escalate_direct_message` is that half, and it is reached from the
        # live worker: `whatsapp/worker._handle_message` (last branch) →
        # `process_pending_whatsapp`, the loop `run_worker_loop.py` runs.
        # `_OPERATOR` STAYS, and removing it would be the lie this file exists
        # to stop: the escalation guarantees the message is put in front of a
        # human, never that one is awake. «بلا انتظار دورك» is still kept by
        # him, and now it is kept where he can see it.
        ("- **تواصل مباشر معي** — أنا اللي بنيت لمّاح، وتلقاني على نفس "
         "المحادثة. سيرة تبي تراجعها، فرصة محتار فيها، عرض وصلك وتبي رأي: "
         "اكتب لي وقت ما تحتاج، بلا موعد وبلا انتظار دورك",
         ("src/career/promises/career_session.py::escalate_direct_message",
          "src/career/telegram/console.py::_run_reply", _OPERATOR)),
        ("- **جلسة مسار** واحدة متى طلبتها: وين موقعك، ووين تقدر توصل، وش "
         "ينقصك بالضبط",
         ("src/career/promises/career_session.py::request_session",)),
        # «اسمك معلّم عندي بنجمة … والرد خلال ٢٤ ساعة» — one line, FOUR
        # claims, and they are backed differently on purpose. (THREE, until
        # 2026-08-07 folded «أشوف رسالتك» into «أعرف مين أنت» and attributed
        # both to code. They are not the same claim and only one of them is.)
        #   the star            `views._plan_marked` — code, and always was.
        #   «أعرف مين أنت»     code SINCE 2026-08-07 and not before: the star
        #                       marked a customer the operator had already
        #                       decided to open, and an ordinary message
        #                       arrived nowhere. `escalate_direct_message`
        #                       opens the ⭐ ticket, and the tickets screen
        #                       renders its kind, its TEN code, its age and
        #                       the SHAPE of the message — which is «أعرف مين
        #                       أنت قبل ما أرد» literally.
        #   «أشوف رسالتك»       NOT code, and the attribution written here on
        #                       2026-08-07 said it was. The code deliberately
        #                       never shows him the message: §15.13 keeps the
        #                       body off the admin channel, the screen above
        #                       carries no word of it, and the media alert
        #                       says so out loud — «ما نقدر نفتحها من هنا —
        #                       افتح محادثته في واتساب». He SEES the message in
        #                       WhatsApp, on his own phone, which is the same
        #                       place the tier sells («تلقاني على نفس
        #                       المحادثة») and is `_OPERATOR`'s half. What the
        #                       code guarantees is that he KNOWS there is one
        #                       to open; reading it is his act, not ours.
        #   «الرد خلال ٢٤ ساعة» STILL NOT CODE, and that is why `_OPERATOR`
        #                       stays. `RESPONSE_SLA_HOURS` is consumed by the
        #                       career-session path only; the ticket is stamped
        #                       with the customer's own send time and the
        #                       screen prints its age, so the duration is
        #                       VISIBLE — and nothing measures it and nothing
        #                       raises AT twenty-four hours (DEVIATIONS D26
        #                       item 5). `console.release_forgotten_tickets`
        #                       does raise, at FORTY-EIGHT, and it is not this
        #                       promise's alarm: it fires at twice the sold
        #                       window and it is about the MUTE the ticket puts
        #                       on the customer's line, not about the reply he
        #                       was promised. Visible is not measured, a
        #                       different clock is not this clock, and the
        #                       register must not round either one up.
        # THE TUPLE IS UNCHANGED and it holds. Two code backers that each
        # genuinely back a part — `_plan_marked` the star, and
        # `escalate_direct_message` «أعرف مين أنت» — plus `_OPERATOR` for the
        # two halves no code keeps: seeing the message, and the 24 hours.
        # Only the attribution above was wrong; nothing about what ships was.
        ("- **اسمك معلّم عندي بنجمة** في لوحتي — أشوف رسالتك وأعرف مين أنت قبل "
         "ما أرد، والرد خلال ٢٤ ساعة",
         ("src/career/promises/career_session.py::escalate_direct_message",
          "src/career/telegram/views.py::_plan_marked", _OPERATOR)),
        ("للي وصل مرحلة يكون فيها القرار أثقل من البحث — ويبي إنسانًا يسأله، "
         "لا نظامًا يرد عليه.",
         (_NO_CLAIM,)),
    ),
}

_STORE_PAGES = pathlib.Path("docs/STORE-PAGES-AR.md")

#: §6 — the three product pages, from its own heading to the next `## `.
_SECTION_SIX = re.compile(r"^## ٦\).*?(?=^## |\Z)", re.M | re.S)

#: ANY `### ` heading, priced or not. The price-matching one below cannot be
#: the only thing that finds a card: a heading it fails to match is a card
#: this file does not know exists, which is precisely the dangerous case.
_CARD_HEADING = re.compile(r"^### .*$", re.M)

#: A card heading, and its price. `[٠-٩\d]` and not `[٠-٩]`: the pattern used
#: to require Arabic-Indic digits, so `### 💎 لمّاح VIP — 999 ريالًا` was not a
#: product card as far as this file was concerned — a whole fourth plan, with
#: whatever it promised, seen by nothing.
_PLAN_HEADING = re.compile(r"^### .*?([٠-٩\d]+)\s*ريال", re.M)

_LATIN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

#: A horizontal rule — layout, not language.
_RULE = re.compile(r"[-—_*]{3,}")

#: What a line means, without how it is dressed. The marker that makes a line
#: a bullet, the emphasis around a phrase and the punctuation between clauses
#: are all formatting: moving them is an edit, not a new promise. Everything
#: else — every word — is the promise, and must match a signed row exactly.
_BULLET_MARK = re.compile(r"^[-*+•]\s+")
_DRESSING = re.compile(r"[*_`~«»\"'()\[\]…،؛,;:.!؟?]+")


def _normalised(line: str) -> str:
    return re.sub(r"\s+", " ", _DRESSING.sub(" ", _BULLET_MARK.sub("", line.strip()))).strip()


def _section_six() -> re.Match[str]:
    """§6 of the store sheet, located rather than assumed."""
    match = _SECTION_SIX.search(_STORE_PAGES.read_text(encoding="utf-8"))
    assert match is not None, (
        "docs/STORE-PAGES-AR.md no longer has a «## ٦)» section — the three "
        "product pages are what this half of the file reads, and it has just "
        "become a guard over nothing"
    )
    return match


def _card_lines(body: str) -> list[str]:
    """Every line of a card that says something. Blank lines and horizontal
    rules are layout; everything else is text a customer reads and therefore a
    claim somebody has to have signed for."""
    return [line.strip() for line in body.splitlines()
            if line.strip() and not _RULE.fullmatch(line.strip())]


def _plan_cards() -> dict[str, list[str]]:
    """§6 → {plan price (Latin digits): every line that card sells}."""
    section = _section_six().group(0)
    headings = list(_PLAN_HEADING.finditer(section))
    cards: dict[str, list[str]] = {}
    for index, heading in enumerate(headings):
        # from the end of the heading LINE — the tail of the heading itself
        # («ريالًا شهريًا») is not one of the card's promises
        start = section.index("\n", heading.end())
        end = headings[index + 1].start() if index + 1 < len(headings) else len(section)
        cards[heading.group(1).translate(_LATIN_DIGITS)] = _card_lines(section[start:end])
    return cards


def _defines(target: str) -> bool:
    """`path::symbol` — does that file define that top-level name?"""
    path_text, _, symbol = target.partition("::")
    path = pathlib.Path(path_text)
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path_text)
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == symbol
        for node in tree.body
    )


def test_the_store_sheet_carries_one_card_per_approved_plan() -> None:
    """The register is keyed by price, so a fourth card — or a renamed one —
    must not slip through as «no lines to check».

    An adversarial review got a whole fourth product page past every assertion
    in this file with `### 💎 لمّاح VIP — 999 ريالًا`, and it took two holes at
    once: the heading pattern demanded Arabic-Indic digits, so a Latin-digit
    price made it not a card; and anything above the first card belonged to no
    card, so the same heading placed BEFORE §6 was read by nothing at all.
    Both are closed by locating every `### ` in the document first and
    requiring each one to be a card this file knows.
    """
    text = _STORE_PAGES.read_text(encoding="utf-8")
    section = _section_six()
    start, stop = section.span()

    stray = [heading.group(0).strip()
             for heading in _CARD_HEADING.finditer(text)
             if not start <= heading.start() < stop]
    assert not stray, (
        f"these `### ` cards are outside §6, where nothing reads them: {stray}"
        " — §6 is the section pasted into Salla, one page per product. A card "
        "anywhere else is sold and unguarded"
    )

    body = section.group(0)
    priced = {heading.start() for heading in _PLAN_HEADING.finditer(body)}
    unpriced = [heading.group(0).strip()
                for heading in _CARD_HEADING.finditer(body)
                if heading.start() not in priced]
    assert not unpriced, (
        f"these §6 cards name no price: {unpriced} — the register is keyed by "
        "price, so a card without one is a page of promises with no row to "
        "match and no plan to belong to"
    )

    preamble = _card_lines(body[body.index("\n"):min(priced, default=len(body))])
    assert not preamble, (
        f"§6 says this before its first product card: {preamble} — text above "
        "the first heading belongs to no card, so it is sold and unregistered. "
        "Put it inside the card it sells, or outside §6"
    )

    assert set(_plan_cards()) == set(APPROVED) == set(_BACKED_PROMISES), (
        f"store cards {sorted(_plan_cards())}, approved {sorted(APPROVED)}, "
        f"register {sorted(_BACKED_PROMISES)} — these three must agree"
    )


def test_every_promise_on_a_plan_card_names_what_backs_it() -> None:
    """The guard a reworded promise cannot walk around.

    `_SOLD_ENTITLEMENTS` asks «is this sentence one I was told about?» and a
    synonym answers no. This asks the opposite question — «is this sentence
    one somebody signed for?» — and a synonym answers no to that too, which is
    the direction that fails safely.

    Every LINE of a card and the WHOLE of each line, both bought with an
    exploit — see the block above `_BACKED_PROMISES`. Briefly: a registered
    bullet could be extended with «ونقدّم عنك على البوابة نيابةً عنك» and stay
    green while promising the one thing Constant 1 forbids, and four bullet
    shapes (indented, `* `, bold, prose) were not bullets at all.

    WHAT REMAINS UNCOVERED, plainly: this reads §6, the three product pages
    pasted into Salla. The refund page, the privacy summary, the founding
    block, the guarantee line and the «مين يكتب لك؟» block are prose about the
    business, not per-plan feature promises, and are not registered — a
    promise moved into one of those sections leaves this guard entirely, and no
    amount of matching inside §6 changes that. And a named backer proves a
    symbol EXISTS, never that it does what the Arabic sentence says — that
    judgement is a human's, made once, at the moment a row is added here.
    """
    for price, lines in sorted(_plan_cards().items()):
        # `.get` and not `[...]`: an unregistered card must report every line
        # it sells, not a KeyError two frames away from the reason
        register = _BACKED_PROMISES.get(price, ())
        signed = {_normalised(text) for text, _ in register}
        assert len(signed) == len(register), (
            f"_BACKED_PROMISES[{price!r}] has two rows that normalise to the "
            "same line — one of them is guarding nothing"
        )
        for line in lines:
            assert _normalised(line) in signed, (
                f"the {price} card says «{line[:110]}» and no row of "
                f"_BACKED_PROMISES accounts for it. A new line on a product "
                f"page is a new promise, and a CHANGED line is a new promise "
                f"too — the register holds whole lines precisely so that a "
                f"clause appended to a signed sentence cannot ride in on it. "
                f"Add a row naming what makes it true, update the row that "
                f"moved, or take the line off the page"
            )
        on_the_page = {_normalised(line) for line in lines}
        for text, _ in register:
            assert _normalised(text) in on_the_page, (
                f"_BACKED_PROMISES[{price!r}] still registers «{text[:110]}» "
                "and no line on that card says it — the store copy moved and "
                "the register did not, so it is now guarding a sentence nobody "
                "sells"
            )


def test_a_line_that_claims_nothing_carries_no_quantity() -> None:
    """`_NO_CLAIM` is the register's escape hatch, so it needs a floor.

    Some lines on a product page really are argument rather than offer — «تفصيل
    سيرة لكل فرصة يحتاج ساعة. ما أحد يسويها.» sells nothing and can name
    nothing. But «this line promises nothing» is the one verdict a tired author
    can give any line at all, and it would retire the guard one sentence at a
    time.

    A machine cannot read the Arabic. It can read a DIGIT, and the promises
    that cost money when they are wrong are the counted ones: two a day,
    forty-four a month, twenty-four hours, thirty days. So a line excused as
    claimless may not contain a number in either script. It is a floor, not a
    ceiling, and it is deliberately the cheapest thing to check.
    """
    quantified = [
        (price, text)
        for price, register in _BACKED_PROMISES.items()
        for text, backers in register
        if _NO_CLAIM in backers and re.search(r"[0-9٠-٩]", text)
    ]
    assert not quantified, (
        f"these lines are registered as promising nothing and count something: "
        f"{quantified} — a number on a sales page is a promise the customer can "
        "hold us to. Name what backs it, or take the number out"
    )


def test_every_backer_a_promise_names_actually_exists() -> None:
    """A register row that points at a deleted function reads as accountability
    and resolves to nothing — the same failure as a superseded-by marker citing
    a changelog entry that was renumbered away."""
    missing = sorted({
        backer
        for register in _BACKED_PROMISES.values()
        for _, backers in register
        for backer in backers
        if backer not in _NOT_CODE and not _defines(backer)
    })
    assert not missing, (
        f"these promises name a backer that does not exist: {missing} — either "
        "the code moved and the register must follow it, or the promise lost "
        "what kept it and belongs off the page"
    )


def test_no_promise_rests_on_the_operator_alone_without_saying_so() -> None:
    """Operator-kept promises are a legitimate product decision — «الوصول
    المباشر إلى الإنسان الذي بنى لمّاح» is the 449 card's whole argument, and
    building a reminder machine instead would have been worse.

    They are also the exact shape the pre-launch check caught: a sentence on a
    page and a human expected to remember. So they are allowed and COUNTED,
    and every one of them must also name something in code — the star that
    puts the customer in front of the operator's eyes, the ledger that makes a
    session one-per-period. A promise resting on memory alone has nothing to
    fail loudly, and this is where that gets noticed.
    """
    bare = [
        (price, fragment)
        for price, register in _BACKED_PROMISES.items()
        for fragment, backers in register
        if _OPERATOR in backers and set(backers) == {_OPERATOR}
    ]
    assert not bare, (
        f"these promises are kept by the operator remembering, and by nothing "
        f"else: {bare} — the 449 rewrite exists because that shape breaks "
        "silently. Give it the piece of code that makes it visible, or reword it"
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

#: A struck-through line: the smaller marker, for one sentence rather than a
#: whole passage. `.stale` renders as `line-through` + reduced opacity, which
#: is what tells a reader THIS line is not on offer while the section around
#: it still is.
#:
#: `span` is in here because the whitepaper now strikes PARTS of sentences —
#: a line that carries a retired promise and a shipped one in the same breath
#: («الترقية وسط الدورة …» beside «الوقف المؤقت لا يمدد المدة») is more
#: honestly marked half-struck than struck whole (DEVIATIONS D26). Requested by
#: the owner of that file, who could see the marker and this guard could not.
_STALE = re.compile(r"<(li|tr|span) class=\"stale\">.*?</\1>", re.DOTALL)


def _unmarked_text(html: str) -> str:
    """The whitepaper with every superseded banner and every struck line
    removed — what is left is what the page still SELLS.

    Depth counting is only needed for the banners: `div`/`span` nest inside
    each other, `li` and `tr` do not nest in this document, so a non-greedy
    match to the matching close tag is exact for them.
    """
    struck = [match.group(0) for match in _STALE.finditer(html)]
    for block in _sup_blocks(html) + struck:
        html = html.replace(block, " ")
    return html


#: Each passage of the whitepaper the changelog overrides:
#:   (section id,
#:    a literal fragment proving the passage is still there,
#:    the changelog entry number that governs it,
#:    what the reader would otherwise act on)
#:
#: The fragment is asserted PRESENT on purpose. Without it the test would pass
#: by deletion — and deleting a superseded decision is exactly what the
#: governance rule forbids, because the history is the reason.
#:
#: A SEQUENCE and not a mapping by section, because one section can contradict
#: the changelog in more than one place and §04 does it three times: the
#: catalogue, the entitlements table, and the price-measurement framework that
#: still says the numbers land after thirty alpha days. Keyed by section, the
#: last one written would have silently replaced the others.
_SUPERSEDED: tuple[tuple[str, str, str, str], ...] = (
    ("s3", "سعر واحد 279 ← ثلاث باقات", "19",
     "the reversal log's own line, reversed a second time — three plans "
     "became two"),
    ("s4", "~149", "19",
     "a three-tier catalogue at 149/279 — retired on 2 August"),
    ("s4", "خطاب تقديم كامل لكل فرصة", "23",
     "the تنفيذي card sells a full cover letter per opportunity; nothing "
     "reads `cover_letter`, and the renderer, its byte-verbatim template and "
     "that template's guard were all deleted on 6 August (DEVIATIONS D23)"),
    ("s4", "المرساة الاسترشادية 149/279/449", "19",
     "prices presented as pending a 30-day alpha measurement; they were "
     "fixed early on real measured cost"),
    ("s5", "الدفع يمدد 30 يومًا", "16",
     "renewal described as extending one row; the code writes one row "
     "per Salla order and retires the previous one"),
    ("s13", "باقات الاشتراك الثلاث", "19",
     "a C9 exit criterion that can never be met — the third plan is "
     "deliberately retired from sale"),
)


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
    for section_id, fragment, entry, why in _SUPERSEDED:
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
    for _, _, entry, _ in _SUPERSEDED:
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

    The column name is not the whole of it, and the first version of this guard
    passed while the page still sold all four. The table rows carry the LATIN
    column names and were struck; the plan cards above them carry the ARABIC
    sentences — «نبذة تقديم قصيرة جاهزة للنسخ», «أولوية معالجة في الطابور»,
    «خطاب تقديم كامل لكل فرصة», «مراجعتك البشرية … شهريًا» — and were not.
    The sentences are what gets copied into the store, so the sentences are
    what has to be marked; a struck row above a live promise reads as a
    formatting change.
    """
    text = _WHITEPAPER.read_text(encoding="utf-8")
    marked = "\n".join(_sup_blocks(text))
    still_offered = _unmarked_text(text)
    for column, phrases in _SOLD_ENTITLEMENTS.items():
        if _read_by_reachable_module(column):
            continue
        if column in text:
            assert column in marked, (
                f"the whitepaper sells {column} and no module the product "
                f"loads reads it outside models.py "
                "reads it — mark the row superseded or implement the feature"
            )
        for phrase in phrases:
            assert phrase not in still_offered, (
                f"the whitepaper still offers «{phrase}» outside any marker "
                f"while no loaded module reads {column} — strike the line (class="
                "\"stale\") or name it in the superseded-by banner"
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


# ── the cross-module claims this repository's prose leans on ────────────────
#
# WHY THIS SECTION EXISTS, and why it is five assertions and not fifty.
#
# On 2026-08-07 a reviewer found five sentences that were true when written and
# false when read, and the expensive one was not the wrongest — it was the one
# other files CITED. `promises/career_session._mid_flow` said
# `handle_enrichment` returns False «first line, when
# `deps.achievement_renderer is None` — that is the SHIPPED default», and by
# the time anybody checked, two agents and a project-state note had copied the
# claim onward. The check took one `grep`: `scripts/run_worker_loop.py` has
# passed a real renderer since F-ENRICH's own commit, and the first line had
# been deleted that morning.
#
# The rest of this file already knows the shape of the answer. A promise that
# names a backer is checked against the tree (`_defines`), not against
# somebody's memory of the tree. What follows applies the same rule one level
# up: a handful of PRESENT-TENSE facts about other modules that prose in this
# repository rests its argument on, each one turned into a question ast can
# answer.
#
# The bar for being here is deliberately high, because a guard for a claim
# nobody leans on is just a second thing to keep true. A claim earns a test
# when another module cites it, or a shipped guard's justification depends on
# it. Prose that merely describes is left as prose — describing is what prose
# is for, and a test cannot tell a stale description from a wrong one anyway.


def _function(path: str, name: str) -> ast.FunctionDef:
    """The top-level `def name` in that file. Missing is a failure, not None —
    every caller here is asserting something ABOUT it."""
    for node in _parsed(pathlib.Path(path)).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path} no longer defines {name}()")


def _py_files() -> list[pathlib.Path]:
    return sorted(_SRC.rglob("*.py")) + sorted(pathlib.Path("scripts").glob("*.py"))


def _calls_of(name: str) -> set[str]:
    """Every file under `src/` or `scripts/` that CALLS this function."""
    callers: set[str] = set()
    for path in _py_files():
        for node in ast.walk(_parsed(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (func.id if isinstance(func, ast.Name)
                      else func.attr if isinstance(func, ast.Attribute) else None)
            if called == name:
                callers.add(str(path))
    return callers


def _tests_renderer_missing(node: ast.expr) -> bool:
    """`deps.achievement_renderer is None`, however it is spelled."""
    return any(
        isinstance(inner, ast.Attribute) and inner.attr == "achievement_renderer"
        for inner in ast.walk(node)
    )


def test_handle_enrichment_decides_ownership_before_capability() -> None:
    """The claim: an open enrichment session is OWNED by `handle_enrichment`
    whatever a process has wired, so a missing renderer can no longer make the
    worker fall through and raise a لمّاح+ support ticket against a customer
    who was answering our own question.

    Three files rest on it — `promises/career_session._mid_flow` (whose guard
    is written as a defence-in-depth against branch order and NOT against a
    renderer), the function's own AUDIT docstring, and
    `tests/test_onboarding_enrichment.py`'s header. The property is structural
    and therefore checkable: the renderer test must sit AFTER every `return
    False`, and the branch it guards must end in `return True` — «we stop and
    close», never «this is not mine».
    """
    body = _function("src/career/onboarding/orchestrator.py",
                     "handle_enrichment").body
    disowns = [i for i, statement in enumerate(body)
               if any(isinstance(node, ast.Return)
                      and isinstance(node.value, ast.Constant)
                      and node.value.value is False
                      for node in ast.walk(statement))]
    capability = [i for i, statement in enumerate(body)
                  if isinstance(statement, ast.If)
                  and _tests_renderer_missing(statement.test)]
    assert capability, (
        "handle_enrichment no longer tests deps.achievement_renderer at its "
        "top level — three docstrings describe a capability gate here"
    )
    assert min(capability) > max(disowns), (
        "handle_enrichment tests CAPABILITY before it has finished deciding "
        "OWNERSHIP. That is the 2026-08-07 bug exactly: a process without a "
        "renderer disowns a message that belongs to an open enrichment "
        "session, the worker falls through, and the customer's answer becomes "
        "a support ticket raised against him"
    )
    tail = body[min(capability)].body[-1]
    assert (isinstance(tail, ast.Return)
            and isinstance(tail.value, ast.Constant)
            and tail.value.value is True), (
        "the no-renderer branch no longer ends in `return True` — a flow we "
        "cannot run must be CLOSED and answered, not handed back to the caller"
    )


def test_a_forgotten_ticket_is_swept_by_something_other_than_the_console() -> None:
    """The claim: a support ticket nobody closes stops muting its customer.

    `promises.career_session.escalate_direct_message` writes down the residue
    of its own one-open-ticket-per-customer dedupe, and until 2026-08-07 that
    residue read «nothing sweeps a forgotten one». The sentence was cited BY
    `telegram/console.release_forgotten_tickets` as the request it fulfils,
    and it outlived the sweep it asked for by a day.

    The console's own docstring makes the second half load-bearing: releasing
    off the operator's console traffic «releases fastest for the customers who
    need it least», because the operator who stopped opening the watchtower is
    the one who forgot the ticket. So a caller OUTSIDE the console is the
    thing being asserted — not merely that a sweeper exists.
    """
    console = "src/career/telegram/console.py"
    _function(console, "release_forgotten_tickets")
    elsewhere = _calls_of("release_forgotten_tickets") - {console}
    assert elsewhere, (
        "release_forgotten_tickets is called only from the module that "
        "defines it. Two docstrings — career_session.escalate_direct_message "
        "and console.release_forgotten_tickets itself — say a ticket is swept "
        "while the operator sleeps. Restore the caller, or make both of them "
        "say that the mute is only lifted by the operator opening the console"
    )


def test_the_only_trigger_of_a_career_session_is_the_operator() -> None:
    """The claim: `request_session` is pulled by a human, never by a keyword.

    `_open_or_used`'s AUDIT note calls this the strongest argument against a
    keyword trigger, and it is an argument that only holds while nothing can
    CANCEL: a false positive would consume the customer's one session for the
    period with no shipped way to give it back. DEVIATIONS D26 item 13 rests
    on the same fact and describes the promise as a «pull» rather than a
    «push» because of it. Both halves are checked here, because it is the
    PAIR that is safe — an inbound trigger would be defensible the day a
    cancel path exists, and a cancel path needs its own answer to
    cancel→request→cancel (that answer is a money question and is Fahad's).
    """
    callers = _calls_of("request_session")
    assert callers == {"src/career/telegram/console.py"}, (
        f"request_session is called from {sorted(callers)}. It was the "
        "operator's own button and nothing else, which is what makes a false "
        "positive impossible rather than merely unlikely — and a request "
        "recorded in error cannot be cleared by any shipped path"
    )
    # SCOPED to the module that owns the ledger, and to calls of `_transition`
    # by name — not to `to_status=` anywhere in the tree. The first draft of
    # this assertion swept `src/` and failed on `salla/subscriptions.py`, whose
    # eleven-state machine uses the SAME keyword about a different table. That
    # is D26's own lesson arriving inside its own guard: a matching spelling is
    # not a matching fact. `_transition` is module-private and is the only
    # writer of `career_sessions.status` after `request_session` creates the
    # row, so one file is not a shortcut here — it is the whole surface.
    session_module = pathlib.Path("src/career/promises/career_session.py")
    written = {
        keyword.value.id
        for node in ast.walk(_parsed(session_module))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "_transition"
        for keyword in node.keywords
        if keyword.arg == "to_status" and isinstance(keyword.value, ast.Name)
    }
    assert written == {"SCHEDULED", "COMPLETED"}, (
        f"career-session transitions now write {sorted(written)}. If CANCELED "
        "has acquired a writer, `_open_or_used`'s note is out of date and the "
        "unbounded cancel→request→cancel loop it names needs its answer first"
    )
    assert not _calls_of("_transition") - {str(session_module)}, (
        "`_transition` is called from outside the module that defines it — it "
        "is the private writer the note above assumes is private"
    )


def test_the_overdue_sweep_names_the_cadence_it_is_actually_run_at() -> None:
    """The claim: `escalate_overdue` is swept HOURLY, with the nightly kept as
    a backstop at the hour its own unit file names.

    RE-PINNED 2026-08-07, and the previous pin is why this one is written the
    way it is. Until then the claim was «runs once a day, at a stated hour»,
    and this test read the hour out of `career-engine-nightly.timer` and
    demanded the docstring say it — because the function has no timer of its
    own (`cv.daily_run.sweep_promises` → the delivery day → the unit), four
    links none of which are visible from the function, and the docstring said
    «04:30» for five days after Fahad moved the run to 11:00.

    That pin did its job and then became the thing it guards against: the
    cadence changed, so a test that only checks for «11:00» would go on
    passing over a docstring describing a schedule the code no longer runs.
    A register entry that survives the change it was meant to catch is worse
    than none, so this asserts the NEW truth in three parts, each of which is
    a fact about the tree rather than a sentence about it:

    1. an hourly caller genuinely EXISTS outside `cv.daily_run` — the point of
       the change is worthless if the wiring is missing, and «rides the worker
       loop» is exactly the kind of cross-module claim that rots;
    2. the interval the module declares is at most an hour, and the docstring
       pair says so — a constant raised to six hours with the prose still
       reading «hourly» is the same defect in the other direction;
    3. the nightly hour is STILL named, because the backstop is still real and
       its hour is still the worst case whenever the worker is down. That half
       of the old pin was never wrong; it had simply stopped being the whole
       schedule.
    """
    module = "src/career/promises/career_session.py"
    doc = ast.get_docstring(_function(module, "escalate_overdue")) or ""
    entry = ast.get_docstring(
        _function(module, "escalate_overdue_and_commit")) or ""

    # 1 — the hourly caller, in the tree and not in a sentence
    callers = _calls_of("escalate_overdue_and_commit") - {module}
    assert "scripts/run_worker_loop.py" in callers, (
        f"escalate_overdue_and_commit is called from {sorted(callers)}. Both "
        "docstrings say this promise is measured hourly from the conversation "
        "worker; without that caller the only schedule is the nightly and the "
        "24-hour promise is measured up to 23 hours after it expired"
    )

    # 2 — the declared interval, and the prose that describes it
    from career.promises.career_session import SLA_SWEEP_INTERVAL_SECONDS

    assert SLA_SWEEP_INTERVAL_SECONDS <= 3600.0, (
        f"the SLA sweep interval is now {SLA_SWEEP_INTERVAL_SECONDS}s. That "
        "is the worst-case lateness of the whole measurement, against a "
        "promise 24 hours long — and both docstrings still call it hourly"
    )
    assert "HOURLY" in doc, (
        "escalate_overdue's docstring no longer states its cadence. That "
        "paragraph is where a reader learns how long «the promise broke» and "
        "«the operator was told» can be apart"
    )
    assert "WHY AN HOUR" in entry, (
        "escalate_overdue_and_commit no longer argues its cadence. The hour "
        "is a decision — against the minute, against a job armed at each "
        "request's own deadline — and an unargued interval is one nobody can "
        "safely change"
    )

    # 3 — the backstop, still named, still at the hour the unit really fires
    timer = pathlib.Path("ops/systemd/career-engine-nightly.timer")
    fires = re.search(r"OnCalendar=\S+\s+(\d{2}:\d{2})", timer.read_text(encoding="utf-8"))
    assert fires is not None, f"{timer} no longer states an OnCalendar time"
    assert fires.group(1) in doc, (
        f"the nightly timer fires at {fires.group(1)} and "
        "career_session.escalate_overdue's docstring does not say so. It is "
        "still a caller — the backstop for the hour the worker is not there — "
        "and its hour is still the worst case when the worker is down"
    )
    assert "cv.daily_run.sweep_promises" in doc and "backstop" in doc, (
        "the docstring no longer says which caller is the schedule and which "
        "is the backstop. With two callers that distinction is the whole "
        "cadence: the reader cannot otherwise tell an hourly promise from a "
        "nightly one"
    )
