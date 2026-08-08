"""The nightly-run entry point (whitepaper §06) — the systemd timer target.

Thin composition over the tested engine: Settings → owner engine →
``HttpSearchApiClient`` (D13) + ``PythonJobSpyClient`` + ``UrllibPageFetcher``
→ :func:`career.engine.run.run_nightly`. Flags follow D9: ``--digest-only``
is the DEFAULT (no send path exists in the engine at all — delivery is C7's)
and has an explicit ``--no-digest-only`` off form; no flag governs two
effects. The report goes to stdout as JSON with TEN codes instead of tenant
UUIDs (journal = same no-PII discipline as the admin channel, §15.13).

Exit codes are honest and boring, and §15 constant 12 («ورموز الخروج تعكس
الحقيقة») decides what «honest» means: the code reports what happened to the
CUSTOMERS' day, not what happened to the process. 0 when every tenant's day
closed in a truthful state, 1 when discovery collapsed, 3 when at least one
tenant's delivery genuinely failed, 5 when the §05 subscription lifecycle did
not complete for somebody. See :func:`exit_code_for`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, with_loader_criteria

from career.config import (
    PRICE_TEST_MAX_DAYS,
    PriceTestState,
    get_settings,
    price_test_window,
)
from career.db.models import DiscoveryRun, PlanEntitlement, Subscription, Tenant
from career.engine.enrichment import UrllibPageFetcher
from career.engine.run import RunReport, run_nightly
from career.engine.sources import HttpSearchApiClient, PythonJobSpyClient
from career.logging_filters import install_secret_redaction
from career.salla.provisioning import TOKEN_EXPIRY_WARN_DAYS
from career.whatsapp.client import (
    TokenHealth,
    TokenVerdict,
    inspect_token,
    token_problems,
)

logger = logging.getLogger("career.engine.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_nightly",
        description="One nightly discovery/gate/rank run (whitepaper §06).",
    )
    parser.add_argument(
        "--digest-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="record the run as digest-only (D9: no send, no ledgers). "
             "AUDIT ح-3: the default now FOLLOWS --deliver (delivering run "
             "⇒ not digest-only) so the record can never contradict what "
             "actually happened; pass the flag explicitly to override.",
    )
    # None ⇒ fall back to Settings (§06: caps live in configuration)
    parser.add_argument("--max-per-query", type=int, default=None)
    parser.add_argument("--retrieval-cap", type=int, default=None)
    parser.add_argument("--enrich-cap", type=int, default=None)
    parser.add_argument(
        "--tenant", type=uuid.UUID, action="append", default=None,
        help="scope the run to specific tenant id(s); repeatable",
    )
    parser.add_argument(
        "--include-weekend",
        action="store_true",
        help="manual canary runs only — deliver on a Riyadh weekend too",
    )
    parser.add_argument(
        "--deliver",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the delivery phase after discovery (Sun-Thu, skipped "
             "automatically without WhatsApp credentials); --no-deliver is "
             "the explicit off form (D9).",
    )
    return parser


def _preferred_daily(settings: Any) -> Any:
    """The cheapest APPROVED daily template, asked at run time.

    Never blocks or fails the run — but the FALLBACK is the expensive part, so
    it is chosen deliberately rather than by accident. When Meta cannot be
    reached we cannot know what is approved, and the long-standing name Meta
    classified MARKETING is the only one we have ever seen it accept. A slow
    Meta at eleven in the morning therefore bills the whole day at roughly
    three times the utility rate AND exposes it to per-user marketing caps, so
    the fallback is announced loudly instead of taken in silence.

    The listing is PAGINATED. Reading only the first page silently drops
    templates once the account grows past a page — including, eventually, the
    utility one this function exists to find.
    """
    from career.whatsapp.templates import preferred_daily_template

    if not (settings.whatsapp_access_token and settings.whatsapp_waba_id):
        return preferred_daily_template(None)

    def _fallback(why: str) -> Any:
        chosen = preferred_daily_template(None)
        logger.error(
            "could not confirm approved templates (%s) — falling back to %s "
            "(%s), which bills at the marketing rate", why, chosen.name,
            chosen.category,
        )
        return chosen

    try:
        import httpx

        approved: set[str] = set()
        url: str | None = (
            f"https://graph.facebook.com/v21.0/{settings.whatsapp_waba_id}"
            "/message_templates"
        )
        params: dict[str, Any] | None = {"fields": "name,status", "limit": 100}
        headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
        for _ in range(10):          # a page cap, not a trust boundary
            if url is None:
                break
            response = httpx.get(url, params=params, headers=headers,
                                 timeout=20.0)
            if response.status_code != 200:
                return _fallback(f"HTTP {response.status_code}")
            body = response.json()
            approved |= {
                row.get("name") for row in body.get("data", [])
                if row.get("status") == "APPROVED"
            }
            url = ((body.get("paging") or {}).get("next"))
            params = None            # the `next` URL already carries them
    except Exception:  # noqa: BLE001 — a template probe never stops delivery
        logger.warning("template probe failed", exc_info=True)
        return _fallback("probe raised")

    chosen = preferred_daily_template(approved)
    logger.info("daily template: %s (%s)", chosen.name, chosen.category)
    return chosen


def summarize(
    report: RunReport, tenant_codes: dict[uuid.UUID, str]
) -> dict[str, Any]:
    """The stdout/journal shape: status honest, tenants keyed by TEN code."""
    return {
        "run_id": str(report.run_id),
        "status": report.status,
        "counts": report.counts,
        "tenants": {
            tenant_codes.get(tenant_id, str(tenant_id)): payload
            for tenant_id, payload in report.per_tenant.items()
        },
    }


#: The codes the operator's runbook keys off. They are a vocabulary, not a
#: severity scale: systemd only ever asks «is it zero», but the number is what
#: the human reads in ``systemctl status`` and it must point at ONE runbook
#: entry. 2 was already spent on «SEARCHAPI_API_KEY is empty».
EXIT_OK = 0
EXIT_DISCOVERY_FAILED = 1
EXIT_NO_SEARCH_KEY = 2
EXIT_DELIVERY_FAILED = 3
#: 4 is the twin of 2 and points at the same shape of runbook entry: a
#: credential is missing, so the run could not even attempt the thing it exists
#: to do. It is NOT 3 — 3 sends the operator hunting for which customer broke,
#: and here nobody broke: nobody was served at all.
EXIT_NO_WHATSAPP_TOKEN = 4
#: 5 is the §05 subscription lifecycle — the sweep that runs BEFORE the engine
#: and moves periods to grace, expires them, and sends the renewal and recovery
#: nudges. Its own runbook entry, because what the operator has to do about it
#: is nothing like 3: nobody's CV is missing, a customer's renewal CLOCK did not
#: tick, and the journal line names which customer so the next run can be
#: watched. Until 2026-08-07 this failure had no number at all — the sweep was
#: wrapped in a broad `except` that logged and let the night exit 0.
EXIT_LIFECYCLE_FAILED = 5

#: The four things ``delivery_phase`` can say, named rather than spelled out at
#: every call site: the exit code now keys off one of them, and a verdict that
#: turns on a string literal typed twice is a verdict one typo from silence.
PHASE_RAN = "ran"
PHASE_NO_DELIVER = "skipped:no-deliver"
PHASE_NO_CREDENTIALS = "skipped:no-whatsapp-credentials"
PHASE_NO_TENANTS = "skipped:no-tenants"


def delivery_phase_for(
    *, deliver: bool, has_whatsapp_token: bool, has_tenants: bool
) -> str:
    """Why the delivery phase did or did not run — the reason, not the ashes.

    Lifted out of ``main`` because the verdict now turns on it: one of these
    four answers means «total outage» and the other three do not, and a
    decision with a customer-facing consequence does not belong in the one
    function in this module that no test can reach. The order matters and is
    the operator's own reading order: what he asked for, then what he is
    missing, then who there was to serve.
    """
    if not deliver:
        return PHASE_NO_DELIVER
    if not has_whatsapp_token:
        return PHASE_NO_CREDENTIALS
    if not has_tenants:
        return PHASE_NO_TENANTS
    return PHASE_RAN

#: The four day states that mean a human has to look at tonight (§15.12).
FAILED_DAY_STATES: frozenset[str] = frozenset({
    "DISCOVERY_FAILED", "CV_GENERATION_FAILED", "WHATSAPP_FAILED",
    "LEDGER_FAILED",
})
#: The four that are the truth arriving safely. NO_MATCHES is the one that
#: keeps being mistaken for a fault: a day with no fitting job is the gate
#: doing its job, and paging the operator for it teaches him to mute the pager.
#: SKIPPED_OPTED_OUT is the same shape — the customer asked for silence and
#: got it. PARTIAL_DELIVERY already reached the customer with what worked.
HONEST_DAY_STATES: frozenset[str] = frozenset({
    "DELIVERED", "PARTIAL_DELIVERY", "NO_MATCHES", "SKIPPED_OPTED_OUT",
})


def exit_code_for(
    status: str,
    delivery_states: Iterable[str] | None = None,
    *,
    delivery_phase: str | None = None,
    lifecycle_failed: bool = False,
) -> int:
    """The night's verdict as one number — §15 constant 12.

    THE INCIDENT. On 2 August 2026 the run closed the only real customer's day
    ``CV_GENERATION_FAILED`` (a UniqueViolation on the one-delivery-per-day
    constraint, full traceback in the journal) and returned 0. The delivery
    phase had never been allowed to influence the return value, so
    ``OnFailure=career-alert@%n.service`` did not fire, systemd recorded a
    clean success, and the failure was found by reading logs days later.

    ANY tenant failing is enough to fail the night, deliberately. The
    alternative — a threshold, or «most of them worked» — sounds reasonable
    at a hundred customers and is indefensible at one: today a single failure
    IS the whole night, and a rule that stays quiet while one paying customer
    receives nothing is exactly the rule that produced the incident. The exit
    code is a summons, not a statistic; WHO failed is in the journal summary
    and in the admin channel, which is where per-customer detail belongs.

    ``discovery_failed`` still outranks, and keeps code 1. On that path every
    tenant's day closes DISCOVERY_FAILED anyway, so both rules agree on «not
    zero»; 1 is kept because it points at a different runbook entry (every
    source down — check the providers) than 3 (discovery worked, the delivery
    pipeline broke for someone).

    An EMPTY or missing ``delivery_states`` is a 0. Three honest nights
    produce no day state at all — a Riyadh weekend (§08 delivers Sun–Thu),
    a re-run on a day whose tenants were already served, and a bundle held
    for a closed WhatsApp window that the descend path closes later. None of
    them is a failure, and the weekly Friday page they would cause is how a
    truthful alert gets ignored. What the emptiness MEANS is reported instead,
    in ``summarize_delivery``.

    ``delivery_phase`` is the ONE emptiness that is not honest. When
    ``WHATSAPP_ACCESS_TOKEN`` is blank the delivery phase never runs at all:
    no tenant is served, no day state is written, ``delivery`` is ``{}`` — and
    under the rule above that is a 0, every night, forever, for a total
    outage. The credential this depends on is a temporary Meta token that has
    been on the «replace before it dies» list since 17 July, so this is the
    likeliest way the product goes dark, and the emptiness rule was exactly
    the wrong shape to catch it. It gets code 4 rather than 3 because the
    runbook entry is «put a token in the file», not «find out whose delivery
    broke». ``--no-deliver`` (a digest run the operator asked for) and «no
    tenants tonight» stay 0: both are intended.

    ``lifecycle_failed`` is the §05 sweep, and it is here because of what the
    2026-08-07 fix changed UNDERNEATH it. That sweep used to run every customer
    in one transaction under one broad ``except``, so any failure discarded the
    whole night's `subscription_events` marks — and then the run carried on and
    exited 0. It now commits per customer and keeps going, which is the right
    behaviour and is also exactly how a failure becomes invisible: the night
    finishes, most customers were swept, and the one whose transaction rolled
    back is a log line nobody reads. «أي عميل يفشل ← الليلة تفشل» is not
    satisfied by surviving a customer's failure, so the survival is paid for
    with a number. It ranks BELOW delivery: a customer who received nothing
    today (3) outranks a customer whose renewal clock did not tick tonight (5),
    and the sweep is idempotent, so tomorrow's run redoes exactly the work that
    rolled back.

    An unrecognised state counts as a failure: a ninth day state added
    without deciding its side of this line should shout, not go quiet.
    """
    if status == "discovery_failed":
        # kept first: every tenant closed DISCOVERY_FAILED on that path, so
        # both rules agree on «not zero», and 1 names the bigger fire.
        return EXIT_DISCOVERY_FAILED
    if delivery_phase == PHASE_NO_CREDENTIALS:
        return EXIT_NO_WHATSAPP_TOKEN
    if any(state not in HONEST_DAY_STATES for state in (delivery_states or ())):
        return EXIT_DELIVERY_FAILED
    if lifecycle_failed:
        return EXIT_LIFECYCLE_FAILED
    return EXIT_OK


def summarize_delivery(
    *,
    tenant_codes: dict[uuid.UUID, str],
    intended: Iterable[uuid.UUID],
    states: dict[uuid.UUID, str],
    expired: Iterable[tuple[uuid.UUID, date, str]] = (),
) -> dict[str, Any]:
    """The delivery half of the journal line, with its silences named.

    AUDIT P0-7: the summary used to print ``"delivery": {}`` on eleven of the
    last sixteen nights, and the audit read that as a reporting bug. It is
    not — the dict faithfully mirrors what ``run_daily_delivery`` returned,
    and it returns nothing for a weekend, for a tenant already served today,
    and for a bundle held until the customer's window reopens. The bug was
    that all three, plus «nothing happened at all», printed identically. So
    the tenants the run INTENDED to serve but did not close are now listed by
    TEN code: an empty ``delivery`` beside a populated ``delivery_unclosed``
    is a legible night, and an empty ``delivery`` beside an empty
    ``delivery_unclosed`` truly means there was no one to serve.

    ``delivery_expired`` is the third channel, and it is a LIST of rows rather
    than a code→state map because these days are not tonight: they are the
    PREVIOUS days the stale-bundle sweep closed at the top of this run, they
    carry their own date, and one tenant can bring more than one. Folding them
    into ``delivery`` by tenant code would have been shorter and would have
    dropped a day every time a tenant was both expired and served — see
    ``cv.daily_run.run_daily_delivery``. Until this key existed those closures
    reached no reader at all: not the journal, not the exit code.
    """
    return {
        "delivery": {
            tenant_codes.get(tid, "TEN-????"): state
            for tid, state in states.items()
        },
        "delivery_unclosed": sorted(
            tenant_codes.get(tid, "TEN-????")
            for tid in intended if tid not in states
        ),
        "delivery_expired": [
            {
                "tenant": tenant_codes.get(tid, "TEN-????"),
                "run_date": str(run_date),
                "state": state,
            }
            for tid, run_date, state in expired
        ],
    }


# ── boot verification: the environment against the database (P0-8) ─────────


@dataclass(frozen=True)
class EnvProblem:
    """One provable contradiction between ``.env`` and the money trail.

    ``key`` is the environment variable a human has to edit; ``english`` goes
    to the journal (the operator's harvester forwards ``ERROR:`` lines) and
    ``arabic`` to the admin channel. The Arabic line carries NO Latin token —
    a variable name inside an Arabic sentence breaks the line's direction in
    his client, so the names are listed separately by :func:`format_env_alert`.

    ``notice`` is the one row that is NOT a contradiction: a state the operator
    put the system into on purpose, which must still be said out loud every
    morning so it cannot be forgotten. It exists because the alternative was
    worse in both directions — stay silent about an open price-test window and
    it outlives the test; print it under «الإعدادات تخالف قاعدة البيانات» with
    a variable name to «correct» and the daily red alert becomes noise on a day
    when nothing is wrong. A notice is logged at WARNING, not ERROR, and its
    key is never listed as a thing to fix.
    """

    key: str
    english: str
    arabic: str
    notice: bool = False


class TokenState(StrEnum):
    """The four answers the credential store can give, as four names.

    They were three names and a ``None`` until 2026-08-07, and the collapsed
    pair cost the worst instruction this file can print — see
    :attr:`ABSENT` and :attr:`UNDATED`.
    """

    #: The store said how many days are left. ``days_left`` is a number.
    MEASURED = "measured"
    #: A credential IS held and its expiry was never recorded. The sale path
    #: works right now; what is missing is a date. `store_credentials` creates
    #: this state deliberately (``expires_at=None`` clears the expiry rather
    #: than keeping a wrong one), so it is a normal state, not a corruption.
    UNDATED = "undated"
    #: The store answered and holds no credential at all. THE ONE STATE that
    #: earns «reinstall the app», because reinstalling is the only way a new
    #: credential is ever issued — and because there is nothing to lose.
    ABSENT = "absent"
    #: Nobody could be asked: the module is not deployed on this host, or it
    #: raised. The check degrades to the configured date.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class TokenLife:
    """What the credential store answers about the token that EXISTS.

    The boot check used to read ``SALLA_TOKEN_EXPIRES_AT`` — a date a human
    typed into a file — and that is a report about what was CONFIGURED, not
    about what is left. On 2026-07-29 the two parted company: the file kept
    saying what somebody had written down while the credential itself died,
    and nine days of paid orders failed to provision. So the check now asks
    the store that holds the live credential, and falls back to the file only
    when there is no store to ask.

    WHY THIS IS A STATE AND NOT AN ``int | None``. It used to be exactly that,
    and it documented ``days_left is None`` as «the store answered, and its
    answer is that it holds NO credential». ``tokens.days_left()`` returns
    ``None`` for two facts, and the other one is «a credential we cannot
    date» — so with a live token stored, this check printed «the credential
    store holds NO Salla token … only reinstalling the app on the store issues
    a new one». Reinstalling fires ``app.uninstalled`` and the red «البيع
    واقف» alert: the instruction for the emptiest state was being given for a
    working one. ``scripts/refresh_salla_token.py`` read the same ``None`` as
    «we do not know». The fix is not a comment reminding the next caller which
    it is — it is that there is nothing left to remember: construct one of the
    four states, and the invariant below refuses the pairs that mean nothing.
    """

    state: TokenState
    days_left: int | None = None

    def __post_init__(self) -> None:
        measured = self.state is TokenState.MEASURED
        if measured != (self.days_left is not None):
            raise ValueError(
                f"TokenLife({self.state}) cannot carry days_left="
                f"{self.days_left!r}"
            )

    @property
    def readable(self) -> bool:
        """Was the store asked at all? The fallback to the configured date
        hangs on this, and on nothing else."""
        return self.state is not TokenState.UNREADABLE

    @classmethod
    def measured(cls, days_left: int) -> TokenLife:
        return cls(TokenState.MEASURED, days_left)

    @classmethod
    def undated(cls) -> TokenLife:
        return cls(TokenState.UNDATED)

    @classmethod
    def absent(cls) -> TokenLife:
        return cls(TokenState.ABSENT)

    @classmethod
    def unreadable(cls) -> TokenLife:
        return cls(TokenState.UNREADABLE)

    @classmethod
    def from_store(cls, life: Any) -> TokenLife:
        """Translate ``career.salla.tokens.CredentialLife``.

        Duck-typed on purpose: this module must import the credential store
        lazily (see :func:`read_token_life`) and a boot check that cannot start
        on a host without it is a boot check that stops a boot.
        """
        if not life.present:
            return cls.absent()
        if life.days_left is None:
            return cls.undated()
        return cls.measured(int(life.days_left))


def parse_product_catalog(raw: str) -> dict[str, str]:
    """``SALLA_PRODUCT_CATALOG``: product id → plan code. Tolerant by design —
    a malformed map must not stop a process, it must be REPORTED (an empty
    catalog is itself one of the conditions the boot check names)."""
    try:
        parsed = json.loads(raw or "{}")
        return {str(k): str(v) for k, v in parsed.items()}
    except (ValueError, AttributeError):
        return {}


def parse_product_pricing(raw: str) -> dict[str, tuple[Decimal, str]]:
    """``SALLA_PRODUCT_PRICING``: product id → (amount, currency), the §09
    triple-match prices. Same tolerance, same reason."""
    try:
        parsed = json.loads(raw or "{}")
        return {
            str(k): (Decimal(str(v[0])), str(v[1]))
            for k, v in parsed.items()
        }
    except (ValueError, AttributeError, LookupError, ArithmeticError):
        return {}


def approved_plan_prices(session: Session) -> dict[str, Decimal]:
    """plan code → the price the DATABASE says that plan costs.

    ``indicative_price_sar`` is not the money path (the §09 match reads the
    real amount from the environment) but it is what the watchtower's revenue
    view quotes to the operator, so the environment disagreeing with it means
    one of the two is lying about what a customer pays.
    """
    return {
        code: Decimal(price)
        for code, price in session.execute(
            select(PlanEntitlement.plan_code,
                   PlanEntitlement.indicative_price_sar)
        ).all()
    }


def canonical_sale_plans() -> dict[str, Decimal]:
    """plan code → the exact price the store must charge, for plans ON SALE.

    Sellability is not a column. Migration 0020 retired `basic` without
    touching a single row — deleting it would orphan the historical
    subscriptions that point at it, so «retired» is expressed as ABSENCE from
    the one table that decides what a product may be wired to:
    ``scripts/wire_salla_products.PLANS``, the table the wiring tool writes
    the catalog FROM. Reading it here rather than restating it is the whole
    point: a hardcoded list in this file would be a fourth copy of a fact
    that already has three, and the fourth is the one nobody updates.

    Raises RuntimeError when the table cannot be read — an unverifiable
    check must say so out loud rather than pass by default.
    """
    import importlib.util

    path = (Path(__file__).resolve().parents[3] / "scripts"
            / "wire_salla_products.py")
    spec = importlib.util.spec_from_file_location("_career_sale_plans", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"canonical plan table unreadable at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {str(plan): Decimal(str(price))
            for plan, price in module.PLANS.values()}


def verify_environment(
    settings: Any,
    *,
    approved_prices: dict[str, Decimal],
    sale_plans: dict[str, Decimal],
    today: date,
    warn_days: int = TOKEN_EXPIRY_WARN_DAYS,
    token_life: TokenLife | None = None,
    token_health: TokenHealth | None = None,
) -> list[EnvProblem]:
    """Every way the selling environment can contradict the database.

    THE INCIDENT. For twelve days the environment sold product 786318419 as
    plan `basic` at 149.00 SAR: a plan migration 0020 had retired and a price
    the store no longer charges. Nothing said a word. The §09 triple match is
    correct and fails CLOSED, so the first symptom would have been a real
    buyer paying 199 and being refused with AMOUNT_MISMATCH and a TERMINAL
    status on his order — the one failure mode this product cannot afford.
    A boot warning for an EMPTY catalog already existed; a wrong catalog was
    invisible, which is worse, because it looks configured.

    Pure: it takes the two authorities as arguments so the whole matrix is
    testable without a database, a store or a clock.
    """
    problems: list[EnvProblem] = []
    catalog = parse_product_catalog(settings.salla_product_catalog)
    pricing = parse_product_pricing(settings.salla_product_pricing)
    # getattr, not attribute access: the settings object here is whatever the
    # caller passed, and a boot check that raises AttributeError on a host
    # whose file predates this variable is a boot check that stops a boot.
    window = price_test_window(
        getattr(settings, "salla_price_test_until", ""), today
    )
    #: cataloged products currently priced BELOW their plan's approved price —
    #: i.e. what «the store is at a test price» actually looks like from here.
    cheap: list[str] = []

    for product_id, plan_code in sorted(catalog.items()):
        if plan_code not in approved_prices:
            problems.append(EnvProblem(
                "SALLA_PRODUCT_CATALOG",
                f"product {product_id} maps to plan {plan_code!r}, which has "
                "no row in plan_entitlements — an order for it cannot be "
                "provisioned",
                "منتج في الكتالوج مربوط بباقة غير موجودة في قاعدة البيانات",
            ))
            continue
        if plan_code not in sale_plans:
            problems.append(EnvProblem(
                "SALLA_PRODUCT_CATALOG",
                f"product {product_id} maps to plan {plan_code!r}, which is "
                "RETIRED from sale — a purchase would provision a plan the "
                "store no longer offers",
                "منتج في الكتالوج مربوط بباقة متوقفة عن البيع",
            ))
        # ح-4 kept: a cataloged product with no price fails closed at §09, so
        # the coverage hole is still announced at boot, not at refund time.
        if product_id not in pricing:
            problems.append(EnvProblem(
                "SALLA_PRODUCT_PRICING",
                f"cataloged product {product_id} has no price — its orders "
                "will FAIL CLOSED to manual review",
                "منتج في الكتالوج بلا سعر مضبوط — طلباته ستُحوّل للمراجعة "
                "اليدوية",
            ))
            continue
        amount, currency = pricing[product_id]
        expected = approved_prices[plan_code]
        if currency.upper() != "SAR":
            problems.append(EnvProblem(
                "SALLA_PRODUCT_PRICING",
                f"product {product_id} is priced in {currency!r}; the "
                "approved price is in SAR, so the two cannot be compared",
                "سعر منتج مضبوط بعملة غير الريال",
            ))
        elif amount != expected:
            if amount < expected:
                cheap.append(product_id)
            if amount < expected and window.suppresses_capture:
                # THE ALARM THAT USED TO BE FALSE. §09 keys on the product id
                # and this product carries its own price, so an order at the
                # test price provisions perfectly — «a real order will be
                # refused with AMOUNT_MISMATCH» was simply not true, and a
                # daily ERROR that is not true is how the next true one gets
                # skipped. What IS true is worth a line of its own: the store
                # is knowingly cheap, and while it is, nobody's founding price
                # is being recorded.
                problems.append(EnvProblem(
                    "SALLA_PRICE_TEST_UNTIL",
                    f"price test window OPEN until {window.raw}: product "
                    f"{product_id} (plan {plan_code!r}) is deliberately at "
                    f"{amount} SAR instead of {expected} SAR. Its orders "
                    "provision normally and NO founder price lock is captured "
                    "while this lasts",
                    "نافذة سعر الاختبار مفتوحة — المنتجات بسعر رمزي مقصود\n"
                    "الطلبات تُزوَّد عادي ولا يُسجَّل قفل سعر لأي مشترٍ\n"
                    "تنتهي النافذة في\n"
                    f"{window.raw}",
                    notice=True,
                ))
            elif amount < expected and window.state is PriceTestState.EXPIRED:
                # The self-closing date did its job and the products did not.
                # From this morning capture is armed again over a test price:
                # every new buyer is recording one riyal as his founding price,
                # which authorises buying the real pass for it and fails his
                # next real payment closed.
                problems.append(EnvProblem(
                    "SALLA_PRODUCT_PRICING",
                    f"the price test window CLOSED on {window.raw} and product "
                    f"{product_id} (plan {plan_code!r}) is STILL at {amount} "
                    f"SAR instead of {expected} SAR — founder price locks are "
                    "being captured again, so every purchase from now records "
                    "the test price as that customer's permanent founding "
                    "price. Restore the store price and re-run "
                    "scripts/wire_salla_products.py",
                    "انتهت نافذة سعر الاختبار والمنتجات ما زالت بالسعر الرمزي\n"
                    "كل عملية شراء الآن تُسجّل السعر الرمزي كسعر مقفول دائم "
                    "للمشتري\n"
                    "أعد أسعار المتجر ثم أعد تشغيل أداة الربط",
                ))
            else:
                problems.append(EnvProblem(
                    "SALLA_PRODUCT_PRICING",
                    f"product {product_id} (plan {plan_code!r}) is priced "
                    f"{amount} SAR; the database approves {expected} SAR — a "
                    "real order will be refused with AMOUNT_MISMATCH",
                    "سعر منتج في الإعدادات لا يطابق السعر المعتمد في قاعدة "
                    "البيانات — الطلب الحقيقي سيُرفض",
                ))

    problems.extend(_price_test_window_problems(window, cheap, today))

    # The third copy of the same fact: the wiring table and the database must
    # agree about what a plan costs, or fixing one of them fixes nothing.
    for plan_code, price in sorted(sale_plans.items()):
        if plan_code in approved_prices and approved_prices[plan_code] != price:
            problems.append(EnvProblem(
                "SALLA_PRODUCT_PRICING",
                f"plan {plan_code!r} costs {price} SAR in the canonical sale "
                f"table but {approved_prices[plan_code]} SAR in "
                "plan_entitlements — the two authorities disagree",
                "جدول الأسعار المعتمد وقاعدة البيانات لا يتفقان على سعر باقة",
            ))

    problems.extend(
        _token_expiry_problems(settings, today, warn_days, token_life)
    )

    # The credential the whole product speaks through, and the one this check
    # did not look at. It watched four Salla facts — all of them about SELLING,
    # all of them degrading the new-order path only — while the token that
    # DELIVERS to everyone who already bought was unwatched, and it is the
    # temporary one: a Meta user token that has been «replace it before it
    # dies» in the project notes since 17 July. Empty means the nightly run
    # skips its delivery phase entirely and every paying customer gets nothing
    # (see exit_code_for, which now also refuses to call that night a success).
    if not (getattr(settings, "whatsapp_access_token", "") or "").strip():
        problems.append(EnvProblem(
            "WHATSAPP_ACCESS_TOKEN",
            "empty — the nightly run skips delivery entirely and NO customer "
            "receives anything",
            "توكن واتساب غير مضبوط — التسليم اليومي لن يعمل ولن يصل العملاء شيء",
        ))
    elif not (getattr(settings, "whatsapp_phone_number_id", "") or "").strip():
        # A token with nothing to send FROM fails at the first Graph call, per
        # message, for every customer — the same outage one layer down.
        problems.append(EnvProblem(
            "WHATSAPP_PHONE_NUMBER_ID",
            "empty — the WhatsApp client has no number to send from, so every "
            "delivery attempt fails at Meta",
            "رقم واتساب المرسِل غير مضبوط — كل محاولة إرسال سترفضها ميتا",
        ))
    else:
        # CONFIGURED is not ALIVE. Everything above this line asks whether
        # somebody typed something into a file — the exact question that was
        # being asked about `SALLA_TOKEN_EXPIRES_AT` on 29 July while the
        # credential it described was already dead. `token_health` is Meta's
        # own answer about the token that EXISTS, read on this run.
        problems.extend(whatsapp_token_problems(token_health, warn_days))

    if not (settings.salla_store_url or "").strip():
        # §16: lifecycle.py degrades to a link-less renewal message rather
        # than printing a broken URL, which is right — and silent. A customer
        # whose subscription just ended is told to renew with no way to.
        problems.append(EnvProblem(
            "SALLA_STORE_URL",
            "empty — renewal and expiry messages go out with no way for the "
            "customer to renew",
            "رابط المتجر غير مضبوط — رسائل التجديد تصل بلا رابط",
        ))
    return problems


def _price_test_window_problems(
    window: Any, cheap: list[str], today: date
) -> list[EnvProblem]:
    """The window itself, judged apart from any one product's price.

    Three states are worth a morning of the operator's attention, and all
    three are ways the window and the store can disagree with each other:

    UNREADABLE — somebody meant to open a window and typed something that is
    not a date. `price_lock` treats that as OPEN (a lock captured at a test
    price is permanent money; a lock not captured is not), so the promise is
    switched off with no closing date at all. Loud, daily, until it is fixed
    or cleared.

    OPEN with nothing cheap — the mirror of the trap, and the reason a naked
    boolean flag was rejected: the window is suspending every new customer's
    founding price while the store charges full price for it. Nothing is
    broken today; the promise simply is not being kept, silently, and the fix
    is one line.

    OPEN for too long — the bound the wiring tool refuses to write is asserted
    again here, because the file can also be edited by hand.

    EXPIRED is deliberately silent when the prices are back: it is then the
    same state as ABSENT, and nagging about a stale date that changes nothing
    is how the channel that carries the other three gets muted.
    """
    if window.state is PriceTestState.UNREADABLE:
        return [EnvProblem(
            "SALLA_PRICE_TEST_UNTIL",
            f"unreadable date {window.raw!r} — it is treated as an OPEN price "
            "test window with no closing day, so NO customer's founding price "
            "is being recorded. Set it to an ISO date or clear it",
            "تاريخ نافذة سعر الاختبار غير مقروء — قفل سعر المؤسسين معطّل بلا "
            "موعد انتهاء\n"
            "اضبط التاريخ أو امسحه",
        )]
    if window.state is not PriceTestState.OPEN:
        return []
    problems: list[EnvProblem] = []
    if not cheap:
        problems.append(EnvProblem(
            "SALLA_PRICE_TEST_UNTIL",
            f"a price test window is open until {window.raw} but every "
            "cataloged product is at its approved price — the founder price "
            "lock is suspended for nothing, and every customer who buys today "
            "gets no locked price. Clear it",
            "نافذة سعر الاختبار مفتوحة والمنتجات بأسعارها الحقيقية\n"
            "قفل سعر المؤسسين معطّل بلا سبب — امسح تاريخ النافذة",
        ))
    remaining = window.days_remaining(today)
    if remaining is not None and remaining > PRICE_TEST_MAX_DAYS:
        problems.append(EnvProblem(
            "SALLA_PRICE_TEST_UNTIL",
            f"the price test window runs {remaining} more days (until "
            f"{window.raw}); the maximum is {PRICE_TEST_MAX_DAYS}. Every "
            "customer who buys before it closes carries no founding price",
            "نافذة سعر الاختبار مفتوحة لمدة أطول من المسموح\n"
            "كل من يشترك قبل إغلاقها لن يكون له سعر مقفول",
        ))
    return problems


#: What the operator can actually DO, in one Arabic clause, on every line that
#: reports a dying credential. Salla's Easy Mode never shows the token to a
#: human, so «renew it» is not an instruction anyone can follow: reinstalling
#: the app on the store re-fires `app.store.authorize`, which is the only
#: moment a replacement credential is ever offered (CHANGELOG §29).
_REINSTALL_AR = "أعد تثبيت التطبيق على متجرك من لوحة سلة"

#: The English half of the same instruction, for the journal line.
_REINSTALL_HINT = (
    "reinstall the app on the store (that re-fires app.store.authorize, which "
    "is the only way a new credential is ever issued)"
)


def _token_expiry_problems(
    settings: Any, today: date, warn_days: int,
    token_life: TokenLife | None = None,
) -> list[EnvProblem]:
    """A Salla token dies quietly: the first symptom is a paid order that
    provisions nothing. The existing warning lives inside the provisioning
    path, so it only speaks when an order is already being handled — too late
    to be a warning. This one speaks at boot, before anyone buys.

    TWO SOURCES, AND THEY ARE NOT EQUAL. When the credential store can be
    asked, its answer wins: it measures what is LEFT of the token that
    actually exists, and the environment variable measures what a human wrote
    down once. The 2026-07-29 outage is precisely the gap between the two.
    The file remains the fallback for a host where the store is not deployed,
    and there its wording and thresholds are unchanged.

    WHY THE LADDER, AND WHY IT IS SILENT ABOVE THE MARGIN. The refresher
    (scripts/refresh_salla_token.py) renews at five days or fewer, daily, at
    09:00 Riyadh. A boot check that finds five days left at 11:00 is therefore
    looking at a day the automation ALREADY failed — which is why the
    threshold is the margin itself and not something larger: above it, a
    healthy system would be warned about every single day, and a warning that
    fires for weeks while nothing is wrong is how a channel gets muted. Below
    it, the sentence escalates as the days run out — «قارب» then «يوشك»
    then «منتهي» — and each one carries a number that has changed since the
    last, so a reminder can never be mistaken for a repetition.
    """
    if token_life is not None and token_life.readable:
        return _stored_token_problems(token_life, warn_days)
    raw = (settings.salla_token_expires_at or "").strip()
    if not raw:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            "unset — nothing can tell whether the Salla token is still alive",
            "تاريخ انتهاء توكن سلة غير مضبوط — لا أحد يعرف إن كان صالحًا",
        )]
    try:
        expires = date.fromisoformat(raw[:10])
    except ValueError:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            f"unparseable date {raw!r} — the expiry watch is silently blind",
            "تاريخ انتهاء توكن سلة غير مقروء — مراقبة الصلاحية معطّلة",
        )]
    days_left = (expires - today).days
    if days_left < 0:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            f"EXPIRED {-days_left} days ago — paid orders will not provision",
            "توكن سلة منتهي — الطلبات المدفوعة لن تُزوَّد",
        )]
    if days_left <= warn_days:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            f"expires in {days_left} days — refresh it before it dies",
            "توكن سلة قارب على الانتهاء",
        )]
    return []


def _stored_token_problems(
    life: TokenLife, warn_days: int
) -> list[EnvProblem]:
    """The store's answer, turned into an escalating ladder.

    It takes the whole :class:`TokenLife` and not a bare ``int | None``, which
    is the point of the 2026-08-07 fix: the argument that used to be passed
    here could not distinguish «no credential» from «a credential with no
    recorded expiry», and this function printed the reinstall instruction for
    both. There is no such argument any more.

    Nothing here is fatal and nothing here can be — see
    :func:`report_environment`. Every rung is a warning that names what is
    left and what to do about it.
    """
    if life.state is TokenState.ABSENT:
        # The store answered and it holds nothing. Not «we do not know»: we
        # know, and the answer is that there is no credential at all, so no
        # order can be provisioned and no refresh can even be attempted. This
        # is the ONE rung that may ask for a reinstall — everywhere else a
        # live credential is on disk and reinstalling would fire
        # `app.uninstalled` and take it down.
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            "the credential store holds NO Salla token — paid orders cannot "
            "provision and there is nothing for the refresher to renew; only "
            "reinstalling the app on the store issues a new one",
            f"لا يوجد اعتماد سلة محفوظ إطلاقًا — {_REINSTALL_AR}",
        )]
    if life.state is TokenState.UNDATED:
        # A CREDENTIAL IS HELD. Nothing is down, and the operator must not be
        # sent to do the one thing that would take it down. What is missing is
        # a date: `store_credentials` clears the expiry when an authorize
        # payload carries none, and the refresher treats an unknown runway as
        # urgent, so a renewal is already scheduled for tonight. The instruction
        # is therefore «wait for it, and look if it does not happen» — not
        # «reinstall».
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            "a Salla credential IS stored but its expiry was never recorded — "
            "nothing can say how long it has; the automatic refresh reads an "
            "unknown runway as urgent and will attempt a renewal at its next "
            "run. Do NOT reinstall the app: that would revoke the credential "
            "that is working right now",
            "يوجد اعتماد سلة محفوظ لكن تاريخ انتهائه غير مسجّل — "
            "التجديد التلقائي سيحاول تجديده في تشغيله القادم، ولا داعي لإعادة "
            "تثبيت التطبيق",
        )]
    # MEASURED, and the invariant in `TokenLife.__post_init__` is what makes
    # that a fact rather than a hope: the other three states returned above.
    days_left = life.days_left if life.days_left is not None else 0
    if days_left < 0:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            f"EXPIRED {-days_left} days ago — paid orders will not provision; "
            f"the automatic refresh has not recovered it, so {_REINSTALL_HINT}",
            f"توكن سلة منتهي والطلبات المدفوعة لا تُزوَّد — {_REINSTALL_AR}",
        )]
    if days_left <= 1:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            f"expires in {days_left} days and the automatic refresh has "
            f"failed every attempt inside the margin — {_REINSTALL_HINT}",
            f"توكن سلة يوشك أن ينتهي والتجديد التلقائي يفشل — {_REINSTALL_AR}",
        )]
    if days_left <= warn_days:
        return [EnvProblem(
            "SALLA_TOKEN_EXPIRES_AT",
            f"expires in {days_left} days — inside the refresh margin, so the "
            "automatic renewal should already have happened and did not; if "
            f"it does not recover today, {_REINSTALL_HINT}",
            f"توكن سلة قارب على الانتهاء والتجديد التلقائي لم ينجح — {_REINSTALL_AR}",
        )]
    return []


def whatsapp_token_problems(
    health: TokenHealth | None, warn_days: int = TOKEN_EXPIRY_WARN_DAYS,
) -> list[EnvProblem]:
    """Meta's live answer about the delivery credential, as boot-check rows.

    THE LADDER ITSELF LIVES IN ``career.whatsapp.client`` and not here, for a
    reason that is structural rather than aesthetic: the scheduled observer
    (``scripts/check_whatsapp_token.py``) has to log the same English
    sentences, and importing THIS module to get them would put that script on
    the wrong side of the D20 owner-role ratchet — `cli` builds an
    RLS-bypassing engine, `tests/test_rls_runtime_role.py` closes over import
    edges, and a credential watchdog that touches no database must not inherit
    superuser reach for four strings. So the wording lives at the Graph
    boundary, where the facts come from, and this is the adapter that turns it
    into the type the boot check speaks.

    WHAT IT WATCHES AND WHAT IT DELIBERATELY DOES NOT. The live credential is a
    Meta **System User** token: `/debug_token` reports ``expires_at: 0`` and
    ``data_access_expires_at: 0`` — it cannot expire, and there is no refresh
    endpoint for it, so NOTHING in this repository tries to renew it. What is
    watched is what can still take it away without warning: revocation, a
    withdrawn scope, a phone number unassigned from the System User, and the
    regression of somebody pasting a temporary token over the permanent one.

    ``None`` and UNREADABLE both return nothing. The first means the caller
    chose not to spend a network call (the worker's boot check — see
    :func:`read_token_health`); the second means Meta could not be reached,
    and putting Meta's uptime on the operator's phone is how the channel that
    must carry «the token is revoked» gets muted.
    """
    return [
        EnvProblem(problem.key, problem.english, problem.arabic)
        for problem in token_problems(health, warn_days)
    ]


def format_env_alert(problems: list[EnvProblem]) -> str:
    """The admin-channel message. Arabic prose and Latin variable names never
    share a line — mixing them reverses the line in the operator's client.

    Notices are separated from contradictions, and the header follows: a
    morning whose only news is «the price test window is open» must not arrive
    wearing the red banner that means «the configuration is wrong», and its
    variable must not appear under «المتغيرات المطلوب تصحيحها» — there is
    nothing to correct, and a list of things to fix that contains a thing that
    is fine is a list he stops reading.
    """
    faults = [problem for problem in problems if not problem.notice]
    notices = [problem for problem in problems if problem.notice]
    lines = ["🔴 فحص الإقلاع: الإعدادات تخالف قاعدة البيانات", ""] if faults \
        else ["🟡 فحص الإقلاع: تنبيه مؤقت", ""]
    seen: list[str] = []
    for problem in faults + notices:
        if problem.arabic not in seen:
            seen.append(problem.arabic)
            lines.append(f"• {problem.arabic}")
    if faults:
        lines.append("")
        lines.append("المتغيرات المطلوب تصحيحها:")
        lines.extend(sorted({problem.key for problem in faults}))
    return "\n".join(lines)


def read_token_life() -> TokenLife:
    """Ask the credential store how much of the live token is LEFT.

    Imported here rather than at module scope on purpose. This is a boot
    check: it must survive a host where ``career.salla.tokens`` is not
    deployed yet, and it must survive a store that raises. Either way the
    answer is «unreadable», the caller degrades to the date in the
    environment file, and nothing anywhere refuses to start — a boot check
    that can stop a process turns «sales are stopped» into «the product is
    down for the people who already paid» (see :func:`report_environment`).
    """
    if "pytest" in sys.modules:
        # A test process must not read the operator's live credential file.
        # Recognised the same way `career.db.session._process_escape` decides
        # a process is pytest — by the module, not by argv — and for the same
        # class of reason: otherwise every unit test of this pure check would
        # silently depend on the state of one machine's secrets, and a real
        # expired credential on the host would turn unrelated suites red while
        # a green suite would prove nothing about either. The ladder itself is
        # exercised by injecting :class:`TokenLife` (tests/
        # test_salla_token_refresh.py), which is what the argument is for.
        return TokenLife.unreadable()
    try:
        from career.salla import tokens

        # `tokens.life()`, not `tokens.days_left()`: the latter cannot tell
        # «no credential» from «a credential we cannot date», and this check
        # prints a different — and destructive — instruction for the two.
        return TokenLife.from_store(tokens.life())
    except Exception:  # noqa: BLE001 — an unreadable store is a fallback, not a fault
        logger.info(
            "no readable Salla credential store — falling back to the "
            "configured expiry date", exc_info=True,
        )
        return TokenLife.unreadable()


def read_token_health(settings: Any) -> TokenHealth:
    """Ask META what the delivery credential is — one read-only GET.

    OPT-IN, and that is the one place this diverges from
    :func:`read_token_life`. That one reads a file on this disk, so calling it
    unconditionally costs nothing and it is wired into every caller. This one
    goes to Graph, and one of the two processes that runs the boot check is
    the conversation worker under ``Restart=always``/``RestartSec=5``: a crash
    loop would put twelve Graph calls a minute on a credential-inspection
    endpoint and add Meta's latency to every restart of a worker that is
    already failing. So ``report_environment`` does not call this by default;
    the daily oneshot (:func:`main`) does, and the dedicated timer
    (``scripts/check_whatsapp_token.py``) does. A boot check must never make a
    boot depend on someone else's uptime.
    """
    if "pytest" in sys.modules:
        # Same rule and same reason as read_token_life: a unit test must never
        # reach the operator's live credential, and a suite whose colour
        # depends on Meta's uptime proves nothing about either.
        return TokenHealth(TokenVerdict.UNREADABLE)
    try:
        return inspect_token(
            getattr(settings, "whatsapp_access_token", "") or "",
            getattr(settings, "whatsapp_phone_number_id", "") or "",
        )
    except Exception:  # noqa: BLE001 — an unreachable Meta is not a boot fault
        logger.info("could not inspect the WhatsApp credential", exc_info=True)
        return TokenHealth(TokenVerdict.UNREADABLE)


def report_environment(
    *,
    settings: Any,
    session: Session,
    admin_client: Any,
    today: date | None = None,
    alert: bool = True,
    token_life: TokenLife | None = None,
    token_health: TokenHealth | None = None,
) -> list[EnvProblem]:
    """Run the boot check and shout — but NEVER stop the process.

    WHY NOTHING HERE IS FATAL. A hard startup failure was the tempting
    answer, and it is the wrong one twice over. The two processes that host
    this check are the conversation worker and the nightly run: both exist to
    serve people who have ALREADY paid. Every condition it detects degrades
    only the NEW-ORDER path, and every one of them already fails CLOSED
    downstream — §09 refuses a mismatched order and marks it for manual
    review rather than provisioning the wrong plan, and a missing store URL
    drops the renewal link rather than printing a broken one. So refusing to
    boot would convert «new sales are blocked» into «the product is down for
    the customers who bought it», which is strictly worse, and it would do so
    over a value the operator can only fix by hand. A condition that made the
    process take money wrongly or deliver the wrong thing WOULD be fatal —
    none of these do.

    WHY THIS WARNING IS NOT THE WARNING NOBODY READ. The old one logged once,
    only for an empty catalog, and only in the worker. This one is ERROR
    (the journal harvester forwards ``ERROR:`` lines), it goes to the admin
    channel the operator actually reads, it fires in BOTH entry points — so
    the nightly timer re-raises it every single morning until someone edits
    the file — and it names the variable and the contradiction, not a mood.

    ``alert=False`` keeps the ERROR log and drops the admin message. The
    worker runs under ``Restart=always``/``RestartSec=5``: a crash loop would
    put this alert on his phone twelve times a minute, and the far side of
    «a warning nobody reads» is a warning too loud to read. The daily oneshot
    owns the Telegram copy — exactly one a day, which is the cadence a stale
    file deserves.
    """
    try:
        problems = verify_environment(
            settings,
            approved_prices=approved_plan_prices(session),
            sale_plans=canonical_sale_plans(),
            today=today or datetime.now(UTC).astimezone().date(),
            token_life=token_life if token_life is not None else read_token_life(),
            # NOT defaulted to a live fetch — see :func:`read_token_health`.
            token_health=token_health,
        )
    except Exception:  # noqa: BLE001 — a boot check never blocks a boot
        logger.error("boot environment check could not run", exc_info=True)
        return []
    for problem in problems:
        if problem.notice:
            # WARNING, not ERROR: the harvester forwards ERROR lines, and a
            # deliberate state that logs ERROR every morning trains the reader
            # to skim the level that carries the outages.
            logger.warning("BOOT NOTICE %s: %s", problem.key, problem.english)
        else:
            logger.error("BOOT CHECK %s: %s", problem.key, problem.english)
    if problems and alert:
        try:
            admin_client.send_admin(format_env_alert(problems))
        except Exception:  # noqa: BLE001 — alerting never breaks the boot
            logger.warning("boot check alert failed", exc_info=True)
    if alert:
        _report_seat_supply(settings=settings, session=session,
                            admin_client=admin_client)
    return problems


def _report_seat_supply(
    *, settings: Any, session: Session, admin_client: Any
) -> None:
    """«ما نبيع الكرسي رقم ٣١» — the one published promise nothing compared.

    Two counters describe one wave: Salla holds a quantity per seat product and
    decrements it on every sale, we hold `FOUNDING_SEATS_CAP` and count rows,
    and nothing in the repository ever read the first. Read live on 2026-08-08
    the store was configured to sell forty seats against a page that says
    thirty.

    OUTSIDE :func:`verify_environment`, on purpose. That function is pure and
    must stay pure — it is the matrix a hundred tests drive without a network,
    and a check that leaves the machine cannot be one of its rows. And it is
    guarded by ``alert``: the same reason `read_token_health` is opt-in, one
    layer up. The worker runs under ``Restart=always``/``RestartSec=5``, so a
    crash loop would put two Salla product reads a second beside an already
    failing process; the daily oneshot owns the network copy.

    GET only, silent when the two counters agree, and it never raises: this is
    a boot check, and a boot check that can stop a boot is how «sales are
    misconfigured» becomes «the product is down for the people who paid».
    """
    try:
        from career.salla.seats import (
            founding_seats,
            read_seat_supply,
            supply_lines_ar,
            supply_verdict,
        )

        api_key = (getattr(settings, "salla_api_key", "") or "").strip()
        catalog = parse_product_catalog(
            getattr(settings, "salla_product_catalog", "") or "{}"
        )
        if not api_key or not catalog:
            # Nothing to ask with, or nothing to ask about. Both are already
            # reported by the rows above; a second sentence about the same
            # emptiness is noise.
            return
        verdict = supply_verdict(
            founding_seats(session),
            read_seat_supply(api_key=api_key, product_catalog=catalog),
        )
        lines = supply_lines_ar(verdict)
        if not lines:
            return
        logger.error(
            "SEAT SUPPLY drift %+d: %d taken, %d still sellable, cap %d",
            verdict.drift, verdict.seats.taken, verdict.supply.sellable,
            verdict.seats.cap,
        )
        admin_client.send_admin("\n".join(lines))
    except Exception:  # noqa: BLE001 — a counter check never blocks a boot
        logger.warning("seat supply check could not run", exc_info=True)


class JournalAdmin:
    """Admin messages → the journal (PII-free by contract, §15.13)."""

    def send_admin(self, text: str) -> str:
        logger.info("ADMIN: %s", text)
        return "journal"


def admin_client_for(settings: Any) -> Any:
    """Telegram when it is configured, the journal otherwise. Hoisted out of
    ``main`` so the boot check can speak BEFORE the run starts."""
    if settings.telegram_admin_bot_token and settings.telegram_admin_chat_id:
        from career.telegram.admin import HttpTelegramAdminClient

        return HttpTelegramAdminClient(
            settings.telegram_admin_bot_token,
            settings.telegram_admin_chat_id,
        )
    return JournalAdmin()


# ── the §05 lifecycle sweep, one customer per transaction ───────────────────


@dataclass(frozen=True)
class LifecycleSweep:
    """What the §05 sweep managed, and whose night it could not finish.

    ``failed`` carries TEN codes and never a tenant uuid (§15.13 / AUDIT ك-17):
    this object is printed into the journal.
    """

    counts: dict[str, int] = field(default_factory=dict)
    #: How many customers the sweep opened a transaction for.
    attempted: int = 0
    #: The TEN codes whose transaction was rolled back.
    failed: tuple[str, ...] = ()
    #: True when the sweep never got as far as a customer at all.
    aborted: bool = False

    @property
    def clean(self) -> bool:
        return not self.failed and not self.aborted

    def summary(self) -> dict[str, Any]:
        """The journal shape — the same silences named as in `summarize_delivery`."""
        return {
            "customers": self.attempted,
            "counts": self.counts,
            "failed": list(self.failed),
            "aborted": self.aborted,
        }


def _scope_to_one_customer(session: Session, tenant_id: uuid.UUID) -> None:
    """Make every ``Subscription`` this session can SEE belong to one customer.

    ``with_loader_criteria`` through ``do_orm_execute`` is SQLAlchemy's own
    «global WHERE criteria» recipe, and it is used here rather than a new
    argument on ``sweep_subscription_lifecycle`` because the sweep is §05's
    authority and must keep deciding what it decides: this changes what it is
    shown, never what it concludes.

    WHY THE SCOPE IS THE CUSTOMER AND NOT THE SUBSCRIPTION. Every question the
    sweep asks about a row is answered from that row, its own events, the
    tenant's channel — or from the tenant's OTHER subscriptions:
    ``_superseded_by_renewal`` exists to find «a live row of the same customer
    with a later period», and it is what stops an ACTIVE paying customer being
    walked through their previous period all over again. Scope a session to a
    single subscription id and that question silently answers «no», and the
    renewed customer gets a «نفتقدك» recovery template. Scoped to the tenant
    it is asked and answered exactly as it is in one global pass, because it
    was always a within-tenant question. So is `_live_channel`, so is
    `_event_exists`. There is no cross-customer read in the sweep for this to
    break, which is the property that makes one-transaction-per-customer
    equivalent to one transaction for everybody — minus the amplification.
    """

    @sa_event.listens_for(session, "do_orm_execute")
    def _one_customer_only(state: Any) -> None:
        if state.is_select:
            state.statement = state.statement.options(
                with_loader_criteria(
                    Subscription,
                    Subscription.tenant_id == tenant_id,
                    include_aliases=True,
                )
            )


def sweep_lifecycle_per_customer(
    new_session: Callable[[], Session],
    *,
    now: datetime,
    whatsapp_client: Any = None,
    store_url: str | None = None,
    sweep: Callable[..., dict[str, int]] | None = None,
) -> LifecycleSweep:
    """Run §05 for every customer, each in its own transaction.

    THE AMPLIFIER THIS REPLACES. Until 2026-08-07 this was four lines in
    ``main()``: one ``Session``, the whole sweep, one ``session.commit()`` at
    the end, all of it inside ``except Exception: logger.error(...)``. The
    sweep marks each send it makes with a ``subscription_events`` row, and
    those marks ARE its idempotency — «this customer has already had their
    day-27 reminder» is a row and nothing else. So one exception anywhere in
    the pass rolled back the marks for every customer already processed that
    night. Meta had been charged for those templates, the database recorded
    none of them, and the next night re-sent and re-paid for all of them. The
    2026-08-06 fix removed one TRIGGER of that (a Meta message id too long for
    its column, `salla.lifecycle._record_send`) and said in as many words that
    the amplifier was not its to change. This is the amplifier.

    A commit per customer makes the sweep's own idempotency DURABLE: a mark
    written for TEN-0002 survives whatever happens to TEN-0009 four rows later,
    and the customers after the failure are still swept instead of never being
    reached at all.

    THE BOUNDARY IS THE CUSTOMER, NOT THE STEP, and that is the whole of the
    design. Committing at every flush would be finer and would be WRONG: the
    grace path transitions ACTIVE→GRACE and only then sends the renewal
    reminder that the transition is the trigger for. Commit between those two
    and a failure in the second leaves a customer parked in GRACE, which the
    next sweep reads as «already handled» — the reminder is not retried, it is
    lost, and the customer is never asked to renew. Per customer, the unit is
    all-or-nothing: a rollback restores exactly the state tomorrow's run
    expects, which is why a partial sweep leaves nothing that reads as done.

    WHAT IS STILL NOT TRANSACTIONAL, said out loud: the SENDS. A template that
    went to Meta and then lost its mark to a rollback is a template that will
    be sent, and paid for, again tomorrow. That residue is not removed here —
    it is bounded. It used to be every customer the sweep had reached; it is
    now the one customer whose transaction failed.

    ``sweep`` is injectable so a test can fail one customer deliberately; it
    defaults to the real §05 authority, imported late for the same reason
    ``main()`` always imported it late (this module must import on a host where
    the Salla side is not deployed).
    """
    if sweep is None:
        from career.salla.lifecycle import sweep_subscription_lifecycle

        sweep = sweep_subscription_lifecycle

    try:
        with new_session() as session:
            # EVERY tenant that holds a subscription, not just the states §05
            # currently sweeps. The status filter belongs to the sweep and
            # only to the sweep: a copy of it here would be a second authority
            # on «who is in scope» and would go stale the first time §05 adds
            # a state (PAUSED was added once already). The cost of the
            # drift-proof version is two indexed selects for a customer with
            # nothing due.
            tenant_ids = list(
                session.execute(
                    select(Subscription.tenant_id).distinct()
                ).scalars()
            )
            # TEN codes, resolved once, so no log line below can print a uuid.
            codes: dict[uuid.UUID, str] = {
                row.id: row.code
                for row in session.execute(
                    select(Tenant.id, Tenant.code).where(
                        Tenant.id.in_(tenant_ids)
                    )
                ).all()
            } if tenant_ids else {}
    except Exception:  # noqa: BLE001 — the sweep never blocks the run
        logger.error(
            "subscription lifecycle: could not even list the customers to "
            "sweep — NOBODY was swept tonight", exc_info=True,
        )
        return LifecycleSweep(aborted=True)

    # TEN codes only, from here down: every log line below is a journal line.
    def code_for(tenant_id: uuid.UUID) -> str:
        return codes.get(tenant_id, "TEN-????")

    totals: dict[str, int] = {}
    failed: list[str] = []
    for tenant_id in sorted(tenant_ids, key=code_for):
        try:
            with new_session() as session:
                _scope_to_one_customer(session, tenant_id)
                counts = sweep(
                    session, now=now, whatsapp_client=whatsapp_client,
                    store_url=store_url,
                )
                session.commit()
        except Exception:  # noqa: BLE001 — one customer, not the night
            failed.append(code_for(tenant_id))
            logger.error(
                "subscription lifecycle sweep failed for %s — that customer's "
                "marks were rolled back and tomorrow's run redoes them; every "
                "other customer's are committed",
                code_for(tenant_id), exc_info=True,
            )
            continue
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value

    if any(totals.values()):
        logger.info("subscription lifecycle sweep: %s", totals)
    if failed:
        # ERROR because it is the level the operator's journal harvester
        # forwards, and named because the exit code below is only a summons.
        logger.error(
            "subscription lifecycle sweep did not complete for %d of %d "
            "customers: %s", len(failed), len(tenant_ids), ", ".join(failed),
        )
    return LifecycleSweep(
        counts=totals, attempted=len(tenant_ids), failed=tuple(failed),
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — thin
    # composition over tested parts; exercised live by the C6 exit gate.
    logging.basicConfig(level=logging.INFO)
    install_secret_redaction()  # AFTER basicConfig — arms the handler it made
    args = build_parser().parse_args(argv)

    settings = get_settings()
    if not settings.searchapi_api_key:
        logger.error("SEARCHAPI_API_KEY is empty — refusing a blind run")
        return EXIT_NO_SEARCH_KEY

    engine = create_engine(settings.owner_database_url, future=True)
    admin = admin_client_for(settings)

    # P0-8: the environment against the database, every single morning. The
    # nightly timer is the only thing on this box that runs daily and has a
    # human's attention, so it is where stale selling configuration gets
    # re-reported until someone fixes it. It never stops the run — tonight's
    # customers have already paid (see report_environment).
    #
    # The WhatsApp credential is inspected LIVE here and only here among the
    # boot checks: this is a daily oneshot, so it is one Graph call a day, and
    # it is the second independent witness — after the 08:30 timer — on the
    # morning of the run whose customers the credential would fail.
    with Session(engine) as session:
        report_environment(settings=settings, session=session,
                           admin_client=admin,
                           token_health=read_token_health(settings))

    # §05 lifecycle sweep BEFORE the engine: a just-expired subscription
    # must not seed tonight's query families.
    #
    # One transaction PER CUSTOMER, and a verdict that survives to the exit
    # code. Until 2026-08-07 these were four lines that ran every customer in
    # one Session under one broad `except` — see
    # :func:`sweep_lifecycle_per_customer` for what that cost.
    try:
        from career.whatsapp.client import HttpWhatsAppClient as _WaClient

        lifecycle_wa = (
            _WaClient(settings.whatsapp_access_token,
                      settings.whatsapp_phone_number_id)
            if settings.whatsapp_access_token else None
        )
        lifecycle = sweep_lifecycle_per_customer(
            lambda: Session(engine), now=datetime.now(UTC),
            whatsapp_client=lifecycle_wa,
            store_url=settings.salla_store_url,
        )
    except Exception:  # noqa: BLE001 — the sweep never blocks the run
        # Everything INSIDE the sweep is already per-customer; reaching here
        # means it never started (the WhatsApp client, the import), so nobody
        # was swept and the night says so.
        logger.error("subscription lifecycle sweep failed to start",
                     exc_info=True)
        lifecycle = LifecycleSweep(aborted=True)

    # §12 retention: kept in full while subscribed, 90 days after it ends,
    # then professionally deleted. That was a published promise in the consent
    # text, the whitepaper and the store's privacy page with no job behind it.
    try:
        from career.onboarding.retention import sweep_retention
        from career.storage import FilesystemStorageAdapter

        with Session(engine) as session:
            counts = sweep_retention(
                session, now=datetime.now(UTC),
                storage=FilesystemStorageAdapter(settings.storage_root),
            )
            session.commit()
        if counts.get("swept"):
            logger.info("retention sweep: %s", counts)
    except Exception:  # noqa: BLE001 — never blocks the run
        logger.error("retention sweep failed", exc_info=True)

    try:
        with Session(engine) as session:
            report = run_nightly(
                session,
                searchapi=HttpSearchApiClient(settings.searchapi_api_key),
                jobspy_client=PythonJobSpyClient(),
                fetcher=UrllibPageFetcher(),
                now=datetime.now(UTC),
                tenant_ids=args.tenant,
                # ح-3: honest default — the record mirrors the actual intent
                digest_only=(args.digest_only if args.digest_only is not None
                             else not args.deliver),
                max_per_query=args.max_per_query or settings.engine_max_per_query,
                retrieval_cap=args.retrieval_cap or settings.engine_retrieval_cap,
                enrich_cap=args.enrich_cap or settings.engine_enrich_cap,
            )
            codes = {
                row.id: row.code
                for row in session.execute(
                    select(Tenant.id, Tenant.code).where(
                        Tenant.id.in_(list(report.per_tenant))
                    )
                ).all()
            } if report.per_tenant else {}
    finally:
        engine.dispose()

    summary = summarize(report, codes)
    # Named in the journal beside the delivery, and for the same reason: a
    # sweep that half-completed and a sweep that did everything used to print
    # identically, which is nothing.
    summary["lifecycle"] = lifecycle.summary()

    if report.status in ("discovery_failed", "partial"):
        try:
            admin.send_admin(
                # Direction-pure, one fact per line: his client REVERSES any
                # line that mixes Arabic with Latin letters or digits, and both
                # the status and the counts are Latin (bidi guard,
                # tests/test_alert_direction_purity.py).
                "⚠️ التشغيلة الليلية لم تكتمل كما ينبغي\n"
                f"{report.status}\n"
                f"counts={report.counts}"
            )
        except Exception:  # noqa: BLE001 — alerting never breaks the run
            logger.warning("admin alert failed", exc_info=True)

    # SearchAPI credit watch (trial reads all-zero → quota_alert stays quiet)
    try:
        import json as _json
        from urllib.request import urlopen

        from career.engine.quota import quota_alert

        with urlopen(
            "https://www.searchapi.io/api/v1/me?api_key="
            + settings.searchapi_api_key,
            timeout=20,
        ) as resp:
            account = _json.load(resp).get("account") or {}
        alert = quota_alert(account)
        if alert:
            admin.send_admin(alert)
    except Exception:  # noqa: BLE001 — credit watch never breaks the run
        logger.warning("searchapi credit check failed", exc_info=True)

    # ── the delivery phase (C7): engine result → CV → WhatsApp → close ──
    # NOTE this phase is also the ONLY writer of tenant day states (§15.12),
    # so it must run even on a night that discovered nothing: a
    # discovery_failed report now carries every active tenant with an empty
    # result list (engine.run._tenants_without_results), and each one closes
    # DISCOVERY_FAILED here. No CV is generated and no message is sent on
    # that path — the empty final list sees to that.
    # P0-7: name the reason instead of leaving the reader to infer it from a
    # missing key. A run that never entered the phase used to print no
    # «delivery» at all, which reads exactly like a run that entered it and
    # closed nobody.
    summary["delivery_phase"] = delivery_phase_for(
        deliver=bool(args.deliver),
        has_whatsapp_token=bool(settings.whatsapp_access_token),
        has_tenants=bool(report.per_tenant),
    )
    delivery_phase_ran = summary["delivery_phase"] == PHASE_RAN
    if args.digest_only is None and not delivery_phase_ran:
        # ح-3: intended to deliver but the phase never ran (no creds / no
        # tenants) — flip the record so it never claims sends that didn't
        # happen NOR a digest that actually delivered.
        try:
            with Session(engine) as session:
                run_row = session.get(DiscoveryRun, report.run_id)
                if run_row is not None:
                    run_row.digest_only = True
                    session.commit()
        except Exception:  # noqa: BLE001 — honesty patch must not kill the run
            logger.warning("digest-only backfill failed", exc_info=True)
    if delivery_phase_ran:
        from career.cv.daily_run import DailyDeps, run_daily_delivery
        from career.cv.generate import AnthropicLlmClient
        from career.onboarding.achievement_render import AnthropicExamplesWriter
        from career.storage import FilesystemStorageAdapter
        from career.whatsapp.client import HttpWhatsAppClient

        storage = FilesystemStorageAdapter(settings.storage_root)
        deps = DailyDeps(
            storage=storage,
            whatsapp_client=HttpWhatsAppClient(
                settings.whatsapp_access_token,
                settings.whatsapp_phone_number_id,
                storage=storage,
            ),
            admin_client=admin,
            llm=AnthropicLlmClient(api_key=settings.anthropic_api_key),
            examples_writer=AnthropicExamplesWriter(
                api_key=settings.anthropic_api_key
            ),
            # Ask the live account which daily template is approved and take
            # the cheapest one. `daily_opportunities_utility` was approved
            # under the MARKETING category despite its name, and Meta refuses
            # to re-categorise an approved template — so a UTILITY-shaped
            # replacement was submitted, and the run adopts it the moment
            # Meta approves it, with no deploy and no human step.
            daily_template=_preferred_daily(settings),
        )
        engine2 = create_engine(settings.owner_database_url, future=True)
        try:
            with Session(engine2) as session:
                canary_tid = None
                if settings.canary_test_phone:
                    from career.db.models import CustomerChannel

                    # live-bug fix: Meta stores the phone without «+»,
                    # settings carry it — match every spelling (phones.py)
                    from career.whatsapp.phones import phone_variants

                    canary_tid = session.execute(
                        select(CustomerChannel.tenant_id).where(
                            CustomerChannel.phone_e164.in_(
                                phone_variants(settings.canary_test_phone)
                            )
                        )
                    ).scalars().first()
                # Filled by the stale-bundle sweep with the PREVIOUS days it
                # closed. Four live nights (21/22/23 July, 2 August) failed a
                # real customer from that sweep and exited 0 because those
                # closures had no way back to this function.
                expired_states: list[Any] = []
                states = run_daily_delivery(
                    session, report=report, deps=deps,
                    now=datetime.now(UTC),
                    include_weekend=args.include_weekend,
                    canary_tenant_id=canary_tid,
                    expired_out=expired_states,
                    # Fahad, 2 August: «كل شي الساعة ١١». The canary hour
                    # belonged to the review phase — it pushed every other
                    # customer's delivery to noon. Ordering is kept (his
                    # lands first, so a bad CV is still seen first) but the
                    # wait is gone: everyone is served at eleven.
                    canary_delay_seconds=0.0,
                )
                # read INSIDE the session — the rows expire on close
                # AUDIT ك-17: TEN codes in the journal, never raw uuids
                closed_ids = set(states) | {s.tenant_id for s in expired_states}
                dcodes = {
                    row.id: row.code
                    for row in session.execute(
                        select(Tenant.id, Tenant.code).where(
                            Tenant.id.in_(sorted(closed_ids))
                        )
                    ).all()
                } if closed_ids else {}
                summary.update(summarize_delivery(
                    tenant_codes={**codes, **dcodes},
                    intended=list(report.per_tenant),
                    states={tid: state.state for tid, state in states.items()},
                    expired=[(s.tenant_id, s.run_date, s.state)
                             for s in expired_states],
                ))
                session.commit()
        finally:
            engine2.dispose()

    print(json.dumps(summary, ensure_ascii=False, default=str))

    # Every tenant-day this run CLOSED, tonight's and the previous days the
    # stale-bundle sweep finished off. Both halves are the same kind of fact
    # and the verdict must be computed over both — reading only the first half
    # is the exact defect that let four expired nights exit 0.
    closed_states = [
        *summary.get("delivery", {}).values(),
        *(row["state"] for row in summary.get("delivery_expired", [])),
    ]
    failed = sorted(
        code for code, state in summary.get("delivery", {}).items()
        if state not in HONEST_DAY_STATES
    ) + sorted(
        f"{row['tenant']} ({row['run_date']})"
        for row in summary.get("delivery_expired", [])
        if row["state"] not in HONEST_DAY_STATES
    )
    if failed:
        # The exit code summons the operator; this line tells him WHO, and it
        # is an ERROR because that is the only level his journal harvester
        # forwards. Before P0-7 neither existed.
        logger.error("delivery failed tonight for %s", ", ".join(failed))
    if summary["delivery_phase"] == PHASE_NO_CREDENTIALS:
        logger.error(
            "WHATSAPP_ACCESS_TOKEN is empty — the delivery phase never ran "
            "and no customer was served tonight"
        )
    return exit_code_for(report.status, closed_states,
                         delivery_phase=summary["delivery_phase"],
                         lifecycle_failed=not lifecycle.clean)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
