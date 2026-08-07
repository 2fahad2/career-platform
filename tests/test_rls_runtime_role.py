"""The guard that did not exist: RLS has to be ON for the running product.

Constant 10 says «RLS مفعّلة وكل استعلام مقيد بالـtenant + اختبارات cross-tenant
هجومية في CI». The schema half was real. The runtime half was not: every
long-running process built ``create_engine(settings.owner_database_url)`` and
``career_owner`` is the bootstrap superuser (``rolsuper=t``, ``rolbypassrls=t``),
so FORCE ROW LEVEL SECURITY was decorative for all three of them and
``tenant_session`` — the helper whose docstring claims to be the §15.10/§15.11
enforcement boundary — had zero production callers. ``test_rls_meta.py`` passed
throughout, because it only ever looked at the catalog.

So this file tests the two things the catalog cannot see:

* **Which role the code actually connects as.** A static scan, because by the
  time a runtime test could notice, the process is already running as a
  superuser. The allow-list below is a ratchet — it may shrink, never grow.
* **What happens when the scope is missing.** Measured before migration 0021: a
  SELECT with no ``app.tenant_id`` returned zero rows *silently*, and UPDATE and
  DELETE touched zero rows *silently*. Only INSERT raised. Silence is the
  dangerous answer — a query that lost its scope reports «nothing found» and the
  caller takes the branch for a customer with no subscription and no pending
  delivery.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

from career.db.session import (
    NO_TENANT_CONTEXT_SQLSTATE,
    app_engine,
    break_glass_session,
    sweep_session,
    tenant_for_phone,
    tenant_for_salla_order,
    tenant_ids_with_status,
    tenant_scope,
    tenant_session,
    tenants_with_pending_work,
)

_REPO = Path(__file__).resolve().parents[1]

#: Modules that still build their engine from the owner (RLS-bypassing) DSN.
#: **This list may shrink and must never grow.** Each entry is one call site of
#: the staged migration in DEVIATIONS D20; adding a new one means a process was
#: written that runs outside RLS, which is the exact regression this file exists
#: to catch. `migrations/env.py` is not listed because Alembic legitimately is
#: the owner — it owns the tables.
_OWNER_ENGINE_ALLOWED: frozenset[str] = frozenset(
    {
        # The three long-running services (D20, stage 2).
        "scripts/run_worker_loop.py",
        "scripts/run_admin_bot.py",
        "src/career/engine/cli.py",
        # The nightly. It contains no DB code at all — six lines, `from
        # career.engine.cli import main`, `sys.exit(main())` — and every name
        # this file scans for is one import away, inside `cli.main`. It was
        # therefore invisible to the first version of this ratchet while being
        # the process that runs the entire nightly as the superuser: the whole
        # of `_owner_reaching_modules` below exists because of this entry.
        # It is not a new grant. It is an old one, finally visible.
        "scripts/run_nightly.py",
        # Operator one-shots: a human runs these by hand, for the whole estate.
        # They are the honest home of owner access and stay there (D20, stage 4).
        "scripts/replay_lost_events.py",
        "scripts/wire_salla_products.py",
        "scripts/demo_engine_tenants.py",
        "scripts/drill_disaster.py",
        "scripts/rehearse_enrichment.py",
        "scripts/ci_create_app_role.py",
    }
)

#: The library. Nothing here has any business knowing the owner DSN exists —
#: `engine/cli.py` is the one exception and it is on the list above.
_LIBRARY_ROOT = "src/career"


def _production_python_files() -> list[Path]:
    """Everything importable that is not a test.

    The root is `src`, not `src/career`: `src/career_core` was outside the
    first version of this scan entirely. It has no database surface today —
    `test_career_core_stays_out_of_the_database` below proves that rather than
    assuming it — but «has none today» is not a property a scan should rely on
    for a package it cannot see at all.
    """
    roots = [_REPO / "src", _REPO / "scripts"]
    files: list[Path] = []
    for root in roots:
        files.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


#: config.py is where the owner DSN is *defined*. Somebody has to hold it —
#: Alembic needs it — and holding it is not the same as dialling it.
_OWNER_DSN_DEFINITION = "src/career/config.py"

#: Attribute names that read the owner's credentials off `Settings`.
#: `db_owner_password` was absent from the first version, and so was `_dsn` —
#: which is the whole of the bypass, because `config.py` exposes the DSN
#: builder itself: `create_engine(settings._dsn("career_owner",
#: settings.db_owner_password))` never touches `owner_database_url` and never
#: touches `db_owner_user`. It read clean.
_OWNER_ATTRIBUTES = frozenset(
    {"owner_database_url", "db_owner_user", "db_owner_password", "_dsn"}
)

#: Strings that name the owner without going through Settings at all —
#: `scripts/ci_create_app_role.py` does exactly this with raw psycopg. The
#: role name itself is here because a hand-assembled DSN spells it out.
_OWNER_LITERALS = frozenset({"DB_OWNER_USER", "DB_OWNER_PASSWORD", "career_owner"})

#: Markers that make a string a CONNECTION STRING rather than a sentence about
#: one. The role name is checked inside these and nowhere else, because the
#: docstring of `career.db.session` names ``career_owner`` in prose on purpose:
#: describing the hole is how the next reader learns why this file exists, and
#: a guard that punishes the explanation gets the explanation deleted.
_DSN_MARKERS = ("://", "dbname=", "user=")

#: The single engine factory (`career.db.session.engine_for`) and the file it
#: lives in. Since 6 أغسطس this test no longer carries a copy of the rule about
#: which DSN is acceptable — the rule is enforced in the source, at engine
#: construction and again at connect, and duplicating it here would mean two
#: rules that drift. What is left for a static scan is the structural half:
#: **`create_engine` is called in exactly one place, and everything else asks
#: that place.** A module that builds its own engine is an offender whatever
#: DSN it passes, because the guard cannot vet an engine it never sees.
_ENGINE_FACTORY_MODULE = "src/career/db/session.py"
_ENGINE_FACTORY_CALLABLE = "engine_for"


def _owner_signals(path: Path) -> set[str]:
    """Why this module is (or is not) an owner-engine call site.

    Prose that discusses the problem — this file, and the docstring of
    `career.db.session` — is not the problem, so attribute access is read from
    the AST rather than grepped. A precise «is this passed to create_engine?»
    check is walked around by one local variable, and a ratchet is only useful
    if it is hard to trip over by accident. So there are two independent
    detectors and either one is enough:

    * the module NAMES the owner (an attribute or a literal above), or
    * it calls `create_engine` at all, anywhere other than the factory.

    The second exists because the first is a list of names and a list of names
    is always one rename behind. It used to make an exception for
    `app_database_url`, which meant this file held a second copy of the
    product's rule about acceptable DSNs; now it holds none. Bypassing the
    factory is the offence, and the reason it is worth calling one is that
    `engine_for` sees the URL a local variable is hiding and this scan never
    will.
    """
    is_factory = path.resolve() == (_REPO / _ENGINE_FACTORY_MODULE).resolve()
    signals: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _OWNER_ATTRIBUTES:
            signals.add(f"names settings.{node.attr}")
        if isinstance(node, ast.Constant) and node.value in _OWNER_LITERALS:
            signals.add(f"names the literal {node.value!r}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "career_owner" in node.value
            and any(marker in node.value for marker in _DSN_MARKERS)
        ):
            signals.add("hand-builds a DSN for the owner role")
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            == "create_engine"
            and not is_factory
        ):
            signals.add(
                "calls create_engine directly instead of "
                f"career.db.session.{_ENGINE_FACTORY_CALLABLE}"
            )
    return signals


def _module_name(path: Path) -> str | None:
    """The dotted name another module would import this file by."""
    try:
        rel = path.relative_to(_REPO / "src")
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path) -> set[str]:
    """Dotted names this file imports, absolute and relative both resolved."""
    out: set[str] = set()
    package = (_module_name(path) or "").rsplit(".", 1)
    base = package[0] if len(package) > 1 else ""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            root = node.module or ""
            if node.level:  # `from . import x` inside the package
                prefix = base.rsplit(".", node.level - 1)[0] if node.level > 1 else base
                root = f"{prefix}.{root}" if root else prefix
            out.add(root)
            out.update(f"{root}.{alias.name}" for alias in node.names)
    return out


def _owner_reaching_modules() -> dict[str, str]:
    """Direct offenders, plus everything that reaches one through an import.

    `scripts/run_nightly.py` is six lines long, contains no database code, and
    runs the entire nightly as the superuser:

        from career.engine.cli import main
        sys.exit(main())

    A scanner that looks for names finds nothing in it — the names are one
    import away. That is not an exotic evasion; it is the ordinary shape of a
    systemd shim, and it means «which files name the owner» was never the
    question. «Which processes end up connected as the owner» is, and an
    import edge is the cheapest honest approximation of it available to a
    static test.
    """
    files = _production_python_files()
    by_module = {m: p for p in files if (m := _module_name(p))}
    reaching = {
        str(p.relative_to(_REPO)): "; ".join(sorted(signals))
        for p in files
        if str(p.relative_to(_REPO)) != _OWNER_DSN_DEFINITION
        and (signals := _owner_signals(p))
    }
    # Transitive closure over intra-repo imports. Small graph, so the naive
    # fixpoint is clearer than anything cleverer.
    changed = True
    while changed:
        changed = False
        for path in files:
            rel = str(path.relative_to(_REPO))
            if rel in reaching or rel == _OWNER_DSN_DEFINITION:
                continue
            for dotted in _imported_modules(path):
                target = by_module.get(dotted)
                if target is None:
                    continue
                hit = str(target.relative_to(_REPO))
                if hit in reaching:
                    reaching[rel] = f"imports {hit}, which reaches the owner"
                    changed = True
                    break
    return reaching


def test_no_new_production_module_builds_an_owner_engine() -> None:
    """The regression guard that did not exist. It is a ratchet, not a snapshot."""
    reaching = _owner_reaching_modules()
    offenders = sorted(reaching)
    unexpected = [p for p in offenders if p not in _OWNER_ENGINE_ALLOWED]
    assert not unexpected, (
        "these modules connect as the RLS-bypassing owner role and are not on "
        f"the D20 migration list: {[(p, reaching[p]) for p in unexpected]}. If this is "
        "deliberate, it needs a DEVIATIONS entry and an explicit addition "
        "here — not a silent one."
    )
    # And the other direction: an entry that no longer offends must be removed,
    # or the list stops meaning anything.
    stale = sorted(_OWNER_ENGINE_ALLOWED - set(offenders))
    assert not stale, (
        f"these are on the owner allow-list but no longer use the owner DSN: "
        f"{stale} — delete them from _OWNER_ENGINE_ALLOWED, the ratchet only "
        "counts if it tightens."
    )


def test_the_session_module_itself_has_no_owner_engine() -> None:
    """`career.db.session` is the destination of D20. It must stay clean.

    Its docstring names the owner DSN — describing the hole is how the next
    reader learns why this file exists — so the check is on what the code does,
    not on what the prose says.
    """
    path = _REPO / _LIBRARY_ROOT / "db" / "session.py"
    assert not _owner_signals(path)
    assert "app_database_url" in path.read_text(encoding="utf-8")


#: Every way found so far of reaching the owner without tripping the FIRST
#: version of this scan. Each one is written the way a real author would write
#: it, and each is asserted to be seen — because a ratchet nobody has tried to
#: walk past is a ratchet nobody has tested.
_OWNER_BYPASS_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "the DSN builder config.py already exposes",
        "from sqlalchemy import create_engine\n"
        "from career.config import get_settings\n"
        "settings = get_settings()\n"
        "engine = create_engine(\n"
        "    settings._dsn('career_owner', settings.db_owner_password))\n",
    ),
    (
        "the password attribute the first scan did not know",
        "from career.config import get_settings\n"
        "dsn = f'postgresql+psycopg://x:{get_settings().db_owner_password}@h/d'\n",
    ),
    (
        "the role name spelled out in a hand-built DSN",
        "PASSWORD = 'unused'\n"
        "DSN = 'postgresql+psycopg://career_owner:%s@127.0.0.1:5432/career'\n",
    ),
    (
        "a DSN laundered through a local variable",
        "from sqlalchemy import create_engine\n"
        "def build(url: str):\n"
        "    return create_engine(url, future=True)\n",
    ),
)


@pytest.mark.parametrize(("label", "source"), _OWNER_BYPASS_SHAPES, ids=lambda v: v[:40])
def test_each_known_owner_bypass_is_seen(
    tmp_path: Path, label: str, source: str
) -> None:
    module = tmp_path / "new_service.py"
    module.write_text(source, encoding="utf-8")
    assert _owner_signals(module), f"the owner ratchet does not see: {label}"


def test_the_ratchet_leaves_an_ordinary_app_module_alone() -> None:
    """`career.db.session` is the shape every migrated call site should have,
    and it must read clean or the ratchet is noise people learn to ignore."""
    assert not _owner_signals(_REPO / "src" / "career" / "db" / "session.py")


def test_an_import_shim_cannot_hide_the_owner_behind_one_line(tmp_path: Path) -> None:
    """The `run_nightly.py` shape, proven rather than asserted in prose.

    Written against a throwaway pair of files: the module that dials the owner
    and the six-line shim that runs it. The shim names nothing this scan looks
    for; the import edge is the only thing that connects it to a superuser
    connection, and it is what the closure follows.
    """
    inner = tmp_path / "src" / "career" / "engine"
    inner.mkdir(parents=True)
    (inner / "cli.py").write_text(
        "from sqlalchemy import create_engine\n"
        "from career.config import get_settings\n"
        "def main():\n"
        "    return create_engine(get_settings().owner_database_url)\n",
        encoding="utf-8",
    )
    shim = tmp_path / "scripts"
    shim.mkdir()
    (shim / "run_nightly.py").write_text(
        "import sys\nfrom career.engine.cli import main\nsys.exit(main())\n",
        encoding="utf-8",
    )
    assert not _owner_signals(shim / "run_nightly.py"), (
        "the shim must be invisible to the NAME scan — that is the premise"
    )
    assert "career.engine.cli" in _imported_modules(shim / "run_nightly.py")


def test_career_core_stays_out_of_the_database() -> None:
    """The directory the first scan could not see, checked rather than assumed.

    `src/career_core` is pure by charter — «no network, no LLM, no database» —
    and is now inside the scan roots. This says the charter is still true, so
    the day somebody gives it a session the scan above is already watching and
    this line says why that is a decision, not a detail.
    """
    root = _REPO / "src" / "career_core"
    assert root.is_dir(), "career_core moved — update the scan roots too"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for dotted in _imported_modules(path):
            if dotted.split(".")[0] in {"sqlalchemy", "psycopg", "psycopg2"} or (
                dotted.startswith("career.db")
            ):
                offenders.append(f"{path.relative_to(_REPO)} imports {dotted}")
    assert not offenders, (
        f"career_core grew a database surface: {offenders} — it is imported by "
        "the gate and by pure-function tests precisely because it has none"
    )


def test_what_this_file_cannot_prove_is_proven_at_runtime_instead() -> None:
    """The honest limit of a name scan, and the thing that now closes it.

    A name-based AST scan can only see the names in front of it. It cannot
    resolve `getattr(settings, "owner_" + "database_url")`, a DSN read out of a
    file or a vault at runtime, a psycopg connection built from four separate
    `os.environ` reads with no recognisable literal, or a library the app
    imports that dials the database itself. This test used to say so and stop,
    because the pass that wrote it owned only test files.

    It is closed now, in the source where it belongs: `career.db.session`
    checks the role at engine construction and again on every DBAPI connection
    in the process, so the four shapes above fail at the point where they are
    no longer disguised — the username on its way to libpq. What is asserted
    here is only that the seam still exists and still refuses; how it behaves
    is `tests/test_db_engine_guard.py`.
    """
    from career.db.session import ForbiddenDatabaseRole, engine_for

    with pytest.raises(ForbiddenDatabaseRole):
        engine_for("postgresql+psycopg://a_role_nobody_declared:x@127.0.0.1/career")
    assert _OWNER_ENGINE_ALLOWED, "the ratchet is empty — read the docstring"


def test_the_two_allow_lists_cannot_drift_apart() -> None:
    """This file's list of scripts and the guard's list of processes are one list.

    They are written in two places because they are enforced in two ways — a
    static scan of source and a runtime check of a username — and two lists of
    the same thing is exactly how a script gets removed from one and quietly
    keeps its owner rights through the other. So they are compared instead of
    trusted.
    """
    from career.db.session import (
        _D20_LEGACY_ENTRYPOINTS,
        _OPERATOR_ONE_SHOT_ENTRYPOINTS,
    )

    guard_names = set(_D20_LEGACY_ENTRYPOINTS) | set(_OPERATOR_ONE_SHOT_ENTRYPOINTS)
    scanned_scripts = {
        Path(p).name for p in _OWNER_ENGINE_ALLOWED if p.startswith("scripts/")
    }
    assert scanned_scripts == guard_names, (
        "the AST allow-list and the runtime escape lists disagree: only in the "
        f"scan {sorted(scanned_scripts - guard_names)}, only in the guard "
        f"{sorted(guard_names - scanned_scripts)}"
    )


def test_the_factory_is_the_only_place_that_builds_an_engine() -> None:
    """The structural half of the guard, which the runtime half cannot check.

    `engine_for` can only vet an engine it is asked to build. A module that
    calls `create_engine` itself is still caught — by the connect listener, at
    its first connection — but that is a failure at 04:30 rather than at
    review, so the scan keeps saying it earlier and louder.
    """
    builders = sorted(
        rel
        for path in _production_python_files()
        if (rel := str(path.relative_to(_REPO))) != _ENGINE_FACTORY_MODULE
        and any(
            "create_engine" in signal for signal in _owner_signals(path)
        )
    )
    unexpected = [b for b in builders if b not in _OWNER_ENGINE_ALLOWED]
    assert not unexpected, (
        f"these build their own engine instead of calling {_ENGINE_FACTORY_CALLABLE}: "
        f"{unexpected}"
    )


def test_runtime_role_is_not_superuser_and_cannot_bypass_rls(
    owner_engine: Engine,
) -> None:
    """The role the application connects as, asked of the connection itself.

    Not hard-coded to 'career_app': the point is to check whatever DB_USER the
    running configuration actually dials, so pointing the app at the owner would
    fail here instead of passing quietly.
    """
    with app_engine.connect() as conn:
        me = conn.execute(text("SELECT current_user")).scalar_one()
    with Session(owner_engine) as s:
        row = s.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"),
            {"r": me},
        ).one()
    assert row.rolsuper is False, f"{me} is a superuser — RLS would be bypassed"
    assert row.rolbypassrls is False, f"{me} has BYPASSRLS — RLS would be bypassed"


def test_owner_role_is_the_one_that_bypasses_and_it_is_not_the_runtime_role(
    owner_engine: Engine,
) -> None:
    """The split exists in the catalog; D20 is about which one the code picks.

    Recorded here so a future reader does not have to rediscover that the
    migration/admin role and the runtime role were already two different roles —
    the gap was never the schema, it was that production chose the wrong one.
    """
    with Session(owner_engine) as s:
        owner = s.execute(text("SELECT current_user")).scalar_one()
    with app_engine.connect() as conn:
        runtime = conn.execute(text("SELECT current_user")).scalar_one()
    assert owner != runtime


def test_every_tenant_policy_goes_through_the_fail_closed_function(
    owner_engine: Engine,
) -> None:
    """Catalog-wide, so it covers all 30 tables, not the handful seeded below."""
    with Session(owner_engine) as s:
        rows = s.execute(
            text(
                "SELECT tablename, policyname, qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public' AND policyname LIKE '%_isolation'"
            )
        ).all()
    assert rows, "no isolation policies found — wrong database?"
    stale = [
        f"{r.tablename}.{r.policyname}"
        for r in rows
        if "current_tenant" not in (r.qual or "")
        or "current_tenant" not in (r.with_check or "")
    ]
    assert not stale, (
        f"policies still using the silent expression: {stale} — a policy that "
        "compares against NULL returns the empty set instead of refusing."
    )


def test_the_runtime_role_cannot_become_the_sweep_or_the_owner() -> None:
    """SET ROLE is checked against session_user, so this must be a real login."""
    for target in ("career_sweep", "career_owner"):
        with pytest.raises(DBAPIError) as err:
            with app_engine.connect() as conn:
                conn.execute(text(f"SET ROLE {target}"))
        assert "permission denied" in str(err.value).lower()


# ---------------------------------------------------------------- fail closed --


def _seed_channel(session: Session, tenant_id: str, phone: str) -> str:
    channel_id = str(uuid.uuid4())
    session.execute(
        text(
            "INSERT INTO customer_channels "
            "(id, tenant_id, provider, phone_e164, created_at) "
            "VALUES (:id, :tid, 'whatsapp', :phone, now())"
        ),
        {"id": channel_id, "tid": tenant_id, "phone": phone},
    )
    return channel_id


def _seed_document(session: Session, tenant_id: str, key: str) -> None:
    session.execute(
        text(
            "INSERT INTO documents "
            "(id, tenant_id, storage_key, content_sha256, content_type, "
            " size_bytes, status) "
            "VALUES (gen_random_uuid(), :tid, :key, :sha, 'application/pdf', "
            "        1024, 'active')"
        ),
        {"tid": tenant_id, "key": key, "sha": "0" * 64},
    )


#: (label, statement) — one per shape of query the live entry points run.
#: The labels name the process that runs that shape today on the owner engine:
#: the worker loop reads channels to route, privacy reads documents to export,
#: the nightly writes decisions, the console updates state.
_UNSCOPED_STATEMENTS = (
    ("read (worker routing / privacy export)", "SELECT count(*) FROM customer_channels"),
    ("read (privacy export)", "SELECT count(*) FROM documents"),
    ("write (privacy erasure)", "DELETE FROM documents"),
    ("update (console / lifecycle)", "UPDATE customer_channels SET display_name = 'x'"),
    (
        "insert (worker / nightly)",
        "INSERT INTO documents (id, tenant_id, storage_key, content_sha256, "
        "content_type, size_bytes, status) VALUES (gen_random_uuid(), "
        "'00000000-0000-0000-0000-000000000001', 'x', repeat('0',64), "
        "'application/pdf', 1, 'active')",
    ),
)


@pytest.mark.parametrize(("label", "statement"), _UNSCOPED_STATEMENTS)
def test_missing_tenant_context_refuses_instead_of_returning_nothing(
    two_tenants: tuple[str, str], label: str, statement: str
) -> None:
    """Before 0021 the first four of these returned 0 rows and no error."""
    a, _ = two_tenants
    with tenant_session(a) as s:
        _seed_document(s, a, f"tenants/a/{uuid.uuid4()}.pdf")
        _seed_channel(s, a, f"+96650{uuid.uuid4().int % 10_000_000:07d}")

    with pytest.raises(DBAPIError) as err:
        with app_engine.begin() as conn:
            conn.execute(text(statement))
    assert err.value.orig.sqlstate == NO_TENANT_CONTEXT_SQLSTATE, label
    assert "no app.tenant_id bound" in str(err.value)


def test_the_refusal_names_the_way_out() -> None:
    """An error that does not say what to do instead gets worked around."""
    with pytest.raises(DBAPIError) as err:
        with app_engine.begin() as conn:
            conn.execute(text("SELECT count(*) FROM documents"))
    assert "tenant_session" in str(err.value)


# ------------------------------------------------------------ sweep capability --


def test_sweeps_route_across_tenants_without_seeing_a_single_row(
    two_tenants: tuple[str, str],
) -> None:
    """The whole point: answer «which tenant» without opening anyone's data."""
    a, b = two_tenants
    phone_b = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    with tenant_session(b) as s:
        _seed_channel(s, b, phone_b)

    with sweep_session() as s:
        assert tenant_for_phone(s, phone_b) == b
        assert tenant_for_phone(s, "+966500000000") is None
        assert tenant_for_salla_order(s, "no-such-order") is None
        # and the same session may not touch a tenant table
        with pytest.raises(DBAPIError) as err:
            s.execute(text("SELECT count(*) FROM customer_channels"))
        assert err.value.orig.sqlstate == NO_TENANT_CONTEXT_SQLSTATE
    assert a  # the other tenant was never named in any of the above


