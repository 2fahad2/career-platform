"""The honest daily close — constant §15.12 made executable.

ONE state per tenant per day out of exactly seven; never a silent success.
Suppression is written ONLY for delivered groups (a failed job returns
tomorrow, a delivered one never repeats inside the TTL); a ledger-write
failure is itself the honest LEDGER_FAILED state, not an exception. The
admin summary carries TEN-#### codes and numbers only (§15.13). Usage
events are the raw §14 fuel; cost_allocations is their per-day rollup.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import CostAllocation, DeliveryMessage, TenantDayState, UsageEvent
from career.engine.ranking import record_suppression_by_url

logger = logging.getLogger("career.cv")

DAILY_STATES = (
    "DELIVERED",
    "NO_MATCHES",
    "PARTIAL_DELIVERY",
    "DISCOVERY_FAILED",
    "CV_GENERATION_FAILED",
    "WHATSAPP_FAILED",
    "LEDGER_FAILED",
    # CHANGELOG §12 (Fahad, option A): event-driven — written ONLY when the
    # delivery was skipped because the customer opted out of messages. The
    # seven computational states above stay untouched.
    "SKIPPED_OPTED_OUT",
)


def daily_state(
    *,
    ledger_ok: bool,
    discovery_ok: bool,
    gate_passes: int,
    cv_resolved: int,
    cv_failed: int,
    delivered: int,
    failed_sends: int,
) -> str:
    """The single-state authority — decision order is part of the contract."""
    if not ledger_ok:
        return "LEDGER_FAILED"
    if not discovery_ok:
        return "DISCOVERY_FAILED"
    if gate_passes == 0:
        return "NO_MATCHES"
    if cv_resolved == 0:
        return "CV_GENERATION_FAILED"
    if delivered == 0 and failed_sends > 0:
        return "WHATSAPP_FAILED"
    if failed_sends > 0 or cv_failed > 0:
        return "PARTIAL_DELIVERY"
    return "DELIVERED"


Suppressor = Callable[..., None]


def close_tenant_day(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    run_date: date,
    now: datetime,
    discovery_ok: bool,
    gate_passes: int,
    cv_resolved: int,
    cv_failed: int,
    delivered_groups: list[str],
    failed_groups: list[str],
    suppressor: Suppressor = record_suppression_by_url,
) -> TenantDayState:
    """Suppress ONLY the delivered, then record the one honest state.
    A suppression failure becomes LEDGER_FAILED — recorded, never raised."""
    ledger_ok = True
    try:
        # audit fix: the whole day's ledger write is ONE savepoint — a
        # mid-loop failure must not leave earlier groups half-committed
        # next to a LEDGER_FAILED state (§15.3 atomicity).
        with session.begin_nested():
            for group in delivered_groups:
                suppressor(session, tenant_id=tenant_id, url=group, now=now)
    except Exception:  # noqa: BLE001 — the failure IS the state (§15.12)
        logger.error("suppression ledger write failed", exc_info=True)
        ledger_ok = False

    state_value = daily_state(
        ledger_ok=ledger_ok,
        discovery_ok=discovery_ok,
        gate_passes=gate_passes,
        cv_resolved=cv_resolved,
        cv_failed=cv_failed,
        delivered=len(delivered_groups),
        failed_sends=len(failed_groups),
    )
    counts = {
        "gate_passes": gate_passes,
        "cv_resolved": cv_resolved,
        "cv_failed": cv_failed,
        "delivered": len(delivered_groups),
        "failed_sends": len(failed_groups),
    }

    row = session.execute(
        select(TenantDayState).where(
            TenantDayState.tenant_id == tenant_id,
            TenantDayState.run_date == run_date,
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantDayState(
            id=uuid.uuid4(), tenant_id=tenant_id, run_date=run_date,
            state=state_value, counts=counts, recorded_at=now,
        )
        session.add(row)
    elif _outranks(state_value, row.state):
        row.state = state_value
        row.counts = counts
        row.recorded_at = now
    session.flush()
    return row


#: A day that ALREADY delivered cannot be un-delivered by a later pass.
#:
#: Re-running the nightly on the same Riyadh day is a normal recovery action,
#: and it used to destroy the truth: a DELIVERED day whose second pass found
#: nothing new was rewritten NO_MATCHES, and the customer who had already
#: received his jobs was told «بحثنا اليوم ولم نجد فرصًا». Worse, a second pass
#: that DID find something crashed on the one-delivery-per-day constraint and
#: rewrote the day as CV_GENERATION_FAILED — after paying for the CV.
#:
#: So the ledger only ever moves UP. Rank is «how much did the customer
#: actually receive», not «what happened last».
_STATE_RANK: dict[str, int] = {
    "DELIVERED": 100,
    "PARTIAL_DELIVERY": 90,
    "SKIPPED_OPTED_OUT": 50,
    "NO_MATCHES": 40,
    "WHATSAPP_FAILED": 30,
    "CV_GENERATION_FAILED": 20,
    "DISCOVERY_FAILED": 10,
    "LEDGER_FAILED": 5,
}


def _outranks(new_state: str, existing: str) -> bool:
    """True when the new state may replace the recorded one.

    Equal ranks still replace: a second DELIVERED pass legitimately refreshes
    its counts. Only a DEMOTION is refused, because nothing that happens after
    a delivery can make that delivery not have happened.
    """
    return _STATE_RANK.get(new_state, 0) >= _STATE_RANK.get(existing, 0)


def close_skipped_opted_out(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    run_date: date,
    now: datetime,
    gate_passes: int,
) -> TenantDayState:
    """CHANGELOG §12: the eighth honest state — opted-out customer whose day
    had gate passes. Recorded, never counted as success or failure."""
    counts = {"gate_passes": gate_passes, "cv_resolved": 0, "cv_failed": 0,
              "delivered": 0, "failed_sends": 0}
    row = session.execute(
        select(TenantDayState).where(
            TenantDayState.tenant_id == tenant_id,
            TenantDayState.run_date == run_date,
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantDayState(
            id=uuid.uuid4(), tenant_id=tenant_id, run_date=run_date,
            state="SKIPPED_OPTED_OUT", counts=counts, recorded_at=now,
        )
        session.add(row)
    else:
        row.state = "SKIPPED_OPTED_OUT"
        row.counts = counts
        row.recorded_at = now
    session.flush()
    return row


# ── the admin daily summary (TEN codes + numbers only, §15.13) ───────────────


def format_admin_summary(
    run_date: date, rows: list[tuple[str, str, dict[str, int]]]
) -> str:
    lines = [f"📊 ملخص التشغيلة اليومية — {run_date.isoformat()}"]
    for code, state, counts in rows:
        numbers = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"{code} · {state} · {numbers}")
    if not rows:
        lines.append("لا عملاء نشطين اليوم.")
    return "\n".join(lines)


# ── the ONE price table (§14) ────────────────────────────────────────────────

#: claude-opus-4-8 list prices, USD per token. Cache writes bill at 1.25× the
#: input rate and cache reads at 0.1× — the API reports those two separately,
#: so the meter must too: a cached workload priced at the plain input rate
#: reads as ~10× its real cost, which is exactly the kind of wrong number that
#: hides a runaway bill.
LLM_USD_PER_INPUT_TOKEN = Decimal("0.000005")
LLM_USD_PER_OUTPUT_TOKEN = Decimal("0.000025")
LLM_USD_PER_CACHE_WRITE_TOKEN = Decimal("0.00000625")
LLM_USD_PER_CACHE_READ_TOKEN = Decimal("0.0000005")

#: Every metered LLM call site, one kind each. The operator's screens iterate
#: THIS tuple, so a paid boundary that forgets to register here shows up as a
#: missing category rather than as silence.
LLM_KINDS: tuple[str, ...] = (
    "llm_generation",    # CV tailoring (cv/generate.py)
    "llm_extraction",    # CV fact extraction (onboarding/extraction.py)
    "llm_render",        # achievement bullet render — 3 per panel
    "llm_judge",         # bullet panel judge
    "llm_examples",      # icebreaker examples writer
    "llm_intent",        # conversation intent classifier
)
#: SearchAPI.io google_jobs credits (engine/sources.py), split across the
#: tenants whose approved paths seeded the query family.
SEARCH_KINDS: tuple[str, ...] = ("search_api",)
#: Meta bills TEMPLATE messages per category and marketing costs ~2.4× utility,
#: so one «messages» number would hide the expensive half.
WHATSAPP_KINDS: tuple[str, ...] = ("wa_utility", "wa_marketing", "wa_unknown")
#: Manual operator counters (telegram/console.py) — real cost, no dollar price.
MANUAL_KINDS: tuple[str, ...] = ("support_minutes", "human_review")

SPEND_KINDS: tuple[str, ...] = LLM_KINDS + SEARCH_KINDS + WHATSAPP_KINDS


def llm_cost_usd(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Decimal | None:
    """USD for one metered Claude call, or None when nothing was spent."""
    if (input_tokens <= 0 and output_tokens <= 0
            and cache_write_tokens <= 0 and cache_read_tokens <= 0):
        return None
    return (
        LLM_USD_PER_INPUT_TOKEN * max(input_tokens, 0)
        + LLM_USD_PER_OUTPUT_TOKEN * max(output_tokens, 0)
        + LLM_USD_PER_CACHE_WRITE_TOKEN * max(cache_write_tokens, 0)
        + LLM_USD_PER_CACHE_READ_TOKEN * max(cache_read_tokens, 0)
    )


# ── the client-side token counters (§14 fuel, no database) ───────────────────


def _int_of(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):  # pragma: no cover — a drifted SDK shape
        return 0


class TokenCounter:
    """Per-instance token totals for a live Anthropic client.

    Every paid Claude boundary in the product mixes this in and calls
    :meth:`absorb_usage` on the raw response, so the REAL numbers the API
    reported are what get metered — never an estimate. The client itself never
    touches the database: the call site (which knows the tenant) reads the
    delta around one call and records it, which keeps every boundary
    injectable and every test network-free.
    """

    def __init__(self) -> None:  # pragma: no cover — subclasses call this
        self.reset_token_counters()

    def reset_token_counters(self) -> None:
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_cache_read_tokens = 0

    def absorb_usage(self, response: Any) -> None:
        """Accumulate one response's usage. Tolerates a response with no usage
        block (fakes, error paths) — accounting never breaks a customer turn."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.total_input_tokens += _int_of(getattr(usage, "input_tokens", 0))
        self.total_output_tokens += _int_of(getattr(usage, "output_tokens", 0))
        self.total_cache_write_tokens += _int_of(
            getattr(usage, "cache_creation_input_tokens", 0)
        )
        self.total_cache_read_tokens += _int_of(
            getattr(usage, "cache_read_input_tokens", 0)
        )


