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
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.cv import close as close_mod
from career.cv import deliver, generate, publish
from career.cv.render import render_cv_pdf
from career.cv.schemas import ContactInfo, TailoredCV
from career.db.models import (
    CustomerChannel,
    CustomerProfile,
    Delivery,
    ForbiddenClaim,
    JobPosting,
    OnboardingSession,
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
    DELIVERY_PENDING,
    deliver_adaptive,
    record_out,
)
from career.whatsapp.templates import DAILY_UTILITY, TemplateSpec
from career.whatsapp.window import WindowState, window_state

logger = logging.getLogger("career.cv")

#: A held bundle whose window never opened before the NEXT run day — the day
#: must still close honestly instead of silently never existing (§15.12).
DELIVERY_EXPIRED = "EXPIRED_WINDOW"

#: claude-opus-4-8 list prices (USD per token) — the §14 cost fuel.
_LLM_USD_PER_INPUT_TOKEN = Decimal("0.000005")
_LLM_USD_PER_OUTPUT_TOKEN = Decimal("0.000025")


def _llm_cost_usd(input_tokens: int, output_tokens: int) -> Decimal | None:
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    return (
        _LLM_USD_PER_INPUT_TOKEN * input_tokens
        + _LLM_USD_PER_OUTPUT_TOKEN * output_tokens
    )

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
    #: F-ENRICH icebreaker writer (optional — None skips the examples menu).
    examples_writer: Any = None


def _load_bank(session: Session, tenant_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    rows = session.execute(
        select(ProfileFact).where(
            ProfileFact.tenant_id == tenant_id,
            ProfileFact.status.in_(BANK_STATUSES),
        )
    ).scalars().all()
    bank: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row.payload or {})
        # inject the fact id so F-ENRICH conversation achievements can be
        # folded into their parent role (normalize.build_master_cv).
        payload["_fact_id"] = str(row.id)
        bank.setdefault(row.category, []).append(payload)
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


_ZERO_DAY_AR = (
    "🔍 بحثنا اليوم ولم نجد فرصًا تطابق معاييرك — هذا طبيعي في بعض الأيام.\n"
    "بحث الغد يبدأ تلقائيًا، ومعاييرك كما هي."
)


def _send_zero_day(
    session: Session, *, tenant_id: uuid.UUID, deps: DailyDeps, now: datetime
) -> None:
    """Best-effort §08 zero-day note — never affects the day's state."""
    try:
        channel = session.execute(
            select(CustomerChannel).where(
                CustomerChannel.tenant_id == tenant_id
            )
        ).scalars().first()
        if channel is None:
            return
        state = window_state(
            last_inbound_at=channel.last_inbound_at,
            opt_out_at=channel.opt_out_at, now=now,
        )
        if state is not WindowState.OPEN:
            return
        mid = deps.whatsapp_client.send_text(channel.phone_e164, _ZERO_DAY_AR)
        record_out(session, tenant_id=tenant_id, channel_id=channel.id,
                   kind="text", wa_message_id=mid, now=now)
    except Exception:  # noqa: BLE001
        logger.warning("zero-day note failed", exc_info=True)



