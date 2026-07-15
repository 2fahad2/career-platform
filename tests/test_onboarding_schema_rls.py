"""Adversarial RLS tests for the C5 onboarding schema (§15.10).

Every C5 table must confine reads/writes to the bound tenant, fail closed with
no tenant context, and reject forged cross-tenant writes. consent_events is
additionally append-only for the app role: consent history is evidence — the
app can add events but never rewrite them (whitepaper §12).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from career.db.session import app_engine, tenant_session

# Minimal valid INSERT per C5 table. :id/:tid are bound per call; tables whose
# rows need a subscription get :sub bound too.
_C5_INSERTS: dict[str, str] = {
    "onboarding_sessions": (
        "INSERT INTO onboarding_sessions (id, tenant_id, subscription_id, state) "
        "VALUES (:id, :tid, :sub, 'PAID_UNCLAIMED')"
    ),
    "consent_events": (
        "INSERT INTO consent_events (id, tenant_id, purpose, action) "
        "VALUES (:id, :tid, 'basic_processing', 'granted')"
    ),
    "customer_profiles": (
        "INSERT INTO customer_profiles (id, tenant_id) VALUES (:id, :tid)"
    ),
    "profile_facts": (
        "INSERT INTO profile_facts (id, tenant_id, category, source) "
        "VALUES (:id, :tid, 'experience', 'cv_extraction')"
    ),
    "forbidden_claims": (
        "INSERT INTO forbidden_claims (id, tenant_id, claim, source) "
        "VALUES (:id, :tid, 'PMP certification', 'customer_rejected')"
    ),
    "career_path_assessments": (
        "INSERT INTO career_path_assessments (id, tenant_id, requested_path) "
        "VALUES (:id, :tid, 'business_analyst')"
    ),
    "search_policies": (
        "INSERT INTO search_policies (id, tenant_id, version) VALUES (:id, :tid, 1)"
    ),
    "privacy_requests": (
        "INSERT INTO privacy_requests (id, tenant_id, kind) VALUES (:id, :tid, 'export')"
    ),
    "cv_uploads": (
        "INSERT INTO cv_uploads (id, tenant_id) VALUES (:id, :tid)"
    ),
}


def _make_subscription(session, tenant_id: str) -> str:
    sub_id = str(uuid.uuid4())
    session.execute(
        text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_code, status, salla_order_id, amount_sar, currency) "
            "VALUES (:id, :tid, 'basic', 'PAID_UNCLAIMED', :order_id, 1, 'SAR')"
        ),
        {"id": sub_id, "tid": tenant_id, "order_id": f"RLS-{uuid.uuid4()}"},
    )
    return sub_id


def _insert(session, table: str, tenant_id: str, sub_id: str | None = None) -> str:
    row_id = str(uuid.uuid4())
    session.execute(
        text(_C5_INSERTS[table]), {"id": row_id, "tid": tenant_id, "sub": sub_id}
    )
    return row_id


@pytest.mark.parametrize("table", sorted(_C5_INSERTS))
def test_c5_table_tenant_isolation(two_tenants: tuple[str, str], table: str) -> None:
    """A's row is invisible to B (even with an explicit WHERE) and invisible
    with no tenant context at all — fail closed."""
    a, b = two_tenants
    with tenant_session(a) as s:
        sub = _make_subscription(s, a) if table == "onboarding_sessions" else None
        _insert(s, table, a, sub)

    with tenant_session(b) as s:
        visible = s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        filtered = s.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :a"), {"a": a}
        ).scalar_one()
    assert visible == 0
    assert filtered == 0

    with app_engine.connect() as conn:  # no app.tenant_id bound → NULL GUC
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    assert count == 0


@pytest.mark.parametrize("table", sorted(_C5_INSERTS))
def test_c5_forged_cross_tenant_write_rejected(
    two_tenants: tuple[str, str], table: str
) -> None:
    """Inserting a row that claims tenant B while bound to A violates WITH CHECK."""
    a, b = two_tenants
    sub = None
    if table == "onboarding_sessions":
        with tenant_session(b) as s:
            sub = _make_subscription(s, b)
    with pytest.raises((ProgrammingError, Exception)) as excinfo:
        with tenant_session(a) as s:
            _insert(s, table, b, sub)
    assert "row-level security" in str(excinfo.value).lower()


def test_consent_events_are_append_only(two_tenants: tuple[str, str]) -> None:
    """The app role can INSERT and SELECT consent events but never UPDATE or
    DELETE them — withdrawal is a new event, not an edit."""
    a, _ = two_tenants
    with tenant_session(a) as s:
        row_id = _insert(s, "consent_events", a)

    with tenant_session(a) as s:
        got = s.execute(
            text("SELECT action FROM consent_events WHERE id = :id"), {"id": row_id}
        ).scalar_one()
        assert got == "granted"

    with pytest.raises(ProgrammingError) as upd_err:
        with tenant_session(a) as s:
            s.execute(
                text("UPDATE consent_events SET action = 'withdrawn' WHERE id = :id"),
                {"id": row_id},
            )
    assert "permission denied" in str(upd_err.value).lower()

    with pytest.raises(ProgrammingError) as del_err:
        with tenant_session(a) as s:
            s.execute(text("DELETE FROM consent_events WHERE id = :id"), {"id": row_id})
    assert "permission denied" in str(del_err.value).lower()

    # Withdrawal is expressed as a NEW event.
    with tenant_session(a) as s:
        s.execute(
            text(
                "INSERT INTO consent_events (id, tenant_id, purpose, action) "
                "VALUES (:id, :tid, 'basic_processing', 'withdrawn')"
            ),
            {"id": str(uuid.uuid4()), "tid": a},
        )
        history = s.execute(
            text(
                "SELECT action FROM consent_events "
                "WHERE purpose = 'basic_processing' ORDER BY occurred_at, action"
            )
        ).scalars().all()
    assert history == ["granted", "withdrawn"]
