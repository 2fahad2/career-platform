"""§14 cost metering — every paid external call, on ONE mechanism.

Closure-audit finding: metering covered the CV generation and the CV
extraction and nothing else, so the operator could not answer «what does this
customer cost me?» and could not see a runaway bill forming. Ten spend
categories now land in the SAME ``usage_events`` / ``cost_allocations`` pair:

* six Claude boundaries — generation, extraction, panel render, panel judge,
  icebreaker examples, intent classification — each recording the REAL token
  numbers the API reported, including prompt-cache tokens (priced differently);
* SearchAPI.io google_jobs credits, split across the tenants whose approved
  paths created the query family;
* WhatsApp TEMPLATE messages, priced per Meta category (marketing ≠ utility),
  derived from the delivery ledger.

Constant: no test here makes a real API call — every boundary takes an
injected client, exactly as the boundaries themselves were built to allow.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from career.cv import close
from career.onboarding import achievement_render as render_mod
from career.onboarding import bullet_panel, enrichment, extraction, intent

NOW = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
DAY = date(2026, 7, 31)


# ── zero-network doubles ─────────────────────────────────────────────────────


class _Usage:
    def __init__(self, inp: int, out: int, write: int = 0, read: int = 0) -> None:
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = write
        self.cache_read_input_tokens = read


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, payload: Any, usage: _Usage | None = None,
                 stop_reason: str = "end_turn") -> None:
        self.content = [_Block(json.dumps(payload))]
        self.stop_reason = stop_reason
        self.usage = usage


class _Messages:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self._response


class _Anthropic:
    def __init__(self, response: _Response) -> None:
        self.messages = _Messages(response)


class _MeteredClient(close.TokenCounter):
    """A pipeline double that bills like the real boundaries do: it reports
    tokens through the SAME TokenCounter the live clients mix in."""

    def __init__(self, per_call: tuple[int, int] = (100, 40),
                 fail: Exception | None = None) -> None:
        self.reset_token_counters()
        self._per_call = per_call
        self._fail = fail
        self.calls = 0

    def _bill(self) -> None:
        self.calls += 1
        self.absorb_usage(_Response({}, _Usage(*self._per_call)))
        if self._fail is not None:
            raise self._fail

    # the four shapes the pipeline calls
    def render(self, arabic_answer: str, *, angle: str = "",
               instruction: str = "") -> dict[str, Any]:
        self._bill()
        return {"is_achievement": True, "english_bullet": f"Led a team{angle[:1]}",
                "qualitative_only": True, "arabic_gloss": "ملخص"}

    def judge(self, arabic_answer: str, candidates: list[str]) -> dict[str, Any]:
        self._bill()
        return {"winner_index": 0, "reason": "faithful"}

    def write(self, role_title: str, role_description: str) -> list[str]:
        self._bill()
        return ["كنت مسؤول عن الجرد"]

    def classify(self, reply: str, *, draft: str | None) -> dict[str, Any]:
        self._bill()
        return {"intent": "answer", "topic": "other", "confidence": 0.9}


def _seed_tenant(owner_session: Session, code: str | None = None) -> uuid.UUID:
    tid = uuid.uuid4()
    owner_session.execute(
        sql_text("INSERT INTO tenants (id, code) VALUES (:id, :c)"),
        {"id": str(tid), "c": code or f"TEN-M{uuid.uuid4().hex[:4]}"},
    )
    owner_session.commit()
    return tid


def _cleanup(owner_session: Session, *tids: uuid.UUID) -> None:
    for tid in tids:
        owner_session.execute(
            sql_text("DELETE FROM tenants WHERE id = :id"), {"id": str(tid)}
        )
    owner_session.commit()


def _usage(owner_session: Session, tid: uuid.UUID) -> list[Any]:
    return owner_session.execute(
        sql_text("SELECT kind, input_tokens, output_tokens, cache_write_tokens,"
                 " cache_read_tokens, cost_usd FROM usage_events"
                 " WHERE tenant_id = :t ORDER BY kind"),
        {"t": str(tid)},
    ).all()


# ── the price table + the token counter (pure) ───────────────────────────────


def test_cache_tokens_are_priced_at_their_own_rates() -> None:
    """A cache READ costs a tenth of an input token and a cache WRITE a
    quarter more. Pricing them as plain input would overstate a cached call by
    ~10× — the exact kind of wrong number that hides a real runaway."""
    plain = close.llm_cost_usd(1000, 0)
    cached_read = close.llm_cost_usd(0, 0, 0, 1000)
    cached_write = close.llm_cost_usd(0, 0, 1000, 0)
    assert plain == Decimal("0.005")
    assert cached_read == Decimal("0.0005")
    assert cached_write == Decimal("0.00625")
    assert close.llm_cost_usd(0, 0) is None  # nothing spent → nothing recorded


def test_token_counter_absorbs_the_real_api_numbers() -> None:
    counter = _MeteredClient()
    counter.absorb_usage(_Response({}, _Usage(10, 5, 7, 3)))
    counter.absorb_usage(_Response({}, _Usage(1, 1, 1, 1)))
    assert close.token_snapshot(counter) == (11, 6, 8, 4)
    # a response with no usage block (fake, error path) must not crash a turn
    counter.absorb_usage(_Response({}))
    assert close.token_snapshot(counter) == (11, 6, 8, 4)


def test_token_snapshot_tolerates_an_uninstrumented_client() -> None:
    assert close.token_snapshot(object()) == (0, 0, 0, 0)


# ── coverage: every Claude boundary in the product counts its tokens ─────────


ANTHROPIC_BOUNDARIES = {
    "llm_generation": "career.cv.generate:AnthropicLlmClient",
    "llm_extraction": "career.onboarding.extraction:AnthropicExtractor",
    "llm_render": "career.onboarding.achievement_render:AnthropicAchievementRenderer",
    "llm_examples": "career.onboarding.achievement_render:AnthropicExamplesWriter",
    "llm_judge": "career.onboarding.bullet_panel:AnthropicBulletJudge",
    "llm_intent": "career.onboarding.intent:AnthropicIntentClassifier",
}


def _resolve(path: str) -> type:
    import importlib

    module, name = path.split(":")
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize("kind", sorted(ANTHROPIC_BOUNDARIES))
def test_every_claude_boundary_is_a_token_counter(kind: str) -> None:
    assert issubclass(_resolve(ANTHROPIC_BOUNDARIES[kind]), close.TokenCounter)
    assert kind in close.LLM_KINDS


@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("career.cv.generate:AnthropicLlmClient",
         lambda c: c.complete("prompt")),
        ("career.onboarding.achievement_render:AnthropicAchievementRenderer",
         lambda c: c.render("قللت الوقت")),
        ("career.onboarding.achievement_render:AnthropicExamplesWriter",
         lambda c: c.write("محاسب", "جرد")),
        ("career.onboarding.bullet_panel:AnthropicBulletJudge",
         lambda c: c.judge("قللت الوقت", ["A", "B"])),
        ("career.onboarding.intent:AnthropicIntentClassifier",
         lambda c: c.classify("ابدع", draft=None)),
    ],
)
def test_live_boundaries_record_usage_from_the_response(path: str, call: Any) -> None:
    """Injected transport, zero network: the token totals must come from the
    response the API returned, never from an estimate."""
    fake = _Anthropic(_Response({"winner_index": 0, "reason": "x",
                                 "examples": [], "is_achievement": False,
                                 "intent": "answer", "topic": "other",
                                 "confidence": 1.0},
                                _Usage(120, 30, 8, 64)))
    client = _resolve(path)(client=fake)
    call(client)
    assert close.token_snapshot(client) == (120, 30, 8, 64)


def test_no_unmetered_messages_create_call_site_exists() -> None:
    """Regression guard: a NEW paid Claude boundary must register itself here
    (and mix in TokenCounter) rather than quietly spending unmetered."""
    src = Path(__file__).resolve().parents[1] / "src" / "career"
    sites = [
        f"{path.relative_to(src)}:{n}"
        for path in sorted(src.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ".messages.create(" in line
    ]
    assert len(sites) == len(ANTHROPIC_BOUNDARIES), sites


# ── the meter: one row per call, real numbers, never blocking ────────────────


def test_meter_records_one_row_per_call_with_the_real_numbers(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        client = _MeteredClient(per_call=(1000, 200))
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        with meter.around("llm_render", client):
            client.render("قللت الوقت")
        with meter.around("llm_judge", client):
            client.judge("قللت الوقت", ["A"])
        owner_session.commit()

        rows = _usage(owner_session, tid)
        assert [r.kind for r in rows] == ["llm_judge", "llm_render"]
        for row in rows:
            assert row.input_tokens == 1000 and row.output_tokens == 200
            assert row.cost_usd == Decimal("0.010000")
    finally:
        _cleanup(owner_session, tid)


def test_meter_records_cache_tokens_separately(owner_session: Session) -> None:
    tid = _seed_tenant(owner_session)
    try:
        client = _MeteredClient()
        client.absorb_usage(_Response({}, _Usage(0, 0, 400, 8000)))
        # spend that happened BEFORE the block is not this call's
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        with meter.around("llm_render", client):
            client.absorb_usage(_Response({}, _Usage(50, 10, 100, 2000)))
        owner_session.commit()

        row = _usage(owner_session, tid)[0]
        assert (row.input_tokens, row.output_tokens) == (50, 10)
        assert (row.cache_write_tokens, row.cache_read_tokens) == (100, 2000)
        assert row.cost_usd == close.llm_cost_usd(50, 10, 100, 2000)
    finally:
        _cleanup(owner_session, tid)


def test_meter_records_the_spend_even_when_the_call_raises(
    owner_session: Session,
) -> None:
    """Tokens burned by a call that then failed are still real money — the
    meter runs in `finally`, and the customer's error still propagates."""
    tid = _seed_tenant(owner_session)
    try:
        client = _MeteredClient(fail=RuntimeError("provider blew up"))
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        with pytest.raises(RuntimeError), meter.around("llm_render", client):
            client.render("قللت الوقت")
        owner_session.commit()
        assert [r.kind for r in _usage(owner_session, tid)] == ["llm_render"]
    finally:
        _cleanup(owner_session, tid)


