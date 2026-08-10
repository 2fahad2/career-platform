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

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from career.db.models import (
    CostAllocation,
    DeliveryMessage,
    Subscription,
    TenantDayState,
    UsageEvent,
)
from career.engine.ranking import record_suppression_by_url
from career.salla import subscriptions as sub_states

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
    ledger_ok: bool = True,
) -> TenantDayState:
    """Suppress ONLY the delivered, then record the one honest state.
    A suppression failure becomes LEDGER_FAILED — recorded, never raised.

    ``ledger_ok=False`` IS THE CALLER SAYING «THIS DAY'S LEDGER IS ALREADY
    LOST» (CHANGELOG 39, 2026-08-10). The sends are not undoable and the rows
    that record them are; `cv/daily_run`'s crash close knows the delivery
    landed and its ledger write did not, and until now had no way to say so
    through this door — this function derived ``ledger_ok`` from ONE source,
    its own suppression loop, so it could only ever answer a state claiming the
    delivery was recorded. It composed :func:`daily_state` and
    :func:`_record_day_state` by hand instead, which works and goes around the
    one function that knows what closing a day means.

    THE TWO SOURCES ARE **AND**-ed, never replaced. A caller's answer can only
    make the state worse: `True` is «I know of no upstream loss», which is the
    default and says nothing about the suppression write this function is about
    to attempt, and no argument may certify a ledger this function itself
    watched fail.

    AND IT WRITES NO SUPPRESSION WHEN THE CALLER SAYS FALSE — the coupling
    decided here rather than left to each caller's memory, because forgetting
    it is silent and permanent. Suppression is the claim «this posting was
    delivered, do not offer it again»; on a day whose delivery rows were lost
    it is a claim with nothing behind it, and its cost runs the wrong way: a
    suppressed LEDGER_FAILED day makes the operator's re-run find NOTHING,
    close `NO_MATCHES` — which :func:`_outranks` does not stop, since only
    DELIVERED and PARTIAL_DELIVERY are protected — and overwrite the alarm.
    A duplicate send is visible to one customer and he can say so; a silently
    cleared alarm is invisible to everybody. The re-run is the recovery, so it
    is kept possible, and ``delivered_groups`` still passes through in full so
    the COUNTS stay true: `LEDGER_FAILED` beside `delivered: 1` is what the
    operator can act on, and manufacturing a zero there was half of what
    CHANGELOG 39 was about.
    """
    caller_ledger_ok = ledger_ok
    suppression_ok = True
    if caller_ledger_ok:
        try:
            # audit fix: the whole day's ledger write is ONE savepoint — a
            # mid-loop failure must not leave earlier groups half-committed
            # next to a LEDGER_FAILED state (§15.3 atomicity).
            with session.begin_nested():
                for group in delivered_groups:
                    suppressor(session, tenant_id=tenant_id, url=group, now=now)
        except Exception:  # noqa: BLE001 — the failure IS the state (§15.12)
            logger.error("suppression ledger write failed", exc_info=True)
            suppression_ok = False
    elif delivered_groups:
        # ERROR, not warning: this is the one line that tells the operator the
        # re-run is both possible and necessary, and warnings do not leave the
        # box (`whatsapp/worker`: the harvester forwards «ERROR:» lines only).
        logger.error(
            "day closed with its ledger already lost — %d delivered group(s) "
            "left UNSUPPRESSED on purpose so a re-run can still find them; "
            "the day is LEDGER_FAILED with its real counts",
            len(delivered_groups),
        )

    state_value = daily_state(
        ledger_ok=caller_ledger_ok and suppression_ok,
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

    return _record_day_state(
        session, tenant_id=tenant_id, run_date=run_date,
        state=state_value, counts=counts, now=now,
    )


#: Only a DELIVERY is protected. A day that already delivered cannot be
#: un-delivered by a later pass — but nothing else outranks anything.
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
#: The ONLY states that may not be overwritten by a later pass, because the
#: customer really did receive something and no later event can undo that.
_PROTECTED: frozenset[str] = frozenset({"DELIVERED", "PARTIAL_DELIVERY"})


def _outranks(new_state: str, existing: str) -> bool:
    """May the new state replace the recorded one?

    The first version of this ranked ALL eight states, and that was wrong in
    a way that broke the very constant it was meant to serve. NO_MATCHES
    outranked every failure, so a morning that found nothing and an afternoon
    re-run whose CV generation then failed was still recorded «no matching
    opportunities» — money spent on Claude, an honest failure discarded, and
    the operator shown a clean day. LEDGER_FAILED, ranked lowest of all, could
    never be recorded at all, though §15.3 and §15.12 both lean on it.

    So only a real DELIVERY is protected. Everything else is the latest truth,
    which is what an honest ledger is for. A promotion into a delivered state
    is always allowed — a re-run that finally lands is exactly the recovery
    the operator is trying to perform.
    """
    if existing not in _PROTECTED:
        return True
    return new_state in _PROTECTED


def _record_day_state(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    run_date: date,
    state: str,
    counts: dict[str, int],
    now: datetime,
) -> TenantDayState:
    """The ONE writer of ``tenant_day_states`` — every path comes through here.

    The monotonic rule (:func:`_outranks`) used to live inside
    ``close_tenant_day`` only, and the second writer that arrived later
    (``close_skipped_opted_out``, CHANGELOG §12) simply did not ask it: a bare
    ``else`` overwrote whatever was there. That path runs AFTER the delivery
    phase, so a customer who received his jobs at 06:00 and pressed «إيقاف» at
    06:10 had his real DELIVERED day — with its real counts — rewritten to
    SKIPPED_OPTED_OUT with ``delivered: 0``, while the suppression rows kept
    recording those very jobs as sent. The ledger the operator trusts then
    contradicted itself, and the number he reports as «سُلِّم اليوم» silently
    lost a real delivery.

    So the gate is no longer something a writer must remember to call — it is
    the only door. A future ninth state gets the rule for free, which is the
    property that failed here: the rule was correct and merely bypassable.
    """
    row = session.execute(
        select(TenantDayState).where(
            TenantDayState.tenant_id == tenant_id,
            TenantDayState.run_date == run_date,
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantDayState(
            id=uuid.uuid4(), tenant_id=tenant_id, run_date=run_date,
            state=state, counts=counts, recorded_at=now,
        )
        session.add(row)
    elif _outranks(state, row.state):
        row.state = state
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
    had gate passes. Recorded, never counted as success or failure.

    An opt-out is an event about TOMORROW, never about what already arrived:
    it cannot un-send this morning's delivery, so it cannot outrank it either
    (see :func:`_record_day_state`, which enforces that for every writer)."""
    counts = {"gate_passes": gate_passes, "cv_resolved": 0, "cv_failed": 0,
              "delivered": 0, "failed_sends": 0}
    return _record_day_state(
        session, tenant_id=tenant_id, run_date=run_date,
        state="SKIPPED_OPTED_OUT", counts=counts, now=now,
    )


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


# ── revenue: the money that arrived and stayed (§05) ─────────────────────────

#: The subscription statuses that are NOT revenue.
#:
#: The business screen used to sum ``amount_sar`` over EVERY row with no status
#: predicate at all, so the number it showed the operator could only ever be
#: too big — a refunded order, a cancelled one and a chargeback each stayed in
#: «الإيراد» forever, and a chargeback is money that left the account twice.
#: The operator prices the product, decides whether to keep buying search
#: credits and decides whether لمّاح is worth running off this number.
#:
#: DERIVED from the state machine, never restated. ``TERMINAL_STATES`` is
#: salla.subscriptions' own name for «the money went back» — the three states a
#: refund, a cancellation and a chargeback land in — so a twelfth state cannot
#: join the product and count as revenue because nobody remembered this line.
#: PENDING_PAYMENT is added because it is the opposite failure: money that has
#: not arrived. Constant §15.4 means no path writes it today (a subscription is
#: only created on ``payment = paid``), and that is exactly why it belongs
#: here — the day a path does, the safe default must already be «not counted».
#:
#: Everything else — PAID_UNCLAIMED, ONBOARDING, ACTIVE, PAUSED, GRACE,
#: EXPIRED, SUSPENDED — is money we were paid and did not give back, whatever
#: the service is doing now. An expired subscription earned its riyals.
NON_REVENUE_STATUSES: frozenset[str] = (
    sub_states.TERMINAL_STATES | {sub_states.PENDING_PAYMENT}
)


def _riyals(total: Decimal) -> Decimal:
    """Halalas kept, and no trailing «٫٠٠» on a whole riyal.

    The old sum was ``revenue += int(amount or 0)``, which truncates toward
    zero on EVERY row rather than once at the end: at 199.50 a row that is a
    hundred orders wide loses fifty riyals, silently, and always downward.
    ``amount_sar`` is ``Numeric(10, 2)`` precisely so halalas survive the
    database; throwing them away in the reader defeated the column.

    The whole-riyal case is normalised back to an integral Decimal so the
    screens keep printing «١٩٩ ريال» and not «199.00» — the catalogue prices
    everything in whole riyals, so that is the shape the operator reads every
    day, and a formatter change belongs to the renderers, not to the money.
    """
    quantized = total.quantize(Decimal("0.01"))
    whole = quantized.to_integral_value()
    return whole if quantized == whole else quantized


def paid_subscriptions_by_plan(
    session: Session, *, since: datetime | None = None
) -> tuple[dict[str, int], Decimal]:
    """``({plan_code: paid_orders}, riyals)`` — the operator's two money
    numbers, from one query, with :data:`NON_REVENUE_STATUSES` applied.

    The COUNT is filtered by the same rule as the sum, deliberately. They are
    read side by side («اشتراكات جديدة» above «الإيراد»), and a plan showing
    two subscriptions against zero riyals is not a detail the reader is
    supposed to reconcile in his head — it is the screen contradicting itself.

    A renewal and a pre-activation merge both COUNT BOTH ROWS, and that is
    correct rather than the double count it looks like. One subscription row is
    one Salla order (``uq_subscriptions_salla_order_id``) carrying that order's
    own ``amount_sar``: somebody who buys twice before activating has paid
    twice, and renewal.merge_prepaid_orders folds the DAYS into one period
    while retiring the extra row EXPIRED under a ``merged_into_activation``
    event — it never touches the money, which is the whole point of retiring
    rather than deleting. Dropping the retired row would under-report by a
    whole order, which is the same defect pointing the other way.

    ── THE 1-RIYAL TEST PHASE, AND WHY NOTHING IS FILTERED (2026-08-08) ──

    The coming test phase puts a 1.00 SAR row on a REAL plan for every tester
    (the reviewer probe drives exactly that shape), so a plan's count climbs
    while its riyals barely move and the ARPU a reader divides in his head
    collapses. The tempting fix is a predicate here. It is refused, on four
    grounds, and this paragraph exists so the next person does not have to
    rediscover them:

    1. **A 1 SAR order is revenue.** Money arrived and stayed. The one thing
       :data:`NON_REVENUE_STATUSES` is careful about is that «revenue» has
       exactly ONE definition in this file, derived from the state machine;
       an amount predicate would be a second one, invented here, keyed on
       nothing the state machine knows.
    2. **Every available discriminator is a guess.** A threshold («under 5
       riyals is not a sale») dies on the first real promotion or coupon and
       on any partial refund. A product id is not visible here and must not
       be — this function reads ``plan_code``, and that separation is what
       keeps the store's catalogue out of the money reader.
    3. **The live data says a threshold would remove the wrong row.** Staging
       today carries two subscriptions: TEN-0002 at 1.00 SAR, which is
       honest, and TEN-0001 at 34900.00 SAR, which is a seeding artefact
       four orders of magnitude wrong and EXPIRED — i.e. revenue by this
       function's own correct rule. A filter tuned to «ignore tiny amounts»
       keeps the number that is actually lying and drops the one that is not.
    4. **The distortion is not in either number — it is in the PAIR.** The
       screen prints per-plan COUNTS beside one GLOBAL total, and nothing on
       it asserts the division the reader performs anyway. The honest repair
       is to give the operator per-plan riyals so «لمّاح: ١٠ · ٤٠٦ ريال» is
       self-contradicting on its face; that is a change to
       ``telegram/console``/``views`` (see the T2 report for the patch), not
       to what counts as money.

    And the cheapest cure is upstream of all of it: do not map the 1-riyal
    test product onto a real ``plan_code``. Give it its own, and the breakdown
    separates itself — for this screen, for founding seats, and for the price
    lock the shared plan code already poisons
    (``tests/test_reviewer_one_riyal_probe.py``).
    """
    query = select(
        Subscription.plan_code,
        func.count(),
        func.coalesce(func.sum(Subscription.amount_sar), 0),
    ).where(Subscription.status.not_in(sorted(NON_REVENUE_STATUSES)))
    if since is not None:
        query = query.where(Subscription.created_at >= since)

    by_plan: dict[str, int] = {}
    total = Decimal("0")
    for plan_code, count, amount in session.execute(
        query.group_by(Subscription.plan_code)
    ).all():
        by_plan[str(plan_code)] = int(count)
        total += Decimal(str(amount or 0))
    return by_plan, _riyals(total)


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
    """The configured per-message price. §06 keeps provider list prices in the
    settings, not the code, so the operator can correct them without a deploy —
    and on 2026-08-08 they needed correcting.

    Meta's live Saudi Arabia rate card, read the same day the categories were:

        marketing  $0.0501/message   (configured default: $0.0384 — 23% low)
        utility    $0.0107/message   (configured default: $0.0157 — 47% high)

    KSA's marketing rate was raised effective 2026-04-01 and the defaults
    predate it, so the true multiple is ~4.7×, not the ~2.4× the old comments
    around here assumed. Utility also has VOLUME TIERS in KSA (stepping to
    $0.0080) and marketing has none — one more reason the category matters.

    CLOSED 2026-08-10, MARKED IN PLACE rather than deleted. The two parenthesised
    defaults above are HISTORY: `career/config.py` now ships 0.0107 and 0.0501
    and carries the measurement note, so this docstring no longer describes a
    live drift. It is kept because the drift is the argument — it is why the
    band is recorded on the row (0030) instead of assumed, and a reader who
    finds only today's correct numbers cannot see that a third party's price
    moved under a default for four months without anything noticing. Deleting
    the paragraph would delete the reason.

    The remaining live half is unchanged and is the part to act on: these are a
    third party's numbers and they move again. ``WHATSAPP_USD_PER_{UTILITY,
    MARKETING}_MESSAGE`` in `.env` corrects a host with no deploy at all, and
    the three `.env*.example` templates carry the same pair (corrected 2026-08-10;
    they shipped the old one until then, so a host built from a template silently
    undid the code fix).
    """
    from career.config import get_settings

    settings = get_settings()
    if kind == "wa_utility":
        return Decimal(str(settings.whatsapp_usd_per_utility_message))
    return Decimal(str(settings.whatsapp_usd_per_marketing_message))


def _wa_kind_of(template_name: str | None, category: str | None = None) -> str:
    """The billing kind for one template send.

    ONE definition, one precedence, most-specific-first — and the order is the
    contract, because this function is the only place «what did this send
    cost» is decided (see :func:`spend_by_kind` for what happened the last time
    there were two):

    1. ``category`` — what Meta's own delivery receipt said it BILLED this
       exact message at, recorded on the row at receipt time (0030). A
       measurement of THIS send.
    2. today's :func:`templates.billed_category` for the template name — a
       measurement of the ACCOUNT, correct for a send that happened close
       enough to it.
    3. ``wa_unknown`` — no measurement of any kind, priced at the marketing
       rate, which is the err-expensive rule this function has always used.

    A recorded category is therefore never overridden by a name: `wa_unknown`
    is not a third price band, it is the bucket for «nobody ever measured
    this», and a row Meta itself priced has been measured.

    AUDIT 2026-08-08 — this read ``spec.category``, the category we SUBMITTED
    the template under, and priced the bill from it. Meta had re-categorised
    five of the eight live templates from UTILITY to MARKETING after approving
    them, so every `welcome_activation`, `onboarding_reminder`,
    `renewal_reminder`, `daily_service_update` and `daily_opportunities_utility`
    send was billed here at the utility rate while Meta charged the marketing
    one — on what `salla/lifecycle._live_channel` documents as the MAJORITY of
    the account's billed template traffic. On Meta's live Saudi Arabia card
    (verified 2026-08-08) that is $0.0107 recorded against $0.0501 charged:
    **21% of the real price.** The number was not slightly off; it was derived
    from the wrong source.

    It now reads :func:`templates.billed_category`, which answers from a dated
    measurement of the live account and falls back to MARKETING — the same
    err-expensive rule ``wa_unknown`` already followed.

    THE LIMIT IS NOW BOUNDED TO ROWS THAT CARRY NO CATEGORY. It used to be
    every row: the registry snapshot is TODAY's category applied to all of
    history, and Meta moves categories (five templates on 2026-07-17, again on
    2026-08-02), so re-running :func:`rollup_costs` over an old day priced it
    at a band that day was not billed at — the same input producing a
    different number depending on when it was asked. 0030 put the band on the
    row, filled from Meta's receipt by `worker._handle_status`, and step 1
    above reads it. A row with no recorded band still takes step 2, with the
    old limit unchanged: bounded, dated, and pointing the safe way (up).
    """
    from career.whatsapp.templates import REGISTRY, billed_category

    if category:
        return _WA_KIND_BY_CATEGORY.get(str(category).strip().lower(), "wa_unknown")
    name = str(template_name or "")
    if name not in REGISTRY:
        return "wa_unknown"
    return _WA_KIND_BY_CATEGORY.get(str(billed_category(name)), "wa_unknown")


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

    Idempotent in its WRITE was never the same as stable in its VALUE, and
    until 0030 it was not stable: the category came from a registry Meta
    re-writes, so this function answered differently about the same historical
    day depending on the day it was asked. The grouping now carries
    ``category`` — the band Meta's receipt reported for that send — so a row
    that has one is priced at what it was billed at, forever, and only a row
    with none still asks the registry. Same one definition
    (:func:`_wa_kind_of`); it just stopped having to guess.

    ── ``kind == 'template'`` IS LOAD-BEARING FOR CORRECTNESS ─────────────────
    READ THIS BEFORE WRITING ANY OTHER SPEND QUERY. That line looks like a
    narrowing to the rows we care about. It is the only thing standing between
    the bill and the majority of `delivery_messages.category`.

    `worker._handle_status` stamps the band on EVERY row whose wa_message_id
    matches the receipt, whatever its kind — text, document, template — because
    the band is a fact about the send. Meta labels the free-form service replies
    it delivers inside the open 24h window, and charges NOTHING for, with
    ``pricing.category = 'service'``: measured on staging 2026-08-10, 212 of the
    230 receipts carrying a band, i.e. ~91% of this column will read `service`.

    `service` is in neither :data:`_WA_KIND_BY_CATEGORY` nor the template
    registry, so :func:`_wa_kind_of` prices it `wa_unknown` — deliberately the
    MARKETING rate, because for a TEMPLATE «never measured» must err expensive.
    Ask the same question without the kind filter and that err-expensive rule
    lands on the messages Meta billed at zero: every free reply priced at
    $0.0501, the largest overstatement this file can produce, on the cheapest
    traffic we have.

    Mapping `service` to a zero price in :data:`_WA_KIND_BY_CATEGORY` is the
    tempting fix and is rejected: it would make the filter look optional while
    quietly deciding billability from the BAND, and it is the row's `kind` that
    says whether Meta billed at all — a template Meta happened to label
    `service` would then be free. One filter, stated, beats a lookup table that
    silently means two things.
    """
    query = select(
        DeliveryMessage.template_name, DeliveryMessage.category, func.count(),
    ).where(
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
    for template_name, category, count in session.execute(
        query.group_by(DeliveryMessage.template_name, DeliveryMessage.category)
    ).all():
        kind = _wa_kind_of(template_name, category)
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


def _priceable_rows(
    session: Session, *, tenant_id: uuid.UUID, day: date
) -> tuple[int, int]:
    """``(usage rows, billed template rows)`` this tenant-day still HAS.

    Deliberately coarser than :func:`spend_by_kind`: the same two predicates
    that decide whether a row is priceable at all — :data:`SPEND_KINDS`, and
    «a template Meta did not refuse» — with none of the mapping, the grouping
    or the pricing that turns them into a kind. It answers ONE question, and
    only :func:`_retire_allocations` asks it: is there evidence here that a
    derivation returning «nothing» must be wrong about?

    It is not a second definition of spend — it computes no money and no kind,
    and it is never compared to one. It reads no column past the two filters,
    which is why it still answers on a host where the derivation itself cannot
    (a `delivery_messages.category` that does not exist is what broke this
    whole path on staging; see :func:`_retire_allocations`).
    """
    usage = session.execute(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.kind.in_(SPEND_KINDS),
            func.date(UsageEvent.occurred_at) == day,
        )
    ).scalar_one()
    billed = session.execute(
        select(func.count(DeliveryMessage.id)).where(
            DeliveryMessage.tenant_id == tenant_id,
            DeliveryMessage.kind == "template",
            DeliveryMessage.status != "failed",
            func.date(DeliveryMessage.created_at) == day,
        )
    ).scalar_one()
    return int(usage), int(billed)


def _retire_allocations(
    session: Session, *, tenant_id: uuid.UUID, day: date, keep: set[str],
) -> None:
    """Drop the kinds this tenant-day no longer has, so a re-run REPLACES the
    day instead of accumulating over it.

    :func:`_upsert_allocation` is keyed ``(tenant, day, category)`` and
    :func:`rollup_costs` only ever wrote the kinds that exist NOW, so a kind
    that stopped being produced kept its row forever and the archive counted
    the same spend twice. That is not an edge case; since 0030 it is the
    ORDINARY path. `whatsapp/delivery.py` rolls the day up in the same breath
    as the send, when the ledger row still carries no band and the kind derives
    as `wa_marketing`; Meta's receipt lands seconds later and fills it; the next
    rollup derives `wa_utility` — and the day's archive then claimed two
    WhatsApp messages at $0.0608 for the one message that was actually sent.

    DELETE, NOT ZERO — the choice matters and the arguments are not symmetric.

    * A zeroed row is a CLAIM, and a false one: «this tenant spent $0 on
      wa_marketing that day» is a sentence about the day, and the day never had
      a wa_marketing message. The row is an artifact of the order two of our
      own writes happened in. An archive that keeps a line for every kind a
      derivation ever passed through is describing our derivation, not the
      tenant's day.
    * There is nothing to preserve by keeping the shell. `cost_allocations`
      carries no timestamp, no source and no history column, so a zeroed row
      records no fact about WHEN or WHY it stopped — it is not an audit trail,
      it is a tombstone with nothing written on it. The real derivation history
      lives in `usage_events` and `delivery_messages`, which are append-only and
      still there.
    * Zero rows accumulate. Every kind any tenant-day ever briefly derived stays
      forever, and the day's rows stop being readable at a glance — the exact
      noise that trains a reader to stop reading (the same rule as the seat
      panel's silent-when-it-changes-nothing offset).

    The one property delete costs us is «a row that once existed still does»,
    and that is precisely the property that made the double count possible.

    SCOPED to one ``(tenant, day)``, which is the unit :func:`rollup_costs`
    recomputes in full. Nothing outside that pair may be touched by a re-run of
    it.

    ── AN EMPTY ANSWER HAS THREE CAUSES, AND THIS PARAGRAPH KNEW TWO ─────────

    ``keep`` empty is the one branch that destroys the whole pair, and it was
    blessed as «the day cost nothing, or its ledger rows were erased under §12
    — the archive should follow the evidence it is derived from rather than
    outlive it». Both remain true. The third was not hypothetical even on the
    day this was written: **the derivation is broken.**

    On staging, `whatsapp_spend` was RAISING on every call — the host was two
    migrations behind and `delivery_messages.category` did not exist there — so
    :func:`spend_by_kind` raised, so :func:`rollup_costs` raised, at all five
    call sites, and every one of them swallows it with `logger.warning`. By
    this repository's own rule (`whatsapp/worker.py`: warnings do not leave the
    box, the operator's harvester forwards «ERROR:» lines only) it had been
    failing silently. That variant never reaches this line — it raises above
    it — but it is the proof that «the derivation cannot answer» is a live
    state of this system, and the next one need only answer WRONGLY (a
    predicate that stops matching, a kind list that loses a name, a day
    boundary that moves) to arrive here as an empty ``keep`` with a full day
    behind it. An empty answer is indistinguishable from a broken derivation,
    and this is the branch that deletes.

    So the two are separated by MEASUREMENT, not by assumption. If the day has
    archived kinds and :func:`_priceable_rows` still finds rows the derivation
    would have priced, the derivation — not the day — is what changed, and the
    pair is left exactly as it is, at ERROR, which is the level that leaves the
    box. If the evidence is genuinely gone (§12 erasure, or a day that only
    ever had `MANUAL_KINDS`, or a day whose one template send ended `failed`
    and was never billed) the counts are zero and the delete proceeds, because
    that is the same «follow the evidence» rule pointing the other way.

    The refusal costs an over-stated day, which is the direction everything
    around here already leans, and it costs it only until a derivation that
    works runs again. The alternative cost is the archive of a real day erased
    on the word of a reader that had stopped reading.
    """
    if not keep:
        archived = session.execute(
            select(func.count()).select_from(CostAllocation).where(
                CostAllocation.tenant_id == tenant_id,
                CostAllocation.day == day,
            )
        ).scalar_one()
        usage_rows, billed_rows = (
            _priceable_rows(session, tenant_id=tenant_id, day=day)
            if archived else (0, 0)
        )
        if archived and (usage_rows or billed_rows):
            logger.error(
                "cost rollup derived NO spend for %s while the day still has "
                "%d archived kinds over %d usage rows and %d billed template "
                "rows — the derivation is broken, not the day; archive left "
                "as-is, nothing retired",
                day.isoformat(), int(archived), usage_rows, billed_rows,
            )
            return

    query = delete(CostAllocation).where(
        CostAllocation.tenant_id == tenant_id,
        CostAllocation.day == day,
    )
    if keep:
        query = query.where(CostAllocation.category.not_in(keep))
    session.execute(query)


def spend_by_kind(
    session: Session,
    *,
    tenant_id: uuid.UUID | None = None,
    day: date | None = None,
    since: datetime | None = None,
) -> dict[str, tuple[int, Decimal]]:
    """``{kind: (events, usd)}`` — the ONE definition of «what did this cost».

    This function exists because there were two, and they disagreed. The
    operator's screens computed spend from ``usage_events`` filtered to
    :data:`SPEND_KINDS` and added the derived WhatsApp half; ``rollup_costs``
    computed it from ``usage_events`` with NO kind filter at all and added the
    same WhatsApp half. Two expressions for one number, in two files, is the
    shape every §14 defect this month has had — and the table the second one
    wrote is read by nothing, so the disagreement was invisible: the row said
    one thing, the screen said another, and only the screen was ever looked at.
    (``tests/test_untested_gaps.py`` even documents ``cost_allocations`` as
    «what the business screen and the weekly report read». They never have.)

    :data:`MANUAL_KINDS` are deliberately outside this: ``support_minutes`` and
    ``human_review`` are real cost with no dollar price, so a dollar rollup
    would carry them as permanent zero-cost categories, and the console reads
    those two counters straight from ``usage_events`` where they belong.

    The WhatsApp half stays DERIVED from ``delivery_messages`` rather than
    summed from ``usage_events`` — see :func:`whatsapp_spend` for why.
    """
    query = select(
        UsageEvent.kind,
        func.count(UsageEvent.id),
        func.coalesce(func.sum(UsageEvent.cost_usd), 0),
    ).where(UsageEvent.kind.in_(SPEND_KINDS))
    if tenant_id is not None:
        query = query.where(UsageEvent.tenant_id == tenant_id)
    if day is not None:
        query = query.where(func.date(UsageEvent.occurred_at) == day)
    if since is not None:
        query = query.where(UsageEvent.occurred_at >= since)

    out: dict[str, tuple[int, Decimal]] = {
        str(kind): (int(events), Decimal(cost))
        for kind, events, cost in session.execute(
            query.group_by(UsageEvent.kind)
        ).all()
    }
    for kind, (events, cost) in whatsapp_spend(
        session, tenant_id=tenant_id, day=day, since=since
    ).items():
        prior_events, prior_cost = out.get(kind, (0, Decimal("0")))
        out[kind] = (prior_events + events, prior_cost + cost)
    return out


#: One rollup at a time per ``(tenant, day)`` — the pair :func:`rollup_costs`
#: recomputes in full, and therefore the only pair two rollups can fight over.
#: DERIVED from the pair rather than a fixed number like
#: `promises.guarantee._SWEEP_LOCK_KEY`, because two different pairs share no
#: row and must never stand each other down. `hashtextextended` can of course
#: collide — with another pair, or with one of those two fixed keys — and the
#: cost of a collision is one rollup that stands down and runs again later,
#: never a wrong row: that is why the losing side does NOTHING rather than
#: something reduced.
_ROLLUP_LOCK_NAMESPACE = "career.cv.close.rollup_costs"


def _rollup_lock(session: Session, *, tenant_id: uuid.UUID, day: date) -> bool:
    """Try to become the only rollup of this tenant-day. Never waits."""
    return bool(
        session.execute(
            select(
                func.pg_try_advisory_xact_lock(
                    func.hashtextextended(
                        f"{_ROLLUP_LOCK_NAMESPACE}:{tenant_id}:{day.isoformat()}",
                        0,
                    )
                )
            )
        ).scalar_one()
    )


def rollup_costs(session: Session, *, tenant_id: uuid.UUID, day: date) -> None:
    """Persist :func:`spend_by_kind` for one tenant-day — idempotent by
    construction (SET, not increment), so the whole day's bill lands in one
    table however many call sites reach it.

    ``cost_allocations`` is an ARCHIVE, not an authority. Five call sites write
    it and nothing reads it: every operator number recomputes from
    ``usage_events``, which is the right choice and must stay that way. The
    events table is append-only, carries a real timestamp per row (so it can
    answer «the last 7 days», which a per-day rollup cannot) and cannot go
    stale; this rollup is best-effort, written from paths that log-and-swallow
    their own failures, so a screen reading it would under-report at exactly
    the moment metering broke — silently, which is the one thing §15.12 exists
    to forbid.

    So the writes were not deleted (whitepaper §14 names this table, and
    removing it is a docs-first decision, not a refactor) — the second
    DEFINITION was. Writer and readers now evaluate the same expression, and
    the table can no longer disagree with the screen about what money is.

    «SET, NOT INCREMENT» WAS ONLY HALF OF IDEMPOTENT, and the missing half was
    a double count: setting each kind that exists now says nothing about a kind
    that existed at the last run and does not now, so its row survived every
    later rollup. :func:`_retire_allocations` is the other half — after it, the
    rows for a tenant-day are exactly :func:`spend_by_kind`'s answer for that
    tenant-day, which is what «the whole day's bill lands in one table» has to
    mean if the sentence is to be true.

    ── ONE ROLLUP AT A TIME PER (TENANT, DAY), 2026-08-10 ────────────────────

    That other half arrived with a race under it. ``fresh`` is read in ONE
    statement and the DELETE runs in a later one, and under READ COMMITTED the
    DELETE takes a NEW snapshot — so it removes rows another rollup committed
    after ``fresh`` was read, using a keep-set that predates them. Two rollups
    do reach the same pair: the send-time one (`whatsapp/delivery.py`,
    `whatsapp/worker.py`) and the nightly (`cv/daily_run.py`). The exposed
    window is the upsert loop above — milliseconds — and inside it the
    send-time rollup deletes the `llm_generation` the nightly has just
    committed, leaving a day that generated a CV with an archive reading
    `['wa_marketing']`.

    THE LOCK IS THE **TRY** FORM, and the blocking `pg_advisory_xact_lock` is
    rejected on two grounds that are about THIS repository, not about locks:

    * **Hold time is not the rollup, it is the caller's transaction.** An xact
      lock is released at COMMIT, and `whatsapp/worker` rolls up ~130 lines
      before its commit with four customer replies and an admin send in
      between (CHANGELOG 39 put the durability point exactly there, on
      purpose). A blocking lock would be held across those HTTP calls — and
      the transaction queued behind it is sometimes the customer-facing
      worker turn, which would then wait on a batch job's network.
    * **It would deadlock, silently.** `daily_run` rolls up MANY pairs inside
      ONE transaction, in whatever order the stale deliveries come back; two
      such waiters in different orders is a textbook deadlock, Postgres aborts
      one, and all five call sites swallow the abort with `logger.warning` —
      i.e. invisibly (see :func:`_retire_allocations` for what that costs).
      `pg_try_advisory_xact_lock` never waits, so it cannot deadlock, and it
      is already this repository's pattern for «another pass is doing exactly
      this work» (`promises/guarantee`, `promises/career_session`).

    **THE LOSER SKIPS THE WHOLE ROLLUP, UPSERTS INCLUDED**, and that is what
    makes the try form correct rather than half of it. A loser that still
    upserted would write rows the winner's keep-set — read before them —
    does not contain, and the winner's DELETE would erase those: the same
    defect with the roles swapped. Doing nothing is the only thing a rollup
    that cannot see the whole pair may safely do.

    WHAT STANDING DOWN COSTS: this pair keeps the answer the other rollup
    wrote until some later rollup reaches it, so it can be seconds stale. Both
    rollups recompute the SAME pair in full, so the loss is bounded by the gap
    between the two snapshots, and this table is a best-effort archive nothing
    reads. Staleness in a table nothing reads is not comparable to erasing a
    real kind out of it.

    REJECTED, both cheaper on their face: ``SELECT … FOR UPDATE`` over the
    pair's rows locks only rows that EXIST, and the erased kind is an INSERT by
    the other rollup — no row lock in READ COMMITTED covers a row that is not
    there yet. Re-deriving the keep-set inside the DELETE
    (``category NOT IN (SELECT …)``, one statement, one snapshot) does close it
    for the ``usage_events`` half and cannot express the WhatsApp half at all:
    that kind comes from :func:`_wa_kind_of`, whose entire purpose is to be the
    ONE place a send's band is decided, and a SQL transcription of it beside
    the Python one is the second definition this file was written to delete.
    """
    if not _rollup_lock(session, tenant_id=tenant_id, day=day):
        # Another rollup holds this exact pair. It recomputes the same day from
        # the same tables, so standing down loses at most the seconds between
        # its snapshot and ours — and doing HALF of a rollup beside it is the
        # interleaving this lock exists to end.
        logger.info("cost rollup for %s already in flight — standing down",
                    day.isoformat())
        return

    fresh = spend_by_kind(session, tenant_id=tenant_id, day=day)
    for kind, (events, cost) in fresh.items():
        _upsert_allocation(
            session, tenant_id=tenant_id, day=day, category=kind,
            events=events, cost_usd=cost,
        )
    # After the upserts, never before — and the reason first written here was
    # WRONG. CORRECTED IN PLACE 2026-08-10, not dropped: it said «a failure
    # between the two must leave the day OVER-stated rather than missing a kind
    # it really had», and there is no «between». Both run in the CALLER's one
    # transaction (this function only flushes), so a failure after the upserts
    # rolls them back with the delete and the state that sentence protects is
    # unreachable. The order stands on a smaller claim: the upsert keeps a
    # surviving row's identity instead of deleting and recreating it, and the
    # keep-set the DELETE is given is the upserts' own answer, which is easier
    # to read in that order than the reverse.
    _retire_allocations(session, tenant_id=tenant_id, day=day, keep=set(fresh))
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
