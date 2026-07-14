"""Shared fixtures for DB-backed tests.

These tests require a running Postgres 16 with migration 0001 applied and the
non-superuser ``career_app`` role present (Compose brings both up). Connection
details come from the environment (the same Settings the app uses). If no
database is reachable the DB tests are skipped locally with a clear reason —
never silently treated as passing. In CI (``CI_REQUIRE_DB=1``) an unreachable
database is a hard FAILURE, so the isolation guarantee is actually exercised
(whitepaper §13 C2 exit condition: cross-tenant tests must pass in CI).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.config import get_settings


def _require_db() -> bool:
    return os.environ.get("CI_REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    """Engine for the owner role — bypasses RLS, used to seed/clean tenants."""
    settings = get_settings()
    engine = create_engine(settings.owner_database_url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        msg = f"Postgres not reachable for RLS tests: {exc}"
        if _require_db():
            pytest.fail(msg)  # CI: must not silently skip the isolation gate
        pytest.skip(msg)
    yield engine
    engine.dispose()


@pytest.fixture()
def two_tenants(owner_engine: Engine) -> Iterator[tuple[str, str]]:
    """Seed two tenants (owner bypasses RLS) and clean them up afterwards."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, code) VALUES (:id, :code)"),
            {"id": a, "code": "TEN-AAAA"},
        )
        conn.execute(
            text("INSERT INTO tenants (id, code) VALUES (:id, :code)"),
            {"id": b, "code": "TEN-BBBB"},
        )
    yield a, b
    with owner_engine.begin() as conn:
        # CASCADE removes any documents created during the test.
        conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})


@pytest.fixture()
def owner_session(owner_engine: Engine) -> Iterator[Session]:
    """A plain owner-role session (bypasses RLS) for provisioning-side tests."""
    session = Session(owner_engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def clean_billing(owner_engine: Engine) -> Iterator[None]:
    """Delete tenants + webhook_events created during a billing test (tenant
    delete cascades subscriptions/tokens/events)."""
    def ids(table: str) -> set[str]:
        with Session(owner_engine) as s:
            return {str(r[0]) for r in s.execute(text(f"SELECT id::text FROM {table}"))}

    before_t = ids("tenants")
    before_w = ids("webhook_events")
    yield
    new_t = ids("tenants") - before_t
    new_w = ids("webhook_events") - before_w
    with Session(owner_engine) as s:
        if new_t:
            s.execute(text("DELETE FROM tenants WHERE id::text = ANY(:ids)"),
                      {"ids": list(new_t)})
        if new_w:
            s.execute(text("DELETE FROM webhook_events WHERE id::text = ANY(:ids)"),
                      {"ids": list(new_w)})
        s.commit()