_COUNTER_FIELDS = (
    "total_input_tokens", "total_output_tokens",
    "total_cache_write_tokens", "total_cache_read_tokens",
)


def token_snapshot(client: Any) -> tuple[int, int, int, int]:
    """The four counters of a (possibly un-instrumented) client."""
    return tuple(  # type: ignore[return-value]
        _int_of(getattr(client, field, 0)) for field in _COUNTER_FIELDS
    )


# ── usage → cost allocations (§14) ───────────────────────────────────────────


def record_usage(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    now: datetime,
    run_id: uuid.UUID | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cost_usd: Decimal | None = None,
) -> UsageEvent:
    row = UsageEvent(
        id=uuid.uuid4(), tenant_id=tenant_id, kind=kind, run_id=run_id,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost_usd, occurred_at=now,
    )
    session.add(row)
    session.flush()
    return row


class LlmMeter:
    """Binds (session, tenant) so the PURE pipeline functions can meter a live
    Claude client without knowing anything about the database.

    One :meth:`around` block = one ``usage_events`` row, categorised by kind
    and carrying the real token numbers the API reported. Accounting is
    best-effort by construction: a metering failure is logged and swallowed,
    because a customer's CV must never fail over a bookkeeping row.
    """

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        now: datetime | None = None,
        run_id: uuid.UUID | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now
        self._run_id = run_id

    @contextmanager
    def around(self, kind: str, client: Any, *, always: bool = False) -> Iterator[None]:
        """``always`` records the row even when the client reported no tokens.
        Required where the EVENT itself is load-bearing — MonthlyCapBudget
        counts ``llm_generation`` rows, so a silent skip would disable the
        plan's safety cap. Elsewhere a zero-token call is not spend and is
        not written."""
        before = token_snapshot(client)
        try:
            yield
        finally:
            self._record(kind, client, before, always=always)

    def _record(
        self, kind: str, client: Any, before: tuple[int, ...], *, always: bool = False
    ) -> None:
        after = token_snapshot(client)
        used = [max(a - b, 0) for a, b in zip(after, before, strict=True)]
        if not any(used) and not always:
            return
        try:
            # a savepoint: this may run while an exception is unwinding, and a
            # failed accounting flush must not poison the caller's transaction
            with self._session.begin_nested():
                record_usage(
                    self._session, tenant_id=self._tenant_id, kind=kind,
                    run_id=self._run_id, now=self._now or datetime.now(UTC),
                    input_tokens=used[0] or None,
                    output_tokens=used[1] or None,
                    cache_write_tokens=used[2] or None,
                    cache_read_tokens=used[3] or None,
                    cost_usd=llm_cost_usd(*used),
                )
        except Exception:  # noqa: BLE001 — accounting never blocks a customer
            logger.warning("llm usage record failed: kind=%s", kind, exc_info=True)


