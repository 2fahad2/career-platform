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
from sqlalchemy.exc import ProgrammingError

from career.db.session import app_engine, tenant_session


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


def test_no_tenant_context_sees_nothing(two_tenants: tuple[str, str]) -> None:
    """With no app.tenant_id set, the GUC is NULL and the policy matches no rows —
    fail closed, never fail open."""
    a, _ = two_tenants
    with tenant_session(a) as s:
        _insert_document(s, a, "tenants/a/documents/x.pdf")

    with app_engine.connect() as conn:
        # No set_config → current_setting('app.tenant_id', true) is NULL.
        count = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
    assert count == 0
