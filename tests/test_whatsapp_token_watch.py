"""The credential the whole product speaks through, and the day nobody asked.

THE INCIDENT THIS IS WRITTEN AFTER. The Salla token expired on 2026-07-29 and
the first symptom anyone saw was paid orders provisioning nothing, nine days
later. The cadence audit that followed found the Meta access token had no
scheduled observer at all: `report_environment` asked whether
``WHATSAPP_ACCESS_TOKEN`` was a non-empty string — a question about a file,
which is exactly the question `SALLA_TOKEN_EXPIRES_AT` was answering
confidently and wrongly for nine days.

THE FACT THAT DECIDED THE DESIGN, and it is pinned first below because
everything else follows from it. A read-only ``GET /debug_token`` against the
live staging credential on 2026-08-08 answered ``type: SYSTEM_USER``,
``expires_at: 0``, ``data_access_expires_at: 0``, ``is_valid: true``. The token
**cannot expire**, and Meta publishes no refresh endpoint for a System User
token. So no renewal was built — a scheduled refresher for this credential
could only ever fail — and what is tested here is the set of things that CAN
still take it away with no warning: revocation, a withdrawn scope, a phone
number unassigned from the System User, and the regression of a temporary
token being pasted over the permanent one.

Every test runs against an injected transport. Nothing here reaches Meta,
nothing POSTs, and nothing sends a WhatsApp message — which is also the
property `test_the_watchdog_never_speaks_to_a_customer` asserts about the
production path rather than merely about this file.
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from career.engine import cli
from career.whatsapp import client as wa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_whatsapp_token as watch  # noqa: E402

NOW = datetime(2026, 8, 8, 5, 30, tzinfo=UTC)

#: The live answer, verbatim in shape (no value, no id, no number — §15.13).
LIVE_SCOPES = [
    "whatsapp_business_management",
    "whatsapp_business_messaging",
    "manage_app_solution",
    "whatsapp_business_manage_events",
    "public_profile",
]


def _debug(**overrides: Any) -> dict[str, Any]:
    data = {
        "type": "SYSTEM_USER",
        "application": "Career platform",
        "expires_at": 0,
        "data_access_expires_at": 0,
        "is_valid": True,
        "scopes": list(LIVE_SCOPES),
    }
    data.update(overrides)
    return {"data": data}


class _Graph:
    """A Meta that answers exactly what the test says and records the method.

    It has a ``post`` too, and it explodes: the one behavioural promise this
    whole feature makes to the operator is that a credential watchdog never
    writes anything, never bills anything and never puts a message in front of
    a customer. A fake that could not catch a POST would be proving the wrong
    property.
    """

    def __init__(
        self, debug: Any = None, *, debug_status: int = 200,
        number_status: int = 200, raises: int = 0,
    ) -> None:
        self._debug = _debug() if debug is None else debug
        self._debug_status = debug_status
        self._number_status = number_status
        self._raises = raises
        self.gets: list[str] = []

    def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, Any]:
        self.gets.append(url)
        if self._raises > 0:
            self._raises -= 1
            raise OSError("connection reset by peer")
        if "debug_token" in url:
            return self._debug_status, self._debug
        return self._number_status, ({"id": "x"} if self._number_status == 200
                                     else {"error": {"code": 100}})

    def post(self, *args: Any, **kwargs: Any) -> tuple[int, Any]:
        raise AssertionError("a credential watchdog must never POST to Meta")


class _Admin:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_admin(self, text: str) -> str:
        self.sent.append(text)
        return "ok"


def _inspect(graph: _Graph, *, now: datetime = NOW, **kwargs: Any) -> wa.TokenHealth:
    return wa.inspect_token(
        "EAA-not-a-real-token", "111222333", now=now, transport=graph, **kwargs
    )


# ── the fact, pinned ─────────────────────────────────────────────────────────


def test_the_configured_token_is_a_system_user_token_that_cannot_expire() -> None:
    """What `/debug_token` really said on 2026-08-08, as a regression guard.

    If this ever stops describing the live credential the answer is NOT to
    edit the expectation — it is that the permanent token was replaced, which
    is the finding the whole feature exists to surface.
    """
    health = _inspect(_Graph())
    assert health.verdict is wa.TokenVerdict.PERMANENT
    assert health.token_type == "SYSTEM_USER"
    assert health.never_expires is True
    assert health.days_left is None, "a permanent token has no runway to count"
    assert health.data_access_never_expires is True
    assert health.missing_scopes == ()
    assert health.number_addressable is True
    assert wa.token_problems(health) == [], "a healthy credential says nothing"


def _code_without_prose(path: Path) -> str:
    """The module's CODE, with every docstring and comment removed.

    The guard below is «this file does not renew anything», and the file
    explains at length WHY it does not — naming `fb_exchange_token` and Meta's
    endpoints in prose to say they are deliberately unused. A text search would
    read the explanation as the offence, and the fix for that would be to
    delete the explanation, which is the wrong direction entirely.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str) and len(body) > 1):
            del body[0]
    return ast.unparse(tree)


