"""The engine factory, asked the questions the AST ratchet could not ask.

`tests/test_rls_runtime_role.py` scans source for the owner's name and for
modules that build their own engine. It wrote down its own limit honestly: a
name scan cannot resolve ``getattr(settings, "owner_" + "database_url")``, a
DSN read out of a vault at runtime, a psycopg connection assembled from four
separate ``os.environ`` reads with no recognisable literal, or a third-party
library that dials the database itself.

`career.db.session` closes that at the only place where all four stop being
disguised — the username on its way to libpq. This file is where that claim is
exercised: each of the four shapes is written the way a real author would
write it and each is proven to be refused, the escapes are proven to be a
small closed set that each say why they exist, and the staged one is proven to
warn today and to refuse the moment ``DB_ROLE_GUARD=enforce``.

Nothing here needs a database. The last test does open a connection, and it is
the point of the file: a role nobody declared never reaches Postgres at all —
the refusal happens before the credentials leave the process, so the guard
holds even where the password would have been accepted.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from career.config import get_settings
from career.db import session as db_session
from career.db.session import (
    _D20_LEGACY_ENTRYPOINTS,
    _ESCAPES,
    _OPERATOR_ONE_SHOT_ENTRYPOINTS,
    ROLE_GUARD_ENV,
    ForbiddenDatabaseRole,
    _check_role,
    _process_escape,
    app_engine,
    engine_for,
)

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _guard_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the shipped posture: staged escapes warn.

    The mode is read from the environment on every check precisely so that it
    can be flipped without a deploy; that also means a test that sets it must
    not leave it set for the next one.
    """
    monkeypatch.delenv(ROLE_GUARD_ENV, raising=False)
    db_session._warned_escapes.clear()


# ── the ordinary path ────────────────────────────────────────────────────────


def test_the_application_role_needs_no_escape() -> None:
    """A guard that made the normal case awkward would be routed around."""
    engine = engine_for(get_settings().app_database_url)
    assert engine.url.username == get_settings().db_user
    engine.dispose()


def test_the_app_engine_this_module_exports_went_through_the_factory() -> None:
    """The product's own engine is not exempt from the product's own rule."""
    assert app_engine.url.username in db_session._APPLICATION_ROLES


# ── the four shapes a name scan cannot see ───────────────────────────────────


def _dsn_for(role: str) -> str:
    return f"postgresql+psycopg://{role}:pw@127.0.0.1:5433/career_test"


def _assembled_from_four_environment_reads() -> str:
    """A DSN with no recognisable literal in it anywhere.

    Written out rather than described: this is the shape the ratchet's own
    docstring named as invisible to it, and it is invisible — there is no
    attribute access, no role name, and no connection string in the source.
    """
    parts = [os.environ.get(k, d) for k, d in (
        ("X_ROLE", "career_owner"), ("X_PW", "pw"),
        ("X_HOST", "127.0.0.1"), ("X_DB", "career_test"),
    )]
    return "postgresql+psycopg://" + parts[0] + ":" + parts[1] + "@" + parts[2] + "/" + parts[3]


def _read_from_a_vault() -> str:
    """Stands in for a secrets manager: the DSN does not exist until runtime."""
    return _dsn_for("some_role_from_a_vault")


_INVISIBLE_SHAPES: tuple[tuple[str, Any], ...] = (
    (
        "getattr(settings, 'owner_' + 'database_url')",
        lambda: getattr(get_settings(), "owner_" + "database_url"),
    ),
    ("a DSN that only exists at runtime", _read_from_a_vault),
    ("four environment reads and no literal", _assembled_from_four_environment_reads),
)


@pytest.mark.parametrize(("label", "build"), _INVISIBLE_SHAPES, ids=lambda v: str(v)[:40])
def test_the_shapes_no_scan_can_see_are_refused_at_construction(
    label: str, build: Any
) -> None:
    with pytest.raises(ForbiddenDatabaseRole) as err:
        engine_for(build())
    assert "refusing to connect as" in str(err.value), label


def test_a_library_that_dials_the_database_itself_is_still_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth shape, and the one the factory alone could never cover.

    A dependency that builds its own engine never hears of `engine_for`. It
    still has to open a DBAPI connection, and the listener is registered on the
    Engine CLASS, so it applies to an engine this product has never seen. The
    process is pretended to be an unnamed one, because the suite itself holds a
    permanent escape and would otherwise be allowed through.
    """
    monkeypatch.setattr(db_session, "_process_escape", lambda: None)
    rogue = create_engine(_dsn_for("career_owner"))
    with pytest.raises(ForbiddenDatabaseRole):
        rogue.connect()
    rogue.dispose()


def test_a_libpq_conninfo_string_is_read_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some dialects pass `user=… dbname=…` positionally instead of as keywords.

    Checked directly against the listener rather than through a dialect,
    because the point is the parsing: a username hidden in a positional string
    must not read as «no username to judge».
    """
    monkeypatch.setattr(db_session, "_process_escape", lambda: None)
    with pytest.raises(ForbiddenDatabaseRole):
        db_session._guard_every_connection(
            None, None, ["user=career_owner dbname=career_test"], {}
        )


