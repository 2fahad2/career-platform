"""Three live incidents, pinned: the exit code that lied (P0-7), the selling
environment that went stale in silence (P0-8), and the §05 lifecycle sweep
that threw away a whole night of idempotency marks on one bad row.

Most of it is tested against the smallest honest seam. ``main()`` is thin
composition over these functions — it has exactly one ``return
exit_code_for(...)`` and one ``report_environment(...)`` call — so the
decisions are what get exercised here, at every state mix that has actually
occurred on the box or plausibly can.

The last section is different in kind and deliberately so. «One customer's
failure no longer costs every other customer their marks» is a statement about
TRANSACTIONS, and a fake session proves nothing about a transaction: it needs a
real Postgres, a real rollback, and a real row that is still there afterwards.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from career.cv.close import DAILY_STATES
from career.db.models import Subscription, Tenant
from career.engine import cli

# ── P0-7: the night's verdict ────────────────────────────────────────────────


def test_the_eight_states_all_pick_a_side() -> None:
    """A ninth day state must be classified deliberately, not default into
    silence. exit_code_for treats the unknown as a failure precisely so this
    test failing is the loudest thing that happens."""
    assert cli.FAILED_DAY_STATES | cli.HONEST_DAY_STATES == set(DAILY_STATES)
    assert not (cli.FAILED_DAY_STATES & cli.HONEST_DAY_STATES)


def test_the_original_lie_now_exits_non_zero() -> None:
    """2 August 2026: the only real customer's day closed CV_GENERATION_FAILED
    on a UniqueViolation and the run returned 0, so OnFailure never fired."""
    assert cli.exit_code_for("completed", ["CV_GENERATION_FAILED"]) == 3


def test_every_failed_state_fails_the_night() -> None:
    for state in sorted(cli.FAILED_DAY_STATES):
        assert cli.exit_code_for("completed", [state]) != 0, state


def test_no_matches_is_an_honest_success() -> None:
    # the gate finding nothing worth sending is the product working
    assert cli.exit_code_for("completed", ["NO_MATCHES"]) == 0


def test_opted_out_is_an_honest_success() -> None:
    assert cli.exit_code_for("completed", ["SKIPPED_OPTED_OUT"]) == 0


def test_delivered_and_partial_are_successes() -> None:
    assert cli.exit_code_for("completed",
                             ["DELIVERED", "PARTIAL_DELIVERY"]) == 0


def test_one_failure_among_successes_still_fails_the_night() -> None:
    """The deliberate choice: no threshold, no majority. At one customer a
    majority rule is a mute button."""
    assert cli.exit_code_for(
        "completed", ["DELIVERED", "NO_MATCHES", "WHATSAPP_FAILED"]
    ) == cli.EXIT_DELIVERY_FAILED


def test_discovery_failure_outranks_and_keeps_its_own_code() -> None:
    # a different runbook entry: every source down, not a broken pipeline
    assert cli.exit_code_for("discovery_failed", ["DISCOVERY_FAILED"]) == 1
    assert cli.exit_code_for("discovery_failed", []) == 1


def test_partial_discovery_with_a_served_customer_is_still_zero() -> None:
    assert cli.exit_code_for("partial", ["DELIVERED"]) == 0


def test_no_day_states_at_all_is_zero() -> None:
    """Weekend (§08 delivers Sun-Thu), a re-run whose tenants were already
    served, and a bundle held for a closed window all land here. Paging on
    them would fire every Friday and train the operator to ignore it."""
    assert cli.exit_code_for("completed", []) == 0
    assert cli.exit_code_for("completed", None) == 0
    assert cli.exit_code_for("no_active_tenants", []) == 0


def test_an_unknown_state_is_treated_as_a_failure() -> None:
    assert cli.exit_code_for("completed", ["SOMETHING_NEW"]) != 0


def test_the_old_one_argument_call_still_answers() -> None:
    # the C6 exit gate and tests/test_engine_cli.py call it with one argument
    assert cli.exit_code_for("completed") == 0
    assert cli.exit_code_for("discovery_failed") == 1


# ── P0-7: the summary that could not tell its silences apart ────────────────


def test_unclosed_tenants_are_named_beside_an_empty_delivery() -> None:
    """The audit read «"delivery": {}» as a summary bug. It was not — the
    dict mirrored an empty return. The bug was that a weekend, a held bundle
    and «nobody to serve» all printed the same nothing."""
    served, held = uuid.uuid4(), uuid.uuid4()
    out = cli.summarize_delivery(
        tenant_codes={served: "TEN-0002", held: "TEN-0009"},
        intended=[served, held],
        states={served: "DELIVERED"},
    )
    assert out["delivery"] == {"TEN-0002": "DELIVERED"}
    assert out["delivery_unclosed"] == ["TEN-0009"]


def test_a_weekend_night_lists_everyone_as_unclosed() -> None:
    tid = uuid.uuid4()
    out = cli.summarize_delivery(
        tenant_codes={tid: "TEN-0002"}, intended=[tid], states={},
    )
    assert out["delivery"] == {}
    assert out["delivery_unclosed"] == ["TEN-0002"]


def test_a_night_with_nobody_to_serve_is_empty_on_both_sides() -> None:
    out = cli.summarize_delivery(tenant_codes={}, intended=[], states={})
    assert out == {"delivery": {}, "delivery_unclosed": [],
                   "delivery_expired": []}


# ── the exit fix did not close its own incident: the expiry path ────────────


def test_an_expired_previous_day_reaches_the_summary_and_the_verdict() -> None:
    """LIVE EVIDENCE. Four nights closed a real customer WHATSAPP_FAILED and
    exited 0 — 21, 22 and 23 July and 2 August 2026 — every one of them from
    ``expire_stale_held_deliveries`` (career_staging: the day state is stamped
    at 01:3x UTC, the NEXT morning's sweep, not the failed day's run). Those
    tenants never enter today's states, so before ``delivery_expired`` existed
    there was no path from that closure to this number at all."""
    tid = uuid.uuid4()
    out = cli.summarize_delivery(
        tenant_codes={tid: "TEN-0002"}, intended=[], states={},
        expired=[(tid, date(2026, 8, 4), "WHATSAPP_FAILED")],
    )
    assert out["delivery"] == {}                 # nobody served TONIGHT
    assert out["delivery_expired"] == [
        {"tenant": "TEN-0002", "run_date": "2026-08-04",
         "state": "WHATSAPP_FAILED"},
    ]
    verdict = cli.exit_code_for(
        "completed",
        [*out["delivery"].values(),
         *(row["state"] for row in out["delivery_expired"])],
    )
    assert verdict == cli.EXIT_DELIVERY_FAILED


def test_an_expired_day_is_reported_beside_the_tenant_it_did_not_stop() -> None:
    """A tenant can be both: yesterday expired at 04:30 and today delivered a
    minute later. Keyed by tenant id the two days overwrite each other and the
    loser is always the failure — so they travel on separate channels."""
    tid = uuid.uuid4()
    out = cli.summarize_delivery(
        tenant_codes={tid: "TEN-0002"}, intended=[tid],
        states={tid: "DELIVERED"},
        expired=[(tid, date(2026, 8, 4), "WHATSAPP_FAILED")],
    )
    assert out["delivery"] == {"TEN-0002": "DELIVERED"}
    assert [row["state"] for row in out["delivery_expired"]] == \
        ["WHATSAPP_FAILED"]
    assert cli.exit_code_for("completed", ["DELIVERED", "WHATSAPP_FAILED"]) \
        == cli.EXIT_DELIVERY_FAILED


def test_the_expired_channel_never_prints_a_raw_tenant_uuid() -> None:
    tid = uuid.uuid4()
    out = cli.summarize_delivery(
        tenant_codes={}, intended=[], states={},
        expired=[(tid, date(2026, 8, 4), "WHATSAPP_FAILED")],
    )
    assert str(tid) not in str(out)
    assert out["delivery_expired"][0]["tenant"] == "TEN-????"


# ── the total outage that exits 0 forever: no WhatsApp token ────────────────


def test_a_missing_whatsapp_token_is_a_named_reason_not_a_silence() -> None:
    assert cli.delivery_phase_for(
        deliver=True, has_whatsapp_token=False, has_tenants=True,
    ) == cli.PHASE_NO_CREDENTIALS


def test_the_three_honest_phases_keep_their_own_names() -> None:
    assert cli.delivery_phase_for(
        deliver=False, has_whatsapp_token=False, has_tenants=False,
    ) == cli.PHASE_NO_DELIVER            # the operator asked for a digest
    assert cli.delivery_phase_for(
        deliver=True, has_whatsapp_token=True, has_tenants=False,
    ) == cli.PHASE_NO_TENANTS
    assert cli.delivery_phase_for(
        deliver=True, has_whatsapp_token=True, has_tenants=True,
    ) == cli.PHASE_RAN


def test_a_night_with_no_whatsapp_token_can_never_be_a_success() -> None:
    """The emptiness rule («an empty delivery dict is not a failure») is
    right for a weekend and wrong for exactly one case: the token is blank,
    the phase never ran, and NOBODY was served. That night produced an empty
    dict too, and so exited 0 — every night, for as long as the token stayed
    missing. It is the likeliest way this product goes dark: the Meta token in
    use is the temporary one."""
    assert cli.exit_code_for(
        "completed", [], delivery_phase=cli.PHASE_NO_CREDENTIALS,
    ) == cli.EXIT_NO_WHATSAPP_TOKEN


def test_the_honest_empty_nights_still_exit_zero() -> None:
    # a weekend / a digest run / nobody active — none of them a fault
    for phase in (cli.PHASE_NO_DELIVER, cli.PHASE_NO_TENANTS, cli.PHASE_RAN):
        assert cli.exit_code_for("completed", [], delivery_phase=phase) == 0


def test_the_missing_token_gets_its_own_runbook_number() -> None:
    """Not 3. Code 3 sends the operator hunting for whose delivery broke, and
    on this night nobody's broke — nobody was attempted."""
    assert cli.EXIT_NO_WHATSAPP_TOKEN not in (
        cli.EXIT_OK, cli.EXIT_DISCOVERY_FAILED, cli.EXIT_NO_SEARCH_KEY,
        cli.EXIT_DELIVERY_FAILED,
    )