def test_nothing_in_this_repository_tries_to_renew_it() -> None:
    """The half of the brief that was «do not build it».

    A System User token has no `fb_exchange_token` path, so a refresher would
    be a timer that can only fail. The guard is on the shipped artefacts: no
    POST, no exchange endpoint, no token written anywhere.
    """
    code = _code_without_prose(Path(watch.__file__))
    assert "fb_exchange_token" not in code
    assert ".post(" not in code
    assert "graph.facebook.com" not in code, (
        "the watchdog names no endpoint of its own — it asks the send "
        "boundary, so an inspection can never drift to a different Graph "
        "version than the one that delivers"
    )
    unit = (Path(__file__).resolve().parents[1] / "ops" / "systemd"
            / "career-whatsapp-token.service").read_text(encoding="utf-8")
    assert "ExecStart=" in unit and "check_whatsapp_token.py" in unit


# ── the four ways it can still die ───────────────────────────────────────────


def test_a_revoked_token_is_caught_by_the_scheduled_check() -> None:
    """THE test. Before this feature existed nothing on any clock asked Meta
    anything, and a token revoked at 09:00 was first noticed by a customer.

    `is_valid: false` is a total outage in progress: no daily CV, no activation
    welcome, no renewal notice, for everybody, from that second.
    """
    health = _inspect(_Graph(_debug(is_valid=False)))
    assert health.verdict is wa.TokenVerdict.INVALID

    problems = wa.token_problems(health)
    assert [p.key for p in problems] == ["WHATSAPP_ACCESS_TOKEN"]
    assert "META REFUSES IT" in problems[0].english

    admin = _Admin()
    assert watch.run(health=health, admin=admin,
                     state=Path("/nonexistent/stamp")) == 2
    assert len(admin.sent) == 1


def test_a_dead_token_cannot_inspect_itself_and_the_refusal_is_the_answer() -> None:
    """Self-inspection has a property the app-token form does not: when Meta
    refuses to debug the token WITH that token, the refusal IS the verdict.

    And it must not be retried. A verdict repeated three times costs three
    round trips to learn what the first one said.
    """
    graph = _Graph({"error": {"code": 190, "type": "OAuthException"}},
                   debug_status=400)
    health = _inspect(graph)
    assert health.verdict is wa.TokenVerdict.INVALID
    assert health.error_code == 190
    assert len(graph.gets) == 1, "a verdict was retried as if it were weather"


def test_a_withdrawn_scope_is_invisible_to_is_valid_alone() -> None:
    """The token stays valid and every send starts coming back refused. A check
    that read only `is_valid` would certify this outage as healthy."""
    kept = [s for s in LIVE_SCOPES if s != "whatsapp_business_messaging"]
    health = _inspect(_Graph(_debug(scopes=kept)))
    assert health.verdict is wa.TokenVerdict.DEGRADED
    assert health.missing_scopes == ("whatsapp_business_messaging",)

    problems = wa.token_problems(health)
    assert "permission error, not a token error" in problems[0].english
    assert watch.reason_for(health) == watch.REASON_SCOPES


def test_an_unassigned_number_is_invisible_to_debug_token_entirely() -> None:
    """Unassign the phone number from the System User and the token is valid,
    scoped, and useless. `/debug_token` cannot see it — hence the second GET.

    The instruction differs from every other rung and that is the point: a new
    token would not help, and telling him to make one would waste the outage.
    """
    health = _inspect(_Graph(number_status=400))
    assert health.verdict is wa.TokenVerdict.DEGRADED
    assert health.number_addressable is False

    problems = wa.token_problems(health)
    assert [p.key for p in problems] == ["WHATSAPP_PHONE_NUMBER_ID"]
    assert "nothing about the token itself is wrong" in problems[0].english
    assert "رمزًا جديدًا" not in problems[0].arabic

    admin = _Admin()
    watch.run(health=health, admin=admin, state=Path("/nonexistent/stamp"))
    assert "لا داعي لإنشاء رمز جديد" in admin.sent[0]


