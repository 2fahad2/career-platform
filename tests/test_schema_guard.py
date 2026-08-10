"""«لا تسلّم ما لا تستطيع تسجيله» — proved on a database, not on a mock.

THE NIGHT THIS FILE IS ABOUT (2026-08-10, this host). The nightly run
discovered, ranked, generated and SENT four WhatsApp messages to a real
customer — two texts, a document, an interactive card, ``sent`` then
``delivered`` on Meta's receipts — and then the ledger INSERT raised
``UndefinedColumn: column "meta_error_code" of relation "delivery_messages"
does not exist``. The transaction rolled back, no ``delivery_messages`` rows
and no ``deliveries`` row survived, the unit exited 3, and the day was recorded
``CV_GENERATION_FAILED`` while the customer read his CV on his phone. The host
database was at ``0028``; the deployed code writes columns ``0030`` adds.

**Not one test in this suite could fail while that happened**, and
``scripts/gate.sh --host`` could not either — it compares systemd unit files.
So the first thing this file has to be is a test that FAILS AGAINST THE OLD
CODE, and it is built to fail against it twice over:

* :func:`test_a_stale_head_refuses_before_a_single_message_is_sent` asserts the
  fake WhatsApp client recorded **zero** sends. On the old path the identical
  fixture sends four — which is exactly what
  :func:`test_a_matching_head_delivers_the_royal_journey` proves it still does
  when the schema matches, using the same tenant, the same engine report and
  the same deps.
* both call ``cli.run_delivery_phase``, which did not exist before this wave.
  A copy of these assertions written against ``run_daily_delivery`` would have
  passed on 2026-08-10, and a test that passes against the code it was written
  to condemn is not evidence (the lesson of
  ``tests/test_nightly_verdict_wiring.py``).

The stale head is not simulated with a monkeypatch. The revision this suite's
own ``career_test`` database reports is UPDATEd, inside a transaction that is
rolled back and then explicitly restamped, so what the guard reads is what
Postgres actually answers — the same read the delivery session makes. A mocked
``measure_schema`` would prove that a stub returns what the stub was told to
return.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.db import schema_guard
from career.db.schema_guard import (
    CODE_BRANCHED,
    DB_AHEAD,
    DB_BEHIND,
    DB_UNSTAMPED,
    MATCH,
    UNMEASURABLE,
    code_heads,
    known_revisions,
    measure_schema,
    repo_root,
)
from career.engine import cli
from tests.test_alert_direction_purity import line_is_mixed
from tests.test_cv_daily_run import (
    NOW,
    _cleanup,
    _deps,
    _engine_report,
    _seed_active_tenant,
)

ROOT = Path(__file__).resolve().parent.parent


def _stamp(session: Session, *revisions: str) -> None:
    """Make ``alembic_version`` say exactly this. Rolled back by the caller."""
    session.execute(sql_text("DELETE FROM alembic_version"))
    for revision in revisions:
        session.execute(
            sql_text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": revision},
        )


def _restore(session: Session, revisions: list[str]) -> None:
    """Put the disposable test database back on its real head, committed.

    Belt and braces: every test here already works inside a transaction it
    rolls back, but a stamp left wrong would silently poison every later test
    in the session, and «the schema guard broke the suite» is a bad way to
    learn that the guard works.
    """
    session.rollback()
    _stamp(session, *revisions)
    session.commit()


# ═════════════ it MEASURES — no constant, no belief ══════════════════════════


def test_the_head_comes_from_the_scripts_and_never_from_a_constant() -> None:
    """The failure mode the brief named: a hardcoded expected head.

    ``EXPECTED_HEAD = "0030"`` would be a second copy of a fact, edited by the
    same hand that adds the migration it is supposed to police, and drifting
    from the first the moment anyone forgets. This asserts the module's SOURCE
    contains no revision id that exists in ``migrations/versions`` — so the
    guard cannot be «fixed» into the bug by a future reader in a hurry.
    """
    revisions = known_revisions(ROOT)
    assert revisions, "the checkout must have migrations to be measured against"
    tree = ast.parse(Path(schema_guard.__file__).read_text(encoding="utf-8"))
    # Docstrings and comments are exempt: the incident narrative NAMES the two
    # revisions it is about, and a guard that could not describe its own bug
    # would be a worse guard. What is forbidden is a revision reaching the
    # RUNNING code — a constant, a default, a comparison.
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef
                      | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - docstrings
    assert not (literals & revisions), (
        "career.db.schema_guard names a migration revision as a literal — "
        "that is the hardcoded expected-head bug, not the fix for it: "
        f"{sorted(literals & revisions)}"
    )


def test_the_heads_are_the_revisions_nothing_points_back_at() -> None:
    """Cross-check alembic's answer against an independent derivation.

    ``ScriptDirectory.get_heads()`` is the right source because it is what
    ``alembic upgrade head`` itself resolves. That is also a reason to check
    it: this test re-derives the head the way ``scripts/verify_restore.sh``
    does — the revision no other revision declares as its ``down_revision`` —
    and the two must agree. If they ever disagree, one of the two readings is
    of something that is not this tree.
    """
    files = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    declared: set[str] = set()
    parents: set[str] = set()
    for path in files:
        body = path.read_text(encoding="utf-8")
        for name, into in (("revision", declared), ("down_revision", parents)):
            match = re.search(
                rf"^{name}(?::\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]",
                body, re.M,
            )
            if match:
                into.add(match.group(1))
    assert set(code_heads(ROOT)) == declared - parents


def test_a_matching_database_is_the_only_deliverable_verdict(
    owner_session: Session,
) -> None:
    real = list(schema_guard.db_revisions(owner_session))
    try:
        assert measure_schema(owner_session).verdict == MATCH
        assert measure_schema(owner_session).deliverable is True
        assert measure_schema(owner_session).fix == ""
    finally:
        _restore(owner_session, real)


# ═════════════ the two directions, and the branches ══════════════════════════


def test_a_database_behind_the_code_is_refused(owner_session: Session) -> None:
    """Tonight's incident, reproduced from the database side."""
    real = list(schema_guard.db_revisions(owner_session))
    try:
        behind = sorted(known_revisions(ROOT) - set(code_heads(ROOT)))
        assert behind, "a single-revision tree cannot express this test"
        _stamp(owner_session, behind[0])
        verdict = measure_schema(owner_session)
        assert verdict.verdict == DB_BEHIND
        assert verdict.deliverable is False
        assert "alembic upgrade head" in verdict.fix
    finally:
        _restore(owner_session, real)


