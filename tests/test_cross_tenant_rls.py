"""Adversarial cross-tenant isolation tests (§15.10).

Runs as the non-superuser ``career_app`` role. Proves that Row-Level Security
confines every read and write to the tenant bound on the transaction, and that
attempts to cross the boundary fail. This is the CI gate the whitepaper requires:
"cross-tenant tests fail to breach and pass in CI".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

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
    # ("worker loop — inbound dedupe", "processed_messages", True) stood here
    # and asserted something that was never true: the WhatsApp worker dedupes
    # on the unique `inbound_messages.wa_message_id`, and nothing in this
    # codebase has ever written a `processed_messages` row. The table went with
    # the deleted queue subsystem in 0029; the claim went with it.
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


#: Rows the app role may not INSERT and no tenant owns: a discovery run and a
#: job posting, both global tables with SELECT-only grants. `tenant_job_decisions`
#: cannot be seeded without them, so the fixture below borrows the owner engine
#: for exactly these two rows and takes them away again.
@pytest.fixture()
def shared_discovery(owner_engine) -> Iterator[tuple[str, str]]:  # type: ignore[no-untyped-def]
    run_id, posting_id = str(uuid.uuid4()), str(uuid.uuid4())
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO discovery_runs (id, run_date, status) "
                "VALUES (:id, CURRENT_DATE, 'completed')"
            ),
            {"id": run_id},
        )
        conn.execute(
            text(
                "INSERT INTO job_postings "
                "(id, url_identity, url, title, company, source) VALUES "
                "(:id, :ident, :url, 'Engineer', 'Example Co', 'searchapi')"
            ),
            {"id": posting_id, "ident": posting_id, "url": f"https://e.example/{posting_id}"},
        )
    yield run_id, posting_id
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenant_job_decisions WHERE run_id = :r"),
                     {"r": run_id})
        conn.execute(text("DELETE FROM job_postings WHERE id = :id"), {"id": posting_id})
        conn.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": run_id})


def _seed_one_row_everywhere(
    session, tenant_id: str, shared: tuple[str, str]
) -> None:
    """Exactly ONE row in each of the sixteen entry-point tables.

    Written as one ordered function rather than sixteen fixtures because the
    tables are a graph: an onboarding session needs a subscription, a delivery
    needs a channel, a decision needs a run and a posting. The row contents are
    deliberately dull — none of this is asserted on, the only thing that
    matters is that the row EXISTS and belongs to this tenant.
    """
    run_id, posting_id = shared
    sub = str(uuid.uuid4())
    channel = str(uuid.uuid4())
    marker = uuid.uuid4().hex[:10]
    statements: tuple[tuple[str, dict], ...] = (
        ("INSERT INTO subscriptions (id, tenant_id, plan_code, status, "
         " salla_order_id) VALUES (:id, :t, 'professional', 'ACTIVE', :order)",
         {"id": sub, "t": tenant_id, "order": f"ord-{marker}"}),
        ("INSERT INTO customer_channels (id, tenant_id, subscription_id, "
         " provider, phone_e164, created_at) VALUES "
         " (:id, :t, :sub, 'whatsapp', :phone, now())",
         {"id": channel, "t": tenant_id, "sub": sub,
          "phone": f"+96650{uuid.uuid4().int % 10_000_000:07d}"}),
        ("INSERT INTO onboarding_sessions (id, tenant_id, subscription_id, "
         " channel_id, state) VALUES (gen_random_uuid(), :t, :sub, :ch, 'ASK_NAME')",
         {"t": tenant_id, "sub": sub, "ch": channel}),
        ("INSERT INTO activation_tokens (id, tenant_id, subscription_id, "
         " token_hash, expires_at) VALUES (gen_random_uuid(), :t, :sub, :hash, "
         " now() + interval '1 day')",
         {"t": tenant_id, "sub": sub, "hash": marker * 6}),
        ("INSERT INTO subscription_events (id, tenant_id, subscription_id, "
         " event_type) VALUES (gen_random_uuid(), :t, :sub, 'activated')",
         {"t": tenant_id, "sub": sub}),
        ("INSERT INTO deliveries (id, tenant_id, channel_id, run_date, status) "
         " VALUES (gen_random_uuid(), :t, :ch, CURRENT_DATE, 'completed')",
         {"t": tenant_id, "ch": channel}),
        ("INSERT INTO tenant_job_decisions (id, tenant_id, run_id, "
         " job_posting_id, decision, gate_policy_version) VALUES "
         " (gen_random_uuid(), :t, :run, :post, 'pass', 'v1')",
         {"t": tenant_id, "run": run_id, "post": posting_id}),
        ("INSERT INTO usage_events (id, tenant_id, kind) "
         " VALUES (gen_random_uuid(), :t, 'llm_generation')",
         {"t": tenant_id}),
        ("INSERT INTO support_events (id, tenant_id, channel_id, kind, status) "
         " VALUES (gen_random_uuid(), :t, :ch, 'support', 'open')",
         {"t": tenant_id, "ch": channel}),
        ("INSERT INTO documents (id, tenant_id, storage_key, content_sha256, "
         " content_type, size_bytes, status) VALUES (gen_random_uuid(), :t, "
         " :key, :sha, 'application/pdf', 1024, 'active')",
         {"t": tenant_id, "key": f"tenants/{tenant_id}/seed-{marker}.pdf",
          "sha": "0" * 64}),
        ("INSERT INTO cv_uploads (id, tenant_id, original_filename) "
         " VALUES (gen_random_uuid(), :t, 'cv.pdf')",
         {"t": tenant_id}),
        ("INSERT INTO profile_facts (id, tenant_id, category, source) "
         " VALUES (gen_random_uuid(), :t, 'experience', 'cv')",
         {"t": tenant_id}),
        ("INSERT INTO privacy_requests (id, tenant_id, kind) "
         " VALUES (gen_random_uuid(), :t, 'export')",
         {"t": tenant_id}),
        ("INSERT INTO consent_events (id, tenant_id, purpose, action) "
         " VALUES (gen_random_uuid(), :t, 'basic_processing', 'granted')",
         {"t": tenant_id}),
        ("INSERT INTO funnel_sessions (id, tenant_id, subscription_id, "
         " channel_id, state) VALUES (gen_random_uuid(), :t, :sub, :ch, 'INTRO')",
         {"t": tenant_id, "sub": sub, "ch": channel}),
        ("INSERT INTO audit_events (id, tenant_id, actor, action) "
         " VALUES (gen_random_uuid(), :t, 'system', 'seeded')",
         {"t": tenant_id}),
    )
    for statement, params in statements:
        session.execute(text(statement), params)


@pytest.fixture()
def both_tenants_populated(
    two_tenants: tuple[str, str], shared_discovery: tuple[str, str]
) -> tuple[str, str]:
    """A and B each own exactly one row in every entry-point table.

    Cleanup is the `two_tenants` fixture's CASCADE: every table below has an
    ON DELETE CASCADE foreign key to `tenants`, checked in pg_constraint.
    """
    a, b = two_tenants
    for tenant in (a, b):
        with tenant_session(tenant) as s:
            _seed_one_row_everywhere(s, tenant, shared_discovery)
    return a, b


@pytest.mark.parametrize(("entry_point", "table", "mutable"), _ENTRY_POINT_TABLES)
def test_every_entry_point_table_refuses_cross_tenant_reads_and_writes(
    both_tenants_populated: tuple[str, str],
    entry_point: str,
    table: str,
    mutable: bool,
) -> None:
    """Bound to A, aim every verb at B explicitly. Nothing may land.

    This used to seed NOTHING. Its own docstring argued that the assertion was
    «about the policy» — that RLS sits on top of the WHERE clause, so an
    explicit ``tenant_id = B`` under A's scope cannot match whether or not B
    has rows. True, and beside the point: with B's side of every table empty,
    ``count == 0`` and ``rowcount == 0`` are what an EMPTY TABLE returns too.
    Twelve of them could not fail if the policy were dropped,
    which is the one event they exist to catch. A test that cannot fail is not
    evidence, it is furniture.

    So B really owns a row in every one of these tables now, and A owns one
    too. Every assertion below is a refusal that is distinguishable from an
    absence: drop the policy and the count becomes 1, the rowcount becomes 1,
    and the unfiltered read starts returning the other customer's row.
    """
    a, b = both_tenants_populated

    # First: the seed landed. Without this the whole test silently degrades
    # back to the version it replaces the moment a schema change breaks an
    # INSERT above — the failure mode is «still green, guarding nothing».
    with tenant_session(b) as s:
        seeded = s.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :b"), {"b": b}
        ).scalar_one()
    assert seeded == 1, (
        f"{entry_point}: nothing was seeded into {table} for tenant B — the "
        "refusal below would be indistinguishable from an empty table"
    )

    with tenant_session(a) as s:
        visible = s.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :b"), {"b": b}
        ).scalar_one()
        assert visible == 0, f"{entry_point}: {table} leaked across tenants"

        # And the read nobody writes a WHERE clause for. A policy that is
        # dropped rather than weakened shows up here first: the tenant sees
        # the whole table and every count the operator is shown is wrong.
        everything = s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert everything == 1, (
            f"{entry_point}: an unscoped read of {table} under A's scope "
            f"returned {everything} rows — A owns exactly one"
        )

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

    # B's row is still there. The DELETE above returning 0 could also mean «the
    # statement ran, matched B's row, and removed it silently» if the policy
    # were only a USING clause on SELECT — so the survivor is checked, not
    # inferred from a rowcount.
    with tenant_session(b) as s:
        survived = s.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :b"), {"b": b}
        ).scalar_one()
    assert survived == 1, (
        f"{entry_point}: A's cross-tenant DELETE reported 0 rows but B's row "
        f"is gone from {table}"
    )
    assert a