def test_sweep_list_functions_work_unscoped(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with sweep_session() as s:
        assert isinstance(tenant_ids_with_status(s, ["ACTIVE"]), list)
        assert isinstance(tenants_with_pending_work(s), list)
    assert a


def test_the_sweep_role_can_never_write_anything(owner_engine: Engine) -> None:
    """SELECT-only, asserted from the catalog rather than from the function bodies."""
    with Session(owner_engine) as s:
        rows = s.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'career_sweep'"
            )
        ).scalars().all()
        colrows = s.execute(
            text(
                "SELECT privilege_type FROM "
                "information_schema.column_privileges "
                "WHERE grantee = 'career_sweep'"
            )
        ).scalars().all()
    assert set(rows) <= {"SELECT"}, rows
    assert set(colrows) <= {"SELECT"}, colrows
    assert colrows, "career_sweep has no column grants — the capability is missing"


#: Columns the sweep role must NOT be able to read. Every one of them is the
#: customer's own words or their CV; a routing question never needs any of it.
_FORBIDDEN_TO_SWEEP = (
    ("customer_channels", "display_name"),
    ("onboarding_sessions", "context"),
    # ("outbox_events", "payload") was here until 0029 dropped the table with
    # the rest of the deleted queue subsystem. The row is gone rather than
    # relaxed: this test asks the catalog whether a grant EXISTS, and against a
    # relation that no longer exists the question has no answer to give — it
    # would pass for the wrong reason, which is the shape of a security test
    # that has quietly stopped testing anything.
    ("deliveries", "bundle"),
    ("subscriptions", "amount_sar"),
    ("tenants", "code"),
)


