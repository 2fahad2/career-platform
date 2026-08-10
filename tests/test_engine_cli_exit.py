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

import logging as _logging
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
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


def test_a_configured_whatsapp_token_is_not_the_same_as_a_LIVE_one() -> None:
    """The gap this check was blind to, in one assertion.

    Both calls below pass the identical settings — a non-empty
    ``WHATSAPP_ACCESS_TOKEN`` and a non-empty phone number id — and the first
    is silent, because a string in a file is all it ever looked at. That is
    precisely the shape of the 2026-07-29 outage: `SALLA_TOKEN_EXPIRES_AT`
    stayed exactly as somebody had typed it while the credential it described
    was dead, and paid orders failed to provision for nine days.

    The second passes Meta's own answer about the token that EXISTS, and that
    answer is «revoked». Full facts, ladder and cadence live in
    `tests/test_whatsapp_token_watch.py`; what belongs HERE is that the boot
    check consumes them at all.
    """
    from career.whatsapp.client import TokenHealth, TokenVerdict

    assert _check() == []
    problems = cli.verify_environment(
        _settings(), approved_prices=APPROVED, sale_plans=ON_SALE, today=TODAY,
        token_health=TokenHealth(TokenVerdict.INVALID, error_code=190),
    )
    assert [p.key for p in problems] == ["WHATSAPP_ACCESS_TOKEN"]
    assert "META REFUSES IT" in problems[0].english


def test_an_empty_token_is_still_reported_without_asking_meta() -> None:
    """The emptiness check keeps its own rung. There is nothing to inspect, so
    a live answer would be a network call spent to learn what the file already
    says — and the message a human needs is different: «put a token in the
    file», not «your token was revoked»."""
    problems = _check(whatsapp_access_token="")
    assert [p.key for p in problems] == ["WHATSAPP_ACCESS_TOKEN"]
    assert "empty" in problems[0].english


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


# ── the price-test window: the day the alarm has to be TRUE ────────────────
#
# The gateway is proven by dropping the three REAL products to 1.00 SAR for a
# few days. To this check that is a 1-SAR product on a 199 plan, which it has
# always reported as «a real order will be refused with AMOUNT_MISMATCH» — a
# sentence that is FALSE: §09 keys on the product id and that product carries
# its own price, so the order provisions perfectly. A daily red alert whose
# text is wrong is worse than no alert; the next true one is read the same way.

#: The environment mid-test: the store, the pricing map and the window agree.
_TEST_PRICED = {"salla_product_pricing": '{"1": [1.00, "SAR"]}'}


def test_an_open_window_replaces_the_false_alarm_with_a_true_line() -> None:
    problems = _check(**_TEST_PRICED, salla_price_test_until="2026-08-10")
    assert [p.key for p in problems] == ["SALLA_PRICE_TEST_UNTIL"]
    assert problems[0].notice is True
    english = problems[0].english
    assert "AMOUNT_MISMATCH" not in english          # the false half is gone
    assert "OPEN until 2026-08-10" in english        # what is actually true
    assert "NO founder price lock is captured" in english


def test_a_window_that_has_passed_over_cheap_products_escalates() -> None:
    """The date closes itself, and that is only safe if the morning after says
    so: from that moment locks are captured again, at the test price, into
    every new customer's permanent record."""
    problems = _check(**_TEST_PRICED, salla_price_test_until="2026-08-04")
    assert [p.key for p in problems] == ["SALLA_PRODUCT_PRICING"]
    assert problems[0].notice is False
    assert "CLOSED on 2026-08-04" in problems[0].english
    assert "STILL at 1.0 SAR" in problems[0].english
    assert "wire_salla_products" in problems[0].english


def test_an_expired_window_over_restored_prices_says_nothing() -> None:
    """Once the prices are back it is the same state as «no window», and a
    daily reminder about a date that changes nothing is how the three lines
    above get skimmed past."""
    assert _check(salla_price_test_until="2026-08-04") == []


