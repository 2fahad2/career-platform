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
import re
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

# The import walker and the module index belong to the owner-engine ratchet and
# are borrowed rather than copied. Two AST scans that answer «what does this
# file import» is two scans that drift, and the last section of this file is
# the reason that matters: the question it asks is the same one
# `_owner_reaching_modules` asks, narrowed from «the owner» to «any engine».
from tests.test_rls_runtime_role import (  # noqa: E402
    _imported_modules,
    _module_name,
    _production_python_files,
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


# ── which processes must declare a role ──────────────────────────────────────
#
# THE QUESTION THIS ASKS, AND THE ONE IT USED TO ASK.
#
# Until 7 أغسطس this section asserted «every entrypoint in `ops/systemd/` is
# named in one of the two escape lists». That question has a wrong answer, and
# `scripts/refresh_salla_token.py` is the one that found it: a daily oneshot
# that reads the credential file, posts one OAuth exchange to Salla and writes
# the credential file back. It opens no database connection at all — its entire
# in-repo import closure is `career.logging_filters`, `career.config`,
# `career.salla.tokens` and `career.telegram.admin`, and not one of them
# reaches `career.db`. It goes out of its way to keep it that way: `_admin_for`
# picks the operator's channel by hand rather than calling
# `career.engine.cli.admin_client_for`, and says why in its docstring —
# importing the nightly CLI would drag an owner engine into a process that has
# no use for one.
#
# Under the old question that script failed, and the only way to make it pass
# was to hand it an owner escape it does not use and cannot need. An allow-list
# that grows because a test asked for it is the shape of guard this repository
# has spent the week dismantling, so the question is now the one that was
# always meant:
#
#     does every process that ends up with a DATABASE ENGINE have a
#     declared role?
#
# A process that never builds one has no role to declare, and saying so costs
# nothing. A process that does build one and is named nowhere is the D20
# regression the old test existed for, and it still fails here — it just fails
# for the reason that is true.
#
# The answer is read out of the source rather than out of a list somebody has
# to remember to update, because a list somebody has to remember to update has
# been the hole three times this week. The technique is the sibling ratchets':
# `test_no_new_production_module_builds_an_owner_engine` follows import edges
# because `scripts/run_nightly.py` is six lines that run the whole nightly as
# the superuser, and the `tenant_day_states` single-writer guard scans the
# whole tree because the invariant was never about the one file the authority
# happens to live in. Its three helpers are IMPORTED below rather than copied:
# an import walker in two files is an import walker that drifts.
#
# WHAT IS NOT DECIDABLE FAILS CLOSED. An entrypoint whose file is missing, does
# not parse, or reaches a module that imports by a name computed at runtime is
# treated as needing a declaration, because the honest answer there is «this
# scan cannot tell» and the safe reading of that is «assume it connects».


def _module_index() -> dict[str, Path]:
    """Dotted module name → file, for every production module in the tree."""
    return {m: p for p in _production_python_files() if (m := _module_name(p))}


#: The two calls that produce an Engine. `engine_for` is the sanctioned door
#: and `create_engine` is every other one; a module that reaches either has an
#: engine in its process, whichever DSN it ends up passing. Importing
#: `career.db.session` counts by construction rather than by special case —
#: that module calls both at import time to build `app_engine`, so the closure
#: below finds it without this file holding a second opinion about it.
_ENGINE_BUILDERS = frozenset({"create_engine", "engine_for"})

#: Calls that make the import graph unreadable from here. Past one of these a
#: static scan is guessing, and the safe guess is «it connects».
_OPAQUE_CALLS = frozenset(
    {"import_module", "__import__", "exec", "eval", "run_path", "run_module",
     "exec_module", "load_source"}
)

#: systemd's Exec* prefixes: `-` ignore failure, `@` override argv[0], `+`/`!`
#: privilege modifiers, `:` no variable expansion. They ride on the front of
#: the executable and would otherwise make the path unresolvable.
_EXEC_PREFIXES = "-@+!:"

#: A path ending in `.py`, as it appears in a unit file or a shell wrapper.
_PY_TOKEN = re.compile(r"[\w./@%-]*\.py\b")

#: Where a bare basename is looked for when a unit names one. `tests/` is
#: deliberately absent — systemd does not start the suite, and a shell comment
#: that mentions a test file must not read as an entrypoint.
_ENTRYPOINT_ROOTS = ("scripts", "src", "ops")


def _resolve_entrypoint(token: str) -> Path | None:
    """The file a unit's Exec* token names, or None if it cannot be found."""
    candidate = Path(token)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    for root in _ENTRYPOINT_ROOTS:
        for found in (_REPO / root).rglob(candidate.name):
            if "__pycache__" not in found.parts:
                return found
    return None


def _systemd_entrypoints(units_dir: Path) -> dict[str, Path | None]:
    """Every Python entrypoint systemd starts, by basename → file on disk.

    Two things it reads that the old version did not.

    ALL the `Exec*` directives, not only `ExecStart=`. A connection opened from
    an `ExecStartPre=` is as real as one opened from the main process.

    ONE LEVEL THROUGH A SHELL WRAPPER. Four units run a `.sh` today and none of
    them invokes Python — but `career-verify-restore` is one `.venv/bin/python
    scripts/something.py` away from being the `run_nightly.py` shim hole in a
    different language, and that hole is the entire reason
    `_owner_reaching_modules` follows edges at all. Only lines that actually
    invoke an interpreter are read, so `for f in *.py` in a restore loop and a
    comment naming a test file stay what they are.
    """
    entrypoints: dict[str, Path | None] = {}
    units = sorted(units_dir.glob("*.service"))
    assert units, f"no unit files found in {units_dir} — has ops/systemd moved?"
    for unit in units:
        for line in unit.read_text(encoding="utf-8").splitlines():
            if not line.startswith("Exec"):
                continue
            tokens = [t.lstrip(_EXEC_PREFIXES) for t in line.partition("=")[2].split()]
            found = [t for t in tokens if t.endswith(".py")]
            for token in tokens:
                wrapper = _resolve_entrypoint(token) if token.endswith(".sh") else None
                if wrapper is None:
                    continue
                found.extend(
                    match.group(0)
                    for text_line in wrapper.read_text(encoding="utf-8").splitlines()
                    if "python" in text_line
                    for match in _PY_TOKEN.finditer(text_line)
                )
            for token in found:
                entrypoints[Path(token).name] = _resolve_entrypoint(token)
    return entrypoints


def _engine_reach(entry: Path | None, name: str) -> str:
    """Why ``entry`` ends up with a database engine — empty string if it never does.

    A fixpoint over intra-repo import edges, exactly as `_owner_reaching_modules`
    does it, and for the same reason: the names are one import away in every
    systemd shim ever written. Lazy imports inside a function count, because
    `ast.walk` does not care where the statement sits and a connection opened on
    the third branch of a Tuesday is still a connection.

    A definite finding outranks a fail-closed one. `career.engine.cli` both
    calls `create_engine` and loads the sale-plan module by path, so whichever
    the walk happened to meet first would otherwise decide the sentence the
    next reader gets. «It builds an engine at this line» is something they can
    act on; «the graph cannot be read» is what is said only when there is no
    such line anywhere in reach.
    """
    if entry is None:
        return (
            f"{name} is started by systemd and no file by that name exists under "
            f"{list(_ENTRYPOINT_ROOTS)} — what it does cannot be read"
        )
    index = _module_index()
    seen: set[Path] = set()
    unreadable = ""
    queue = [entry]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        rel = current.name if not current.is_relative_to(_REPO) else str(
            current.relative_to(_REPO)
        )
        try:
            tree = ast.parse(current.read_text(encoding="utf-8"), filename=str(current))
        except (OSError, SyntaxError, ValueError) as exc:
            return f"{rel} could not be read ({type(exc).__name__})"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if called in _ENGINE_BUILDERS:
                return f"{rel} calls {called}()"
            if called in _OPAQUE_CALLS and not unreadable:
                unreadable = (
                    f"{rel} resolves an import at runtime via {called}() — the "
                    "graph past that point cannot be read statically, so this "
                    "is treated as reaching a database"
                )
        queue.extend(
            target
            for dotted in _imported_modules(current)
            if (target := index.get(dotted)) is not None
        )
    return unreadable


def _entrypoints_needing_a_role(units_dir: Path) -> dict[str, str]:
    """systemd entrypoint → why it must declare a role. Absent means it need not."""
    return {
        name: why
        for name, path in _systemd_entrypoints(units_dir).items()
        if (why := _engine_reach(path, name))
    }


_DECLARE = (
    "Add each to career.db.session._D20_LEGACY_ENTRYPOINTS if it is a "
    "long-running service (the staged D20 escape), or to "
    "_OPERATOR_ONE_SHOT_ENTRYPOINTS if a human runs it by hand — the operator "
    "half must also be added to _OWNER_ENGINE_ALLOWED in "
    "tests/test_rls_runtime_role.py, which is checked against it. If instead "
    "the process never opens a database connection, change nothing here and "
    "keep it that way: this guard only asks the ones that do."
)


def test_every_systemd_process_that_reaches_a_database_declares_its_role() -> None:
    """The regression the old question was really about, asked correctly.

    A service written tomorrow that connects, dropped into `ops/systemd/` with
    no escape naming it, fails at its first connection — at 04:30, in the
    middle of the nightly. It fails here instead, at review, with the two lists
    it could belong to named in the message.
    """
    declared = set(_D20_LEGACY_ENTRYPOINTS) | set(_OPERATOR_ONE_SHOT_ENTRYPOINTS)
    needed = _entrypoints_needing_a_role(_REPO / "ops" / "systemd")
    undeclared = {name: why for name, why in needed.items() if name not in declared}
    assert not undeclared, (
        f"systemd starts these, they end up with a database engine, and no "
        f"escape names them: {undeclared}. {_DECLARE}"
    )


def test_the_processes_the_staged_escape_names_are_still_the_ones_systemd_runs() -> None:
    """Both directions, so the list can only tighten.

    `_D20_LEGACY_ENTRYPOINTS` is a claim about processes systemd starts and
    which of them still reach a database. Retiring an entry is how D20 ends, so
    an entry systemd no longer starts has to be deleted rather than left to
    read as permission — and the three that are left must still be seen as
    reaching, or this guard is passing on air.
    """
    needed = _entrypoints_needing_a_role(_REPO / "ops" / "systemd")
    missing = sorted(set(_D20_LEGACY_ENTRYPOINTS) - set(needed))
    assert not missing, (
        f"these hold the staged D20 escape but systemd no longer starts them "
        f"with a database in reach: {missing} — delete them from "
        "_D20_LEGACY_ENTRYPOINTS, the escape only counts if it shrinks"
    )


def test_a_process_that_never_opens_a_database_is_not_asked_for_a_role() -> None:
    """The case that changed the question, written down so it cannot drift back.

    `scripts/refresh_salla_token.py` rotates the Salla OAuth credential from a
    daily timer. It reads a file, calls Salla, writes the file. If it ever
    grows a database call this fails — and the right fix then is to declare its
    role, not to relax this.
    """
    started = _systemd_entrypoints(_REPO / "ops" / "systemd")
    assert "refresh_salla_token.py" in started, (
        "the Salla token refresh is no longer started by systemd — if the unit "
        "was renamed, rename it here; if it was deleted, delete this test"
    )
    why = _engine_reach(started["refresh_salla_token.py"], "refresh_salla_token.py")
    assert not why, (
        f"the Salla token refresh now reaches a database engine ({why}). It was "
        "exempt because it opens no connection at all; now that it does, it "
        f"needs a declared role. {_DECLARE}"
    )


#: One synthetic service per way a connecting process could arrive in
#: `ops/systemd/` without saying so, written the way a real author would write
#: it. The first is the shape the old test caught. The rest are the shapes it
#: caught only by accident of the name being listed, and that a scan of the
#: entrypoint file alone would miss.
_UNDECLARED_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "a new daemon that builds its own engine",
        "from sqlalchemy import create_engine\n"
        "from career.config import get_settings\n"
        "def main():\n"
        "    engine = create_engine(get_settings().owner_database_url)\n"
        "    return engine.connect()\n",
    ),
    (
        "a new daemon that uses the sanctioned factory",
        "from career.db.session import engine_for\n"
        "def main():\n"
        "    return engine_for('postgresql+psycopg://career_app:x@h/d')\n",
    ),
    (
        "a six-line shim, the run_nightly.py shape",
        "import sys\n"
        "from career.engine.cli import main\n"
        "sys.exit(main())\n",
    ),
    (
        "a daemon that only ever imports a helper that connects",
        "from career.cv.close import format_admin_summary\n"
        "def main():\n"
        "    return format_admin_summary\n",
    ),
    (
        "a lazy import buried three branches deep",
        "def main(flag: bool):\n"
        "    if flag:\n"
        "        from career.db.session import tenant_session\n"
        "        return tenant_session\n"
        "    return None\n",
    ),
    (
        "a module resolved by a name computed at runtime",
        "import importlib\n"
        "def main(which: str):\n"
        "    return importlib.import_module('career.' + which)\n",
    ),
)