def test_a_dsn_that_names_no_user_is_refused() -> None:
    """libpq would fall back to the operating-system user — root, in the container.

    An unpredictable role is worse than a wrong one: it passes on the machine
    where somebody tested it and fails, or silently succeeds as a superuser,
    on the machine where it runs.
    """
    with pytest.raises(ForbiddenDatabaseRole) as err:
        engine_for("postgresql+psycopg://127.0.0.1:5433/career_test")
    assert "no user in the DSN" in str(err.value)


def test_a_dialect_with_no_roles_at_all_is_left_alone() -> None:
    """sqlite has no username to judge, and inventing a verdict for it would
    only teach people to switch the guard off."""
    assert db_session._guard_every_connection(None, None, [], {}) is None


# ── the escapes ──────────────────────────────────────────────────────────────


def test_the_escapes_are_a_small_closed_set() -> None:
    """Four. The number is asserted because «a small number of named escapes»
    is the claim, and a list that grows quietly is how it stops being true."""
    assert set(_ESCAPES) == {
        "alembic", "pytest", "operator_one_shot", "d20_legacy_service",
    }


@pytest.mark.parametrize("name", sorted(_ESCAPES))
def test_every_escape_says_in_code_why_it_exists(name: str) -> None:
    """A reason a reviewer can weigh, not a label. The test is crude on
    purpose — it cannot judge the prose, only that somebody wrote some."""
    why = _ESCAPES[name].why
    assert len(why) > 120, f"{name} has no real justification: {why!r}"


def test_only_the_d20_escape_is_staged() -> None:
    """The other three cannot be migrated away and must not pretend they can.

    Alembic needs the role that owns the tables, the suite has to look at the
    guarantees from outside them, and the operator one-shots are where D20
    stage 4 deliberately leaves owner access. Marking any of them temporary
    would put a permanent warning in the log, which is the fastest way to make
    the log unreadable.
    """
    staged = {n for n, e in _ESCAPES.items() if not e.permanent}
    assert staged == {"d20_legacy_service"}


def test_a_typo_in_an_escape_name_is_not_permission() -> None:
    with pytest.raises(ForbiddenDatabaseRole) as err:
        _check_role("career_owner", escape="alembic_", site="t")
    assert "unknown escape" in str(err.value)


def test_a_privileged_role_with_no_escape_is_refused_from_the_first_day() -> None:
    """This branch raises now, not later, and that is shippable because it is
    empty: the whole existing estate is on one of the two entrypoint lists, so
    nothing that runs today lands here. What lands here is a service written
    tomorrow that reaches for the owner — the exact regression D20 is about."""
    with pytest.raises(ForbiddenDatabaseRole) as err:
        _check_role("career_owner", escape=None, site="a_new_service.py")
    assert "tenant_session" in str(err.value), "a refusal must name the way out"


# ── warn today, refuse on the trigger ────────────────────────────────────────


def test_the_staged_escape_warns_today_instead_of_taking_the_product_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The honest middle. The three services still hold 22 owner call sites;
    a guard that demanded they all move in one deploy would not be deployed."""
    with caplog.at_level(logging.WARNING, logger="career.db"):
        _check_role("career_owner", escape="d20_legacy_service", site="run_worker_loop.py")
    assert any("staged escape" in r.getMessage() for r in caplog.records)


def test_the_warning_is_said_once_per_process_not_once_per_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A line per pooled connection is a line every few seconds, and a warning
    that appears every few seconds is one an operator writes a filter for."""
    with caplog.at_level(logging.WARNING, logger="career.db"):
        for _ in range(5):
            _check_role("career_owner", escape="d20_legacy_service", site="run_nightly.py")
    assert len([r for r in caplog.records if "role guard" in r.getMessage()]) == 1


