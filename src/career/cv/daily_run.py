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
from datetime import date, datetime
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
    LedgerLost,
    anything_landed,
    deliver_adaptive,
    durability_point,
    record_out,
)
from career.whatsapp.templates import DAILY_UTILITY, TemplateSpec
from career.whatsapp.window import WindowState, window_state

logger = logging.getLogger("career.cv")

#: A held bundle whose window never opened before the NEXT run day — the day
#: must still close honestly instead of silently never existing (§15.12).
DELIVERY_EXPIRED = "EXPIRED_WINDOW"

#: Saudi weekend — Friday (4) and Saturday (5) in Python weekday numbering,
#: computed in Asia/Riyadh explicitly (audit fix E: a UTC clock reads the
#: previous day between 00:00 and 03:00 Riyadh).
_WEEKEND = (4, 5)
_RIYADH = ZoneInfo("Asia/Riyadh")

#: §16, the operator channel only. Fahad's client reverses any line that mixes
#: Arabic with Latin letters or European digits, and a COUNT is the one thing
#: that genuinely belongs inside the Arabic sentence rather than on a line of
#: its own — «أُغلقت ٣ تسليمة» reads at a glance where «أُغلقت\n3\nتسليمة»
#: does not. Arabic-Indic digits are Arabic script, so the line stays pure.
#: Deliberately local rather than imported from `telegram.console._ar_digits`:
#: the delivery engine does not depend on the operator console, and the bidi
#: guard proves a helper by its shape (a module-level table whose output
#: alphabet is Arabic-Indic), never by its name — so a second one is expected.
_WESTERN_TO_ARABIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _ar_digits(value: object) -> str:
    """A number that can live INSIDE an Arabic line without scrambling it."""
    return str(value).translate(_WESTERN_TO_ARABIC)


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


def _already_delivered_today(
    session: Session, *, tenant_id: uuid.UUID, run_date: date,
) -> bool:
    """Has this tenant's day already ended in something they RECEIVED?"""
    state = session.execute(
        select(TenantDayState.state).where(
            TenantDayState.tenant_id == tenant_id,
            TenantDayState.run_date == run_date,
        )
    ).scalars().first()
    # DELIVERED only. A PARTIAL day is precisely the one the operator re-runs
    # to finish: a customer who paid for two jobs and received one must not be
    # skipped for the rest of the day, which is what treating PARTIAL as
    # «already received» did — it removed the recovery it was written to
    # protect and dropped the tenant from the run report entirely.
    return state == "DELIVERED"


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
    delivered = [str(g) for g in (results.get("delivered") or [])]
    failed = [str(g) for g in (results.get("failed") or [])]
    # A bundle carrying no counts snapshot (an older row, or one built outside
    # this orchestrator) used to read gate_passes=0 — and the state authority
    # answers NO_MATCHES for zero passes, so a day that actually DELIVERED
    # jobs would have closed as «لا فرص مطابقة»: flattering and false. The
    # groups in the bundle are the floor: each one is a gate pass whose CV was
    # resolved, or it would never have been in the bundle at all (§15.12).
    in_bundle = len(delivered) + len(failed)
    return close_mod.close_tenant_day(
        session,
        tenant_id=delivery.tenant_id,
        run_date=delivery.run_date,
        now=now,
        discovery_ok=True,
        gate_passes=max(int(snapshot.get("gate_passes", 0)), in_bundle),
        cv_resolved=max(int(snapshot.get("cv_resolved", 0)), in_bundle),
        cv_failed=int(snapshot.get("cv_failed", 0)),
        delivered_groups=delivered,
        failed_groups=failed,
        suppressor=suppressor,
    )


@dataclass
class TenantProgress:
    """What one tenant's run knows so far — readable AFTER it crashes.

    CHANGELOG 39: the failure path used to close the day with
    ``cv_resolved=0`` and ``cv_failed=max(passes, 1)`` because the ``except``
    lives one frame above :func:`_run_tenant` and every local it knew died
    with the frame. Those were not stale numbers, they were MANUFACTURED ones:
    a zero hard-coded next to a `max()` that cannot return zero. On 10 August
    they wrote ``cv_resolved=0, cv_failed=6, delivered=0`` for a night that
    generated one CV, delivered it, and had it read — and six was the number
    of jobs that PASSED the gate, not the number that failed.

    So the facts leave the frame as they become true, in an object the CALLER
    owns. Nothing here is a prediction: each field is written at the moment it
    stops being one, which is why the crash path can use them without knowing
    where the crash happened.

    ``failed`` holds «resolved CVs that have NOT reached the customer», and it
    starts full for that reason — before the send, every resolved CV is an
    unsent one. Delivery moves entries out of it. Without that the state
    authority would read ``cv_resolved=N, delivered=0, failed_sends=0`` for a
    crash between resolution and the send and answer DELIVERED, which is the
    same class of lie in the opposite direction.

    ``landed`` is the ONE thing the day state hinges on: whether any message
    reached the customer in this run. It is what separates «the ledger failed»
    from «the pipeline failed before there was anything to record».
    """

    gate_passes: int = 0
    cv_resolved: int = 0
    cv_failed: int = 0
    delivered: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    landed: bool = False