@contextmanager
def metered(meter: LlmMeter | None, kind: str, client: Any) -> Iterator[None]:
    """``meter``-or-nothing, so a pure pipeline function never has to branch.
    None means «no session here» (unit tests, previews) — not «free»."""
    if meter is None or client is None:
        yield
        return
    with meter.around(kind, client):
        yield


# ── WhatsApp: billed per template CATEGORY, derived from the ledger ──────────

#: template category → usage kind. Unknown template names land in wa_unknown
#: and are priced at the MARKETING (higher) rate: an unrecognised template must
#: never make a bill look smaller than it is.
_WA_KIND_BY_CATEGORY = {"utility": "wa_utility", "marketing": "wa_marketing"}


def _wa_price(kind: str) -> Decimal:
    from career.config import get_settings

    settings = get_settings()
    if kind == "wa_utility":
        return Decimal(str(settings.whatsapp_usd_per_utility_message))
    return Decimal(str(settings.whatsapp_usd_per_marketing_message))


def _wa_kind_of(template_name: str | None) -> str:
    from career.whatsapp.templates import REGISTRY

    spec = REGISTRY.get(str(template_name or ""))
    if spec is None:
        return "wa_unknown"
    return _WA_KIND_BY_CATEGORY.get(str(spec.category), "wa_unknown")