def test_a_collapsed_discovery_still_outranks_the_missing_token() -> None:
    assert cli.exit_code_for(
        "discovery_failed", [], delivery_phase=cli.PHASE_NO_CREDENTIALS,
    ) == cli.EXIT_DISCOVERY_FAILED


def test_the_summary_never_prints_a_raw_tenant_uuid() -> None:
    # AUDIT ك-17 — a real 20 July journal line leaked one
    tid = uuid.uuid4()
    out = cli.summarize_delivery(
        tenant_codes={}, intended=[tid], states={tid: "DELIVERED"},
    )
    assert str(tid) not in str(out)
    assert out["delivery"] == {"TEN-????": "DELIVERED"}


# ── P0-8: the environment against the database ──────────────────────────────

#: What the database actually holds after migration 0020.
APPROVED = {
    "basic": Decimal("149"), "cv_analysis": Decimal("29"),
    "professional": Decimal("199"), "executive": Decimal("449"),
}
#: What the canonical sale table offers — `basic` is absent, and that absence
#: IS its retirement (0020 deletes no row: subscriptions point at it).
ON_SALE = {
    "cv_analysis": Decimal("29.00"), "professional": Decimal("199.00"),
    "executive": Decimal("449.00"),
}
TODAY = date(2026, 8, 5)


