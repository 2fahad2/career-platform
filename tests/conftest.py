"""Shared fixtures for DB-backed tests.

These tests require a running Postgres 16 with migration 0001 applied and the
non-superuser ``career_app`` role present (Compose brings both up). Connection
details come from the environment (the same Settings the app uses). If no
database is reachable the DB tests are skipped with a clear reason — they are
never silently treated as passing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from career.config import get_settings


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    """Engine for the owner role — bypasses RLS, used to seed/clean tenants."""
    settings = get_settings()
    engine = create_engine(settings.owner_database_url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable for RLS tests: {exc}")
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