class MonthlyCapBudget:
    """AUDIT ك-11: the plan's monthly_cv_safety_cap, finally read and
    enforced. Counts this month's llm_generation usage events for the tenant;
    at/over the cap, generation is blocked (the honest fallback CV path still
    runs — delivery never silently dies)."""

    def __init__(self, session: Session, *, tenant_id: uuid.UUID,
                 cap: int, now: datetime) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._cap = int(cap)
        self._now = now

    def allow(self) -> tuple[bool, str | None]:
        from career.db.models import UsageEvent

        month_start = self._now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        used = self._session.execute(
            select(func.count()).select_from(UsageEvent).where(
                UsageEvent.tenant_id == self._tenant_id,
                UsageEvent.kind == "llm_generation",
                UsageEvent.occurred_at >= month_start,
            )
        ).scalar_one()
        if used >= self._cap:
            return False, f"monthly_cv_safety_cap reached ({used}/{self._cap})"
        return True, None


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
    counts_in = payload.get("counts") or {}
    raw_passed = int(counts_in.get("passed", len(final)))
    suppressed = int(counts_in.get("suppressed", 0))
    # audit fix: gate_passes = FRESH passes only. A day where every pass was
    # already delivered (suppressed) is honestly NO_MATCHES — nothing failed,
    # the jobs are simply repeats within the TTL (§15.12).
    gate_passes = max(raw_passed - suppressed, len(final))

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
        state = _close()
        # §08 zero-day report: an honest «لا فرص اليوم» reaches the customer
        # when the window is open (free-form); a closed window stays silent —
        # the state row still records the day either way.
        if state.state == "NO_MATCHES":
            _send_zero_day(session, tenant_id=tenant_id, deps=deps, now=now)
        return state

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
    forbidden = [
        claim for (claim,) in session.execute(
            select(ForbiddenClaim.claim).where(
                ForbiddenClaim.tenant_id == tenant_id
            )
        ).all()
    ]

    # AUDIT ك-11: the plan's monthly safety cap, read from entitlements —
    # no entitlement row ⇒ no cap (None), never an invented number.
    from career.db.models import PlanEntitlement

    # The cap belongs to the plan the customer is SERVED under. Joining on
    # «newest row» was right only by accident of insert order — a second
    # analysis purchase (cap 1) would have throttled a pass customer.
    from career.salla.renewal import current_subscription

    served = current_subscription(session, tenant_id)
    cap_row = session.execute(
        select(PlanEntitlement.monthly_cv_safety_cap).where(
            PlanEntitlement.plan_code == served.plan_code
        )
    ).scalars().first() if served is not None else None
    monthly_budget = (
        MonthlyCapBudget(session, tenant_id=tenant_id, cap=cap_row, now=now)
        if cap_row is not None else None
    )

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
            in_before = int(getattr(deps.llm, "total_input_tokens", 0) or 0)
            out_before = int(getattr(deps.llm, "total_output_tokens", 0) or 0)
            tailored = generate.tailor_cv(
                deps.llm, bank=bank,
                current_title=profile.current_title,
                years_experience=profile.years_experience,
                job_title=str(job["title"]), company=str(job["company"]),
                jd_text=str(job["jd_text"]),
                budget=monthly_budget,
                forbidden_claims=forbidden,
            )
            # audit fix D: the LLM spend happened HERE — record it before
            # publish/render can fail, or the §14 cost fuel undercounts.
            in_used = int(getattr(deps.llm, "total_input_tokens", 0) or 0) - in_before
            out_used = int(getattr(deps.llm, "total_output_tokens", 0) or 0) - out_before
            close_mod.record_usage(
                session, tenant_id=tenant_id, kind="llm_generation",
                run_id=report.run_id, now=now,
                input_tokens=in_used or None,
                output_tokens=out_used or None,
                cost_usd=_llm_cost_usd(in_used, out_used),
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
    try:
        delivery = deliver_adaptive(
            session, channel, bundle, run_date=run_date,
            whatsapp_client=deps.whatsapp_client,
            daily_template=deps.daily_template, now=now,
        )
    except Exception as exc:  # noqa: BLE001 — a send-path crash (e.g. the
        # morning template still PENDING at Meta) must close the day
        # honestly, not vanish into a crash-skip (§15.12: no silent states).
        # The reason goes on the ERROR line itself: the operator's feed
        # harvests «ERROR:» lines only, so a Meta code buried in a traceback
        # continuation never reached them — five closed-window days failed
        # with no diagnostic anyone could see (closure audit).
        logger.error("delivery send path crashed — closing WHATSAPP_FAILED: %s",
                     exc, exc_info=True)
        return _close(
            cv_resolved=len(resolved), cv_failed=cv_failed + len(bundle_failures),
            failed=[str(e.get("group")) for e in bundle.get("jobs", [])],
        )
    from career.whatsapp.delivery import DELIVERY_NO_SEND
    if delivery.status == DELIVERY_NO_SEND:
        # CHANGELOG §12: opted-out with matches — the eighth honest state
        return close_mod.close_skipped_opted_out(
            session, tenant_id=tenant_id, run_date=run_date, now=now,
            gate_passes=gate_passes,
        )
    day_state = close_from_delivery(
        session, delivery=delivery, now=now, suppressor=suppressor
    )
    # F-ENRICH (§13): if delivery landed and a role is thin, arm ONE
    # enrichment nudge — the opening message is sent by the conversation
    # worker (open window), never here, and never affects the day's state.
    if day_state is not None and day_state.state in ("DELIVERED", "PARTIAL_DELIVERY"):
        _maybe_arm_enrichment(session, tenant_id=tenant_id, deps=deps, now=now)
    return day_state  # None ⇒ held; the descend path closes it later


def _maybe_arm_enrichment(
    session: Session, *, tenant_id: uuid.UUID, deps: DailyDeps, now: datetime
) -> None:
    """Detect a thin role and, when the customer's window is OPEN (they just
    received today's delivery, so it usually is), arm a once-ever nudge and
    send the opening Saudi-colloquial question. Best-effort; never raises into
    the delivery path, never blocks it."""
    try:
        from career.onboarding import enrichment as enr

        thin = enr.thin_roles(session, tenant_id=tenant_id)
        if not thin:
            return
        channel = session.execute(
            select(CustomerChannel).where(CustomerChannel.tenant_id == tenant_id)
        ).scalars().first()
        if channel is None or window_state(
            last_inbound_at=channel.last_inbound_at,
            opt_out_at=channel.opt_out_at, now=now,
        ) is not WindowState.OPEN:
            return  # closed window → a future open-window run catches it
        journey = session.execute(
            select(OnboardingSession).where(
                OnboardingSession.tenant_id == tenant_id,
                OnboardingSession.state == "ACTIVE",
            )
        ).scalars().first()
        if journey is None:
            return
        context = dict(journey.context or {})
        role = thin[0]
        if enr.enqueue_enrichment(
            session, tenant_id=tenant_id, role_fact_id=role.id,
            journey_context=context, trigger="lazy_generation", now=now,
        ):
            # icebreaker examples (D14) — optional garnish, scrubbed of any
            # digit/foreign-Latin so a pick can never inject a false atom
            examples = enr.prepare_examples(
                session, role_fact_id=role.id, writer=deps.examples_writer
            )
            if examples:
                context["enrichment"]["examples"] = examples
            journey.context = context
            mid = deps.whatsapp_client.send_interactive(
                channel.phone_e164,
                enr.opening_message(session, role_fact_id=role.id),
                enr.OPENING_BUTTONS,
            )
            record_out(session, tenant_id=tenant_id, channel_id=channel.id,
                       kind="interactive", wa_message_id=mid, now=now)
            if examples:
                from career.onboarding.achievement_render import (
                    format_examples_message,
                )

                mid2 = deps.whatsapp_client.send_text(
                    channel.phone_e164, format_examples_message(examples)
                )
                record_out(session, tenant_id=tenant_id,
                           channel_id=channel.id, kind="text",
                           wa_message_id=mid2, now=now)
            session.flush()
    except Exception:  # noqa: BLE001 — enrichment never breaks delivery
        logger.warning("enrichment arm failed", exc_info=True)


def expire_stale_held_deliveries(
    session: Session,
    *,
    now: datetime,
    suppressor: close_mod.Suppressor = record_suppression_by_url,
) -> int:
    """Audit fix (§15.12 gap): a PENDING_WINDOW bundle from a PREVIOUS run
    day whose customer never opened the window would otherwise leave that day
    with no state at all. Expire it and close the day honestly — nothing was
    delivered, so the state authority yields WHATSAPP_FAILED. The descend
    path no longer matches the row (status left PENDING only for today)."""
    today = now.astimezone(_RIYADH).date()
    stale = session.execute(
        select(Delivery).where(
            Delivery.status == DELIVERY_PENDING,
            Delivery.run_date < today,
        )
    ).scalars().all()
    for delivery in stale:
        delivery.status = DELIVERY_EXPIRED
        snapshot = delivery.bundle.get("close") or {}
        groups = [
            str(e.get("group")) for e in delivery.bundle.get("jobs", [])
        ]
        close_mod.close_tenant_day(
            session,
            tenant_id=delivery.tenant_id,
            run_date=delivery.run_date,
            now=now,
            discovery_ok=True,
            gate_passes=int(snapshot.get("gate_passes", len(groups))),
            cv_resolved=int(snapshot.get("cv_resolved", len(groups))),
            cv_failed=int(snapshot.get("cv_failed", 0)),
            delivered_groups=[],
            failed_groups=groups,
            suppressor=suppressor,
        )
        try:
            # AUDIT ك-15: expired days carried real usage (generation) that
            # never reached cost_allocations — roll it up here too (§14).
            close_mod.rollup_costs(
                session, tenant_id=delivery.tenant_id, day=delivery.run_date
            )
        except Exception:  # noqa: BLE001 — accounting never blocks expiry
            logger.warning("expiry cost rollup failed", exc_info=True)
    return len(stale)


def run_daily_delivery(
    session: Session,
    *,
    report: RunReport,
    deps: DailyDeps,
    now: datetime,
    generation_budget: int = publish.MAX_GENERATIONS_PER_RUN,
    renderer: Callable[..., Any] = render_cv_pdf,
    suppressor: close_mod.Suppressor = record_suppression_by_url,
    include_weekend: bool = False,
    canary_tenant_id: uuid.UUID | None = None,
    canary_delay_seconds: float = 0.0,
    sleeper: Callable[[float], None] | None = None,
) -> dict[uuid.UUID, TenantDayState]:
    """One delivery day. Tenants are isolated — one tenant's crash never
    touches the others; the admin summary reports every closed tenant.
    ``include_weekend`` exists for manual canary runs only — the timer
    never sets it (§08: Sunday–Thursday)."""
    expired = expire_stale_held_deliveries(session, suppressor=suppressor, now=now)
    if expired:
        try:
            deps.admin_client.send_admin(
                f"⌛ أُغلقت {expired} تسليمة معلقة من يوم سابق — "
                "النافذة لم تُفتح (WHATSAPP_FAILED)"
            )
        except Exception:  # noqa: BLE001 — reporting never breaks the day
            logger.warning("expiry admin note failed", exc_info=True)

    if not include_weekend and now.astimezone(_RIYADH).weekday() in _WEEKEND:
        logger.info("weekend — no delivery day (§08)")
        return {}

    # §14 canary ordering: the operator's tenant runs FIRST; when real
    # customers follow, an hour-class delay separates them so a bad morning
    # is caught on the canary before it reaches anyone else.
    ordered = sorted(
        report.per_tenant.items(),
        key=lambda kv: (kv[0] != canary_tenant_id,),
    )
    canary_present = canary_tenant_id in report.per_tenant

    states: dict[uuid.UUID, TenantDayState] = {}
    for index, (tenant_id, payload) in enumerate(ordered):
        if (index == 1 and canary_present and canary_delay_seconds > 0):
            (sleeper or __import__("time").sleep)(canary_delay_seconds)
        try:
            state = _run_tenant(
                session, tenant_id=tenant_id, payload=payload, report=report,
                deps=deps, now=now, generation_budget=generation_budget,
                renderer=renderer, suppressor=suppressor,
            )
        except Exception:  # noqa: BLE001 — tenant isolation is the contract
            logger.error("tenant delivery crashed", exc_info=True)
            # AUDIT ح-1/ك-10: roll back ONLY this tenant's uncommitted work
            # (earlier tenants are already committed), then close the day
            # honestly — a crashed pipeline is CV_GENERATION_FAILED, never a
            # silent no-state day (§15.12).
            session.rollback()
            try:
                counts_in = payload.get("counts") or {}
                passes = int(counts_in.get("passed",
                                           len(payload.get("final") or [])))
                fallback = close_mod.close_tenant_day(
                    session, tenant_id=tenant_id,
                    run_date=now.astimezone(_RIYADH).date(), now=now,
                    discovery_ok=True, gate_passes=passes,
                    cv_resolved=0, cv_failed=max(passes, 1),
                    delivered_groups=[], failed_groups=[],
                    suppressor=suppressor,
                )
                states[tenant_id] = fallback
                session.commit()
            except Exception:  # noqa: BLE001 — fallback must not cascade
                logger.error("fallback day close failed", exc_info=True)
                session.rollback()
            continue
        if state is not None:
            states[tenant_id] = state
            try:
                # §14: fold the day's raw usage events into the cost rollup
                close_mod.rollup_costs(
                    session, tenant_id=tenant_id, day=state.run_date
                )
            except Exception:  # noqa: BLE001 — accounting never breaks the day
                logger.warning("cost rollup failed", exc_info=True)
        # ح-1: DURABILITY POINT — WhatsApp messages for this tenant are
        # already on the wire; their delivery rows, ledger and day state
        # must survive any later crash (and the canary hour-long sleep no
        # longer holds an open transaction). §15.3/§15.12.
        session.commit()

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