@pytest.mark.parametrize(("table", "column"), _FORBIDDEN_TO_SWEEP)
def test_the_sweep_ceiling_is_in_the_catalog_not_in_the_code(
    owner_engine: Engine, table: str, column: str
) -> None:
    """Column grants are what stop this from being a second blanket role.

    A future sweep function could be written badly; it still cannot select a
    column its owner was never granted. That is the difference between a
    narrow capability and a renamed problem.
    """
    with Session(owner_engine) as s:
        granted = s.execute(
            text(
                "SELECT count(*) FROM information_schema.column_privileges "
                "WHERE grantee = 'career_sweep' AND table_name = :t "
                "AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar_one()
    assert granted == 0, f"career_sweep can read {table}.{column} — widen with care"


@pytest.mark.parametrize(("table", "column"), _FORBIDDEN_TO_SWEEP)
def test_the_sweep_ceiling_actually_refuses(
    owner_engine: Engine, table: str, column: str
) -> None:
    """The grant check above, exercised — a catalog row nobody enforces is a lie.

    career_sweep is NOLOGIN, so this borrows the owner's session to SET ROLE;
    that is exactly the privilege context a SECURITY DEFINER sweep function
    body runs in.
    """
    with Session(owner_engine) as s:
        s.execute(text("SET ROLE career_sweep"))
        with pytest.raises(ProgrammingError) as err:
            s.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
        assert "permission denied" in str(err.value).lower()
        s.rollback()


# ----------------------------------------------------------------- break glass --


def test_break_glass_demands_a_reason(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with pytest.raises(DBAPIError) as err:
        with break_glass_session(a, "oops"):
            pass
    assert "at least 10 characters" in str(err.value)


def test_break_glass_opens_the_tenant_and_leaves_a_trail(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, b = two_tenants
    with tenant_session(b) as s:
        _seed_document(s, b, "tenants/b/only.pdf")

    reason = f"operator investigating delivery {uuid.uuid4()}"
    with break_glass_session(b, reason) as s:
        keys = s.execute(text("SELECT storage_key FROM documents")).scalars().all()
    assert keys == ["tenants/b/only.pdf"]

    with Session(owner_engine) as s:
        row = s.execute(
            text(
                "SELECT db_user, target_tenant_id FROM break_glass_log "
                "WHERE reason = :r"
            ),
            {"r": reason},
        ).one()
        s.execute(text("DELETE FROM break_glass_log WHERE reason = :r"), {"r": reason})
        s.commit()
    assert str(row.target_tenant_id) == b
    assert row.db_user != "career_owner"
    assert a


def test_the_process_that_breaks_the_glass_cannot_touch_the_record() -> None:
    """An audit trail the audited party can edit is decoration."""
    for statement in (
        "SELECT count(*) FROM break_glass_log",
        "DELETE FROM break_glass_log",
        "UPDATE break_glass_log SET reason = 'nothing happened'",
    ):
        with pytest.raises(DBAPIError) as err:
            with app_engine.begin() as conn:
                conn.execute(text(statement))
        assert "permission denied" in str(err.value).lower()


def test_every_security_definer_function_pins_its_search_path(
    owner_engine: Engine,
) -> None:
    """Without it, whoever can create a table earlier on the path picks the data.

    Every function in `app` runs as a role the caller cannot become; an
    unpinned search_path would hand that privilege to anything that can put a
    `customer_channels` in front of `public`.
    """
    with Session(owner_engine) as s:
        rows = s.execute(
            text(
                "SELECT p.proname, p.proconfig FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'app' AND p.prosecdef"
            )
        ).all()
    assert rows, "no SECURITY DEFINER functions in schema app — 0021 not applied?"
    unpinned = [
        r.proname
        for r in rows
        if "search_path=pg_catalog, public" not in (r.proconfig or [])
    ]
    assert not unpinned, f"SECURITY DEFINER without a pinned search_path: {unpinned}"


def test_break_glass_is_not_reachable_by_accident(owner_engine: Engine) -> None:
    """It is a named door. PUBLIC must not hold the key."""
    with Session(owner_engine) as s:
        public_has = s.execute(
            text(
                "SELECT has_function_privilege('public', "
                "'app.break_glass(uuid, text)', 'EXECUTE')"
            )
        ).scalar_one()
    assert public_has is False


# ------------------------------------------------------------------ ergonomics --


def test_tenant_scope_restores_the_previous_binding(
    two_tenants: tuple[str, str],
) -> None:
    """The seam D20 migrates call sites through — it must not leak a scope."""
    a, b = two_tenants
    with tenant_session(a) as s:
        assert s.execute(
            text("SELECT current_setting('app.tenant_id', true)")
        ).scalar_one() == a
        with tenant_scope(s, b):
            assert s.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            ).scalar_one() == b
        assert s.execute(
            text("SELECT current_setting('app.tenant_id', true)")
        ).scalar_one() == a


def test_a_non_uuid_tenant_fails_at_the_call_not_at_the_query() -> None:
    """Empty string used to mean «no rows, quietly». Now it means «error later»."""
    for bad in ("", "not-a-uuid", None):
        with pytest.raises(ValueError, match="not a UUID"):
            with tenant_session(bad):  # type: ignore[arg-type]
                pass