def test_a_window_open_over_full_prices_is_a_promise_switched_off() -> None:
    """The mirror of the trap, and the reason this is a date the operator sets
    rather than a flag the code infers: nothing is broken, and every customer
    who buys today silently gets no locked price."""
    problems = _check(salla_price_test_until="2026-08-10")
    assert [p.key for p in problems] == ["SALLA_PRICE_TEST_UNTIL"]
    assert problems[0].notice is False
    assert "suspended for nothing" in problems[0].english


def test_a_window_longer_than_the_maximum_is_caught_by_hand_too() -> None:
    """The wiring tool refuses to write one; the file can still be edited."""
    problems = _check(**_TEST_PRICED, salla_price_test_until="2027-01-01")
    keys = [p.key for p in problems]
    assert keys.count("SALLA_PRICE_TEST_UNTIL") == 2   # the notice, and this
    assert any("the maximum is" in p.english and not p.notice
               for p in problems)


def test_an_unreadable_window_is_reported_and_read_as_open() -> None:
    """`price_lock` treats an unparseable date as an open window — a lock not
    captured is recoverable, a lock captured at a test price is not. That
    choice is only defensible while it is impossible to sit in unnoticed."""
    problems = _check(salla_price_test_until="next friday")
    assert [p.key for p in problems] == ["SALLA_PRICE_TEST_UNTIL"]
    assert problems[0].notice is False
    assert "treated as an OPEN price test window" in problems[0].english


def test_an_absent_window_is_the_default_and_changes_nothing() -> None:
    """A settings object that has never heard of the variable — the shape every
    other caller and every older host has — must behave exactly as before."""
    assert _check() == []
    assert _check(**_TEST_PRICED)[0].english.endswith("AMOUNT_MISMATCH")


def test_the_window_alert_is_direction_pure_including_its_date() -> None:
    """The date is the trap here: an ISO date is ASCII digits, and a line that
    mixes them with Arabic arrives at him reversed. The real repository rule
    is imported rather than restated — the hand-written checks in this file
    look only for Latin LETTERS and would pass a scrambled line."""
    from tests.test_alert_direction_purity import line_is_mixed

    for until in ("2026-08-10", "2026-08-04", "next friday", "2027-01-01"):
        text = cli.format_env_alert(
            _check(**_TEST_PRICED, salla_price_test_until=until))
        assert not [line for line in text.splitlines() if line_is_mixed(line)]


def test_a_notice_alone_does_not_wear_the_red_banner() -> None:
    """A morning whose only news is «the window is open» is not a morning when
    the configuration contradicts the database, and it must not arrive looking
    like one — nor list a variable to «correct» when there is nothing to fix."""
    text = cli.format_env_alert(
        _check(**_TEST_PRICED, salla_price_test_until="2026-08-10"))
    assert "🔴" not in text and "🟡" in text
    assert "المتغيرات المطلوب تصحيحها" not in text
    # …while a real fault beside it puts the banner back and lists only IT
    both = cli.format_env_alert(
        _check(**_TEST_PRICED, salla_price_test_until="2026-08-10",
               salla_store_url=""))
    assert "🔴" in both
    assert "SALLA_STORE_URL" in both and "SALLA_PRICE_TEST_UNTIL" not in both


def test_a_notice_is_logged_but_not_as_an_error(monkeypatch, caplog) -> None:
    """ERROR is the level his journal harvester forwards. A deliberate state
    that shouts ERROR every morning teaches him to skim the level that carries
    the outages."""
    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
    admin = _Admin()
    with caplog.at_level("WARNING"):
        problems = cli.report_environment(
            settings=_settings(**_TEST_PRICED,
                               salla_price_test_until="2026-08-10"),
            session=None, admin_client=admin, today=TODAY,
        )
    assert [p.notice for p in problems] == [True]
    assert any("BOOT NOTICE" in r.message and r.levelname == "WARNING"
               for r in caplog.records)
    assert not any("BOOT CHECK" in r.message for r in caplog.records)
    assert len(admin.sent) == 1          # he is still told, every morning


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


