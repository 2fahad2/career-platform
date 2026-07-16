"""Seed or clean the two C6-exit-gate demo tenants on STAGING (whitepaper §13).

The live exit condition needs two ACTIVE tenants with different policies on
one pool. Until real onboarded customers exist (C5 live gate), these two
clearly-labeled demo tenants stand in:

- TEN-9901 «demo-riyadh»: الرياض, min 8,000 SAR, balanced, limit 3
- TEN-9902 «demo-jeddah»: جدة, min 12,000 SAR, strict, limit 2

``seed`` is idempotent (re-running replaces the pair); ``clean`` deletes the
pair (FK cascade removes their subscriptions/policies/decisions/suppressions)
and touches NOTHING else — the shared job pool and run history stay.

Usage: python scripts/demo_engine_tenants.py seed|clean
"""

from __future__ import annotations

import json
import sys
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from career.config import get_settings

DEMO_CODES = ("TEN-9901", "TEN-9902")

_POLICIES = {
    "TEN-9901": {"minsal": 8000.0, "unknown": "balanced", "city": "الرياض",
                 "limit": 3},
    "TEN-9902": {"minsal": 12000.0, "unknown": "strict", "city": "جدة",
                 "limit": 2},
}


def _clean(session: Session) -> int:
    removed = 0
    for code in DEMO_CODES:
        cursor = session.execute(
            text("DELETE FROM tenants WHERE code = :c"), {"c": code}
        )
        removed += getattr(cursor, "rowcount", 0) or 0
    return removed


def _seed(session: Session) -> None:
    _clean(session)  # idempotent: always a fresh, known pair
    for code in DEMO_CODES:
        tenant_id = str(uuid.uuid4())
        policy = _POLICIES[code]
        session.execute(
            text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
            {"id": tenant_id, "c": code},
        )
        session.execute(
            text(
                "INSERT INTO subscriptions (id, tenant_id, plan_code, status, "
                "salla_order_id, amount_sar, currency) VALUES "
                "(:id, :tid, 'basic', 'ACTIVE', :oid, 0, 'SAR')"
            ),
            {"id": str(uuid.uuid4()), "tid": tenant_id,
             "oid": f"DEMO-{code}"},  # unmistakably not a Salla order
        )
        session.execute(
            text(
                "INSERT INTO search_policies (id, tenant_id, version, status, "
                "approved_paths, cities, min_salary_sar, unknown_salary_policy, "
                "remote_policy, sectors_preferred, sectors_avoided, "
                "banned_companies, daily_job_limit) VALUES "
                "(:id, :tid, 1, 'active', CAST(:paths AS jsonb), "
                "CAST(:cities AS jsonb), :minsal, :unknown, 'hybrid', "
                "'{}', '{}', '{}', :lim)"
            ),
            {"id": str(uuid.uuid4()), "tid": tenant_id,
             "paths": json.dumps({"primary": "business_analyst",
                                  "secondary": None, "stretch": None}),
             "cities": json.dumps({"cities": [policy["city"]],
                                   "willing_to_relocate": False}),
             "minsal": policy["minsal"], "unknown": policy["unknown"],
             "lim": policy["limit"]},
        )
        print(f"seeded {code} ({policy['city']}, min {policy['minsal']:.0f}, "
              f"{policy['unknown']}, limit {policy['limit']})")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("seed", "clean"):
        print(__doc__)
        return 2
    settings = get_settings()
    engine = create_engine(settings.owner_database_url, future=True)
    try:
        with Session(engine) as session:
            if sys.argv[1] == "seed":
                _seed(session)
            else:
                print(f"removed {_clean(session)} demo tenant(s)")
            session.commit()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