@pytest.mark.parametrize(("label", "source"), _UNDECLARED_SHAPES, ids=lambda v: v[:40])
def test_a_new_service_that_connects_cannot_arrive_undeclared(
    tmp_path: Path, label: str, source: str
) -> None:
    """The unit and the script are written, the guard is asked, both are removed.

    tmp_path rather than the real tree on purpose — a guard's guard that
    littered `ops/systemd/` when it failed would be deleted by the next person
    it inconvenienced. The unit names the script by ABSOLUTE path, which is how
    every unit in this repository names its entrypoint, so nothing about the
    resolution is special-cased for the test.
    """
    script = tmp_path / "run_new_service.py"
    script.write_text(source, encoding="utf-8")
    units = tmp_path / "systemd"
    units.mkdir()
    (units / "career-new-service.service").write_text(
        "[Unit]\nDescription=something new\n\n"
        f"[Service]\nExecStart=/usr/bin/env /venv/bin/python {script}\n",
        encoding="utf-8",
    )

    needed = _entrypoints_needing_a_role(units)
    declared = set(_D20_LEGACY_ENTRYPOINTS) | set(_OPERATOR_ONE_SHOT_ENTRYPOINTS)
    assert "run_new_service.py" in needed, (
        f"a new service arrives undeclared and the guard does not see it: {label}"
    )
    assert "run_new_service.py" not in declared, (
        "the synthetic name is on a real escape list — rename it, this test "
        "proves nothing while it is"
    )