def test_a_broken_meter_never_breaks_the_customer_turn(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        client = _MeteredClient()

        class _Exploding:
            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("accounting database is down")

        meter = close.LlmMeter(_Exploding(), tenant_id=tid, now=NOW)  # type: ignore[arg-type]
        with meter.around("llm_render", client):
            out = client.render("قللت الوقت")
        assert out["is_achievement"] is True  # the turn completed regardless
        assert _usage(owner_session, tid) == []
    finally:
        _cleanup(owner_session, tid)


def test_a_zero_token_call_is_not_spend_unless_the_event_is_load_bearing(
    owner_session: Session,
) -> None:
    """MonthlyCapBudget counts llm_generation ROWS, so that call site records
    even at zero; everywhere else a zero-token call is not a bill."""
    tid = _seed_tenant(owner_session)
    try:
        silent = _MeteredClient(per_call=(0, 0))
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        with meter.around("llm_render", silent):
            silent.render("x")
        with meter.around("llm_generation", silent, always=True):
            silent.render("x")
        owner_session.commit()
        assert [r.kind for r in _usage(owner_session, tid)] == ["llm_generation"]
    finally:
        _cleanup(owner_session, tid)


# ── the pipeline call sites ──────────────────────────────────────────────────


def test_panel_meters_renders_and_the_judge_under_separate_categories(
    owner_session: Session,
) -> None:
    """The panel is the most expensive turn in the product: three renders plus
    a judge call for ONE customer sentence. Renders and judgement are billed
    separately, so they must not be lumped into one number."""
    tid = _seed_tenant(owner_session)
    try:
        renderer = _MeteredClient(per_call=(300, 60))
        judge = _MeteredClient(per_call=(500, 20))
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        out = bullet_panel.run_panel(
            renderer, arabic_answer="قدت فريق", vocabulary=set(),
            judge=judge, meter=meter,
        )
        owner_session.commit()
        assert out["status"] == "ok"
        kinds = [r.kind for r in _usage(owner_session, tid)]
        assert kinds.count("llm_render") == len(bullet_panel.PANEL_ANGLES)
        assert kinds.count("llm_judge") == 1
    finally:
        _cleanup(owner_session, tid)


def test_the_single_shot_renderer_meters_its_retry_too(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        renderer = _MeteredClient(per_call=(300, 60))
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        render_mod.render_achievement(
            renderer, arabic_answer="قدت فريق", vocabulary=set(), meter=meter,
        )
        owner_session.commit()
        assert [r.kind for r in _usage(owner_session, tid)] == ["llm_render"]
        assert renderer.calls == 1
    finally:
        _cleanup(owner_session, tid)


def test_the_pure_pipeline_still_runs_unmetered() -> None:
    """`meter=None` means «no session here», not «free» — the pure unit tests
    that own the panel's behaviour must keep working untouched."""
    renderer = _MeteredClient()
    out = bullet_panel.run_panel(
        renderer, arabic_answer="قدت فريق", vocabulary=set(),
    )
    assert out["status"] == "ok"


def test_intent_classification_is_metered(owner_session: Session) -> None:
    tid = _seed_tenant(owner_session)
    try:
        classifier = _MeteredClient(per_call=(90, 12))
        meter = close.LlmMeter(owner_session, tenant_id=tid, now=NOW)
        verdict = intent.resolve_intent(
            "قللت وقت الجرد", draft=None, classifier=classifier,
            deterministic=intent.ANSWER, meter=meter,
        )
        owner_session.commit()
        assert verdict[0] == intent.ANSWER
        rows = _usage(owner_session, tid)
        assert [r.kind for r in rows] == ["llm_intent"]
        assert rows[0].input_tokens == 90
    finally:
        _cleanup(owner_session, tid)


def test_examples_writer_is_metered_against_the_role_owner(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        fact_id = uuid.uuid4()
        owner_session.execute(
            sql_text(
                "INSERT INTO profile_facts (id, tenant_id, category, payload,"
                " status, source) VALUES (:i, :t, 'experience', :p,"
                " 'CONFIRMED', 'cv_extraction')"
            ),
            {"i": str(fact_id), "t": str(tid),
             "p": json.dumps({"title": "محاسب", "description": "جرد"})},
        )
        owner_session.commit()
        writer = _MeteredClient(per_call=(200, 80))
        enrichment.prepare_examples(
            owner_session, role_fact_id=fact_id, writer=writer
        )
        owner_session.commit()
        rows = _usage(owner_session, tid)
        assert [r.kind for r in rows] == ["llm_examples"]
        assert rows[0].output_tokens == 80
    finally:
        _cleanup(owner_session, tid)


def test_cv_extraction_is_metered_on_the_shared_mechanism(
    owner_session: Session,
) -> None:
    from career.onboarding.consents import REQUIRED_KEYS, record_consent

    tid = _seed_tenant(owner_session)
    try:
        for purpose in REQUIRED_KEYS:
            record_consent(owner_session, tenant_id=tid, purpose=purpose,
                           action="granted")
        owner_session.commit()

        class _Extractor(close.TokenCounter):
            def __init__(self) -> None:
                self.reset_token_counters()

            def extract(self, cv_text: str) -> extraction.ExtractedFacts:
                self.absorb_usage(_Response({}, _Usage(4000, 900, 0, 1500)))
                return extraction.ExtractedFacts(
                    experiences=[], education=[], certifications=[],
                    skills=["SQL"], languages=[], achievements=[],
                )

        extraction.run_extraction(
            owner_session, tenant_id=tid, cv_text="Senior analyst at Delta",
            known_name=None, extractor=_Extractor(),
        )
        owner_session.commit()
        rows = _usage(owner_session, tid)
        assert [r.kind for r in rows] == ["llm_extraction"]
        assert rows[0].input_tokens == 4000
        assert rows[0].cache_read_tokens == 1500
        assert rows[0].cost_usd == close.llm_cost_usd(4000, 900, 0, 1500)
    finally:
        _cleanup(owner_session, tid)


# ── SearchAPI.io: shared discovery, honestly split ───────────────────────────


def test_searchapi_credits_split_across_the_family_tenants(
    owner_session: Session,
) -> None:
    """A query family exists because SEVERAL active tenants approved that path
    (D5) and one query serves them all — charging its whole cost to one tenant
    would invent a runaway that isn't there."""
    from career.engine.families import QueryFamily
    from career.engine.run import record_search_costs

    a, b = _seed_tenant(owner_session), _seed_tenant(owner_session)
    try:
        family = QueryFamily(
            family="business_analyst", aliases=("Business Analyst",),
            locations=("Riyadh, Saudi Arabia", "Saudi Arabia"),
            tenant_ids=(a, b),
        )
        record_search_costs(
            owner_session, families=[family], searches={"business_analyst": 8},
            run_id=uuid.uuid4(), now=NOW,
        )
        owner_session.commit()
        for tid in (a, b):
            rows = _usage(owner_session, tid)
            assert [r.kind for r in rows] == ["search_api"]
            assert rows[0].input_tokens == 8          # credits consumed
            assert rows[0].cost_usd == Decimal("0.016000")  # 4 credits @ 0.004
    finally:
        _cleanup(owner_session, a, b)


def test_a_family_with_no_credits_records_nothing(owner_session: Session) -> None:
    from career.engine.families import QueryFamily
    from career.engine.run import record_search_costs

    tid = _seed_tenant(owner_session)
    try:
        record_search_costs(
            owner_session,
            families=[QueryFamily(family="f", aliases=(), locations=(),
                                  tenant_ids=(tid,))],
            searches={}, run_id=uuid.uuid4(), now=NOW,
        )
        owner_session.commit()
        assert _usage(owner_session, tid) == []
    finally:
        _cleanup(owner_session, tid)


def test_discovery_counts_only_the_queries_that_reached_the_provider() -> None:
    """A transport error never became a billed credit — the run's cost must
    not include queries the provider never saw."""
    from career.engine.families import QueryFamily
    from career.engine.run import _discover

    class _DownSearchApi:
        def search(self, params: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError("provider latency window")

    class _NoJobSpy:
        def scrape(self, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    family = QueryFamily(
        family="business_analyst", aliases=("Business Analyst", "BA"),
        locations=("Riyadh, Saudi Arabia",), tenant_ids=(uuid.uuid4(),),
    )
    _jobs, sources, searches = _discover(
        [family], _DownSearchApi(), _NoJobSpy(), max_per_query=5
    )
    assert sources["searchapi_google_jobs"]["status"] == "error"
    assert searches.get("business_analyst", 0) == 0


# ── WhatsApp: billed per Meta template category ──────────────────────────────


def _seed_channel(owner_session: Session, tid: uuid.UUID) -> uuid.UUID:
    cid = uuid.uuid4()
    owner_session.execute(
        sql_text("INSERT INTO customer_channels (id, tenant_id, provider,"
                 " phone_e164) VALUES (:i, :t, 'whatsapp', :p)"),
        {"i": str(cid), "t": str(tid), "p": f"+96650{uuid.uuid4().int % 10**7:07d}"},
    )
    return cid


def _seed_message(
    owner_session: Session, tid: uuid.UUID, cid: uuid.UUID, *,
    kind: str, template_name: str | None, status: str = "delivered",
) -> None:
    owner_session.execute(
        sql_text("INSERT INTO delivery_messages (id, tenant_id, channel_id,"
                 " wa_message_id, kind, template_name, status, created_at)"
                 " VALUES (:i, :t, :c, :w, :k, :n, :s, :d)"),
        {"i": str(uuid.uuid4()), "t": str(tid), "c": str(cid),
         "w": f"wamid.{uuid.uuid4().hex}", "k": kind, "n": template_name,
         "s": status, "d": NOW},
    )


def test_whatsapp_templates_are_priced_per_category(
    owner_session: Session,
) -> None:
    """Meta bills marketing at multiples of utility — one «messages» number
    would hide the expensive half of the bill.

    The utility fixture is `subscription_daily_report` because, as of the
    2026-08-08 measurement, it is the ONLY one of the eight live templates Meta
    actually categorises as UTILITY.
    """
    tid = _seed_tenant(owner_session)
    try:
        cid = _seed_channel(owner_session, tid)
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="subscription_daily_report")
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="subscription_daily_report")
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="recovery")
        owner_session.commit()

        spend = close.whatsapp_spend(owner_session, tenant_id=tid, day=DAY)
        assert spend["wa_utility"] == (2, Decimal("0.0314"))
        assert spend["wa_marketing"] == (1, Decimal("0.0384"))
    finally:
        _cleanup(owner_session, tid)


def test_the_bill_follows_metas_category_not_the_templates_name(
    owner_session: Session,
) -> None:
    """AUDIT 2026-08-08 — the regression this file could not previously catch.

    `daily_opportunities_utility` is APPROVED at Meta as **MARKETING**
    (``previous_category: UTILITY`` — Meta re-categorised it after approving
    it), and so are `welcome_activation`, `onboarding_reminder`,
    `renewal_reminder` and `daily_service_update`. The bill used to read the
    category we SUBMITTED them under and charged all five at the utility rate,
    on the majority of the account's template traffic. Against Meta's live
    Saudi Arabia card (utility $0.0107, marketing $0.0501, verified the same
    day) that recorded 21% of what Meta charges. A name is not evidence.

    The Decimals below are the CONFIGURED rates, not Meta's — the settings are
    themselves stale (see `close._wa_price`), and this test is about which
    bucket a send lands in, not about what the operator has typed into `.env`.
    """
    tid = _seed_tenant(owner_session)
    try:
        cid = _seed_channel(owner_session, tid)
        for name in ("daily_opportunities_utility", "daily_service_update",
                     "welcome_activation", "onboarding_reminder",
                     "renewal_reminder"):
            _seed_message(owner_session, tid, cid, kind="template",
                          template_name=name)
        owner_session.commit()

        spend = close.whatsapp_spend(owner_session, tenant_id=tid, day=DAY)
        assert "wa_utility" not in spend, spend
        assert spend["wa_marketing"] == (5, Decimal("0.1920"))
    finally:
        _cleanup(owner_session, tid)


def test_free_form_service_replies_and_failed_sends_are_not_billed(
    owner_session: Session,
) -> None:
    """Inside the open 24h window a service message costs nothing, and a send
    that failed was never delivered — neither may inflate the bill."""
    tid = _seed_tenant(owner_session)
    try:
        cid = _seed_channel(owner_session, tid)
        _seed_message(owner_session, tid, cid, kind="text", template_name=None)
        _seed_message(owner_session, tid, cid, kind="document", template_name=None)
        _seed_message(owner_session, tid, cid, kind="interactive", template_name=None)
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="recovery", status="failed")
        owner_session.commit()
        assert close.whatsapp_spend(owner_session, tenant_id=tid, day=DAY) == {}
    finally:
        _cleanup(owner_session, tid)


def test_an_unknown_template_is_priced_conservatively(
    owner_session: Session,
) -> None:
    """An unrecognised template name must never make a bill look smaller than
    it is — it lands in its own bucket at the higher (marketing) rate."""
    tid = _seed_tenant(owner_session)
    try:
        cid = _seed_channel(owner_session, tid)
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="some_template_added_in_meta_console")
        owner_session.commit()
        spend = close.whatsapp_spend(owner_session, tenant_id=tid, day=DAY)
        assert spend["wa_unknown"] == (1, Decimal("0.0384"))
    finally:
        _cleanup(owner_session, tid)