def _close_after_crash(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    run_date: date,
    now: datetime,
    progress: TenantProgress,
) -> TenantDayState:
    """Close a tenant's day after :func:`_run_tenant` raised — with the counts
    that were true when it did, and never with invented ones.

    THE STATE. ``ledger_ok = not progress.landed``, and that one line is the
    whole decision. If nothing reached the customer, the crash is upstream of
    delivery and the state authority reads the real counts and answers
    honestly (no gate passes → NO_MATCHES, no resolved CV → CV_GENERATION_
    FAILED, resolved but unsent → WHATSAPP_FAILED). If something DID reach
    them, then the sends succeeded and the only thing that can still have gone
    wrong is the recording of the day — so `LEDGER_FAILED`, which
    :func:`close.daily_state` asks before every other question and whose
    module docstring already says a ledger-write failure IS the honest state
    rather than an exception. CHANGELOG 39: no ninth state; the missing thing
    was the ROUTE to the seventh, not the seventh.

    A row saying ``LEDGER_FAILED`` beside ``delivered: 1`` is something the
    operator can act on. ``CV_GENERATION_FAILED`` beside ``delivered: 0`` sent
    him searching a generation pipeline that had worked perfectly.

    WHY THIS DOES NOT CALL `close.close_tenant_day`. That function derives
    ``ledger_ok`` from ONE source — whether its own suppression loop raised —
    and has no parameter for «the ledger already failed upstream», so it can
    only ever answer a state that claims the delivery was recorded. The tidier
    home for this is a ``ledger_ok: bool = True`` argument on it, folded into
    its own result with ``and``; that is an edit to `cv/close.py` and belongs
    to its owner. Until then the composition is explicit and goes through the
    same single door every other writer uses — :func:`close._record_day_state`
    is the ONE writer of the table, so the monotonic rule that protects a real
    DELIVERED day from being overwritten still applies here for free.

    NO SUPPRESSION IS WRITTEN, deliberately, and it is a real cost rather than
    an omission: the delivered jobs stay unsuppressed, so tomorrow's gate can
    offer them again. Writing them here was rejected because the alternative is
    worse in a way that is hard to see — a suppressed LEDGER_FAILED day makes
    the operator's re-run find nothing, close NO_MATCHES, and OVERWRITE the
    alarm (NO_MATCHES is not protected by :func:`close._outranks`). A duplicate
    send is visible to one customer; a silently cleared alarm is invisible to
    everybody. The re-run is the recovery, so it is kept possible.
    """
    ledger_ok = not progress.landed
    state_value = close_mod.daily_state(
        ledger_ok=ledger_ok,
        discovery_ok=True,
        gate_passes=progress.gate_passes,
        cv_resolved=progress.cv_resolved,
        cv_failed=progress.cv_failed,
        delivered=len(progress.delivered),
        failed_sends=len(progress.failed),
    )
    counts = {
        "gate_passes": progress.gate_passes,
        "cv_resolved": progress.cv_resolved,
        "cv_failed": progress.cv_failed,
        "delivered": len(progress.delivered),
        "failed_sends": len(progress.failed),
    }
    return close_mod._record_day_state(
        session, tenant_id=tenant_id, run_date=run_date,
        state=state_value, counts=counts, now=now,
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
    progress: TenantProgress | None = None,
) -> TenantDayState | None:
    # Optional so the signature stays callable from a test or a future caller
    # that does not care; the orchestrator always passes one, because the
    # crash path is exactly the caller that cannot ask this frame anything.
    progress = progress if progress is not None else TenantProgress()
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
    progress.gate_passes = gate_passes

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
            # audit fix D: the LLM spend happens HERE — the meter's `finally`
            # records it even when tailoring raises, so a blocked CV can never
            # leave its real token spend uncounted (§14).
            meter = close_mod.LlmMeter(
                session, tenant_id=tenant_id, now=now, run_id=report.run_id
            )
            # always=True: MonthlyCapBudget (ك-11) counts these rows, so the
            # event must exist even if the transport reported no usage block
            with meter.around("llm_generation", deps.llm, always=True):
                tailored = generate.tailor_cv(
                    deps.llm, bank=bank,
                    current_title=profile.current_title,
                    years_experience=profile.years_experience,
                    job_title=str(job["title"]), company=str(job["company"]),
                    jd_text=str(job["jd_text"]),
                    budget=monthly_budget,
                    forbidden_claims=forbidden,
                    # §15.8: the bank can carry the customer's own words
                    # verbatim (a free-text onboarding answer becomes an
                    # experience title), so their name is stripped too — not
                    # just the mechanical emails and phone numbers.
                    known_name=profile.cv_full_name,
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
            # the RUN's clock, not the wall clock. resolve_tailored_cv only
            # uses it to stamp the quarantine directory, and that stamp is
            # forensic: when a night is re-run, or the process crosses
            # midnight between the run's `now` and the moment a broken pair is
            # quarantined, a wall-clock default files the evidence under a day
            # the run report never mentions — so the one artefact of a failed
            # CV sits in a directory nobody looking at that night would open.
            now=now,
        )

    processed = publish.generate_missing_cvs(
        jobs, resolve=_resolve, budget=generation_budget
    )
    resolved = [j for j in processed if j.get("cv_key")]
    cv_failed = len(processed) - len(resolved)
    progress.cv_resolved = len(resolved)
    progress.cv_failed = cv_failed
    # Every resolved CV is an UNSENT one until a send says otherwise — see
    # TenantProgress.failed for why the pessimistic start is the honest one.
    progress.failed = [str(j.get("url")) for j in resolved]
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
    progress.cv_failed = cv_failed + len(bundle_failures)
    progress.failed = [str(e.get("group")) for e in bundle.get("jobs", [])]
    try:
        delivery = deliver_adaptive(
            session, channel, bundle, run_date=run_date,
            whatsapp_client=deps.whatsapp_client,
            daily_template=deps.daily_template, now=now,
        )
    except LedgerLost as lost:
        # Caught BEFORE the generic handler below, and re-raised rather than
        # closed here. The messages landed, so «WHATSAPP_FAILED» would be
        # false about the one thing that worked; the honest close for a lost
        # ledger is the orchestrator's crash path, which has the tenant's
        # session and writes LEDGER_FAILED with exactly these counts.
        progress.landed = lost.landed
        progress.delivered = lost.delivered
        progress.failed = lost.failed
        raise
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
    # What actually reached the customer, read off the delivery before any
    # further work can raise and take the answer with it.
    results = delivery.bundle.get("results") or {}
    progress.delivered = [str(g) for g in (results.get("delivered") or [])]
    progress.failed = [str(g) for g in (results.get("failed") or [])]
    # NOT «the delivered list is non-empty» — a held template and a generic
    # parts bundle both land without delivering a group. One rule, next to
    # the statuses it reads (:func:`delivery.anything_landed`).
    progress.landed = anything_landed(delivery)
    # THE DURABILITY POINT (CHANGELOG 39). `close_from_delivery` writes the
    # suppression ledger and flushes the day state — the first work after the
    # last send that can raise, and on 10 August it did, on a column the host
    # database did not have. Everything the customer has already received is
    # committed here so that raise can only cost the day's bookkeeping.
    #
    # It is redundant TODAY — `deliver_adaptive` commits at its own durability
    # point and nothing between the two writes — and it is kept anyway,
    # because the redundancy ends the moment anybody inserts a line above it,
    # and that line is exactly how the first hole was dug.
    if not durability_point(session, what="the day's delivery"):
        raise LedgerLost(delivered=progress.delivered, failed=progress.failed,
                         landed=progress.landed)
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
) -> list[TenantDayState]:
    """Audit fix (§15.12 gap): a PENDING_WINDOW bundle from a PREVIOUS run
    day whose customer never opened the window would otherwise leave that day
    with no state at all. Expire it and close the day honestly — nothing was
    delivered, so the state authority yields WHATSAPP_FAILED. The descend
    path no longer matches the row (status left PENDING only for today).

    RETURNS THE DAY STATES IT CLOSED, and that is the whole point of the
    change. This function used to return a COUNT, and a count is not something
    the night's verdict can be computed from: the tenants it fails never enter
    ``run_daily_delivery``'s ``states``, so they never reached the run
    summary and ``exit_code_for`` never saw them. Four real nights closed
    WHATSAPP_FAILED from exactly here — 21, 22 and 23 July and 2 August 2026,
    each one written by the NEXT morning's sweep (the day states are stamped
    at 01:3x UTC, the sweep's hour, not the failed day's) — and every one of
    those runs exited 0 with ``OnFailure`` silent. The exit-code fix landed in
    the same commit and did not close its own incident, because it was wired
    to the only channel this path never used.
    """
    today = now.astimezone(_RIYADH).date()
    stale = session.execute(
        select(Delivery).where(
            Delivery.status == DELIVERY_PENDING,
            Delivery.run_date < today,
        )
    ).scalars().all()
    closed: list[TenantDayState] = []
    for delivery in stale:
        delivery.status = DELIVERY_EXPIRED
        snapshot = delivery.bundle.get("close") or {}
        groups = [
            str(e.get("group")) for e in delivery.bundle.get("jobs", [])
        ]
        state = close_mod.close_tenant_day(
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
        if state is not None:
            closed.append(state)
        try:
            # AUDIT ك-15: expired days carried real usage (generation) that
            # never reached cost_allocations — roll it up here too (§14).
            close_mod.rollup_costs(
                session, tenant_id=delivery.tenant_id, day=delivery.run_date
            )
        except Exception:  # noqa: BLE001 — accounting never blocks expiry
            logger.error("expiry cost rollup failed", exc_info=True)
    return closed


def sweep_promises(
    session: Session, *, deps: DailyDeps, now: datetime
) -> dict[str, int]:
    """The two store promises whose clocks do not observe our delivery week.

    The 72-hour start guarantee is written in flat hours («خلال ٧٢ ساعة من
    تفعيل اشتراكك») and the لمّاح+ session SLA in flat hours too («والرد خلال
    ٢٤ ساعة»), while §08 delivers Sunday–Thursday. So both are swept from HERE
    — above the weekend early return in :func:`run_daily_delivery` — because a
    guarantee that runs out on a Friday has to be noticed on Friday, not found
    on Sunday with the operator two days late to a conversation about money.

    Riding the nightly rather than a timer of their own was chosen for the
    reason 0023's weekly-report marker exists: another systemd unit is another
    thing that can be down without anybody noticing, and this one already
    runs, already has an admin client, and already fails loudly when it does
    not. That reasoning is still right about the UNIT and was wrong about the
    HOUR, which nothing here stated: this run fires at 11:00 Riyadh, so a
    promise breaking at 12:05 waited until 11:00 the next morning — a day of
    lateness on clocks written in hours.

    BOTH halves therefore run HOURLY now, from the conversation worker's
    housekeeping block, and BOTH of these calls are the BACKSTOP rather than
    the schedule — for the hour the worker is not there (a wedged cycle, a
    deploy, a host that came back without it). The cadence arguments, and why
    two callers can never double-apply or double-page, are written out in
    :func:`career.promises.guarantee.sweep_and_commit` and
    :func:`career.promises.career_session.escalate_overdue_and_commit`.

    (This docstring said until 2026-08-08 that «the session SLA still has this
    caller only, and still carries the same ~23h worst case». It was false the
    moment it was written — the same wave wired the hourly caller, and
    `escalate_overdue`'s own docstring said so — and it was false about a
    number the store SELLS, in the file a reader checks to find out how the
    promise is measured. Both promises are measured the same way, and both are
    described that way here.)

    Best-effort by construction: a promise sweep must never be the reason a
    paying customer's delivery day does not run. Each half goes through its own
    ``*_and_commit`` entry point — «the entry point every SCHEDULED caller
    uses», and this is a scheduled caller — which commits its own work and
    never raises. So a failure in one cannot roll back what the other has
    already written, and neither can escape into the delivery day.

    That contract used to be re-implemented HERE for the SLA half, with a
    try/except and a bare ``session.rollback()``, and the copy was the weaker
    one: a rollback that itself raised — a connection already gone, which is
    the very way the commit above it fails — went straight up into
    :func:`run_daily_delivery`, which calls this FIRST, before any of the
    night's work exists. The entry point guards its own rollback. One contract,
    in one place, tested there.
    """
    from career.promises import career_session, guarantee

    counts: dict[str, int] = {}
    counts.update(guarantee.sweep_and_commit(   # commits; never raises
        session, now=now, admin_client=deps.admin_client,
    ))
    counts.update(career_session.escalate_overdue_and_commit(   # ditto
        session, now=now, admin_client=deps.admin_client,
    ))
    return counts


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
    expired_out: list[TenantDayState] | None = None,
) -> dict[uuid.UUID, TenantDayState]:
    """One delivery day. Tenants are isolated — one tenant's crash never
    touches the others; the admin summary reports every closed tenant.
    ``include_weekend`` exists for manual canary runs only — the timer
    never sets it (§08: Sunday–Thursday).

    ``expired_out``, when given, is filled with the day states the stale-bundle
    sweep closed for PREVIOUS days. They are handed back on their own channel
    rather than merged into the returned dict on purpose: that dict is keyed by
    tenant id and describes TODAY, and a tenant can legitimately have both —
    yesterday expired at 04:30 and today delivered a minute later. Merging them
    would let one of the two days silently overwrite the other, and the one
    that loses is always the failure. The caller (``engine.cli``) reports both
    and fails the night on either; see :func:`expire_stale_held_deliveries` for
    the four live nights that exited 0 while this channel did not exist.
    """
    # FIRST, before any of the night's own work exists in this session. The
    # promise sweep rolls its session back if it fails (it must never be the
    # reason a delivery day does not run), and a rollback after the stale-
    # bundle expiry below would silently discard day states that had already
    # been closed.
    sweep_promises(session, deps=deps, now=now)

    expired = expire_stale_held_deliveries(session, suppressor=suppressor, now=now)
    if expired_out is not None:
        expired_out.extend(expired)
    if expired:
        try:
            deps.admin_client.send_admin(
                # The count folds into the sentence in Arabic-Indic digits;
                # the day state is an IDENTIFIER the operator greps the
                # journal for, so it keeps its exact spelling on its own line.
                f"⌛ أُغلقت {_ar_digits(len(expired))} تسليمة معلقة من يوم سابق"
                " — النافذة لم تُفتح\n"
                "WHATSAPP_FAILED"
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
    today = now.astimezone(_RIYADH).date()   # the same Riyadh day _run_tenant uses
    for index, (tenant_id, payload) in enumerate(ordered):
        if (index == 1 and canary_present and canary_delay_seconds > 0):
            (sleeper or __import__("time").sleep)(canary_delay_seconds)
        if _already_delivered_today(session, tenant_id=tenant_id,
                                    run_date=today):
            # Re-running the nightly the same Riyadh day is a normal recovery
            # action after a partial failure, and it used to be destructive:
            # a second pass that found nothing rewrote a DELIVERED day as
            # NO_MATCHES and told a customer who HAD received his jobs that we
            # found none; a pass that found something new paid for the CV and
            # then crashed on the one-delivery-per-day constraint, rewriting
            # the day CV_GENERATION_FAILED. A customer is served once a day —
            # skip them, do not re-serve and do not overwrite.
            logger.info("tenant already delivered today — skipping re-run")
            continue
        # The crash path's only witness. Seeded from the payload so a tenant
        # that raises before `_run_tenant` computes anything still closes with
        # the night's real gate count rather than a zero.
        counts_in = payload.get("counts") or {}
        progress = TenantProgress(
            gate_passes=int(counts_in.get("passed",
                                          len(payload.get("final") or [])))
        )
        try:
            state = _run_tenant(
                session, tenant_id=tenant_id, payload=payload, report=report,
                deps=deps, now=now, generation_budget=generation_budget,
                renderer=renderer, suppressor=suppressor, progress=progress,
            )
        except Exception:  # noqa: BLE001 — tenant isolation is the contract
            logger.error("tenant delivery crashed", exc_info=True)
            # AUDIT ح-1/ك-10: roll back ONLY this tenant's uncommitted work
            # (earlier tenants are already committed), then close the day
            # honestly — never a silent no-state day (§15.12).
            #
            # AND WHAT THIS ROLLBACK CAN NO LONGER DO, deliberately
            # (CHANGELOG 39): it cannot undo a HALF-finished tenant any more.
            # Everything up to the last durability point is already committed,
            # because the half it would undo is the half the customer already
            # received — four messages on a real phone on 10 August, erased
            # from the ledger by this very line. What it still undoes is
            # everything written after that point, which is the only part
            # nobody outside this process has seen.
            session.rollback()
            try:
                # The counts come from `progress`, which survived the frame.
                # They used to be manufactured here — a hard-coded
                # `cv_resolved=0` beside `cv_failed=max(passes, 1)` — because
                # this handler had nothing else to say.
                fallback = _close_after_crash(
                    session, tenant_id=tenant_id,
                    run_date=now.astimezone(_RIYADH).date(), now=now,
                    progress=progress,
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
                logger.error("cost rollup failed", exc_info=True)
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