def test_a_service_that_only_talks_to_the_network_is_left_alone(
    tmp_path: Path,
) -> None:
    """The other half of «does it work».

    A guard that demands a role from every oneshot is a guard whose allow-list
    grows for reasons unrelated to the database, which is the failure this
    replaced. The shape below is the token refresh's: read a file, call an API,
    write a file, tell the operator.
    """
    script = tmp_path / "run_rotate_thing.py"
    script.write_text(
        "import httpx\n"
        "from career.logging_filters import register_secret\n"
        "from career.salla.tokens import current, store_credentials\n"
        "def main():\n"
        "    token = current().refresh_token\n"
        "    fresh = httpx.post('https://example.invalid/token', data={'t': token})\n"
        "    register_secret(fresh.text)\n"
        "    return store_credentials(access_token=fresh.text)\n",
        encoding="utf-8",
    )
    units = tmp_path / "systemd"
    units.mkdir()
    (units / "career-rotate.service").write_text(
        f"[Service]\nExecStart=/venv/bin/python {script}\n", encoding="utf-8"
    )
    assert _entrypoints_needing_a_role(units) == {}


def test_a_shell_wrapper_does_not_hide_a_connecting_process(tmp_path: Path) -> None:
    """`ExecStart=…/something.sh` is one line away from being a shim.

    Four units already run a shell script. None of them invokes Python today,
    and «today» is not a property a scan should rest on for a file it can read.
    """
    script = tmp_path / "run_wrapped.py"
    script.write_text(
        "from career.db.session import sweep_session\n"
        "def main():\n"
        "    return sweep_session\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "wrap.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# housekeeping, see tests/test_ops_watchdogs.py\n"
        "for f in *.py; do echo \"$f\"; done\n"
        f"exec /venv/bin/python {script} \"$@\"\n",
        encoding="utf-8",
    )
    units = tmp_path / "systemd"
    units.mkdir()
    (units / "career-wrapped.service").write_text(
        f"[Service]\nExecStart=-{wrapper} %i\n", encoding="utf-8"
    )

    needed = _entrypoints_needing_a_role(units)
    assert "run_wrapped.py" in needed, "a wrapped entrypoint is invisible again"
    assert "test_ops_watchdogs.py" not in needed, (
        "a comment naming a test file is being read as an entrypoint — that is "
        "how a guard earns the reputation that gets it deleted"
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