# ── «ما نبيع الكرسي رقم ٣١»: the counter nothing ever compared ─────────────


def _seat_settings(**overrides: str) -> SimpleNamespace:
    return _settings(salla_api_key="k", **overrides)


def _patch_seats(monkeypatch, *, taken: int, sellable: int, reads: list) -> None:
    from career.salla import seats

    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
    monkeypatch.setattr(
        seats, "founding_seats",
        lambda session, **k: seats.Seats(cap=30, taken=taken,
                                         by_plan={"professional": taken}))

    def _read(**kwargs: Any) -> Any:
        reads.append(kwargs)
        return seats.SeatSupply(by_product={"1": sellable})

    monkeypatch.setattr(seats, "read_seat_supply", _read)


def test_a_store_that_will_oversell_the_wave_is_reported(monkeypatch) -> None:
    """The live configuration on 2026-08-08: one seat taken and forty still
    sellable against a page that says thirty. Constant 4 forbids refusing a
    paid order, so the dashboard is the only place this can be stopped — which
    means it has to be SAID before somebody buys the thirty-first seat."""
    reads: list = []
    _patch_seats(monkeypatch, taken=1, sellable=40, reads=reads)
    admin = _Admin()
    cli.report_environment(settings=_seat_settings(), session=None,
                           admin_client=admin, today=TODAY)
    assert len(reads) == 1
    assert any("كراسي التأسيس" in m for m in admin.sent)


def test_two_counters_that_agree_say_nothing(monkeypatch) -> None:
    """A nightly «all good» beside a boot check is how a channel stops being
    read."""
    reads: list = []
    _patch_seats(monkeypatch, taken=1, sellable=29, reads=reads)
    admin = _Admin()
    assert cli.report_environment(settings=_seat_settings(), session=None,
                                  admin_client=admin, today=TODAY) == []
    assert admin.sent == []


def test_the_worker_never_spends_a_salla_read_on_a_restart(monkeypatch) -> None:
    """`alert=False` is the conversation worker under Restart=always/
    RestartSec=5. Two product reads per restart, twelve restarts a minute, is
    a crash loop hammering Salla — the same argument that keeps the WhatsApp
    token inspection out of the worker's boot check."""
    reads: list = []
    _patch_seats(monkeypatch, taken=1, sellable=40, reads=reads)
    admin = _Admin()
    cli.report_environment(settings=_seat_settings(), session=None,
                           admin_client=admin, today=TODAY, alert=False)
    assert reads == [] and admin.sent == []


def test_no_api_key_reads_nothing_and_repeats_no_emptiness(monkeypatch) -> None:
    reads: list = []
    _patch_seats(monkeypatch, taken=1, sellable=40, reads=reads)
    admin = _Admin()
    cli.report_environment(settings=_settings(), session=None,
                           admin_client=admin, today=TODAY)
    assert reads == [] and admin.sent == []


def test_a_seat_read_that_explodes_never_reaches_the_caller(monkeypatch) -> None:
    """It runs inside the boot check of the two processes that serve people who
    have already paid, so an unreachable Salla is a log line and nothing more."""
    from career.salla import seats

    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)

    def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("salla down")

    monkeypatch.setattr(seats, "read_seat_supply", _boom)
    assert cli.report_environment(settings=_seat_settings(), session=None,
                                  admin_client=_Admin(), today=TODAY) == []


# ── P0-8: the authorities are real, not restated here ──────────────────────


def test_the_sale_table_is_read_from_the_wiring_tool() -> None:
    """`basic` is retired by being ABSENT from the table the wiring tool
    writes the catalog from — nothing in this repo may keep its own copy."""
    plans = cli.canonical_sale_plans()
    assert "basic" not in plans
    assert plans["professional"] == Decimal("199.00")
    assert set(plans) == {"cv_analysis", "professional", "executive"}