def test_rollup_folds_whatsapp_in_and_stays_idempotent(
    owner_session: Session,
) -> None:
    tid = _seed_tenant(owner_session)
    try:
        cid = _seed_channel(owner_session, tid)
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="subscription_daily_report")
        close.record_usage(
            owner_session, tenant_id=tid, kind="llm_render", now=NOW,
            input_tokens=1000, output_tokens=100, cost_usd=Decimal("0.0075"),
        )
        close.record_usage(
            owner_session, tenant_id=tid, kind="search_api", now=NOW,
            input_tokens=4, cost_usd=Decimal("0.016"),
        )
        owner_session.commit()

        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)
        close.rollup_costs(owner_session, tenant_id=tid, day=DAY)  # idempotent
        owner_session.commit()

        rows = owner_session.execute(
            sql_text("SELECT category, events, cost_usd FROM cost_allocations"
                     " WHERE tenant_id = :t ORDER BY category"),
            {"t": str(tid)},
        ).all()
        assert [(r.category, r.events) for r in rows] == [
            ("llm_render", 1), ("search_api", 1), ("wa_utility", 1),
        ]
        assert sum(r.cost_usd for r in rows) == Decimal("0.039200")
    finally:
        _cleanup(owner_session, tid)


# ── the operator's screens ───────────────────────────────────────────────────


