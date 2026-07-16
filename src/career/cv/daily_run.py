"""The daily delivery orchestration — engine result → CV → WhatsApp → close.

Composes only tested authorities: the C6 report's final lists, the §7.5
resolver + budget loop (every CV passes tailor→publish→binding), the grouped
adaptive delivery (C4), the suppression ledger, and the ONE honest day state.
Delivery days are Sunday–Thursday (§08) — the weekend is an honest skip.

Window semantics: a tenant whose 24h window is CLOSED gets the morning
template and a HELD bundle (C4). Their day closes when the bundle actually
descends — :func:`close_from_delivery` runs both from this orchestrator (for
terminal deliveries) and from the descend path, reading the counts snapshot
this module stamps into the bundle at build time.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.cv import close as close_mod
from career.cv import deliver, generate, publish
from career.cv.render import render_cv_pdf
from career.cv.schemas import ContactInfo, TailoredCV
from career.db.models import (
    CustomerChannel,
    CustomerProfile,
    Delivery,
    JobPosting,
    ProfileFact,
    Tenant,
    TenantDayState,
    TenantJobDecision,
)
from career.engine.families import _CITY_LOCATIONS
from career.engine.ranking import record_suppression_by_url
from career.engine.run import RunReport
from career.onboarding.confirmation import BANK_STATUSES
from career.storage import StorageAdapter
from career.telegram.admin import TelegramAdminClient
from career.whatsapp.client import WhatsAppClient
from career.whatsapp.delivery import (
    DELIVERY_COMPLETED,
    DELIVERY_PARTIAL,
    deliver_adaptive,
)
from career.whatsapp.templates import DAILY_UTILITY, TemplateSpec

logger = logging.getLogger("career.cv")

#: Saudi weekend — Friday (4) and Saturday (5) in Python weekday numbering,
#: computed in Asia/Riyadh explicitly (audit fix E: a UTC clock reads the
#: previous day between 00:00 and 03:00 Riyadh).
_WEEKEND = (4, 5)
_RIYADH = ZoneInfo("Asia/Riyadh")


@dataclass
class DailyDeps:
    storage: StorageAdapter
    whatsapp_client: WhatsAppClient
    admin_client: TelegramAdminClient
    llm: generate.LlmClient
    daily_template: TemplateSpec = field(default_factory=lambda: DAILY_UTILITY)


def _load_bank(session: Session, tenant_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    rows = session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.status.in_(BANK_STATUSES),
        )
    ).scalars().all()
    bank: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bank.setdefault(row.category, []).append(dict(row.payload or {}))
    return bank


def _contact_for(profile: CustomerProfile, channel: CustomerChannel) -> ContactInfo:
    location = _CITY_LOCATIONS.get(profile.city or "", "Saudi Arabia")
    return ContactInfo(
        name=profile.cv_full_name or "",
        email=profile.email or "",
        phone=channel.phone_e164,
        location=location,
        linkedin=profile.linkedin_url,
    )


def _inject_contact(cv: TailoredCV, contact: ContactInfo) -> TailoredCV:
    """§15.8: the real identity enters HERE, at assembly — nowhere earlier."""
    return cv.model_copy(update={
        "master_cv": cv.master_cv.model_copy(update={"contact": contact}),
    })


def close_from_delivery(
    session: Session,
    *,
    delivery: Delivery,
    now: datetime,
    suppressor: close_mod.Suppressor = record_suppression_by_url,
) -> TenantDayState | None:
    """Close the tenant's day from a TERMINAL delivery, using the counts
    snapshot stamped into the bundle at build time. Pending deliveries are
    not closable yet — the descend path calls this again when they land."""
    if delivery.status not in (DELIVERY_COMPLETED, DELIVERY_PARTIAL):
        return None
    snapshot = delivery.bundle.get("close") or {}
    results = delivery.bundle.get("results") or {}
    return close_mod.close_tenant_day(
        session,
        tenant_id=delivery.tenant_id,
        run_date=delivery.run_date,
        now=now,
        discovery_ok=True,
        gate_passes=int(snapshot.get("gate_passes", 0)),
        cv_resolved=int(snapshot.get("cv_resolved", 0)),
        cv_failed=int(snapshot.get("cv_failed", 0)),
        delivered_groups=list(results.get("delivered", [])),
        failed_groups=list(results.get("failed", [])),
        suppressor=suppressor,
    )


def _run_tenant(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payload: dict[str, Any],
    report: RunReport,
    deps: DailyDeps,
    now: datetime,
    generation_budget: int,
    renderer: Callable[..., Any],
    suppressor: close_mod.Suppressor,
) -> TenantDayState | None:
    run_date = now.astimezone(_RIYADH).date()
    discovery_ok = report.status != "discovery_failed"
    final = list(payload.get("final") or [])
    gate_passes = int((payload.get("counts") or {}).get("passed", len(final)))

    def _close(
        *, cv_resolved: int = 0, cv_failed: int = 0,
        delivered: list[str] | None = None, failed: list[str] | None = None,
    ) -> TenantDayState:
        return close_mod.close_tenant_day(
            session, tenant_id=tenant_id, run_date=run_date, now=now,
            discovery_ok=discovery_ok, gate_passes=gate_passes,
            cv_resolved=cv_resolved, cv_failed=cv_failed,
            delivered_groups=delivered or [], failed_groups=failed or [],
            suppressor=suppressor,
        )

    if not discovery_ok or not final:
        return _close()

    channel = session.execute(
        select(CustomerChannel).where(CustomerChannel.tenant_id == tenant_id)
    ).scalars().first()
    profile = session.execute(
        select(CustomerProfile).where(CustomerProfile.tenant_id == tenant_id)
    ).scalars().first()
    if channel is None or profile is None:
        logger.error("tenant lacks channel/profile — nothing sendable")
        return _close(cv_resolved=len(final),
                      failed=[str(i.get("url")) for i in final])

    bank = _load_bank(session, tenant_id)
    contact = _contact_for(profile, channel)

    jobs: list[dict[str, Any]] = []
    for item in final:
        posting = session.get(JobPosting, uuid.UUID(str(item["posting_id"])))
        if posting is None:
            continue
        decision = session.execute(
            select(TenantJobDecision).where(
                TenantJobDecision.tenant_id == tenant_id,
                TenantJobDecision.run_id == report.run_id,
                TenantJobDecision.job_posting_id == posting.id,
            )
        ).scalars().first()
        jobs.append({
            "url": posting.url,
            "title": posting.title,
            "company": posting.company,
            "location": posting.location,
            "jd_text": posting.jd_snippet or posting.description_snippet or "",
            "reasons": dict(decision.reasons) if decision else {},
            "portal": (posting.route or {}).get("route_type"),
            "cv_attachment_status": "no_tailored_cv",
        })

    def _resolve(job: dict[str, Any]) -> publish.ResolveResult:
        def _generator() -> dict[str, str]:
            tailored = generate.tailor_cv(
                deps.llm, bank=bank,
                current_title=profile.current_title,
                years_experience=profile.years_experience,
                job_title=str(job["title"]), company=str(job["company"]),
                jd_text=str(job["jd_text"]),
            )
            # audit fix D: the LLM spend happened HERE — record it before
            # publish/render can fail, or the §14 cost fuel undercounts.
            close_mod.record_usage(
                session, tenant_id=tenant_id, kind="llm_generation",
                run_id=report.run_id, now=now,
            )
            return publish.publish_cv_pair(
                deps.storage, tenant_id=str(tenant_id),
                cv=_inject_contact(tailored, contact),
                job_url=str(job["url"]), location=job.get("location"),
                portal=job.get("portal"), job_analysis={}, now=now,
                renderer=renderer,
            )

        return publish.resolve_tailored_cv(
            deps.storage, tenant_id=str(tenant_id), job_url=str(job["url"]),
            generation_enabled=True, dry_run=False, contact=contact,
            generator=_generator,
        )

    processed = publish.generate_missing_cvs(
        jobs, resolve=_resolve, budget=generation_budget
    )
    resolved = [j for j in processed if j.get("cv_key")]
    cv_failed = len(processed) - len(resolved)
    if not resolved:
        return _close(cv_resolved=0, cv_failed=cv_failed)

    bundle, bundle_failures = deliver.build_daily_bundle(
        resolved, customer_name=profile.cv_full_name or ""
    )
    bundle["close"] = {
        "gate_passes": gate_passes,
        "cv_resolved": len(resolved),
        "cv_failed": cv_failed + len(bundle_failures),
    }
    delivery = deliver_adaptive(
        session, channel, bundle, run_date=run_date,
        whatsapp_client=deps.whatsapp_client,
        daily_template=deps.daily_template, now=now,
    )
    state = close_from_delivery(
        session, delivery=delivery, now=now, suppressor=suppressor
    )
    return state  # None ⇒ held for the window; the descend path closes it


def run_daily_delivery(
    session: Session,
    *,
    report: RunReport,
    deps: DailyDeps,
    now: datetime,
    generation_budget: int = publish.MAX_GENERATIONS_PER_RUN,
    renderer: Callable[..., Any] = render_cv_pdf,
    suppressor: close_mod.Suppressor = record_suppression_by_url,
) -> dict[uuid.UUID, TenantDayState]:
    """One delivery day. Tenants are isolated — one tenant's crash never
    touches the others; the admin summary reports every closed tenant."""
    riyadh_now = now.astimezone(_RIYADH)
    if riyadh_now.weekday() in _WEEKEND:
        logger.info("weekend — no delivery day (§08)")
        return {}

    states: dict[uuid.UUID, TenantDayState] = {}
    for tenant_id, payload in report.per_tenant.items():
        try:
            state = _run_tenant(
                session, tenant_id=tenant_id, payload=payload, report=report,
                deps=deps, now=now, generation_budget=generation_budget,
                renderer=renderer, suppressor=suppressor,
            )
        except Exception:  # noqa: BLE001 — tenant isolation is the contract
            logger.error("tenant delivery crashed", exc_info=True)
            continue
        if state is not None:
            states[tenant_id] = state

    rows: list[tuple[str, str, dict[str, int]]] = []
    for tenant_id, state in states.items():
        tenant = session.get(Tenant, tenant_id)
        code = tenant.code if tenant else str(tenant_id)
        rows.append((code, state.state, dict(state.counts)))
    try:
        deps.admin_client.send_admin(
            close_mod.format_admin_summary(now.date(), rows)
        )
    except Exception:  # noqa: BLE001 — the summary must never break the day
        logger.warning("admin summary send failed", exc_info=True)
    return states