# ── the wiring tool's test price: the same proof, at the price being tested ─


def _wiring() -> Any:
    """The wiring script as a module — loaded exactly the way
    `cli.canonical_sale_plans` loads it, so this tests the real file."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" \
        / "wire_salla_products.py"
    spec = importlib.util.spec_from_file_location("_wiring_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TODAY = date(2026, 8, 8)


def test_the_test_price_is_wired_for_every_plan_and_bounded_in_time() -> None:
    """The pricing map must match what Salla actually charges or every single
    purchase is refused — so the test price is wired for all three products,
    and it goes through the same mandatory rehearsal as any other price."""
    prices, refusals = _wiring().prices_for_window(
        Decimal("1.00"), date(2026, 8, 12), _TODAY)
    assert refusals == []
    assert prices == {"cv_analysis": Decimal("1.00"),
                      "professional": Decimal("1.00"),
                      "executive": Decimal("1.00")}


def test_the_canonical_table_is_untouched_by_a_test_price() -> None:
    """`cli.canonical_sale_plans` reads PLANS to check the environment against
    `plan_entitlements`. Rewriting it for the window would make both
    authorities agree on 1.00 and silence the boot check that is watching for
    exactly this."""
    module = _wiring()
    module.prices_for_window(Decimal("1.00"), date(2026, 8, 12), _TODAY)
    assert {plan: price for plan, price in module.PLANS.values()} == {
        "cv_analysis": Decimal("29.00"), "professional": Decimal("199.00"),
        "executive": Decimal("449.00")}
    assert cli.canonical_sale_plans()["professional"] == Decimal("199.00")


def test_a_test_price_without_a_closing_date_is_refused() -> None:
    """The two halves are one flag. A cheap catalog with no window is the trap
    itself: every buyer's founding price recorded as the test price, forever —
    and this tool is the only writer that can make that state impossible."""
    _p, refusals = _wiring().prices_for_window(Decimal("1.00"), None, _TODAY)
    assert refusals and "one flag in two halves" in refusals[0]
    _p2, refusals2 = _wiring().prices_for_window(
        None, date(2026, 8, 12), _TODAY)
    assert refusals2


def test_a_window_that_closes_in_the_past_is_refused() -> None:
    _p, refusals = _wiring().prices_for_window(
        Decimal("1.00"), date(2026, 8, 7), _TODAY)
    assert refusals and "in the past" in refusals[0]


def test_a_window_longer_than_the_maximum_is_refused() -> None:
    """While it is open NO customer records the price they bought at. A few
    days of that is a rehearsal; a season of it is a broken promise."""
    module = _wiring()
    _p, refusals = module.prices_for_window(
        Decimal("1.00"),
        _TODAY + timedelta(days=module.PRICE_TEST_MAX_DAYS + 1), _TODAY)
    assert refusals and "maximum is" in refusals[0]


def test_a_test_price_that_is_not_below_the_real_one_is_refused() -> None:
    """A «test price» at or above the real price is a price CHANGE, and this
    repository changes prices by editing the whitepaper first."""
    _p, refusals = _wiring().prices_for_window(
        Decimal("199.00"), date(2026, 8, 12), _TODAY)
    assert any("not below the real price" in line for line in refusals)


def test_no_flags_at_all_wires_the_real_prices_and_closes_the_window() -> None:
    prices, refusals = _wiring().prices_for_window(None, None, _TODAY)
    assert refusals == []
    assert prices["professional"] == Decimal("199.00")


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


# ── the nightly template probe: the poll half of the pair (2026-08-08) ───────
#
# `preferred_daily_template` chooses on META'S OWN ANSWER when it is handed a
# {name: category} mapping — the form its docstring calls «the only form that
# cannot be wrong» — and falls back to a hand-ordered guess about categories
# when it is handed a bare set of names. This probe asked Meta for
# `fields=name,status`, so a set of names was all it could produce, so the
# guess is the branch that ran every night while Meta had quietly moved five of
# the eight templates from UTILITY to MARKETING.
#
# The rows below are the live account's own shape, read with a read-only GET on
# 2026-08-08 (`GET /{waba}/message_templates
# ?fields=name,status,category,previous_category`), Meta's `hello_world` and
# `jaspers_market_*` samples included — they are on the account and they are
# not ours.

@pytest.fixture(autouse=True)
def _no_leaked_template_observations() -> Any:
    from career.whatsapp.templates import forget_live_observations

    forget_live_observations()
    yield
    forget_live_observations()


def _meta_rows(**overrides: str) -> list[dict[str, Any]]:
    live = {
        "subscription_daily_report": "UTILITY",
        "daily_service_update": "MARKETING",
        "daily_opportunities_utility": "MARKETING",
        "daily_opportunities_marketing": "MARKETING",
        "welcome_activation": "MARKETING",
        "onboarding_reminder": "MARKETING",
        "renewal_reminder": "MARKETING",
        "recovery": "MARKETING",
        "hello_world": "UTILITY",
        "jaspers_market_plain_text_v1": "MARKETING",
    }
    live.update(overrides)
    was_utility = {
        "daily_service_update", "daily_opportunities_utility",
        "welcome_activation", "onboarding_reminder", "renewal_reminder",
    }
    rows: list[dict[str, Any]] = []
    for name, category in live.items():
        row: dict[str, Any] = {
            "name": name, "status": "APPROVED", "category": category,
        }
        if name in was_utility:
            row["previous_category"] = "UTILITY"
        rows.append(row)
    return rows


def _wire_meta(monkeypatch: Any, rows: list[dict[str, Any]],
               ) -> list[dict[str, Any]]:
    """Answer the probe's GET and record what it ASKED for."""
    import httpx

    asked: list[dict[str, Any]] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"data": rows, "paging": {}}

    def _get(url: str, params: Any = None, headers: Any = None,
             timeout: float = 0.0) -> Any:
        asked.append(dict(params or {}))
        return _Response()

    monkeypatch.setattr(httpx, "get", _get)
    return asked


