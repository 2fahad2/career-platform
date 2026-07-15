"""Query-family derivation acceptance tests (whitepaper §06, D5) — before code.

Families derive from the UNION of ACTIVE tenants' approved paths (all three
slots — discovery casts the wide net; the per-tenant gate narrows later), with
Arabic+English aliases from the single path registry and locations mapped from
the tenants' policy cities. Nothing is hardcoded per specialization: no active
tenants → no families.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.engine import families

NOW = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)


def _seed_tenant(
    owner: Session, *, code: str, sub_status: str, paths_approved: dict | None,
    cities: list[str], remote: str | None,
) -> str:
    tenant_id = str(uuid.uuid4())
    owner.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:id, :code)"),
        {"id": tenant_id, "code": code},
    )
    owner.execute(
        sql_text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_code, status, salla_order_id, amount_sar, currency) "
            "VALUES (:id, :tid, 'basic', :st, :oid, 149, 'SAR')"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant_id, "st": sub_status,
         "oid": f"O-{uuid.uuid4()}"},
    )
    if paths_approved is not None:
        import json

        owner.execute(
            sql_text(
                "INSERT INTO search_policies "
                "(id, tenant_id, version, status, approved_paths, cities, "
                " remote_policy, sectors_preferred, sectors_avoided, banned_companies) "
                "VALUES (:id, :tid, 1, 'active', CAST(:paths AS jsonb), "
                " CAST(:cities AS jsonb), :remote, '{}', '{}', '{}')"
            ),
            {"id": str(uuid.uuid4()), "tid": tenant_id,
             "paths": json.dumps(paths_approved),
             "cities": json.dumps({"cities": cities, "willing_to_relocate": False}),
             "remote": remote},
        )
    return tenant_id


def _cleanup(owner_engine: Engine, tenant_ids: list[str]) -> None:
    with Session(owner_engine) as s:
        for tid in tenant_ids:
            s.execute(sql_text("DELETE FROM tenants WHERE id = :id"), {"id": tid})
        s.commit()


def test_families_are_the_union_of_active_tenants_paths(owner_engine: Engine) -> None:
    tids: list[str] = []
    try:
        with Session(owner_engine) as s:
            tids.append(_seed_tenant(
                s, code=f"TEN-F{uuid.uuid4().hex[:4]}", sub_status="ACTIVE",
                paths_approved={"primary": "business_analyst",
                                "secondary": "it_operations", "stretch": None},
                cities=["الرياض"], remote="hybrid",
            ))
            tids.append(_seed_tenant(
                s, code=f"TEN-F{uuid.uuid4().hex[:4]}", sub_status="ACTIVE",
                paths_approved={"primary": "project_manager", "secondary": None,
                                "stretch": None},
                cities=["جدة"], remote="onsite",
            ))
            # inactive tenant must NOT contribute
            tids.append(_seed_tenant(
                s, code=f"TEN-F{uuid.uuid4().hex[:4]}", sub_status="EXPIRED",
                paths_approved={"primary": "software_engineering", "secondary": None,
                                "stretch": None},
                cities=["الدمام"], remote=None,
            ))
            s.commit()
            derived = families.derive_query_families(s, tenant_ids=[uuid.UUID(t) for t in tids])
    finally:
        _cleanup(owner_engine, tids)

    keys = [f.family for f in derived]
    assert keys == sorted(keys)  # deterministic order
    assert set(keys) == {"business_analyst", "it_operations", "project_manager"}
    assert "software_engineering" not in keys  # inactive excluded

    ba = next(f for f in derived if f.family == "business_analyst")
    # Arabic AND English aliases from the single registry (whitepaper example)
    assert "Business Analyst" in ba.aliases
    assert "محلل أعمال" in ba.aliases
    # locations mapped from the requesting tenant's Arabic city
    assert "Riyadh, Saudi Arabia" in ba.locations
    assert "Saudi Arabia" in ba.locations
    assert "Remote" in ba.locations  # its tenant is hybrid

    pm = next(f for f in derived if f.family == "project_manager")
    assert "Jeddah, Saudi Arabia" in pm.locations
    assert "Remote" not in pm.locations  # its only tenant is onsite


def test_unknown_city_falls_back_to_country_only(owner_engine: Engine) -> None:
    tids: list[str] = []
    try:
        with Session(owner_engine) as s:
            tids.append(_seed_tenant(
                s, code=f"TEN-F{uuid.uuid4().hex[:4]}", sub_status="ACTIVE",
                paths_approved={"primary": "data_analyst", "secondary": None,
                                "stretch": None},
                cities=["حائل"], remote=None,  # not in the city map
            ))
            s.commit()
            derived = families.derive_query_families(s, tenant_ids=[uuid.UUID(t) for t in tids])
    finally:
        _cleanup(owner_engine, tids)
    da = next(f for f in derived if f.family == "data_analyst")
    assert da.locations == ("Saudi Arabia",)  # honest fallback, no invention


def test_no_active_tenants_means_no_families(owner_engine: Engine) -> None:
    with Session(owner_engine) as s:
        derived = families.derive_query_families(s, tenant_ids=[uuid.uuid4()])
    assert derived == []


def test_every_registry_family_has_query_aliases() -> None:
    """The registry is the single authority — every family must be queryable."""
    from career.onboarding.paths import DEFAULT_FAMILIES

    for fam in DEFAULT_FAMILIES:
        assert fam.query_aliases, fam.key
        assert any(a.isascii() for a in fam.query_aliases), fam.key      # English
        assert any(not a.isascii() for a in fam.query_aliases), fam.key  # Arabic
