"""Adversarial RLS + posture tests for the C6 engine schema (§15.10, §06).

The pool is deliberately shared: job ads are public data, so job_postings and
discovery_runs are system tables the app role can only SELECT — writing them
is the owner-role engine's job. Decisions and suppressions are per-tenant and
FORCE-RLS isolated like every other tenant table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from career.db.session import app_engine, tenant_session

NOW = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)


def _seed_pool(owner_engine: Engine) -> tuple[str, str]:
    """A posting + a run, written as the engine (owner role) would."""
    posting_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    with Session(owner_engine) as s:
        s.execute(
            text(
                "INSERT INTO job_postings (id, url_identity, url, title, company, source) "
                "VALUES (:id, :ident, 'https://ex.example/j/1', 'BA', 'Acme', "
                "'serpapi_google_jobs')"
            ),
            {"id": posting_id, "ident": f"joburl:v1:{uuid.uuid4().hex}{uuid.uuid4().hex}"[:75]},
        )
        s.execute(
            text("INSERT INTO discovery_runs (id, run_date) VALUES (:id, '2026-07-15')"),
            {"id": run_id},
        )
        s.commit()
    return posting_id, run_id


def _cleanup_pool(owner_engine: Engine, posting_id: str, run_id: str) -> None:
    with Session(owner_engine) as s:
        s.execute(text("DELETE FROM job_postings WHERE id = :id"), {"id": posting_id})
        s.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": run_id})
        s.commit()


# ── the pool: shared read, owner-only write ──────────────────────────────────


def test_app_role_reads_the_pool_but_cannot_write_it(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    posting_id, run_id = _seed_pool(owner_engine)
    try:
        a, _ = two_tenants
        with tenant_session(a) as s:
            count = s.execute(text("SELECT count(*) FROM job_postings")).scalar_one()
            assert count >= 1  # the pool is shared — visible to every tenant

        with pytest.raises(ProgrammingError) as err:
            with app_engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO job_postings "
                        "(id, url_identity, url, title, company, source) VALUES "
                        "(gen_random_uuid(), 'joburl:v1:forged', 'https://x', 'T', 'C', 'x')"
                    )
                )
                conn.commit()
        assert "permission denied" in str(err.value).lower()
    finally:
        _cleanup_pool(owner_engine, posting_id, run_id)


def test_pool_identity_is_unique(owner_engine: Engine) -> None:
    ident = f"joburl:v1:{'a' * 64}"
    with Session(owner_engine) as s:
        s.execute(
            text(
                "INSERT INTO job_postings (id, url_identity, url, title, company, source) "
                "VALUES (gen_random_uuid(), :i, 'https://a', 'T', 'C', 'jobspy')"
            ),
            {"i": ident},
        )
        s.commit()
    try:
        with pytest.raises(IntegrityError):
            with Session(owner_engine) as s:
                s.execute(
                    text(
                        "INSERT INTO job_postings "
                        "(id, url_identity, url, title, company, source) VALUES "
                        "(gen_random_uuid(), :i, 'https://b', 'T2', 'C2', 'jobspy')"
                    ),
                    {"i": ident},
                )
                s.commit()
    finally:
        with Session(owner_engine) as s:
            s.execute(text("DELETE FROM job_postings WHERE url_identity = :i"), {"i": ident})
            s.commit()


# ── decisions + suppressions: FORCE RLS like everything tenant-scoped ────────


def _insert_decision(session, tenant_id: str, run_id: str, posting_id: str) -> None:
    session.execute(
        text(
            "INSERT INTO tenant_job_decisions "
            "(id, tenant_id, run_id, job_posting_id, decision, gate_policy_version) "
            "VALUES (:id, :tid, :rid, :jid, 'PASS', 'v1')"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant_id, "rid": run_id, "jid": posting_id},
    )


def _insert_suppression(session, tenant_id: str, key: str) -> None:
    session.execute(
        text(
            "INSERT INTO tenant_job_suppressions "
            "(id, tenant_id, suppression_key, delivered_at, expires_at) "
            "VALUES (:id, :tid, :key, :d, :e)"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant_id, "key": key,
         "d": NOW, "e": NOW + timedelta(days=30)},
    )


def test_decisions_are_tenant_isolated(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    posting_id, run_id = _seed_pool(owner_engine)
    try:
        a, b = two_tenants
        with tenant_session(a) as s:
            _insert_decision(s, a, run_id, posting_id)
        with tenant_session(b) as s:
            visible = s.execute(
                text("SELECT count(*) FROM tenant_job_decisions")
            ).scalar_one()
            assert visible == 0
        with pytest.raises((ProgrammingError, Exception)) as err:
            with tenant_session(a) as s:
                _insert_decision(s, b, run_id, posting_id)  # forged tenant
        assert "row-level security" in str(err.value).lower()
        with app_engine.connect() as conn:  # no context → fail closed
            assert conn.execute(
                text("SELECT count(*) FROM tenant_job_decisions")
            ).scalar_one() == 0
    finally:
        _cleanup_pool(owner_engine, posting_id, run_id)


def test_suppressions_are_tenant_isolated_and_unique_per_key(
    two_tenants: tuple[str, str],
) -> None:
    a, b = two_tenants
    key = "https://ex.example/jobs/123"
    with tenant_session(a) as s:
        _insert_suppression(s, a, key)
    with tenant_session(b) as s:
        _insert_suppression(s, b, key)  # same key, other tenant — fine
        assert s.execute(
            text("SELECT count(*) FROM tenant_job_suppressions")
        ).scalar_one() == 1  # sees only its own
    with pytest.raises(IntegrityError):
        with tenant_session(a) as s:
            _insert_suppression(s, a, key)  # duplicate within the tenant


def test_decision_unique_per_tenant_run_job(
    owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    posting_id, run_id = _seed_pool(owner_engine)
    try:
        a, _ = two_tenants
        with tenant_session(a) as s:
            _insert_decision(s, a, run_id, posting_id)
        with pytest.raises(IntegrityError):
            with tenant_session(a) as s:
                _insert_decision(s, a, run_id, posting_id)
    finally:
        _cleanup_pool(owner_engine, posting_id, run_id)
