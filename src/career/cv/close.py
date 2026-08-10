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
    and on 2026-08-08 they need correcting.

    Meta's live Saudi Arabia rate card, read the same day the categories were:

        marketing  $0.0501/message   (configured default: $0.0384 — 23% low)
        utility    $0.0107/message   (configured default: $0.0157 — 47% high)

    KSA's marketing rate was raised effective 2026-04-01 and the defaults
    predate it, so the true multiple is ~4.7×, not the ~2.4× the old comments
    around here assumed. Utility also has VOLUME TIERS in KSA (stepping to
    $0.0080) and marketing has none — one more reason the category matters.
    Correcting the defaults is `career/config.py`, which this change does not
    own; the report carries the patch, and
    ``WHATSAPP_USD_PER_{UTILITY,MARKETING}_MESSAGE`` in `.env` fixes it today
    with no deploy at all.
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
    """
    for kind, (events, cost) in spend_by_kind(
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
