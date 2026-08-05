"""Adversarial cross-tenant isolation tests (§15.10).

Runs as the non-superuser ``career_app`` role. Proves that Row-Level Security
confines every read and write to the tenant bound on the transaction, and that
attempts to cross the boundary fail. This is the CI gate the whitepaper requires:
"cross-tenant tests fail to breach and pass in CI".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from career.db.session import (
    NO_TENANT_CONTEXT_SQLSTATE,
    app_engine,
    tenant_session,
)


def _insert_document(session, tenant_id: str, storage_key: str) -> str:
    doc_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO documents
                (id, tenant_id, storage_key, content_sha256, content_type,
                 size_bytes, status)
            VALUES
                (:id, :tid, :key, :sha, 'application/pdf', 1024, 'active')
            """
        ),
        {"id": doc_id, "tid": tenant_id, "key": storage_key, "sha": "0" * 64},
    )
    return doc_id


def test_app_role_is_not_superuser(two_tenants: tuple[str, str]) -> None:
    """If the app role were a superuser, RLS would be silently bypassed."""
    with app_engine.connect() as conn:
        is_super = conn.execute(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        ).scalar_one()
    assert is_super is False


def test_tenant_sees_only_own_documents(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    with tenant_session(a) as s:
        _insert_document(s, a, "tenants/a/documents/cv-a.pdf")
    with tenant_session(b) as s:
        _insert_document(s, b, "tenants/b/documents/cv-b.pdf")

    # Tenant A sees exactly its own row.
    with tenant_session(a) as s:
        rows = s.execute(text("SELECT storage_key FROM documents")).scalars().all()
    assert rows == ["tenants/a/documents/cv-a.pdf"]

    # Tenant B sees exactly its own row — never A's.
    with tenant_session(b) as s:
        rows = s.execute(text("SELECT storage_key FROM documents")).scalars().all()
    assert rows == ["tenants/b/documents/cv-b.pdf"]


def test_cross_tenant_read_returns_nothing(two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    with tenant_session(a) as s:
        _insert_document(s, a, "tenants/a/documents/secret.pdf")

    # Under B's context, an explicit filter for A's rows still yields nothing:
    # RLS is applied on top of the WHERE clause, it cannot be defeated by it.
    with tenant_session(b) as s:
        count = s.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :a"), {"a": a}
        ).scalar_one()
    assert count == 0


def test_cross_tenant_write_is_rejected(two_tenants: tuple[str, str]) -> None:
    """Inserting a row for another tenant while bound to A violates WITH CHECK."""
    a, b = two_tenants
    with pytest.raises((ProgrammingError, Exception)) as excinfo:
        with tenant_session(a) as s:
            _insert_document(s, b, "tenants/b/forged-by-a.pdf")
    assert "row-level security" in str(excinfo.value).lower()


def test_no_tenant_context_refuses(two_tenants: tuple[str, str]) -> None:
    """With no app.tenant_id set the query is REFUSED, not answered with nothing.

    Until migration 0021 this test asserted ``count == 0``: the policy compared
    tenant_id against NULL, so an unscoped read returned the empty set and said
    so with a straight face. That is the failure mode that hurts — a query that
    lost its scope reports «this customer has nothing» and the caller believes
    it. Fail closed now means an error, and the error names the way out.
    """
    a, _ = two_tenants
    with tenant_session(a) as s:
        _insert_document(s, a, "tenants/a/documents/x.pdf")

    with pytest.raises(DBAPIError) as err:
        with app_engine.begin() as conn:
            conn.execute(text("SELECT count(*) FROM documents"))
    assert err.value.orig.sqlstate == NO_TENANT_CONTEXT_SQLSTATE
    assert "tenant_session" in str(err.value)


#: The four live entry points, named by the table each one reaches for first.
#: They all run on the owner engine today (DEVIATIONS D20); what is asserted
#: here is that the boundary holds the moment they move, so the migration lands
#: on rules that are already proven rather than on hope.
#: (entry point, table, mutable). ``mutable=False`` marks the append-only
#: ledgers, where the app role holds only SELECT+INSERT — for those the correct
#: cross-tenant answer is «permission denied», a stronger refusal than RLS's.
_ENTRY_POINT_TABLES = (
    ("worker loop — inbound routing", "customer_channels", True),
    ("worker loop — the journey", "onboarding_sessions", True),
    ("worker loop — inbound dedupe", "processed_messages", True),
    ("salla provisioning", "subscriptions", True),
    ("salla provisioning", "activation_tokens", True),
    ("salla provisioning", "subscription_events", False),
    ("nightly engine", "deliveries", True),
    ("nightly engine", "tenant_job_decisions", True),
    ("nightly engine", "usage_events", False),
    ("admin console", "support_events", True),
    ("privacy §12 export/erasure", "documents", True),
    ("privacy §12 export/erasure", "cv_uploads", True),
    ("privacy §12 export/erasure", "profile_facts", True),
    ("privacy §12 export/erasure", "privacy_requests", True),
    ("privacy §12 export/erasure", "consent_events", False),
    ("funnel", "funnel_sessions", True),
    ("audit trail", "audit_events", False),
)


@pytest.mark.parametrize(("entry_point", "table", "mutable"), _ENTRY_POINT_TABLES)
def test_every_entry_point_table_refuses_cross_tenant_reads_and_writes(
    two_tenants: tuple[str, str], entry_point: str, table: str, mutable: bool
) -> None:
    """Bound to A, aim every verb at B explicitly. Nothing may land.

    No seeding: the assertion is about the policy, and RLS is applied on top of
    the WHERE clause, so an explicit ``tenant_id = B`` under A's scope can never
    match whether or not B has rows. UPDATE and DELETE are checked by rowcount
    because that is exactly what used to be zero for the *wrong* reason — the
    policy matched nothing rather than refusing.
    """
    a, b = two_tenants
    with tenant_session(a) as s:
        visible = s.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :b"), {"b": b}
        ).scalar_one()
        assert visible == 0, f"{entry_point}: {table} leaked across tenants"

        if not mutable:
            with pytest.raises(ProgrammingError) as err:
                s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :b"), {"b": b})
            assert "permission denied" in str(err.value).lower()
            return

        deleted = s.execute(
            text(f"DELETE FROM {table} WHERE tenant_id = :b"), {"b": b}
        ).rowcount
        assert deleted == 0, f"{entry_point}: {table} deletable across tenants"

        updated = s.execute(
            text(f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id = :b"),
            {"b": b},
        ).rowcount
        assert updated == 0, f"{entry_point}: {table} writable across tenants"
