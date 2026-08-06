"""The honest daily close (constant §15.12) — before code.

ONE honest state per tenant per day out of exactly seven — never a silent
success. Suppression is recorded ONLY for delivered groups (a failed job
returns tomorrow; a delivered one never repeats inside the TTL); a ledger
write failure is itself an honest state (LEDGER_FAILED), not an exception.
The admin summary carries TEN-#### codes and numbers only — no PII, ever.
Usage events roll up into per-tenant/day cost allocations (§14 fuel).
"""

from __future__ import annotations

import ast
import pathlib
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.cv import close

NOW = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
DAY = date(2026, 7, 17)


# ── the one-state authority (pure) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(discovery_ok=False, gate_passes=0, cv_resolved=0, cv_failed=0,
              delivered=0, failed_sends=0), "DISCOVERY_FAILED"),
        (dict(discovery_ok=True, gate_passes=0, cv_resolved=0, cv_failed=0,
              delivered=0, failed_sends=0), "NO_MATCHES"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=0, cv_failed=3,
              delivered=0, failed_sends=0), "CV_GENERATION_FAILED"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
              delivered=0, failed_sends=2), "WHATSAPP_FAILED"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
              delivered=1, failed_sends=1), "PARTIAL_DELIVERY"),
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=3, cv_failed=0,
              delivered=3, failed_sends=0), "DELIVERED"),
        # CV failures with full delivery of the resolved rest → still partial
        (dict(discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
              delivered=2, failed_sends=0), "PARTIAL_DELIVERY"),
    ],
)
def test_daily_state_authority(kwargs: dict, expected: str) -> None:
    assert close.daily_state(ledger_ok=True, **kwargs) == expected


def test_ledger_failure_dominates_everything() -> None:
    assert close.daily_state(
        ledger_ok=False, discovery_ok=True, gate_passes=3, cv_resolved=3,
        cv_failed=0, delivered=3, failed_sends=0,
    ) == "LEDGER_FAILED"


def test_the_vocabulary_is_exactly_the_eight() -> None:
    # CHANGELOG §12: seven computational states + the event-driven eighth
    assert set(close.DAILY_STATES) == {
        "DELIVERED", "NO_MATCHES", "PARTIAL_DELIVERY", "DISCOVERY_FAILED",
        "CV_GENERATION_FAILED", "WHATSAPP_FAILED", "LEDGER_FAILED",
        "SKIPPED_OPTED_OUT",
    }


# ── the atomic close (DB) ────────────────────────────────────────────────────


def _seed_tenant(owner_session: Session) -> uuid.UUID:
    tid = uuid.uuid4()
    owner_session.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
        {"id": str(tid), "c": f"TEN-C{uuid.uuid4().hex[:4]}"},
    )
    owner_session.commit()
    return tid


def _cleanup(owner_session: Session, tid: uuid.UUID) -> None:
    owner_session.execute(
        sql_text("DELETE FROM tenants WHERE id = :id"), {"id": str(tid)}
    )
    owner_session.commit()


