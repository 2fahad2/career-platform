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
    roots = [_REPO / "src" / "career", _REPO / "scripts"]
    files: list[Path] = []
    for root in roots:
        files.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


#: config.py is where the owner DSN is *defined*. Somebody has to hold it —
#: Alembic needs it — and holding it is not the same as dialling it.
_OWNER_DSN_DEFINITION = "src/career/config.py"


def _reaches_for_owner_dsn(path: Path) -> bool:
    """True if the module READS the owner credentials off Settings.

    Three shapes, because the owner can be reached three ways and a scan that
    knows only the first calls the other two clean:
    ``settings.owner_database_url``, ``settings.db_owner_user`` (raw psycopg),
    and ``os.environ["DB_OWNER_USER"]`` — which is how
    `scripts/ci_create_app_role.py` does it, bypassing Settings entirely.

    Prose that discusses the problem — this file, and the docstring of
    `career.db.session` — is not the problem, so attribute access is read from
    the AST rather than grepped. A precise «is this passed to create_engine?»
    check is walked around by one local variable, and a ratchet is only useful
    if it is hard to trip over by accident.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {
            "owner_database_url",
            "db_owner_user",
        }:
            return True
        if isinstance(node, ast.Constant) and node.value in {
            "DB_OWNER_USER",
            "DB_OWNER_PASSWORD",
        }:
            return True
    return False


def test_no_new_production_module_builds_an_owner_engine() -> None:
    """The regression guard that did not exist. It is a ratchet, not a snapshot."""
    offenders = sorted(
        str(p.relative_to(_REPO))
        for p in _production_python_files()
        if str(p.relative_to(_REPO)) != _OWNER_DSN_DEFINITION
        and _reaches_for_owner_dsn(p)
    )
    unexpected = [p for p in offenders if p not in _OWNER_ENGINE_ALLOWED]
    assert not unexpected, (
        "these modules connect as the RLS-bypassing owner role and are not on "
        f"the D20 migration list: {unexpected}. If this is deliberate, it needs "
        "a DEVIATIONS entry and an explicit addition here — not a silent one."
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
    assert not _reaches_for_owner_dsn(path)
    assert "app_database_url" in path.read_text(encoding="utf-8")


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
    ("outbox_events", "payload"),
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