def test_business_data_covers_every_category_and_names_the_top_spender(
    owner_session: Session,
) -> None:
    from career.telegram.console import _business_data

    code = f"TEN-C{uuid.uuid4().hex[:4]}"
    tid = _seed_tenant(owner_session, code=code)
    try:
        cid = _seed_channel(owner_session, tid)
        _seed_message(owner_session, tid, cid, kind="template",
                      template_name="subscription_daily_report")
        for kind, cost in (("llm_render", "0.0300"), ("llm_judge", "0.0100"),
                           ("llm_intent", "0.0010"), ("search_api", "0.0160")):
            close.record_usage(
                owner_session, tenant_id=tid, kind=kind, now=NOW,
                input_tokens=10, cost_usd=Decimal(cost),
            )
        owner_session.commit()

        data = _business_data(owner_session, "all", now=NOW)
        categories = data["spend_by_category"]
        # the audit's «2 of 10»: the panel, the judge, the classifier, the
        # search credits and the template message all show up now
        for kind in ("llm_render", "llm_judge", "llm_intent", "search_api",
                     "wa_utility"):
            assert kind in categories, categories
        assert data["llm_generations"] >= 3          # not just generation+extraction
        assert data["total_cost_usd"] >= Decimal("0.0727")
        assert (code, Decimal("0.057000")) in data["top_cost_tenants"]
    finally:
        _cleanup(owner_session, tid)


