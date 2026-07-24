"""AUDIT ح-5: the RLS meta-test — constitution §15.10 enforced structurally.

Every table carrying a tenant_id column MUST have row security ENABLEd,
FORCEd, at least one tenant-isolation policy, and career_app access limited
to policy-mediated rights. A future migration that forgets any of it fails
CI here — no hostile per-table test needs to exist for the schema invariant
to hold (per-table attack tests remain valuable for behavior; this guards
the catalog).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Tables with tenant_id where RLS is intentionally absent — must stay empty
#: unless a documented deviation adds one.
_ALLOWED_WITHOUT_RLS: frozenset[str] = frozenset()


def _tenant_tables(owner_session: Session) -> list[str]:
    rows = owner_session.execute(text(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN pg_tables t ON t.tablename = c.table_name "
        " AND t.schemaname = 'public' "
        "WHERE c.column_name = 'tenant_id' AND c.table_schema = 'public' "
        "ORDER BY 1"
    )).scalars().all()
    return [r for r in rows if r not in _ALLOWED_WITHOUT_RLS]


def test_every_tenant_table_has_enable_and_force_rls(owner_session: Session) -> None:
    tables = _tenant_tables(owner_session)
    assert tables, "no tenant tables found — wrong database?"
    missing = owner_session.execute(text(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = ANY(:t) AND relkind = 'r' "
        "AND NOT (relrowsecurity AND relforcerowsecurity)"
    ), {"t": tables}).all()
    assert not missing, f"tables missing ENABLE+FORCE RLS: {missing}"


def test_every_tenant_table_has_a_tenant_policy(owner_session: Session) -> None:
    tables = _tenant_tables(owner_session)
    covered = set(owner_session.execute(text(
        "SELECT DISTINCT tablename FROM pg_policies "
        "WHERE schemaname = 'public' AND tablename = ANY(:t) "
        "AND qual LIKE '%tenant_id%'"
    ), {"t": tables}).scalars().all())
    uncovered = sorted(set(tables) - covered)
    assert not uncovered, f"tables with no tenant policy: {uncovered}"


def test_app_role_has_no_rls_bypass(owner_session: Session) -> None:
    """career_app must never gain BYPASSRLS or superuser — the entire
    isolation model rests on it."""
    row = owner_session.execute(text(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'career_app'"
    )).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False
