"""Dynamic query-family derivation (whitepaper §06, D5).

Families are the UNION of ACTIVE tenants' approved paths — all three slots
(Primary/Secondary/Stretch): discovery casts the wide net, the per-tenant gate
narrows later. Aliases come from the single path registry (Arabic + English,
proper case — the whitepaper's binding structure), and locations map from the
requesting tenants' policy cities. Nothing is hardcoded per specialization:
no active tenants → no families, no queries, no spend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import SearchPolicy, Subscription
from career.onboarding.paths import DEFAULT_FAMILIES, PathFamily
from career.salla import subscriptions as sub_states

#: Arabic policy city → SearchAPI-style English location. Unknown cities fall
#: back to the country — an honest net, never an invented location.
_CITY_LOCATIONS: dict[str, str] = {
    "الرياض": "Riyadh, Saudi Arabia",
    "جدة": "Jeddah, Saudi Arabia",
    "الدمام": "Dammam, Saudi Arabia",
    "مكة المكرمة": "Makkah, Saudi Arabia",
    "المدينة المنورة": "Madinah, Saudi Arabia",
}
_COUNTRY = "Saudi Arabia"
_REMOTE = "Remote"
_REMOTE_POLICIES = frozenset({"remote", "hybrid", "any"})


@dataclass(frozen=True)
class QueryFamily:
    """The whitepaper §06 binding structure: family + aliases + locations."""

    family: str
    aliases: tuple[str, ...]
    locations: tuple[str, ...]
    tenant_ids: tuple[uuid.UUID, ...]


def _approved_keys(policy: SearchPolicy) -> list[str]:
    approved = policy.approved_paths or {}
    return [
        key
        for key in (approved.get("primary"), approved.get("secondary"), approved.get("stretch"))
        if key
    ]


def derive_query_families(
    owner_session: Session,
    *,
    tenant_ids: list[uuid.UUID] | None = None,
    registry: tuple[PathFamily, ...] = DEFAULT_FAMILIES,
) -> list[QueryFamily]:
    """Union the approved paths of ACTIVE tenants into query families.

    ``tenant_ids`` scopes the derivation (tests, targeted reruns); None means
    every tenant. Runs as the owner role — it spans tenants by design."""
    active_stmt = select(Subscription.tenant_id).where(
        Subscription.status == sub_states.ACTIVE
    )
    if tenant_ids is not None:
        active_stmt = active_stmt.where(Subscription.tenant_id.in_(tenant_ids))
    active_tenants = set(owner_session.execute(active_stmt).scalars().all())
    if not active_tenants:
        return []

    policies = owner_session.execute(
        select(SearchPolicy).where(
            SearchPolicy.tenant_id.in_(sorted(active_tenants, key=str)),
            SearchPolicy.status == "active",
        )
    ).scalars().all()

    by_key = {f.key: f for f in registry}
    members: dict[str, set[uuid.UUID]] = {}
    locations: dict[str, set[str]] = {}

    for policy in policies:
        cities = (policy.cities or {}).get("cities", [])
        tenant_locations = {
            _CITY_LOCATIONS[c] for c in cities if c in _CITY_LOCATIONS
        }
        tenant_locations.add(_COUNTRY)
        if (policy.remote_policy or "") in _REMOTE_POLICIES:
            tenant_locations.add(_REMOTE)

        for key in _approved_keys(policy):
            if key not in by_key:
                continue  # custom/unknown paths never invent queries
            members.setdefault(key, set()).add(policy.tenant_id)
            locations.setdefault(key, set()).update(tenant_locations)

    result: list[QueryFamily] = []
    for key in sorted(members):
        fam = by_key[key]
        result.append(
            QueryFamily(
                family=key,
                aliases=fam.query_aliases,
                locations=tuple(sorted(locations[key])),
                tenant_ids=tuple(sorted(members[key], key=str)),
            )
        )
    return result