def _settings(**overrides: str) -> SimpleNamespace:
    base = {
        "salla_product_catalog": '{"1": "professional"}',
        "salla_product_pricing": '{"1": [199.00, "SAR"]}',
        "salla_token_expires_at": "2026-12-31",
        "salla_store_url": "https://store.example/lammah",
        # the delivery credentials the check used to ignore entirely
        "whatsapp_access_token": "EAAG-not-a-real-token",
        "whatsapp_phone_number_id": "123456789",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _check(**overrides: str) -> list[cli.EnvProblem]:
    return cli.verify_environment(
        _settings(**overrides), approved_prices=APPROVED,
        sale_plans=ON_SALE, today=TODAY,
    )


def test_a_correct_environment_is_silent() -> None:
    assert _check() == []


def test_the_live_environment_of_5_august_is_caught_whole() -> None:
    """The exact stale file: a retired plan, a retired price, a token that
    died a week ago and no store URL — four faults, twelve days, no word."""
    problems = _check(
        salla_product_catalog='{"786318419": "basic"}',
        salla_product_pricing='{"786318419": [149.00, "SAR"]}',
        salla_token_expires_at="2026-07-29",
        salla_store_url="",
    )
    keys = sorted({p.key for p in problems})
    assert keys == ["SALLA_PRODUCT_CATALOG", "SALLA_STORE_URL",
                    "SALLA_TOKEN_EXPIRES_AT"]
    english = " | ".join(p.english for p in problems)
    assert "RETIRED from sale" in english
    assert "EXPIRED 7 days ago" in english


def test_a_retired_plan_is_caught_even_at_its_own_old_price() -> None:
    """The trap: `basic` at 149 matches plan_entitlements exactly, so a
    price-only check passes it. Sellability is the separate question."""
    problems = _check(
        salla_product_catalog='{"9": "basic"}',
        salla_product_pricing='{"9": [149.00, "SAR"]}',
    )
    assert [p.key for p in problems] == ["SALLA_PRODUCT_CATALOG"]
    assert "RETIRED" in problems[0].english


def test_a_plan_code_with_no_database_row_is_caught() -> None:
    problems = _check(salla_product_catalog='{"9": "platinum"}',
                      salla_product_pricing='{"9": [199.00, "SAR"]}')
    assert any("no row in plan_entitlements" in p.english for p in problems)


def test_a_price_that_disagrees_with_the_database_is_caught() -> None:
    problems = _check(salla_product_pricing='{"1": [149.00, "SAR"]}')
    assert [p.key for p in problems] == ["SALLA_PRODUCT_PRICING"]
    assert "AMOUNT_MISMATCH" in problems[0].english


def test_a_foreign_currency_is_caught_before_the_numbers_are_compared() -> None:
    problems = _check(salla_product_pricing='{"1": [199.00, "USD"]}')
    assert [p.key for p in problems] == ["SALLA_PRODUCT_PRICING"]
    assert "cannot be compared" in problems[0].english


def test_a_cataloged_product_with_no_price_still_reports_ha_4() -> None:
    problems = _check(salla_product_pricing="{}")
    assert [p.key for p in problems] == ["SALLA_PRODUCT_PRICING"]
    assert "FAIL CLOSED" in problems[0].english


def test_the_two_price_authorities_must_agree_with_each_other() -> None:
    """Three copies of «what a pass costs» exist by hand. Fixing one of them
    and not the others is the same silence in a new place."""
    problems = cli.verify_environment(
        _settings(), approved_prices={**APPROVED,
                                      "professional": Decimal("279")},
        sale_plans=ON_SALE, today=TODAY,
    )
    assert any("authorities disagree" in p.english for p in problems)


def test_an_expired_token_is_caught() -> None:
    problems = _check(salla_token_expires_at="2026-08-04")
    assert [p.key for p in problems] == ["SALLA_TOKEN_EXPIRES_AT"]
    assert "EXPIRED 1 days ago" in problems[0].english


def test_a_token_inside_the_warning_window_is_caught() -> None:
    problems = _check(salla_token_expires_at="2026-08-09")   # 4 days, warn 5
    assert [p.key for p in problems] == ["SALLA_TOKEN_EXPIRES_AT"]
    assert "expires in 4 days" in problems[0].english


def test_a_token_beyond_the_window_is_quiet() -> None:
    assert _check(salla_token_expires_at="2026-09-01") == []


def test_an_unset_or_unreadable_expiry_is_not_a_pass() -> None:
    """Blank used to mean «say nothing» in the provisioning path. Blank means
    «the watch is blind», which is the thing we are fixing."""
    assert _check(salla_token_expires_at="")[0].key == "SALLA_TOKEN_EXPIRES_AT"
    assert "unparseable" in _check(salla_token_expires_at="soon")[0].english


def test_a_missing_store_url_is_caught() -> None:
    problems = _check(salla_store_url="   ")
    assert [p.key for p in problems] == ["SALLA_STORE_URL"]


def test_a_missing_whatsapp_token_is_caught_at_boot() -> None:
    """The check watched four Salla facts, every one of them about SELLING and
    every one degrading only the new-order path — and said nothing about the
    credential the whole product DELIVERS through. It is also the temporary
    one: a Meta user token flagged «replace before it dies» since 17 July.
    Empty means the nightly skips delivery and no paying customer gets
    anything, which is the largest failure this file can see coming."""
    problems = _check(whatsapp_access_token="   ")
    assert [p.key for p in problems] == ["WHATSAPP_ACCESS_TOKEN"]
    assert "NO customer receives anything" in problems[0].english


def test_a_token_with_no_number_to_send_from_is_caught_too() -> None:
    problems = _check(whatsapp_phone_number_id="")
    assert [p.key for p in problems] == ["WHATSAPP_PHONE_NUMBER_ID"]


def test_the_whatsapp_alert_stays_direction_pure() -> None:
    text = cli.format_env_alert(_check(whatsapp_access_token=""))
    for line in text.splitlines():
        has_arabic = any("؀" <= ch <= "ۿ" for ch in line)
        has_latin = any(ch.isascii() and ch.isalpha() for ch in line)
        assert not (has_arabic and has_latin), line
    assert "WHATSAPP_ACCESS_TOKEN" in text


def test_an_empty_catalog_reports_only_what_it_can_prove() -> None:
    # nothing is sellable, so nothing is mis-sold; the token and URL still
    # answer for themselves
    assert _check(salla_product_catalog="{}",
                  salla_product_pricing="{}") == []


def test_malformed_json_never_raises() -> None:
    assert _check(salla_product_catalog="not json",
                  salla_product_pricing="[1,2,3]") == []


# ── P0-8: the two halves of «loud» ─────────────────────────────────────────


def test_the_admin_alert_keeps_arabic_and_latin_on_separate_lines() -> None:
    """His client reverses a line that mixes directions, so every variable
    name gets a line of its own (bidi incident, admin channel)."""
    problems = _check(
        salla_product_catalog='{"786318419": "basic"}',
        salla_product_pricing='{"786318419": [149.00, "SAR"]}',
        salla_store_url="",
    )
    text = cli.format_env_alert(problems)
    for line in text.splitlines():
        has_arabic = any("؀" <= ch <= "ۿ" for ch in line)
        has_latin = any(ch.isascii() and ch.isalpha() for ch in line)
        assert not (has_arabic and has_latin), line
    assert "SALLA_PRODUCT_CATALOG" in text
    assert "SALLA_STORE_URL" in text


def test_the_alert_does_not_repeat_the_same_sentence(
) -> None:
    problems = _check(
        salla_product_catalog='{"1": "basic", "2": "basic"}',
        salla_product_pricing='{"1": [149.00, "SAR"], "2": [149.00, "SAR"]}',
    )
    assert len(problems) == 2                      # both products reported
    text = cli.format_env_alert(problems)
    assert text.count("متوقفة عن البيع") == 1      # one sentence for the human


class _Admin:
    def __init__(self, *, explode: bool = False) -> None:
        self.sent: list[str] = []
        self.explode = explode

    def send_admin(self, text: str) -> str:
        if self.explode:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        return "ok"


def test_the_report_logs_error_and_alerts_and_never_raises(
    monkeypatch, caplog
) -> None:
    """ERROR because that is the only level the operator's journal harvester
    forwards; and it returns instead of exiting, because both processes that
    host it are serving customers who have already paid."""
    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
    admin = _Admin()
    with caplog.at_level("ERROR"):
        problems = cli.report_environment(
            settings=_settings(salla_store_url=""), session=None,
            admin_client=admin, today=TODAY,
        )
    assert [p.key for p in problems] == ["SALLA_STORE_URL"]
    assert any("BOOT CHECK SALLA_STORE_URL" in r.message for r in caplog.records)
    assert len(admin.sent) == 1


def test_a_clean_environment_sends_nothing(monkeypatch) -> None:
    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
    admin = _Admin()
    assert cli.report_environment(settings=_settings(), session=None,
                                  admin_client=admin, today=TODAY) == []
    assert admin.sent == []


def test_a_dead_admin_channel_does_not_stop_the_boot(monkeypatch) -> None:
    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
    problems = cli.report_environment(
        settings=_settings(salla_store_url=""), session=None,
        admin_client=_Admin(explode=True), today=TODAY,
    )
    assert [p.key for p in problems] == ["SALLA_STORE_URL"]


def test_the_worker_logs_but_does_not_page_on_every_restart(
    monkeypatch, caplog
) -> None:
    """Restart=always/RestartSec=5: a crash loop must not send the alert
    twelve times a minute. The ERROR line still goes to the harvester."""
    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
    admin = _Admin()
    with caplog.at_level("ERROR"):
        problems = cli.report_environment(
            settings=_settings(salla_store_url=""), session=None,
            admin_client=admin, today=TODAY, alert=False,
        )
    assert [p.key for p in problems] == ["SALLA_STORE_URL"]
    assert any("BOOT CHECK" in r.message for r in caplog.records)
    assert admin.sent == []


def test_an_unreadable_authority_is_logged_not_swallowed(
    monkeypatch, caplog
) -> None:
    def _boom() -> dict[str, Decimal]:
        raise RuntimeError("canonical plan table unreadable")

    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", _boom)
    with caplog.at_level("ERROR"):
        assert cli.report_environment(
            settings=_settings(), session=None, admin_client=_Admin(),
            today=TODAY,
        ) == []
    assert any("could not run" in r.message for r in caplog.records)


# ── P0-8: the authorities are real, not restated here ──────────────────────


def test_the_sale_table_is_read_from_the_wiring_tool() -> None:
    """`basic` is retired by being ABSENT from the table the wiring tool
    writes the catalog from — nothing in this repo may keep its own copy."""
    plans = cli.canonical_sale_plans()
    assert "basic" not in plans
    assert plans["professional"] == Decimal("199.00")
    assert set(plans) == {"cv_analysis", "professional", "executive"}


# ── the §05 sweep: one bad row used to cost the whole night's marks ─────────


def test_a_lifecycle_sweep_that_did_not_finish_can_never_be_a_success() -> None:
    """THE POINT OF THE WHOLE FIX, and the part that is easiest to lose.

    Before 2026-08-07 the sweep ran every customer in ONE transaction under
    one broad `except Exception: logger.error(...)`, and the night carried on
    and exited 0. Making it commit per customer fixes the amplification and
    creates a new way to be silent: the sweep now SURVIVES a customer's
    failure, so «most customers were swept» would look exactly like «all
    customers were swept» to systemd. «أي عميل يفشل ← الليلة تفشل» is not
    satisfied by surviving.
    """
    assert cli.exit_code_for(
        "completed", ["DELIVERED"], delivery_phase=cli.PHASE_RAN,
        lifecycle_failed=True,
    ) == cli.EXIT_LIFECYCLE_FAILED


def test_a_completed_sweep_leaves_an_honest_night_at_zero() -> None:
    """The other side of the line — a guard that only ever proves «this fails»
    gets muted the first time it fires on a good night."""
    assert cli.exit_code_for(
        "completed", ["DELIVERED", "NO_MATCHES"], delivery_phase=cli.PHASE_RAN,
        lifecycle_failed=False,
    ) == cli.EXIT_OK
    # and the argument is optional, so every existing caller still answers
    assert cli.exit_code_for("completed", ["DELIVERED"]) == cli.EXIT_OK


def test_the_lifecycle_number_is_ranked_below_the_ones_it_shares_a_night_with(
) -> None:
    """A customer who received nothing today outranks a customer whose renewal
    clock did not tick tonight; a total outage and a collapsed discovery
    outrank both. One number, one runbook entry — so the ORDER matters as much
    as the number does."""
    assert cli.exit_code_for(
        "completed", ["WHATSAPP_FAILED"], delivery_phase=cli.PHASE_RAN,
        lifecycle_failed=True,
    ) == cli.EXIT_DELIVERY_FAILED
    assert cli.exit_code_for(
        "completed", [], delivery_phase=cli.PHASE_NO_CREDENTIALS,
        lifecycle_failed=True,
    ) == cli.EXIT_NO_WHATSAPP_TOKEN
    assert cli.exit_code_for(
        "discovery_failed", [], lifecycle_failed=True,
    ) == cli.EXIT_DISCOVERY_FAILED


def test_the_sweep_result_answers_the_verdict_in_one_word() -> None:
    """`clean` is what `main()` reads. A sweep that never started is not a
    clean one — that was the old behaviour and it exited 0 every night."""
    assert cli.LifecycleSweep().clean is True
    assert cli.LifecycleSweep(failed=("TEN-0002",)).clean is False
    assert cli.LifecycleSweep(aborted=True).clean is False


def test_the_sweep_summary_never_prints_a_raw_tenant_uuid() -> None:
    """§15.13 / AUDIT ك-17. This object goes into the journal line."""
    swept = cli.LifecycleSweep(
        counts={"reminded": 1}, attempted=2, failed=("TEN-0002",),
    )
    assert swept.summary() == {
        "customers": 2, "counts": {"reminded": 1},
        "failed": ["TEN-0002"], "aborted": False,
    }


# ── the same thing against a real transaction ──────────────────────────────


def _customer(owner_session: Session, *, period_end: Any) -> tuple[str, str]:
    """One paying customer, provisioned and activated the way the product does
    it. Returns (tenant_id, TEN code)."""
    from tests.test_salla_lifecycle import _active_sub

    sub_id = _active_sub(owner_session, period_end=period_end)
    row = owner_session.execute(sql_text(
        "SELECT s.tenant_id::text, t.code FROM subscriptions s"
        " JOIN tenants t ON t.id = s.tenant_id WHERE s.id = :id"),
        {"id": str(sub_id)}).one()
    return str(row[0]), str(row[1])


def _marks(owner_session: Session, tenant_id: str) -> list[str]:
    owner_session.expire_all()
    return [r[0] for r in owner_session.execute(sql_text(
        "SELECT event_type FROM subscription_events WHERE tenant_id = :t"
        " ORDER BY created_at"), {"t": tenant_id}).all()]


def test_one_customers_failure_no_longer_discards_the_others_marks(
    owner_engine: Engine, owner_session: Session, clean_billing: None
) -> None:
    """THE INCIDENT, reproduced and then refuted.

    Three customers all due their day-27 renewal reminder. The sweep raises on
    the middle one — which is what a Meta message id too long for
    `delivery_messages.wa_message_id` did on 2026-08-06, and what anything at
    all can do again.

    Under the old shape (one Session, one commit at the end, one broad
    `except`) the exception unwound the whole transaction: the first
    customer's `renewal_reminder_d3` mark was rolled back although Meta had
    already been paid for that template, and the third customer was never
    reached at all. The next night re-sent and re-paid for both.

    What is asserted here is the mark, not the counter: the mark IS the
    idempotency, so a mark that did not survive the transaction is a template
    that will be bought twice.
    """
    from career.salla.lifecycle import sweep_subscription_lifecycle
    from career.whatsapp.client import FakeWhatsAppClient
    from tests.test_salla_lifecycle import NOW

    due = NOW + timedelta(days=2, hours=12)          # inside the d27 window
    customers = dict(
        _customer(owner_session, period_end=due) for _ in range(3)
    )
    by_code = {code: tid for tid, code in customers.items()}
    doomed_code = sorted(by_code)[1]                 # swept second of three
    doomed = by_code[doomed_code]

    def flaky(session: Session, **kwargs: Any) -> dict[str, int]:
        counts = sweep_subscription_lifecycle(session, **kwargs)
        # Which customer is this? Ask the scoped session THROUGH THE ORM,
        # which is the only way the scope is visible at all — it is a
        # `with_loader_criteria` option, so a raw SELECT walks past it.
        mine = {
            str(t) for t in session.execute(
                select(Subscription.tenant_id).distinct()
            ).scalars()
        }
        if doomed in mine:
            raise RuntimeError("value too long for type character varying(128)")
        return counts

    result = cli.sweep_lifecycle_per_customer(
        lambda: Session(owner_engine), now=NOW,
        whatsapp_client=FakeWhatsAppClient(), sweep=flaky,
    )

    # The marks first, deliberately: they are the claim, and a run that fails
    # on `result.failed` instead has answered a smaller question.
    for tenant_id, code in customers.items():
        marks = _marks(owner_session, tenant_id)
        if code == doomed_code:
            assert "renewal_reminder_d3" not in marks, (
                "the failed customer's transaction did not roll back"
            )
        else:
            assert "renewal_reminder_d3" in marks, (
                f"{code} lost a mark to a DIFFERENT customer's failure — that "
                "is the amplifier, and Meta was already paid for that template"
            )
    assert doomed_code in result.failed
    assert result.clean is False


def test_the_customers_after_the_failure_are_still_swept(
    owner_engine: Engine, owner_session: Session, clean_billing: None
) -> None:
    """The old shape did not merely lose the marks behind it: the exception
    left `sweep_subscription_lifecycle` entirely, so everybody after the bad
    row was never looked at. Their period did not end, their grace did not
    elapse, and nothing said so."""
    from career.salla.lifecycle import sweep_subscription_lifecycle
    from career.whatsapp.client import FakeWhatsAppClient
    from tests.test_salla_lifecycle import NOW

    customers = dict(
        _customer(owner_session, period_end=NOW - timedelta(hours=1))
        for _ in range(2)
    )
    by_code = {code: tid for tid, code in customers.items()}
    first_code = sorted(by_code)[0]

    def explode_on_the_first(session: Session, **kwargs: Any) -> dict[str, int]:
        mine = {
            str(t) for t in session.execute(
                select(Subscription.tenant_id).distinct()
            ).scalars()
        }
        if by_code[first_code] in mine:
            raise RuntimeError("the first customer of the night")
        return sweep_subscription_lifecycle(session, **kwargs)

    result = cli.sweep_lifecycle_per_customer(
        lambda: Session(owner_engine), now=NOW,
        whatsapp_client=FakeWhatsAppClient(), sweep=explode_on_the_first,
    )

    assert first_code in result.failed
    owner_session.expire_all()
    statuses = {
        code: owner_session.execute(sql_text(
            "SELECT status FROM subscriptions WHERE tenant_id = :t"),
            {"t": tid}).scalars().all()
        for tid, code in customers.items()
    }
    assert statuses[first_code] == ["ACTIVE"], "the failure was not rolled back"
    later = sorted(by_code)[1]
    assert statuses[later] == ["GRACE"], (
        "the customer after the failure was never reached"
    )


def test_a_rolled_back_customer_is_redone_by_the_next_run(
    owner_engine: Engine, owner_session: Session, clean_billing: None
) -> None:
    """«Does a partial sweep leave any state the next run reads as already
    done?» — the question that decides the commit BOUNDARY.

    Per customer it cannot: the unit is all-or-nothing, so a rollback restores
    exactly the state tomorrow's run expects. A finer boundary would not be
    safe, and that is not a matter of taste — commit between the ACTIVE→GRACE
    transition and the renewal reminder it triggers, and a failure in the
    second leaves the customer parked in GRACE, which the next sweep reads as
    handled. The reminder is not retried, it is LOST.
    """
    from career.salla.lifecycle import sweep_subscription_lifecycle
    from career.whatsapp.client import FakeWhatsAppClient
    from tests.test_salla_lifecycle import NOW

    tenant_id, code = _customer(
        owner_session, period_end=NOW - timedelta(hours=1)
    )

    def always_fails(session: Session, **kwargs: Any) -> dict[str, int]:
        sweep_subscription_lifecycle(session, **kwargs)
        raise RuntimeError("after the work, before the commit")

    first = cli.sweep_lifecycle_per_customer(
        lambda: Session(owner_engine), now=NOW,
        whatsapp_client=FakeWhatsAppClient(), sweep=always_fails,
    )
    assert code in first.failed
    owner_session.expire_all()
    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE tenant_id = :t"),
        {"t": tenant_id}).scalar_one() == "ACTIVE"

    wa = FakeWhatsAppClient()
    second = cli.sweep_lifecycle_per_customer(
        lambda: Session(owner_engine), now=NOW, whatsapp_client=wa,
    )

    assert second.clean is True
    owner_session.expire_all()
    assert owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE tenant_id = :t"),
        {"t": tenant_id}).scalar_one() == "GRACE"
    assert "renewal_reminder_grace" in _marks(owner_session, tenant_id), (
        "the reminder the transition exists to trigger never went out"
    )