def test_close_records_state_and_suppresses_only_delivered(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        state = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
            delivered_groups=["https://a.example/j/1"],
            failed_groups=["https://a.example/j/2"],
        )
        owner_session.commit()
        assert state.state == "PARTIAL_DELIVERY"
        rows = owner_session.execute(
            sql_text("SELECT suppression_key FROM tenant_job_suppressions "
                     "WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).all()
        assert [r.suppression_key for r in rows] == ["https://a.example/j/1"]
        # rerun the close (idempotent day): upserts, never duplicates
        state2 = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=3, cv_resolved=2, cv_failed=1,
            delivered_groups=["https://a.example/j/1"], failed_groups=[],
        )
        owner_session.commit()
        assert state2.id == state.id
        count = owner_session.execute(
            sql_text("SELECT count(*) FROM tenant_day_states WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalar_one()
        assert count == 1
    finally:
        _cleanup(owner_session, tid)


def test_ledger_write_failure_is_an_honest_state_not_an_exception(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)

    def broken_suppressor(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    try:
        state = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=1, cv_resolved=1, cv_failed=0,
            delivered_groups=["https://a.example/j/1"], failed_groups=[],
            suppressor=broken_suppressor,
        )
        owner_session.commit()
        assert state.state == "LEDGER_FAILED"
    finally:
        _cleanup(owner_session, tid)


def test_opt_out_after_a_real_delivery_never_erases_it(
    owner_session: Session,
) -> None:
    """P1-6. The opt-out close runs AFTER the delivery phase, so this is the
    ordinary morning of a customer who receives his jobs and then presses
    «إيقاف»: his DELIVERED day, with its real counts, must survive. It did not
    — the row was rewritten SKIPPED_OPTED_OUT with delivered: 0 while the
    suppression ledger still recorded those jobs as sent."""
    tid = _seed_tenant(owner_session)
    try:
        delivered = close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=2, cv_resolved=2, cv_failed=0,
            delivered_groups=["https://a.example/j/1", "https://a.example/j/2"],
            failed_groups=[],
        )
        owner_session.commit()
        assert delivered.state == "DELIVERED"

        after = close.close_skipped_opted_out(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW, gate_passes=2,
        )
        owner_session.commit()
        assert after.state == "DELIVERED"
        assert after.counts["delivered"] == 2
    finally:
        _cleanup(owner_session, tid)


def test_opt_out_still_records_a_day_that_delivered_nothing(
    owner_session: Session,
) -> None:
    """The other half of the same rule: nothing was received, so the eighth
    state is the latest truth and must be written."""
    tid = _seed_tenant(owner_session)
    try:
        close.close_tenant_day(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW,
            discovery_ok=True, gate_passes=0, cv_resolved=0, cv_failed=0,
            delivered_groups=[], failed_groups=[],
        )
        owner_session.commit()
        state = close.close_skipped_opted_out(
            owner_session, tenant_id=tid, run_date=DAY, now=NOW, gate_passes=3,
        )
        owner_session.commit()
        assert state.state == "SKIPPED_OPTED_OUT"
        assert state.counts["gate_passes"] == 3
    finally:
        _cleanup(owner_session, tid)


# ── the single writer of tenant_day_states ───────────────────────────────────
#
# The first version of this guard parsed ONE FILE — `src/career/cv/close.py` —
# and asked which functions in it construct `TenantDayState` or assign to
# `.state`/`.counts`/`.recorded_at`. The invariant it was written for is not
# about that file. It is about the TABLE: every row in `tenant_day_states` is
# written by one authority, so the monotonic rule in `_outranks` cannot be
# skipped by a writer who never heard of it. A guard scoped to the file the
# authority happens to live in is satisfied by writing the second writer
# somewhere else, which is the same class of mistake as the incident itself:
# the rule was correct and merely bypassable.
#
# Six ways the old guard could be walked around, every one of them the sort of
# thing a hurried author writes without malice:
#
#   1. the writer lives in `daily_run.py`, `console.py`, or a repair script
#   2. `models.TenantDayState(...)` — the old check demanded a bare `ast.Name`
#   3. `session.execute(update(TenantDayState).values(state=...))` — Core, not ORM
#   4. `async def` — the old loop looked only at `ast.FunctionDef`
#   5. `setattr(row, "state", x)` — a Call, not an `ast.Assign`
#   6. `row.counts["delivered"] = 0` — a Subscript target, not an Attribute one
#
# Plus the one that needs no Python object at all: raw SQL, `UPDATE
# tenant_day_states SET …`, or a table name handed to a statement built at
# runtime. All eight shapes are detected below, over the whole source tree.

_REPO = pathlib.Path(__file__).resolve().parents[1]

#: The one door — a (file, qualified scope) pair, not a bare function name.
#: The old assertion was `writers == {"_record_day_state"}`, which a second
#: module defining its own `_record_day_state` satisfies exactly.
_AUTHORITY = ("src/career/cv/close.py", "_record_day_state")

#: Roots scanned. `src` rather than `src/career` so `src/career_core` is
#: covered the day it grows a database surface (today it has none — it is pure
#: functions by charter, see its `__init__` docstring).
#:
#: `migrations/` is deliberately absent: Alembic owns the table's shape and
#: naming it in DDL is not writing a row. That is a real residual — a data
#: migration that backfills day states would not be seen here — and it is
#: named rather than papered over.
_SCAN_ROOTS = ("src", "scripts")

#: Non-writer sites that still name the table, keyed by (file, scope, shape),
#: with the reason each is not a second authority. A ratchet in both
#: directions: a new shape at the same site fails, and an entry that stops
#: matching fails too, so the list cannot rot into permission.
_TABLE_MENTION_ALLOWED: dict[tuple[str, str, str], str] = {
    ("src/career/db/models.py", "TenantDayState", "table_name_literal"):
        "the ORM declaration — `__tablename__` IS the table name",
    ("scripts/drill_disaster.py", "main", "table_name_literal"):
        "the disaster drill's own teardown: it deletes the two tenants IT "
        "created, by looping a tuple of table names. It removes rows, it never "
        "records a state, and it runs by hand against a disposable database",
}

#: Methods that mutate the object they are called on. Deliberately a list of
#: mutators and not «any method», because `Subscription.tenant_id.in_(...)`
#: is also a call on an attribute named like one of our columns.
_MUTATING_METHODS = frozenset(
    {"update", "pop", "popitem", "clear", "setdefault", "append", "extend",
     "insert", "__setitem__", "__delitem__"}
)

#: Calls that take a mapped class and produce a WRITE. `select(...)` is
#: absent on purpose: reading the table is what the console does all day.
_WRITE_VERBS = frozenset(
    {"insert", "update", "delete", "merge", "add", "add_all",
     "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"}
)


def _day_state_columns() -> frozenset[str]:
    """Read the guarded column names off the model, never a hand-typed tuple.

    The old guard hard-coded ("state", "counts", "recorded_at"). A ninth column
    added tomorrow would have been outside it silently; taken from the mapper,
    the guard widens itself.
    """
    from sqlalchemy import inspect as sa_inspect

    from career.db.models import TenantDayState

    return frozenset(sa_inspect(TenantDayState).columns.keys())


def _model_ref(node: ast.AST) -> bool:
    """`TenantDayState`, `models.TenantDayState`, `db.models.TenantDayState`."""
    return (isinstance(node, ast.Name) and node.id == "TenantDayState") or (
        isinstance(node, ast.Attribute) and node.attr == "TenantDayState"
    )


def _write_sites(paths: list[pathlib.Path], root: pathlib.Path) -> set[tuple[str, str, str]]:
    """Every place in `paths` that can put a row into `tenant_day_states`.

    Returns (relative file, qualified scope, shape). The scope is qualified
    through nested classes and functions so a writer hidden inside a closure is
    still named, and `async def` counts — the old guard's `ast.FunctionDef`
    test made an async writer invisible for no reason anyone chose.
    """
    from career.db.models import TenantDayState

    table = TenantDayState.__tablename__
    columns = _day_state_columns()
    #: A write in raw SQL, however it is quoted or spaced.
    sql_write = re.compile(
        r"(insert\s+into|update|delete\s+from)\s+[\"']?" + re.escape(table),
        re.IGNORECASE,
    )
    found: set[tuple[str, str, str]] = set()

    for path in paths:
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Does this module name the class at all? Attribute writes are only
        # looked for where it does: `.state = x` is far too common a line to
        # flag everywhere, and a module that never names the class cannot get
        # hold of a row except by being handed one — the residual noted in the
        # deliverable, and the reason the table-name and construction checks
        # below are NOT narrowed this way.
        holds_model = any(_model_ref(n) for n in ast.walk(tree))

        def record(scope: list[str], shape: str, *, rel: str = rel) -> None:
            found.add((rel, ".".join(scope) or "<module>", shape))

        def check(node: ast.AST, scope: list[str], *, holds: bool = holds_model) -> None:
            if isinstance(node, ast.Call):
                if _model_ref(node.func):
                    record(scope, "construct")          # shapes 2 and 3 above
                verb = (
                    node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None)
                )
                if verb in _WRITE_VERBS and any(_model_ref(a) for a in node.args):
                    record(scope, f"statement:{verb}")  # update(TenantDayState)
                if holds and verb == "setattr":
                    record(scope, "setattr")            # shape 5
                if (
                    holds
                    and verb in _MUTATING_METHODS
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr in columns
                ):
                    record(scope, "in_place_mutation")  # row.counts.update(…)
            if holds and isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in columns:
                        record(scope, f"attribute_write:{target.attr}")
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr in columns
                    ):
                        record(scope, f"item_write:{target.value.attr}")  # shape 6
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if sql_write.search(node.value):
                    record(scope, "raw_sql_write")
                elif node.value.strip() == table:
                    # the table name alone, which is how a statement gets built
                    # at runtime — `f"DELETE FROM {table} WHERE …"`
                    record(scope, "table_name_literal")

        def descend(node: ast.AST, scope: list[str]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                ):
                    descend(child, [*scope, child.name])
                else:
                    check(child, scope)
                    descend(child, scope)

        descend(tree, [])
    return found