def test_the_environment_variable_is_what_flips_warn_into_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trigger, exercised. Staging sets this first — a process that still
    needs the owner then fails at boot, which is a Tuesday afternoon problem
    rather than an 04:30 one — and production follows it."""
    monkeypatch.setenv(ROLE_GUARD_ENV, "enforce")
    with pytest.raises(ForbiddenDatabaseRole) as err:
        _check_role("career_owner", escape="d20_legacy_service", site="run_worker_loop.py")
    assert "staged escape" in str(err.value)


def test_enforce_does_not_touch_the_permanent_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the flip would break migrations, and would therefore never
    be flipped."""
    monkeypatch.setenv(ROLE_GUARD_ENV, "enforce")
    for name in ("alembic", "pytest", "operator_one_shot"):
        _check_role("career_owner", escape=name, site="t")


# ── which process is which ───────────────────────────────────────────────────


def test_alembic_and_pytest_are_recognised_by_module_not_by_argv() -> None:
    """Both are launched half a dozen ways — `python -m pytest`, a tox shim,
    `alembic upgrade head` — and argv agrees on none of them. Being inside a
    pytest run is itself the evidence."""
    assert _process_escape() == "pytest"


def test_the_processes_systemd_runs_are_the_processes_the_guard_knows(
) -> None:
    """Read out of the unit files rather than trusted from memory.

    The escape list is a claim about which processes exist. `ops/systemd/`
    is where that claim is actually true, and a new unit file added without a
    matching escape is a service that would fail at its first connection —
    better to find that out here than at 04:30.
    """
    known = set(_D20_LEGACY_ENTRYPOINTS) | set(_OPERATOR_ONE_SHOT_ENTRYPOINTS)
    units = sorted((_REPO / "ops" / "systemd").glob("*.service"))
    assert units, "no unit files found — has ops/systemd moved?"
    started: set[str] = set()
    for unit in units:
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                started.update(
                    Path(token).name
                    for token in line.split()
                    if token.endswith(".py")
                )
    unknown = sorted(started - known)
    assert not unknown, (
        f"systemd starts these and the guard has never heard of them: {unknown}"
    )


def test_the_guard_is_installed_in_the_processes_it_was_written_for() -> None:
    """`career.db.models` imports `career.db.session` for the side effect.

    The three D20 services do not import the session module — they build their
    own engine and go straight to the models — so the listener would be
    installed in the API process and absent from exactly the three processes
    that connect as the owner. The import edge is what carries it, so the edge
    is asserted rather than assumed; deleting it as an unused import is the
    obvious way to silently uninstall the guard.
    """
    tree = ast.parse(
        (_REPO / "src" / "career" / "db" / "models.py").read_text(encoding="utf-8")
    )
    imports_session = any(
        isinstance(node, ast.ImportFrom)
        and node.module in {"career.db", "career.db.session"}
        and any(a.name in {"session", "*"} for a in node.names)
        or isinstance(node, ast.Import)
        and any(a.name == "career.db.session" for a in node.names)
        for node in ast.walk(tree)
    )
    assert imports_session, (
        "career.db.models no longer imports career.db.session — the role guard "
        "is now absent from the worker, the admin bot and the nightly"
    )


# ── the refusal happens before Postgres is asked ─────────────────────────────


def test_an_undeclared_role_never_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The difference between a guard and a policy.

    The owner's REAL credentials are used here, against the real career_test
    database, from a process pretending to have no escape. Postgres would
    accept them. The connection is refused inside this process instead, so
    what stops the superuser session is not a password, a firewall or a
    convention — it is that the code cannot get that far.
    """
    monkeypatch.setattr(db_session, "_process_escape", lambda: None)
    rogue = create_engine(get_settings().owner_database_url)
    with pytest.raises(ForbiddenDatabaseRole):
        with rogue.connect() as conn:
            conn.execute(text("SELECT current_user"))
    rogue.dispose()

    # …and with the suite's own escape back in place the very same DSN works,
    # which is what makes the line above a check and not an outage.
    monkeypatch.undo()
    ok = create_engine(get_settings().owner_database_url)
    with ok.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar_one() != ""
    ok.dispose()


def test_an_engine_the_factory_approved_keeps_its_approval_at_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two enforcement points must agree, or the sanctioned path is a trap.

    The listener judges an unknown engine by the process that is running, and a
    one-shot invoked in a way nobody listed — `python -c`, a REPL, a new script
    — is not on that list. Without the factory's verdict travelling with the
    engine, calling `engine_for(..., escape=…)` correctly would still be
    refused at the first connection, which teaches people that the documented
    door is the one that does not open.
    """
    monkeypatch.setattr(db_session, "_process_escape", lambda: None)
    approved = engine_for(get_settings().owner_database_url, escape="operator_one_shot")
    with approved.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    approved.dispose()

    # …while the same DSN with no escape named is still refused in that process.
    with pytest.raises(ForbiddenDatabaseRole):
        create_engine(get_settings().owner_database_url).connect()