def test_the_scope_is_the_customer_so_a_renewed_row_is_still_recognised(
    owner_engine: Engine, owner_session: Session, clean_billing: None
) -> None:
    """The one way this refactor could have broken §16 silently.

    `_superseded_by_renewal` asks «does this customer hold a LIVE row with a
    later period?» — a within-tenant question, and the only thing that stops an
    ACTIVE paying customer being walked through their previous period again.
    Scope a session to one SUBSCRIPTION and that question answers «no» for
    every row, and the renewed customer gets a «نفتقدك» recovery template about
    a period they already paid to replace. Scoped to the CUSTOMER it is
    answered exactly as it is in one global pass.
    """
    from career.whatsapp.client import FakeWhatsAppClient
    from tests.test_salla_lifecycle import NOW

    period_end = NOW - timedelta(days=10)            # recovery is due today
    tenant_id, _code = _customer(owner_session, period_end=period_end)
    owner_session.execute(sql_text(
        "UPDATE subscriptions SET status = 'EXPIRED' WHERE tenant_id = :t"),
        {"t": tenant_id})
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency, current_period_end)"
        " VALUES (gen_random_uuid(), :t, 'professional', 'ACTIVE', :o,"
        " 279.00, 'SAR', :e)"),
        {"t": tenant_id, "o": f"ORD-{uuid.uuid4()}",
         "e": NOW + timedelta(days=20)})
    owner_session.commit()

    wa = FakeWhatsAppClient()
    result = cli.sweep_lifecycle_per_customer(
        lambda: Session(owner_engine), now=NOW, whatsapp_client=wa,
    )

    assert result.clean is True
    assert [m.template_name for m in wa.sent if m.kind == "template"] == []
    assert "recovery_sent" not in _marks(owner_session, tenant_id)