def test_a_database_ahead_of_the_code_is_refused_and_never_downgraded(
    owner_session: Session,
) -> None:
    """The rollback case — fatal too, and for the mirror-image reason.

    The DIRECTION of the fix is the whole point of keeping this verdict
    separate from «behind»: the newer columns already hold rows, so a guard
    that told a woken operator to `alembic downgrade` his way to a tidy report
    would destroy customer data to silence itself.
    """
    real = list(schema_guard.db_revisions(owner_session))
    try:
        _stamp(owner_session, "9999_from_a_newer_deploy")
        verdict = measure_schema(owner_session)
        assert verdict.verdict == DB_AHEAD
        assert verdict.deliverable is False
        assert "downgrade" in verdict.fix          # it is named …
        assert "alembic downgrade" not in verdict.fix  # … only to forbid it
        assert "NOT downgrade" in verdict.fix
    finally:
        _restore(owner_session, real)


def test_an_unstamped_database_is_refused(owner_session: Session) -> None:
    """Empty is not «at zero» — it is «shape unknown, built by hand»."""
    real = list(schema_guard.db_revisions(owner_session))
    try:
        owner_session.execute(sql_text("DELETE FROM alembic_version"))
        verdict = measure_schema(owner_session)
        assert verdict.verdict == DB_UNSTAMPED
        assert verdict.deliverable is False
    finally:
        _restore(owner_session, real)


