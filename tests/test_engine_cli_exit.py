"""Two live incidents, pinned: the exit code that lied (P0-7) and the selling
environment that went stale in silence (P0-8).

Both are tested against the smallest honest seam. ``main()`` is thin
composition over these functions — it has exactly one ``return
exit_code_for(...)`` and one ``report_environment(...)`` call — so the
decisions are what get exercised here, at every state mix that has actually
occurred on the box or plausibly can.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from career.cv.close import DAILY_STATES
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