def test_each_sweep_gets_a_session_that_can_see_exactly_one_customer(
    owner_engine: Engine, owner_session: Session, clean_billing: None
) -> None:
    """What makes the per-customer transaction a per-customer transaction.

    Without the scope the sweep would do the whole night's work inside each
    session and a rollback would still discard everybody's marks — the
    amplifier back through the door, with a commit count that looks fixed.
    """
    from tests.test_salla_lifecycle import NOW

    seen: list[set[str]] = []
    mine = {
        code for _tid, code in (
            _customer(owner_session, period_end=NOW + timedelta(days=30))
            for _ in range(2)
        )
    }

    def observe(session: Session, **_kwargs: Any) -> dict[str, int]:
        seen.append({
            str(t) for t in session.execute(
                select(Tenant.code)
                .join(Subscription, Subscription.tenant_id == Tenant.id)
                .distinct()
            ).scalars()
        })
        return {}

    cli.sweep_lifecycle_per_customer(
        lambda: Session(owner_engine), now=NOW, sweep=observe,
    )

    for one in seen:
        assert len(one) <= 1, f"a sweep saw more than one customer: {one}"
    assert mine <= {code for one in seen for code in one}


def test_the_sweep_reads_its_subscriptions_through_the_orm() -> None:
    """The scope is `with_loader_criteria`, which is an ORM-level filter: a
    raw ``text("SELECT ... FROM subscriptions")`` inside the sweep would walk
    straight past it, and one customer's transaction would silently carry
    another customer's work again. §05 uses the ORM for every one of its
    reads today; this is the guard that says it has to keep doing so."""
    import ast as _ast
    import pathlib as _pathlib

    from career.salla import lifecycle

    tree = _ast.parse(
        _pathlib.Path(lifecycle.__file__).read_text(encoding="utf-8")
    )
    imported = {
        alias.name
        for node in _ast.walk(tree) if isinstance(node, _ast.ImportFrom)
        for alias in node.names
    }
    assert "text" not in imported, (
        "the §05 sweep imported sqlalchemy.text — if it now reads "
        "subscriptions with raw SQL, `cli._scope_to_one_customer` no longer "
        "bounds what one transaction touches"
    )