def _production_write_sites() -> set[tuple[str, str, str]]:
    files = [
        p
        for root in _SCAN_ROOTS
        for p in sorted((_REPO / root).rglob("*.py"))
        if "__pycache__" not in p.parts
    ]
    return _write_sites(files, _REPO)


def test_the_day_state_table_has_exactly_one_writer() -> None:
    """The rule was correct and merely bypassable, so the second writer that
    arrived (CHANGELOG §12's opt-out close) bypassed it and cost a real
    delivery: a customer who received his jobs at 06:00 and pressed «إيقاف» at
    06:10 had his DELIVERED day rewritten SKIPPED_OPTED_OUT with delivered: 0,
    while the suppression ledger still recorded those jobs as sent.

    A ninth state will be written by somebody who never read `_outranks`. This
    fails the moment that somebody touches the ROW — in any file under `src` or
    `scripts`, through the ORM, through Core, or in SQL — instead of going
    through the one door.
    """
    offenders = sorted(
        site for site in _production_write_sites()
        if (site[0], site[1]) != _AUTHORITY
        and site not in _TABLE_MENTION_ALLOWED
    )
    assert not offenders, (
        "tenant_day_states is written outside "
        f"{_AUTHORITY[0]}::{_AUTHORITY[1]} by: {offenders}. Route it through "
        "close._record_day_state — that function IS the monotonic rule, and a "
        "writer that skips it silently un-delivers a real delivery."
    )