def _wa_settings() -> Any:
    return SimpleNamespace(whatsapp_access_token="tok",
                           whatsapp_waba_id="222233339830714")


def test_the_nightly_probe_asks_meta_for_the_category(
    monkeypatch: Any
) -> None:
    """One missing field is the whole defect: without `category` the chooser
    cannot take the branch that cannot be wrong, and `previous_category` is the
    column that proves a template was ACCEPTED as utility and moved
    afterwards rather than submitted wrong."""
    asked = _wire_meta(monkeypatch, _meta_rows())
    cli._preferred_daily(_wa_settings())

    assert asked, "the probe never called Meta"
    fields = str(asked[0].get("fields", ""))
    assert "category" in fields
    assert "previous_category" in fields
    assert "status" in fields and "name" in fields


def test_the_probe_obeys_metas_category_over_our_hand_ordered_guess(
    monkeypatch: Any
) -> None:
    """The two disagree here on purpose: our preference order puts
    `subscription_daily_report` first, and Meta says it is MARKETING today
    while `daily_service_update` is UTILITY. A marketing template is ~4.7×
    the price in KSA and is not delivered AT ALL to a recipient who switched
    «Offers and announcements» off — so the day must follow Meta."""
    _wire_meta(monkeypatch, _meta_rows(
        subscription_daily_report="MARKETING",
        daily_service_update="UTILITY",
    ))
    assert cli._preferred_daily(_wa_settings()).name == "daily_service_update"

    # and when Meta agrees with the order, the order is what runs
    _wire_meta(monkeypatch, _meta_rows())
    assert cli._preferred_daily(_wa_settings()).name == "subscription_daily_report"