def test_a_temporary_token_pasted_over_the_permanent_one_is_a_regression() -> None:
    """The realistic way this product goes dark on a clock.

    The permanent token cannot expire, so the countdown can only come back by
    somebody replacing it — which is exactly what was configured through July.
    Nothing in the environment file would say so; `expires_at` says so free.
    """
    expiry = int((NOW + timedelta(days=57)).timestamp())
    health = _inspect(_Graph(_debug(type="USER", expires_at=expiry)))
    assert health.verdict is wa.TokenVerdict.DATED
    assert health.never_expires is False
    assert health.days_left == 57

    problems = wa.token_problems(health)
    assert "TEMPORARY credential" in problems[0].english
    assert "57 days left" in problems[0].english
    # Said the day it appears, not the day it dies — and not a failed unit for
    # the fifty-seven days in between.
    assert watch.exit_code_for(watch.REASON_TEMPORARY, watch.TIER_MARGIN) == 0
    admin = _Admin()
    watch.run(health=health, admin=admin, state=Path("/nonexistent/stamp"))
    assert len(admin.sent) == 1


def test_a_never_expiring_token_with_a_data_access_clock_is_not_ours() -> None:
    """`expires_at: 0` WITH `data_access_expires_at` set is a long-lived USER
    token: it authenticates long after it may still read what it was granted,
    and the failure lands months later as refusals nobody connects to a
    credential. Ours reports 0 for both, so this is a swap, not a state."""
    health = _inspect(_Graph(_debug(
        type="USER", expires_at=0,
        data_access_expires_at=int((NOW + timedelta(days=80)).timestamp()),
    )))
    problems = wa.token_problems(health)
    assert "data-access clock that DOES run out" in problems[0].english


# ── the escalation, and the silence ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("days", "tier"),
    [(57, watch.TIER_MARGIN), (5, watch.TIER_WARN), (1, watch.TIER_URGENT),
     (-3, watch.TIER_DEAD)],
)
def test_the_countdown_escalates_rather_than_repeating(
    days: int, tier: str
) -> None:
    assert watch.tier_for(watch.REASON_TEMPORARY, days) == tier


def test_every_rung_says_something_the_last_one_did_not() -> None:
    """`refresh_salla_token`'s rule, and the reason for it: an alarm that fires
    flatly every day for weeks gets muted, and a muted channel is how nine days
    pass."""
    seen = set()
    for days in (57, 5, 1, -3):
        health = wa.TokenHealth(wa.TokenVerdict.DATED, token_type="USER",
                                days_left=days)
        text = watch.alert_text(
            watch.REASON_TEMPORARY,
            watch.tier_for(watch.REASON_TEMPORARY, days), health,
        )
        assert text not in seen, f"the {days}-day message repeats an earlier one"
        seen.add(text)


def test_it_does_not_say_the_same_thing_twice(tmp_path: Path) -> None:
    stamp = tmp_path / "last-alert"
    health = wa.TokenHealth(wa.TokenVerdict.INVALID)
    admin = _Admin()
    assert watch.announce(admin, reason=watch.REASON_INVALID, health=health,
                          state=stamp) is True
    assert watch.announce(admin, reason=watch.REASON_INVALID, health=health,
                          state=stamp) is False
    assert len(admin.sent) == 1
    # …and a recovery re-arms it, or the next outage is silent.
    watch.run(health=wa.TokenHealth(wa.TokenVerdict.PERMANENT,
                                    never_expires=True), admin=admin,
              state=stamp)
    assert watch.announce(admin, reason=watch.REASON_INVALID, health=health,
                          state=stamp) is True


def test_an_unreachable_meta_is_a_transient_exit_and_never_an_alarm() -> None:
    """«We could not reach Meta» says nothing about the credential. Alarming on
    it would put Meta's uptime on his phone at night with no action attached —
    and the state that DOES need him becomes indistinguishable from noise.

    The fact still reaches him: a non-zero exit, and the unit's OnFailure=.
    """
    graph = _Graph(raises=3)
    health = _inspect(graph)
    assert health.verdict is wa.TokenVerdict.UNREADABLE
    assert health.readable is False
    assert wa.token_problems(health) == []

    admin = _Admin()
    assert watch.run(health=health, admin=admin,
                     state=Path("/nonexistent/stamp")) == 1
    assert admin.sent == []


def test_a_blip_is_retried_because_a_read_only_get_rotates_nothing() -> None:
    """Where `scripts/refresh_salla_token.py` must never repeat its call —
    Salla's refresh tokens are single-use and a second exchange revokes the
    installation — this one may, and does."""
    graph = _Graph(raises=2)
    assert _inspect(graph).verdict is wa.TokenVerdict.PERMANENT
    assert len(graph.gets) == 4  # two failures, the debug call, the number


def test_a_healthy_credential_is_completely_silent() -> None:
    admin = _Admin()
    health = _inspect(_Graph())
    assert watch.run(health=health, admin=admin,
                     state=Path("/nonexistent/stamp")) == 0
    assert admin.sent == []


# ── the promises ────────────────────────────────────────────────────────────