def test_the_authority_is_still_the_authority() -> None:
    """The other direction: the guard above passes trivially if nobody writes
    the table at all — including on the day somebody renames `_record_day_state`
    or moves it, which is exactly when the assertion should speak up."""
    sites = _production_write_sites()
    authority = {shape for f, scope, shape in sites if (f, scope) == _AUTHORITY}
    assert "construct" in authority and any(
        s.startswith("attribute_write:") for s in authority
    ), (
        f"{_AUTHORITY[0]}::{_AUTHORITY[1]} no longer builds or updates the row "
        f"(shapes seen: {sorted(authority)}) — if the authority moved, move "
        "_AUTHORITY with it; if it was deleted, the guard is now guarding air."
    )


def test_the_table_mention_allowlist_still_earns_its_place() -> None:
    """An allow-list nobody re-checks becomes permission (the lesson from
    `_PRICE_HISTORY` in test_docs_truth.py, learned the same week)."""
    sites = _production_write_sites()
    stale = sorted(set(_TABLE_MENTION_ALLOWED) - sites)
    assert not stale, (
        f"these sites no longer mention {stale} — delete them from "
        "_TABLE_MENTION_ALLOWED so the guard covers those files again"
    )


#: One synthetic module per bypass the reviewer walked through the old guard,
#: written the way a real author would write it. Keeping them here rather than
#: in a throwaway file run once by hand is the point: the guard's own guard has
#: to survive the next edit of the guard.
_BYPASS_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "a writer in another module entirely",
        "from career.db.models import TenantDayState\n"
        "def repair(session, tid, day):\n"
        "    session.add(TenantDayState(tenant_id=tid, run_date=day,\n"
        "                               state='DELIVERED', counts={}))\n",
    ),
    (
        "qualified construction — models.TenantDayState(...)",
        "from career.db import models\n"
        "def repair(session):\n"
        "    session.add(models.TenantDayState(state='NO_MATCHES'))\n",
    ),
    (
        "SQLAlchemy Core — update(TenantDayState)",
        "from sqlalchemy import update\n"
        "from career.db.models import TenantDayState\n"
        "def repair(session):\n"
        "    session.execute(update(TenantDayState).values(state='DELIVERED'))\n",
    ),
    (
        "an async writer",
        "from career.db.models import TenantDayState\n"
        "async def repair(session):\n"
        "    session.add(TenantDayState(state='DELIVERED'))\n",
    ),
    (
        "setattr instead of an assignment",
        "from career.db.models import TenantDayState\n"
        "def repair(row: TenantDayState):\n"
        "    setattr(row, 'state', 'DELIVERED')\n",
    ),
    (
        "an in-place edit of the counts dict",
        "from career.db.models import TenantDayState\n"
        "def repair(row: TenantDayState):\n"
        "    row.counts['delivered'] = 0\n",
    ),
    (
        "an in-place mutation by method",
        "from career.db.models import TenantDayState\n"
        "def repair(row: TenantDayState):\n"
        "    row.counts.update(delivered=0)\n",
    ),
    (
        "raw SQL that never mentions the class",
        "from sqlalchemy import text\n"
        "def repair(session):\n"
        "    session.execute(text(\"UPDATE tenant_day_states SET state='X'\"))\n",
    ),
    (
        "a statement built from the table name at runtime",
        "from sqlalchemy import text\n"
        "TABLE = 'tenant_day_states'\n"
        "def repair(session):\n"
        "    session.execute(text(f'DELETE FROM {TABLE}'))\n",
    ),
    (
        "a writer hidden inside a closure",
        "from career.db.models import TenantDayState\n"
        "def outer(session):\n"
        "    def inner():\n"
        "        session.add(TenantDayState(state='DELIVERED'))\n"
        "    return inner\n",
    ),
    (
        "a writer on a class method",
        "from career.db.models import TenantDayState\n"
        "class Repairer:\n"
        "    async def fix(self, session):\n"
        "        session.add(TenantDayState(state='DELIVERED'))\n",
    ),
)