def whatsapp_spend(
    session: Session,
    *,
    tenant_id: uuid.UUID | None = None,
    day: date | None = None,
    since: datetime | None = None,
) -> dict[str, tuple[int, Decimal]]:
    """Billed WhatsApp spend as ``{kind: (messages, usd)}``.

    Only TEMPLATE messages are billed, and Meta prices them per category, so
    counting «messages» would hide that a marketing send costs ~2.4× a utility
    one. Free-form service replies inside the open 24h window cost nothing and
    are deliberately not counted; a send whose final status is ``failed`` was
    never delivered and is not billed.

    DERIVED from ``delivery_messages`` rather than recorded at the send sites:
    those live in ``whatsapp/delivery.py`` and ``salla/`` (see the report), and
    the ledger row is written in the same transaction as every send, so it is
    the same truth — and staying derived keeps this idempotent for free.
    """
    query = select(DeliveryMessage.template_name, func.count()).where(
        DeliveryMessage.kind == "template",
        DeliveryMessage.status != "failed",
    )
    if tenant_id is not None:
        query = query.where(DeliveryMessage.tenant_id == tenant_id)
    if day is not None:
        query = query.where(func.date(DeliveryMessage.created_at) == day)
    if since is not None:
        query = query.where(DeliveryMessage.created_at >= since)

    out: dict[str, tuple[int, Decimal]] = {}
    for template_name, count in session.execute(
        query.group_by(DeliveryMessage.template_name)
    ).all():
        kind = _wa_kind_of(template_name)
        events, cost = out.get(kind, (0, Decimal("0")))
        out[kind] = (events + int(count), cost + _wa_price(kind) * int(count))
    return out


def _upsert_allocation(
    session: Session, *, tenant_id: uuid.UUID, day: date, category: str,
    events: int, cost_usd: Decimal,
) -> None:
    row = session.execute(
        select(CostAllocation).where(
            CostAllocation.tenant_id == tenant_id,
            CostAllocation.day == day,
            CostAllocation.category == category,
        )
    ).scalar_one_or_none()
    if row is None:
        row = CostAllocation(
            id=uuid.uuid4(), tenant_id=tenant_id, day=day, category=category,
            events=events, cost_usd=cost_usd,
        )
        session.add(row)
    else:
        row.events = events
        row.cost_usd = cost_usd


def rollup_costs(session: Session, *, tenant_id: uuid.UUID, day: date) -> None:
    """Recompute the day's per-category rollup from the raw events PLUS the
    derived WhatsApp template spend — idempotent by construction (SET, not
    increment), so the whole day's bill lands in one table either way."""
    aggregated = session.execute(
        select(
            UsageEvent.kind,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            func.date(UsageEvent.occurred_at) == day,
        )
        .group_by(UsageEvent.kind)
    ).all()
    for kind, events, cost in aggregated:
        _upsert_allocation(
            session, tenant_id=tenant_id, day=day, category=kind,
            events=int(events), cost_usd=Decimal(cost),
        )
    for kind, (events, cost) in whatsapp_spend(
        session, tenant_id=tenant_id, day=day
    ).items():
        _upsert_allocation(
            session, tenant_id=tenant_id, day=day, category=kind,
            events=events, cost_usd=cost,
        )
    session.flush()


class UsageBudget:
    """A BudgetGuard (career.cv.generate) backed by the day's usage events —
    blocks with an explicit reason at the cap; its own failure never blocks
    (that semantic lives in the caller, generate._budget_allows)."""

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        kind: str,
        cap: int,
        day: date,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._kind = kind
        self._cap = cap
        self._day = day

    def allow(self) -> tuple[bool, str | None]:
        count = self._session.execute(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.tenant_id == self._tenant_id,
                UsageEvent.kind == self._kind,
                func.date(UsageEvent.occurred_at) == self._day,
            )
        ).scalar_one()
        if int(count) >= self._cap:
            return (False, f"budget_cap_reached:{self._kind}")
        return (True, None)
