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
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import CostAllocation, TenantDayState, UsageEvent
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
    else:
        row.state = state_value
        row.counts = counts
        row.recorded_at = now
    session.flush()
    return row


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
    cost_usd: Decimal | None = None,
) -> UsageEvent:
    row = UsageEvent(
        id=uuid.uuid4(), tenant_id=tenant_id, kind=kind, run_id=run_id,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd, occurred_at=now,
    )
    session.add(row)
    session.flush()
    return row


def rollup_costs(session: Session, *, tenant_id: uuid.UUID, day: date) -> None:
    """Recompute the day's per-category rollup from the raw events —
    idempotent by construction (SET, not increment)."""
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
        row = session.execute(
            select(CostAllocation).where(
                CostAllocation.tenant_id == tenant_id,
                CostAllocation.day == day,
                CostAllocation.category == kind,
            )
        ).scalar_one_or_none()
        if row is None:
            row = CostAllocation(
                id=uuid.uuid4(), tenant_id=tenant_id, day=day, category=kind,
                events=int(events), cost_usd=Decimal(cost),
            )
            session.add(row)
        else:
            row.events = int(events)
            row.cost_usd = Decimal(cost)
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