_COST_DATA = {
    "total_cost_usd": Decimal("0.4213"),
    "cost_tenants": 3,
    "spend_by_category": {
        "llm_render": (12, Decimal("0.1800")),
        "search_api": (4, Decimal("0.0400")),
        "wa_utility": (5, Decimal("0.0785")),
        "human_review": (1, Decimal("0")),   # real cost, no dollar price
    },
    "top_cost_tenants": [("TEN-0002", Decimal("0.2100"))],
}


def test_cost_block_shows_per_customer_cost_and_the_categories() -> None:
    from career.telegram.views import cost_lines

    text = "\n".join(cost_lines(_COST_DATA))
    assert "0.4213" in text
    assert "0.1404" in text                     # 0.4213 / 3 active spenders
    assert "llm_render x12 0.1800" in text
    assert "TEN-0002 0.2100" in text
    assert "human_review" not in text           # priceless ≠ printed as $0


def test_cost_lines_are_direction_pure() -> None:
    """Fahad's client scrambles a line that mixes Arabic with Latin letters or
    Latin digits — so no line of the spend block may contain both."""
    import re

    from career.telegram.views import cost_lines

    arabic = re.compile(r"[؀-ۿ]")
    latin = re.compile(r"[A-Za-z0-9]")
    lines = cost_lines(_COST_DATA)
    assert lines  # the guard must actually be looking at something
    for line in lines:
        assert not (arabic.search(line) and latin.search(line)), line


def test_weekly_report_carries_the_same_spend_block() -> None:
    from career.telegram.weekly_report import format_weekly_report

    report = format_weekly_report(
        date(2026, 8, 2),
        {"llm_generations": 8, "llm_cost_usd": "0.40", **_COST_DATA},
        {"DELIVERED": 9},
    )
    assert "💵 التكلفة بالدولار — الإجمالي" in report
    assert "0.4213" in report
    assert "TEN-0002 0.2100" in report


def test_no_cost_block_when_there_is_nothing_to_report() -> None:
    from career.telegram.views import cost_lines

    assert cost_lines({}) == []