@pytest.mark.parametrize(("label", "source"), _BYPASS_SHAPES, ids=lambda v: v[:40])
def test_each_known_bypass_is_seen(
    tmp_path: pathlib.Path, label: str, source: str
) -> None:
    """Every shape above was written into the tree, confirmed caught, deleted.

    This is that experiment made permanent. A guard is only worth its docstring
    if somebody has actually tried to walk past it, and «somebody tried once,
    by hand, in August» is not a property the next refactor preserves.
    """
    module = tmp_path / "second_writer.py"
    module.write_text(source, encoding="utf-8")
    assert _write_sites([module], tmp_path), (
        f"the single-writer guard does not see: {label}"
    )


def test_the_bypass_detector_does_not_flag_a_reader(tmp_path: pathlib.Path) -> None:
    """The other half of «does it work»: a guard that flags everything is
    deleted by the first person it inconveniences, and then nothing is guarded.
    Reading the table — which the console does on every screen — is not a
    write, and neither is a column-named attribute on some other model.
    """
    module = tmp_path / "reader.py"
    module.write_text(
        "from sqlalchemy import select\n"
        "from career.db.models import Subscription, TenantDayState\n"
        "def show(session):\n"
        "    rows = session.execute(select(TenantDayState.state)).scalars().all()\n"
        "    session.execute(select(Subscription).where(\n"
        "        Subscription.tenant_id.in_([1])))\n"
        "    banner = 'the tenant_day_states ledger'\n"
        "    return rows, banner\n",
        encoding="utf-8",
    )
    assert _write_sites([module], tmp_path) == set()


# ── the admin summary: TEN codes and numbers only ────────────────────────────


def test_admin_summary_has_codes_and_numbers_never_pii() -> None:
    text = close.format_admin_summary(
        DAY,
        [
            ("TEN-0001", "DELIVERED", {"delivered": 3}),
            ("TEN-0002", "NO_MATCHES", {"evaluated": 41}),
        ],
    )
    assert "TEN-0001" in text and "DELIVERED" in text
    assert "TEN-0002" in text and "NO_MATCHES" in text
    assert "2026-07-17" in text
    for forbidden in ("+9665", "@", "Fahad"):
        assert forbidden not in text


# ── usage → cost allocations (§14 fuel) ──────────────────────────────────────


def test_usage_rolls_up_into_cost_allocations(owner_session: Session) -> None:
    tid = _seed_tenant(owner_session)
    try:
        for cost in ("0.011", "0.014"):
            close.record_usage(
                owner_session, tenant_id=tid, kind="llm_generation",
                input_tokens=1000, output_tokens=400,
                cost_usd=Decimal(cost), now=NOW,
            )
        close.record_usage(
            owner_session, tenant_id=tid, kind="jd_enrichment",
            cost_usd=None, now=NOW,
        )
        owner_session.commit()
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)  # idempotent
        owner_session.commit()
        rows = owner_session.execute(
            sql_text("SELECT category, events, cost_usd FROM cost_allocations "
                     "WHERE tenant_id = :t ORDER BY category"),
            {"t": str(tid)},
        ).all()
        assert [(r.category, r.events) for r in rows] == [
            ("jd_enrichment", 1), ("llm_generation", 2),
        ]
        assert rows[1].cost_usd == Decimal("0.025000")
    finally:
        _cleanup(owner_session, tid)


def test_usage_budget_guard(owner_session: Session) -> None:
    tid = _seed_tenant(owner_session)
    try:
        budget = close.UsageBudget(
            owner_session, tenant_id=tid, kind="llm_generation", cap=2, day=DAY
        )
        assert budget.allow() == (True, None)
        for _ in range(2):
            close.record_usage(
                owner_session, tenant_id=tid, kind="llm_generation", now=NOW
            )
        owner_session.commit()
        allowed, reason = budget.allow()
        assert allowed is False and reason == "budget_cap_reached:llm_generation"
    finally:
        _cleanup(owner_session, tid)