def test_the_watchdog_never_speaks_to_a_customer() -> None:
    """Two GETs and nothing else. `_Graph.post` raises, so a send anywhere on
    this path fails the test rather than reaching a phone."""
    graph = _Graph()
    _inspect(graph)
    assert len(graph.gets) == 2
    assert all("debug_token" in u or u.endswith("?fields=id") for u in graph.gets)


def test_no_credential_can_reach_the_journal_or_the_phone() -> None:
    """§15.13. Enforced by the OBJECT and not by the call sites: `TokenHealth`
    holds no token, no id and no phone number, so a traceback frame and an
    assertion diff are safe by construction."""
    health = _inspect(_Graph())
    rendered = "\n".join([
        repr(health),
        *(p.english + p.arabic for p in wa.token_problems(health)),
        watch.alert_text(watch.REASON_INVALID, watch.TIER_URGENT, health),
    ])
    assert "EAA" not in rendered
    assert "111222333" not in rendered


ARABIC = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z0-9]")


def test_every_operator_line_is_direction_pure() -> None:
    """Fahad's client reverses any line mixing Arabic with Latin letters or
    Latin digits, and these are read on a phone while something is wrong.
    Numbers in an Arabic line go through `ar_num`."""
    healths = [
        wa.TokenHealth(wa.TokenVerdict.INVALID),
        wa.TokenHealth(wa.TokenVerdict.DEGRADED,
                       missing_scopes=("whatsapp_business_messaging",)),
        wa.TokenHealth(wa.TokenVerdict.DEGRADED, number_addressable=False),
        wa.TokenHealth(wa.TokenVerdict.DATED, days_left=-3),
        wa.TokenHealth(wa.TokenVerdict.DATED, days_left=57),
    ]
    lines: list[str] = []
    for health in healths:
        lines.extend(p.arabic for p in wa.token_problems(health))
        reason = watch.reason_for(health)
        if reason not in (watch.REASON_OK, watch.REASON_UNREADABLE):
            lines.extend(watch.alert_text(
                reason, watch.tier_for(reason, health.days_left), health,
            ).splitlines())
    lines.extend(watch.alert_text(
        watch.REASON_UNCONFIGURED, watch.TIER_URGENT,
        wa.TokenHealth(wa.TokenVerdict.UNCONFIGURED),
    ).splitlines())
    assert lines
    for line in lines:
        if ARABIC.search(line):
            assert not LATIN.search(line), f"mixed direction: {line!r}"


def test_the_arabic_numbers_are_arabic() -> None:
    assert watch.ar_num(57) == "٥٧"
    assert watch.ar_num(-3) == "٣", "a Latin minus reverses the line too"


# ── the boot check, and the boot it must never block ────────────────────────


def test_the_boot_check_reports_the_live_verdict() -> None:
    problems = cli.whatsapp_token_problems(
        _inspect(_Graph(_debug(is_valid=False)))
    )
    assert [p.key for p in problems] == ["WHATSAPP_ACCESS_TOKEN"]
    assert isinstance(problems[0], cli.EnvProblem)


def test_the_boot_check_is_silent_when_nobody_asked_meta() -> None:
    """`None` means the caller chose not to spend a network call, and
    UNREADABLE means Meta could not be reached. Neither is a finding."""
    assert cli.whatsapp_token_problems(None) == []
    assert cli.whatsapp_token_problems(
        wa.TokenHealth(wa.TokenVerdict.UNREADABLE)
    ) == []


def test_the_worker_boot_check_makes_no_network_call(monkeypatch) -> None:
    """The conversation worker runs under Restart=always/RestartSec=5. A live
    Graph call in its boot check would be twelve credential inspections a
    minute during a crash loop, on the process that serves people who have
    already paid — so `report_environment` never fetches by default."""
    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the boot check reached the network")

    monkeypatch.setattr(cli, "inspect_token", _explode)
    monkeypatch.setattr(cli, "approved_plan_prices", lambda session: {})
    monkeypatch.setattr(cli, "canonical_sale_plans", lambda: {})
    cli.report_environment(settings=_settings(), session=None,
                           admin_client=_Admin())


def test_the_live_reader_never_runs_inside_a_test_process() -> None:
    """Same rule and same reason as `read_token_life`: a unit test must never
    reach the operator's live credential, and a suite whose colour depends on
    Meta's uptime proves nothing about either."""
    assert cli.read_token_health(_settings()).verdict is wa.TokenVerdict.UNREADABLE


def _settings() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        salla_product_catalog="{}", salla_product_pricing="{}",
        salla_token_expires_at="2026-12-31",
        salla_store_url="https://store.example/x",
        whatsapp_access_token="EAA-not-a-real-token",
        whatsapp_phone_number_id="111222333",
    )