def _branched_tree(root: Path) -> None:
    """A three-revision tree with two unmerged heads, on disk."""
    versions = root / "migrations" / "versions"
    versions.mkdir(parents=True)
    for revision, parent in (("aaa", None), ("bbb", "aaa"), ("ccc", "aaa")):
        down = f'"{parent}"' if parent else "None"
        (versions / f"{revision}.py").write_text(
            f'revision = "{revision}"\ndown_revision = {down}\n'
            "def upgrade() -> None: ...\ndef downgrade() -> None: ...\n",
            encoding="utf-8",
        )


def test_two_heads_on_disk_are_reported_as_a_merge_not_as_an_upgrade(
    owner_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_heads()`` returns a tuple, so the guard must handle a branch.

    «Behind» would be the lazy answer and it would send the operator at
    ``alembic upgrade head``, which REFUSES to run against two heads
    («Multiple head revisions are present»). A fix command that cannot work is
    worse than none: it costs the operator the minutes in which he believes he
    is fixing it.
    """
    real = list(schema_guard.db_revisions(owner_session))
    _branched_tree(tmp_path)
    monkeypatch.setattr(schema_guard, "repo_root", lambda: tmp_path)
    try:
        assert set(code_heads(tmp_path)) == {"bbb", "ccc"}
        _stamp(owner_session, "bbb")
        verdict = measure_schema(owner_session)
        assert verdict.verdict == CODE_BRANCHED
        assert verdict.deliverable is False
        assert "merge heads" in verdict.fix
    finally:
        _restore(owner_session, real)


def test_a_database_holding_every_head_of_a_branch_still_matches(
    owner_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sides can hold several revisions, and equality is the real test.

    A database carrying every head the checkout describes has every column the
    checkout can write — which is the only property this guard is entitled to
    have an opinion about. Refusing here because «branches are untidy» would
    be the guard inventing a policy nobody asked it for.
    """
    real = list(schema_guard.db_revisions(owner_session))
    _branched_tree(tmp_path)
    monkeypatch.setattr(schema_guard, "repo_root", lambda: tmp_path)
    try:
        _stamp(owner_session, "bbb", "ccc")
        verdict = measure_schema(owner_session)
        assert verdict.verdict == MATCH
        assert verdict.deliverable is True
    finally:
        _restore(owner_session, real)


def test_an_unreadable_version_table_refuses_and_leaves_the_session_usable(
    owner_session: Session,
) -> None:
    """Fails CLOSED, and does not become the thing that loses the night.

    Two properties in one test because they are two halves of one promise. A
    guard that cannot measure must refuse (proceeding is what 2026-08-10 cost),
    and a guard whose probe poisons the caller's transaction has destroyed the
    very records the night was supposed to write — that is literally the second
    half of that night's traceback, ``PendingRollbackError``.
    """
    real = list(schema_guard.db_revisions(owner_session))
    try:
        owner_session.execute(sql_text("DROP TABLE alembic_version"))
        verdict = measure_schema(owner_session)
        assert verdict.verdict == UNMEASURABLE
        assert verdict.deliverable is False
        # the session still works — the guard rolled its own failure back
        assert owner_session.execute(sql_text("SELECT 1")).scalar_one() == 1
    finally:
        _restore(owner_session, real)


# ═════════════ the gate: what the customer's phone actually receives ═════════


def test_a_matching_head_delivers_the_royal_journey(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """The control. This is «a run that would have sent», and it sends.

    Without it the refusal test below proves nothing: a fixture that never
    delivers records zero sends whatever the guard does.
    """
    tid, _channel = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        assert report.per_tenant[tid]["final"], "engine must pass the job"
        deps = _deps(tmp_path)
        outcome = cli.run_delivery_phase(
            owner_session, report=report, deps=deps, now=NOW,
        )
        owner_session.commit()

        assert outcome.refused is False
        assert outcome.schema.verdict == MATCH
        assert outcome.states[tid].state == "DELIVERED"
        kinds = [m.kind for m in deps.whatsapp_client.sent]
        assert kinds == ["text", "text", "document", "interactive"]
    finally:
        _cleanup(owner_session, tid)


def test_a_stale_head_refuses_before_a_single_message_is_sent(
    owner_session: Session, clean_billing: None, tmp_path: Any
) -> None:
    """THE TEST. Same customer, same jobs, same deps — one revision behind.

    ``run_daily_delivery`` sends on its first statement (``sweep_promises``
    runs before the stale-bundle sweep, which runs before any tenant loop), so
    «refuses before any send» has to mean the function is never entered at all.
    The assertion is therefore about the client's ledger and not about a
    return value: ZERO recorded sends, of any kind, to anyone.
    """
    real = list(schema_guard.db_revisions(owner_session))
    tid, _channel = _seed_active_tenant(owner_session)
    try:
        report = _engine_report(owner_session, tid)
        assert report.per_tenant[tid]["final"], "the run WOULD have delivered"
        owner_session.commit()

        behind = sorted(known_revisions(ROOT) - set(code_heads(ROOT)))
        _stamp(owner_session, behind[0])
        deps = _deps(tmp_path)
        outcome = cli.run_delivery_phase(
            owner_session, report=report, deps=deps, now=NOW,
        )

        assert outcome.refused is True
        assert outcome.schema.verdict == DB_BEHIND
        assert outcome.states == {}
        assert deps.whatsapp_client.sent == [], (
            "the guard let a message out — on 2026-08-10 four of them reached "
            "a real customer that the ledger then refused to record"
        )
        # nothing was written for the day either: a refusal is not a silent
        # half-run that leaves a delivery row behind for tomorrow to trip on.
        assert owner_session.execute(
            sql_text("SELECT count(*) FROM deliveries WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalar_one() == 0
        assert owner_session.execute(
            sql_text("SELECT count(*) FROM tenant_day_states "
                     "WHERE tenant_id = :t"),
            {"t": str(tid)},
        ).scalar_one() == 0
    finally:
        _restore(owner_session, real)
        _cleanup(owner_session, tid)


# ═════════════ what the night then SAYS ══════════════════════════════════════


def test_the_refused_phase_reaches_the_exit_code_as_its_own_number(
    owner_session: Session,
) -> None:
    """A refused night must not exit 0, and must not exit 3 either.

    0 is what the emptiness rule would answer — no day states were written,
    because nothing was attempted — and that is the exact shape of the bug
    that let a missing WhatsApp token exit 0 forever. 3 would be wrong in the
    other direction: it sends the operator hunting for a customer whose
    delivery broke, and on this night nobody's delivery was even tried.
    """
    real = list(schema_guard.db_revisions(owner_session))
    try:
        behind = sorted(known_revisions(ROOT) - set(code_heads(ROOT)))
        _stamp(owner_session, behind[0])
        stale = measure_schema(owner_session)

        phase = cli.delivery_phase_for(
            deliver=True, has_whatsapp_token=True, has_tenants=True,
            schema=stale,
        )
        assert phase == cli.PHASE_SCHEMA_REFUSED
        assert cli.exit_code_for("completed", [], delivery_phase=phase) == (
            cli.EXIT_SCHEMA_DRIFT
        )
        assert cli.EXIT_SCHEMA_DRIFT not in {
            cli.EXIT_OK, cli.EXIT_DISCOVERY_FAILED, cli.EXIT_NO_SEARCH_KEY,
            cli.EXIT_DELIVERY_FAILED, cli.EXIT_NO_WHATSAPP_TOKEN,
            cli.EXIT_LIFECYCLE_FAILED,
        }
        # discovery collapsing outranks — the bigger fire keeps its number
        assert cli.exit_code_for("discovery_failed", [],
                                 delivery_phase=phase) == (
            cli.EXIT_DISCOVERY_FAILED
        )
    finally:
        _restore(owner_session, real)


def test_a_drift_that_harms_nobody_tonight_does_not_page_as_an_outage() -> None:
    """No token, no tenants, no ``--deliver`` — nothing was going to be sent.

    The drift is still ANNOUNCED (``main`` alerts on the measurement itself,
    not on this decision), but the exit code keeps naming the most actionable
    single thing, because a summons that points at two runbook entries points
    at neither.
    """
    class _Stale:
        deliverable = False

    stale: Any = _Stale()
    assert cli.delivery_phase_for(deliver=False, has_whatsapp_token=True,
                                  has_tenants=True, schema=stale) == (
        cli.PHASE_NO_DELIVER
    )
    assert cli.delivery_phase_for(deliver=True, has_whatsapp_token=False,
                                  has_tenants=True, schema=stale) == (
        cli.PHASE_NO_CREDENTIALS
    )
    assert cli.delivery_phase_for(deliver=True, has_whatsapp_token=True,
                                  has_tenants=False, schema=stale) == (
        cli.PHASE_NO_TENANTS
    )


def test_the_operator_page_is_direction_pure_and_carries_the_command(
    owner_session: Session,
) -> None:
    """Fahad's client reverses any line mixing Arabic with Latin or digits.

    Both revision ids, the verdict token and the shell command are Latin, and
    every one of them has to arrive readable — an alert he cannot read is an
    alert that did not fire. The tree-wide guard in
    ``tests/test_alert_direction_purity.py`` enforces this over the source; this
    checks the RENDERED string for all three shapes of drift, because the
    verdict picks a different Arabic sentence for each.
    """
    real = list(schema_guard.db_revisions(owner_session))
    try:
        for revision in (sorted(known_revisions(ROOT) - set(code_heads(ROOT)))[0],
                         "9999_from_a_newer_deploy"):
            _stamp(owner_session, revision)
            verdict = measure_schema(owner_session)
            alert = cli.format_schema_alert(verdict)
            assert [ln for ln in alert.split("\n") if line_is_mixed(ln)] == []
            assert verdict.verdict in alert
            assert revision in alert
            for head in code_heads(ROOT):
                assert head in alert
    finally:
        _restore(owner_session, real)

    # the unmeasurable shape has no revisions to name at all
    blank = schema_guard.SchemaVerdict(UNMEASURABLE, (), (), "x", "y")
    lines = cli.format_schema_alert(blank).split("\n")
    assert [ln for ln in lines if line_is_mixed(ln)] == []


# ═════════════ the scope this deliberately does NOT cover ════════════════════


def test_the_conversation_worker_is_not_gated_by_this_guard() -> None:
    """A considered rejection, held in place by a test rather than a comment.

    The worker answers inbound customer messages. Refusing there converts «a
    reply we cannot fully record» into total silence at a paying customer who
    just asked a question — a different and probably worse trade than the
    nightly's, which is a scheduled push that re-runs tomorrow having lost a
    day and nothing else. If someone later decides the worker should refuse
    too, that is a product decision with an owner, and this test failing is
    where it gets made rather than where it gets noticed.
    """
    worker = (ROOT / "src" / "career" / "whatsapp" / "worker.py").read_text(
        encoding="utf-8"
    )
    assert "schema_guard" not in worker
    assert "measure_schema" not in worker


def test_the_guard_is_actually_wired_into_the_nightly() -> None:
    """The July lesson: a guard that is not installed is a comment.

    Reads the source of ``main`` rather than trusting that someone kept the
    call — the systemd guard, the entitlement scan and the role ratchet all
    passed while the thing they described was not running anywhere.
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")
    main_body = source.split("def main(", 1)[1]
    assert "measure_schema(session)" in main_body
    assert "run_delivery_phase(" in main_body
    assert "schema=schema" in main_body
    # and the phase itself does not reach run_daily_delivery any other way
    assert "run_daily_delivery(" not in main_body


def test_the_verdict_prints_only_revisions_and_english() -> None:
    """§15.13: the journal and the admin channel carry no PII, ever.

    A schema verdict is structurally incapable of holding a phone or a name —
    it holds revision ids — and this pins that shape so a future «helpful»
    addition (the tenant it refused, the customer it was about to serve) has
    to argue with a test first.
    """
    fields = set(schema_guard.SchemaVerdict.__dataclass_fields__)
    assert fields == {"verdict", "db_revisions", "code_heads", "detail", "fix"}
    assert set(measure_schema.__annotations__) == {"session", "return"}
    assert isinstance(repo_root(), Path)