def test_the_probe_says_a_divergence_out_loud(
    monkeypatch: Any, caplog: Any
) -> None:
    """`category_divergences` existed with zero production callers. A drift
    nobody compares is the defect it replaced, one week older — and the level
    is ERROR because the operator's harvester forwards nothing quieter."""
    _wire_meta(monkeypatch, _meta_rows(subscription_daily_report="MARKETING"))
    with caplog.at_level(_logging.ERROR, logger="career.engine"):
        cli._preferred_daily(_wa_settings())

    loud = [r.getMessage() for r in caplog.records
            if r.levelno >= _logging.ERROR]
    assert any("DIVERGED" in m and "subscription_daily_report" in m
               for m in loud), loud


def test_the_probe_prices_tonight_on_what_it_read_tonight(
    monkeypatch: Any
) -> None:
    """The probe is a measurement, so the rest of the process must use it. The
    file is dated and cannot write itself; the alternative is billing tonight's
    sends at last week's categories, which is how five templates were recorded
    at 21% of their real price."""
    from career.whatsapp.templates import TemplateCategory, billed_category

    assert billed_category("recovery") is TemplateCategory.MARKETING
    _wire_meta(monkeypatch, _meta_rows(recovery="UTILITY"))
    cli._preferred_daily(_wa_settings())
    assert billed_category("recovery") is TemplateCategory.UTILITY

    # …and never about a template that is not ours, however loudly Meta
    # reports it: `hello_world` is Meta's own sample and we never send it
    from career.whatsapp.templates import observed_category

    assert observed_category("hello_world") is None


def test_an_unreachable_meta_still_never_stops_the_night(
    monkeypatch: Any, caplog: Any
) -> None:
    """The probe is advisory. It may cost the day the cheaper template; it may
    never cost the day."""
    import httpx

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(httpx, "get", _boom)
    with caplog.at_level(_logging.ERROR, logger="career.engine"):
        chosen = cli._preferred_daily(_wa_settings())
    assert chosen.name == "subscription_daily_report"
    assert any("falling back" in r.getMessage() for r in caplog.records)


# ── the seat-supply check, and the docstring that outlived its facts ─────────


def test_the_seat_supply_note_rests_on_facts_that_are_still_true() -> None:
    """A comment that quietly becomes right is how the next one quietly
    becomes wrong.

    `_report_seat_supply` led with «ما نبيع الكرسي رقم ٣١» and reported that
    the store «was configured to sell forty seats against a page that says
    thirty». Both were true when written; neither survived the owner's
    2026-08-08 decision to move the wave to forty, after which the store and
    the cap AGREE and the number-versus-number sentence has nothing to report.
    The residual is +1, from a seat held on the retired `basic` pass that no
    product maps to any more.

    The check is not on the prose — a test that greps English rots faster than
    the comment does. It is on the two facts the prose now rests on, so that
    the next move of either one fails HERE and summons the edit, which is the
    only mechanism that has ever kept a docstring true. The retired sentence
    is kept as marked history and this pins that too: erasing it is how the
    same reasoning gets rediscovered from scratch in six months.
    """
    from career.salla.seats import _SEAT_PLANS, FOUNDING_SEATS_CAP

    doc = cli._report_seat_supply.__doc__ or ""

    assert FOUNDING_SEATS_CAP == 40, (
        "the wave moved again — _report_seat_supply's note still says forty "
        "and that the store agrees with it"
    )
    assert "basic" in _SEAT_PLANS, (
        "the note explains the +1 residual as a seat held on `basic`; if that "
        "pass stopped holding a seat the residual explanation is now wrong"
    )
    assert "+1" in doc, "the note no longer states the residual it exists for"
    # …and the superseded claim is present, but only under its own marker
    retired = "ما نبيع الكرسي رقم ٣١"
    assert retired in doc, "the record of what changed was erased, not marked"
    assert doc.index("UNTIL 2026-08-10") < doc.index(retired), (
        "the retired claim reads as current — it must sit under the marker "
        "that dates it"
    )
