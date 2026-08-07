"""OPS watchdogs (Fahad's calls): SearchAPI credit alerts + the canary
evening window nudge. Both pure — deterministic clocks, zero network."""

from __future__ import annotations

import ast
import http.server
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from career import notify as sd
from career.engine.quota import quota_alert
from career.whatsapp.window import window_reminder_due

RIYADH = ZoneInfo("Asia/Riyadh")


# ── quota_alert ──────────────────────────────────────────────────────────────


def test_trial_all_zero_stays_quiet() -> None:
    assert quota_alert({"monthly_allowance": 0, "remaining_credits": 0}) is None


def test_healthy_paid_plan_stays_quiet() -> None:
    assert quota_alert(
        {"monthly_allowance": 10_000, "remaining_credits": 8_000}
    ) is None


def test_low_credits_warns_with_numbers() -> None:
    alert = quota_alert({"monthly_allowance": 10_000, "remaining_credits": 900,
                         "current_month_usage": 9_100})
    assert alert is not None
    # The guarantee is unchanged — the alert still names BOTH real numbers —
    # but it names them in Arabic-Indic digits: Latin digits inside the Arabic
    # sentence reversed the whole line in Fahad's client
    # (tests/test_alert_direction_purity.py).
    assert "٩٠٠" in alert and "١٠٠٠٠" in alert
    # …and the top-up URL is reachable because it stands on its own line
    assert "searchapi.io/pricing" in alert.split("\n")


def test_small_plan_uses_the_floor() -> None:
    # 10% of 500 is 50 < floor(100) — 80 remaining must still warn
    assert quota_alert(
        {"monthly_allowance": 500, "remaining_credits": 80,
         "current_month_usage": 420}
    ) is not None


def test_exhausted_credits_alert_red() -> None:
    alert = quota_alert({"monthly_allowance": 5_000, "remaining_credits": 0,
                         "current_month_usage": 5_000})
    assert alert is not None
    assert "🔴" in alert


def test_garbage_fields_never_raise() -> None:
    assert quota_alert({"monthly_allowance": "n/a", "remaining_credits": None}) is None
    assert quota_alert({}) is None


# ── window_reminder_due ──────────────────────────────────────────────────────

# Wed 2026-07-15 20:00 Riyadh — tomorrow (Thu) is a delivery day.
WED_EVENING = datetime(2026, 7, 15, 20, 0, tzinfo=RIYADH)


def test_nudges_when_window_will_be_closed_at_dawn() -> None:
    stale = WED_EVENING - timedelta(hours=20)   # closed by 04:30 (>24h)
    assert window_reminder_due(
        last_inbound_at=stale, opt_out_at=None, now=WED_EVENING
    )


def test_quiet_when_window_still_open_at_dawn() -> None:
    fresh = WED_EVENING - timedelta(hours=1)    # open until tomorrow 19:00
    assert not window_reminder_due(
        last_inbound_at=fresh, opt_out_at=None, now=WED_EVENING
    )


def test_quiet_outside_the_evening_hours() -> None:
    noon = WED_EVENING.replace(hour=12)
    assert not window_reminder_due(
        last_inbound_at=None, opt_out_at=None, now=noon
    )


def test_quiet_on_thursday_evening_weekend_ahead() -> None:
    thu_evening = WED_EVENING + timedelta(days=1)   # tomorrow = Friday
    assert not window_reminder_due(
        last_inbound_at=None, opt_out_at=None, now=thu_evening
    )


def test_saturday_evening_nudges_for_sunday() -> None:
    sat_evening = datetime(2026, 7, 18, 21, 0, tzinfo=RIYADH)
    assert window_reminder_due(
        last_inbound_at=sat_evening - timedelta(days=2),
        opt_out_at=None, now=sat_evening,
    )


def test_opt_out_never_nudges() -> None:
    assert not window_reminder_due(
        last_inbound_at=None, opt_out_at=WED_EVENING, now=WED_EVENING
    )


def test_utc_clock_is_converted_to_riyadh() -> None:
    # 17:30 UTC == 20:30 Riyadh — inside the evening window
    utc_evening = datetime(2026, 7, 15, 17, 30, tzinfo=UTC)
    assert window_reminder_due(
        last_inbound_at=None, opt_out_at=None, now=utc_evening
    )


def test_paid_plan_quirk_zero_credits_field_stays_quiet() -> None:
    # live-probed: paid plan reports remaining_credits=0 while the monthly
    # allowance is untouched — must NOT fire the red alert
    assert quota_alert({
        "monthly_allowance": 10_000, "remaining_credits": 0,
        "current_month_usage": 0,
    }) is None


def test_paid_plan_quirk_still_warns_when_usage_nears_allowance() -> None:
    alert = quota_alert({
        "monthly_allowance": 10_000, "remaining_credits": 0,
        "current_month_usage": 9_950,
    })
    # ٥٠ = the derived headroom (allowance − usage), in Arabic-Indic digits
    assert alert is not None and "٥٠" in alert


# ── the wedge: heartbeat, watchdog, and the operator message ─────────────────
#
# Everything above this line tests a pure function. Everything below tests the
# WIRING that turns a wedged process into a message on Fahad's phone, because
# the wedge is not a bug in a function — it is a loop that survives everything
# by design, a unit that reports `active` because the process is alive, and an
# alert path nobody ever exercised. Three of those four had a test. The gap
# cost nine hours of silence on 2026-08-06.

REPO = Path(__file__).resolve().parents[1]
UNITS = REPO / "ops" / "systemd"
SCRIPTS = REPO / "scripts"

#: unit file → the always-on loop it runs. Both must gain the heartbeat.
LOOP_UNITS = {
    "career-worker.service": "run_worker_loop.py",
    "career-admin-bot.service": "run_admin_bot.py",
}


def _directives(unit: str) -> dict[str, str]:
    """Unit file → its [Service]/[Unit] directives, comments stripped."""
    out: dict[str, str] = {}
    for line in (UNITS / unit).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _seconds(spec: str) -> float:
    """systemd time span → seconds ("300", "5min", "2min 30s")."""
    if spec.isdigit():
        return float(spec)
    total = 0.0
    for number, unit in re.findall(r"(\d+)\s*(us|ms|s|min|m|h|d)?", spec):
        if not number:
            continue
        total += int(number) * {
            "us": 1e-6, "ms": 1e-3, "s": 1.0, "": 1.0,
            "min": 60.0, "m": 60.0, "h": 3600.0, "d": 86400.0,
        }[unit]
    return total


class TestHeartbeatWiring:
    """`Type=notify` without a `READY=1` is a worker systemd KILLS at start.

    That is the whole ordering constraint, and it is the kind of thing that
    survives a review and dies at deploy time, so it is asserted here in both
    directions: no unit may ask for a protocol its script does not speak.
    """

    def test_loop_units_declare_notify_and_a_watchdog(self) -> None:
        for unit in LOOP_UNITS:
            directives = _directives(unit)
            assert directives.get("Type") == "notify", unit
            assert _seconds(directives["WatchdogSec"]) > 0, unit

    def test_no_unit_asks_for_notify_without_a_script_that_answers(self) -> None:
        for unit_file in sorted(UNITS.glob("*.service")):
            directives = _directives(unit_file.name)
            if directives.get("Type") != "notify":
                continue
            exec_start = directives["ExecStart"]
            scripts = [Path(tok) for tok in exec_start.split() if tok.endswith(".py")]
            assert scripts, f"{unit_file.name}: Type=notify but no script to check"
            for script in scripts:
                source = (REPO / script.relative_to("/root/career")).read_text()
                assert "sd_notify.ready()" in source, f"{unit_file.name} → {script}"

    def test_both_loops_import_the_stdlib_writer(self) -> None:
        # A new dependency is a new way for the worker to fail to start, and
        # this control must never be a reason the worker is down.
        for script in LOOP_UNITS.values():
            source = (SCRIPTS / script).read_text()
            assert "from career import notify as sd_notify" in source
            assert "systemd.daemon" not in source

    def test_ready_is_sent_once_and_only_after_the_boot_checks(self) -> None:
        source = (SCRIPTS / "run_worker_loop.py").read_text()
        assert source.count("sd_notify.ready()") == 1
        # report_environment and the scanner probe are the boot checks; READY=1
        # must follow them, or systemd calls the unit «started» before the
        # selling environment and the malware scanner have been looked at.
        assert source.index("report_environment(") < source.index("sd_notify.ready()")
        assert source.index("scanner_health(") < source.index("sd_notify.ready()")


def _while_true_bodies(source: str) -> list[list[ast.stmt]]:
    return [
        node.body for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.While)
        and isinstance(node.test, ast.Constant) and node.test.value is True
    ]


def _is_watchdog_call(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "watchdog"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "sd_notify"
    )


class TestTheDogIsPettedOnlyByWORK:
    """The one detail the whole mechanism rests on.

    A `WATCHDOG=1` in a `finally`, or in the `except` that keeps these loops
    alive, would have systemd certify BOTH real incidents as healthy: on
    2026-08-04 the cycle raised for 100 seconds against a restarting Postgres,
    and since 2026-08-06 06:58 every cycle raises on a column the database does
    not have. In each case the loop kept looping — so «the loop is running» is
    worth nothing, and only «the cycle COMPLETED» is worth a heartbeat.
    """

    def test_watchdog_is_the_last_statement_of_the_cycle(self) -> None:
        for script in LOOP_UNITS.values():
            bodies = _while_true_bodies((SCRIPTS / script).read_text())
            assert len(bodies) == 1, script
            tries = [n for n in bodies[0] if isinstance(n, ast.Try)]
            assert len(tries) == 1, script
            assert _is_watchdog_call(tries[0].body[-1]), (
                f"{script}: the cycle must end by petting the dog"
            )

    def test_watchdog_appears_in_no_finally_and_no_handler(self) -> None:
        for script in LOOP_UNITS.values():
            tree = ast.parse((SCRIPTS / script).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Try):
                    for stmt in node.finalbody:
                        assert not _is_watchdog_call(stmt), script
                    for handler in node.handlers:
                        for stmt in ast.walk(handler):
                            assert not _is_watchdog_call(stmt), script

    def test_a_failing_cycle_leaves_the_dog_hungry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule above, executed rather than parsed: the loops' own shape
        — work, then pet, all inside one `except Exception: continue` — must
        send one datagram for a cycle that completed and NOTHING for a cycle
        that raised. This is the 2026-08-04 and 2026-08-06 incidents in six
        lines."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(tmp_path / "notify.sock"))
        server.settimeout(0.2)
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify.sock"))

        def cycle(*, explode: bool) -> None:
            try:
                if explode:
                    raise RuntimeError("column webhook_events.next_attempt_at ...")
                sd.watchdog()
            except Exception:  # noqa: BLE001,S110 — mirrors the loops' own guard
                pass  # the loop survives; the dog stays hungry

        try:
            cycle(explode=False)
            assert server.recv(64) == b"WATCHDOG=1"
            cycle(explode=True)
            with pytest.raises(TimeoutError):
                server.recv(64)
        finally:
            server.close()


class TestWatchdogDeadlines:
    """The deadline is derived from what these loops actually do, and the
    derivation is asserted against the constants they actually use — so that
    raising the Telegram long poll, or the worker's poll interval, cannot
    quietly turn a healthy cycle into a «wedge»."""

    def test_worker_deadline_clears_its_slowest_measured_cycle(self) -> None:
        watchdog_s = _seconds(_directives("career-worker.service")["WatchdogSec"])
        # Measured on this host: the slowest customer-message cycle in three
        # weeks of journal was 18s (2026-07-29T21:43:03→21:43:21), and the
        # longest stretch in which no cycle could legitimately succeed was the
        # 100s Postgres restart of 2026-08-04. Anything under that would
        # restart a healthy worker; the alerting cost of a false wedge is the
        # reason this is not simply «small».
        assert watchdog_s >= 3 * 100
        # …and it must still be a WATCHDOG, not a formality: a wedge has to be
        # caught inside the hour it starts, not the day.
        assert watchdog_s <= 600

    def test_bot_deadline_clears_its_own_long_poll(self) -> None:
        source = (SCRIPTS / "run_admin_bot.py").read_text()
        poll = int(re.search(r"^POLL_TIMEOUT = (\d+)", source, re.M).group(1))
        transport_ceiling = poll + 10   # client.get_updates: timeout + 10
        watchdog_s = _seconds(_directives("career-admin-bot.service")["WatchdogSec"])
        # A long poll that legitimately blocks for its full timeout must never
        # read as a wedge, and one cycle can also carry a health screen whose
        # live probes each have their own timeout (≈75s if every one of them
        # hangs) plus a 30s send.
        assert watchdog_s >= 4 * transport_ceiling
        assert watchdog_s >= transport_ceiling + 75 + 30

    def test_start_up_is_given_more_time_than_its_slowest_boot_check(self) -> None:
        # Type=notify makes TimeoutStartSec lethal: no READY=1 in time and the
        # unit is killed at boot. The worker's boot check talks to Postgres and
        # sends a Telegram heartbeat (30s client timeout) before it can report.
        for unit in LOOP_UNITS:
            assert _seconds(_directives(unit)["TimeoutStartSec"]) >= 120


class TestWedgeEscalation:
    """A watchdog that restarts a wedged worker forever, quietly, is the
    incident again with extra steps."""

    def test_watchdog_restarts_cannot_reach_the_start_limit(self) -> None:
        """Not a wish — arithmetic, asserted so the comment cannot drift.

        `career-worker.service` claims a persistent wedge «escalates to the
        alert on its own» through the start rate limit. It cannot: one
        watchdog restart takes WatchdogSec+RestartSec, and 20 of those do not
        fit in a 600s window. This test states the true shape, and the units
        carry ExecStopPost= because of it.
        """
        for unit in LOOP_UNITS:
            directives = _directives(unit)
            cycle = (_seconds(directives["WatchdogSec"])
                     + _seconds(directives["RestartSec"]))
            burst = int(directives["StartLimitBurst"])
            window = _seconds(directives["StartLimitIntervalSec"])
            assert cycle * (burst - 1) > window, (
                f"{unit}: if watchdog restarts CAN exhaust the start limit, "
                "the ExecStopPost alert hook is redundant — reread both"
            )

    def test_both_loops_alert_on_the_watchdog_verdict_directly(self) -> None:
        for unit in LOOP_UNITS:
            hook = _directives(unit)["ExecStopPost"]
            assert hook.startswith("-"), f"{unit}: alerting must never fail the unit"
            assert "alert_unit_failure.sh" in hook

    def test_a_hung_START_is_as_silent_as_a_wedge_and_must_alert_too(self) -> None:
        """`Type=notify` bought the wedge alert and sold a NEW silent loop.

        Under `Type=simple` a start-up that hangs is not a failure at all —
        systemd calls the unit started the moment it forks. `Type=notify`
        makes `TimeoutStartSec=` lethal: a boot check that blocks (the session
        `report_environment` opens carries no `connect_timeout`, so a Postgres
        that accepts the TCP connection and never answers hangs there forever)
        is killed at 180s with `SERVICE_RESULT=timeout`.

        Then the exact arithmetic that forced ExecStopPost= into these units
        applies again, unchanged: one timeout-and-restart costs
        TimeoutStartSec+RestartSec, and twenty of those do not fit in the 600s
        start-limit window either — so the unit never reaches `failed`,
        `OnFailure=` never fires, and the ExecStopPost hook is silent for every
        verdict except `watchdog`. A worker that can never finish starting
        would restart quietly every three minutes forever, which is the
        original incident with a different verdict string.
        """
        for unit in LOOP_UNITS:
            directives = _directives(unit)
            cycle = (_seconds(directives["TimeoutStartSec"])
                     + _seconds(directives["RestartSec"]))
            burst = int(directives["StartLimitBurst"])
            window = _seconds(directives["StartLimitIntervalSec"])
            assert cycle * (burst - 1) > window, (
                f"{unit}: a hung start CAN exhaust the start limit, so "
                "OnFailure= covers it — reread this test"
            )


# ── the operator's message ───────────────────────────────────────────────────


class _Sink(http.server.BaseHTTPRequestHandler):
    """Stands in for api.telegram.org. Records what the script really sent."""

    posts: list[dict[str, str]] = []
    status: int = 200

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        _Sink.posts.append({
            "path": self.path,
            **{k: v[0] for k, v in urllib.parse.parse_qs(body).items()},
        })
        self.send_response(_Sink.status)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self) -> None:  # noqa: N802 — the health probe in post-boot
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture()
def sink(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """A local Telegram, plus a fake env file — so proving the alert path can
    never involve reading (or printing) the real bot token."""
    _Sink.posts = []
    _Sink.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_file = tmp_path / "env"
    env_file.write_text(
        # Shaped like the real thing, including the JSON value that once broke
        # this script when it was still `source`d.
        'TELEGRAM_ADMIN_BOT_TOKEN=123456789:FAKEfake0000000000000000000000\n'
        'TELEGRAM_ADMIN_CHAT_ID=99999999\n'
        'SALLA_PRODUCT_CATALOG={"basic": "1", "pro": "2"}\n',
        encoding="utf-8",
    )
    host, port = server.server_address[0], server.server_address[1]
    yield {
        "env": {
            "CAREER_ENV_FILE": str(env_file),
            "CAREER_TELEGRAM_API": f"http://{host}:{port}",
            # Per-test de-duplication state. Without this every test in this
            # file would write to the real /run/career-alerts and the SECOND
            # run of the suite would be silently suppressed — a test that
            # passes once and then stops proving anything.
            "CAREER_ALERT_STATE_DIR": str(tmp_path / "alerts"),
            "CAREER_POST_BOOT_SETTLE": "0",
            "CAREER_POST_BOOT_HEALTH_URL": f"http://{host}:{port}/health",
        },
        "posts": _Sink.posts,
        "server": server,
    }
    server.shutdown()


def _run_script(name: str, sink: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **sink["env"]}
    return subprocess.run(  # noqa: S603 — fixed argv, our own ops scripts
        [str(SCRIPTS / name), *args], capture_output=True, text=True,
        env=env, timeout=180, check=False,
    )


class TestAlertMessage:
    """«خدمة توقفت ولم تعد تشتغل» was sent for a nightly job that failed one
    check (2026-08-06 10:02, career-engine-nightly), and would be sent for a
    weekly probe that came back red. It is now the message for the case it
    actually describes, and the other cases have their own."""

    def test_a_wedge_says_it_stopped_RESPONDING(self, sink: dict[str, Any]) -> None:
        # systemd sets SERVICE_RESULT for ExecStopPost= — this is the exact
        # environment a watchdog kill produces.
        env = {**os.environ, **sink["env"], "SERVICE_RESULT": "watchdog"}
        done = subprocess.run(  # noqa: S603 — fixed argv, our own ops script
            [str(SCRIPTS / "alert_unit_failure.sh"), "career-worker.service"],
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        text = sink["posts"][-1]["text"]
        assert "لم تعد ترد" in text
        assert "career-worker.service" in text

    def test_an_ordinary_restart_stays_silent(self, sink: dict[str, Any]) -> None:
        """ExecStopPost= fires on EVERY stop, including a deploy's own
        `systemctl restart`. Only the watchdog verdict is worth a phone."""
        for result in ("success", "exit-code", ""):
            env = {**os.environ, **sink["env"], "SERVICE_RESULT": result}
            done = subprocess.run(  # noqa: S603 — fixed argv, our own script
                [str(SCRIPTS / "alert_unit_failure.sh"), "career-worker.service"],
                capture_output=True, text=True, env=env, timeout=60, check=False,
            )
            assert done.returncode == 0
        assert sink["posts"] == []

    def test_a_start_that_never_finishes_also_reaches_the_phone(
        self, sink: dict[str, Any]
    ) -> None:
        """The other verdict no unit can escalate on its own.

        `exit-code` is rightly silent here: a fast crash restarts every ~5s,
        trips the 20-in-600s start limit in about a hundred seconds, and the
        `OnFailure=` path sends «استنفدت محاولات التشغيل». `timeout` cannot do
        that — TimeoutStartSec=180 plus RestartSec=5 fits three times in the
        window, never twenty (see
        TestWedgeEscalation.test_a_hung_START_is_as_silent_as_a_wedge_and_must_alert_too).
        So it has to be told here or it is never told at all, and it must say
        the thing that actually happened: the service did not finish starting,
        which sends the operator to a different half of the journal than a
        cycle that stopped completing.
        """
        env = {**os.environ, **sink["env"], "SERVICE_RESULT": "timeout"}
        done = subprocess.run(  # noqa: S603 — fixed argv, our own ops script
            [str(SCRIPTS / "alert_unit_failure.sh"), "career-worker.service"],
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        assert sink["posts"], (
            "a start-up that hung was killed by systemd and nobody was told — "
            "the unit will keep restarting silently forever"
        )
        text = sink["posts"][-1]["text"]
        assert "لم تكمل الإقلاع" in text, text
        assert "career-worker.service" in text
        # …and it must NOT be confused with the wedge: they send the operator
        # to different evidence.
        assert "لم تعد ترد" not in text, text

    def test_the_onfailure_path_still_alerts(self, sink: dict[str, Any]) -> None:
        """No SERVICE_RESULT in the environment means «asked by OnFailure=»,
        not «stopped cleanly» — the distinction is unset-versus-empty, and
        getting it backwards would silence every alert this host can raise."""
        env = {k: v for k, v in os.environ.items() if k != "SERVICE_RESULT"}
        done = subprocess.run(  # noqa: S603 — fixed argv, our own script
            [str(SCRIPTS / "alert_unit_failure.sh"), "career-worker.service"],
            capture_output=True, text=True,
            env={**env, **sink["env"]}, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "career-worker.service" in sink["posts"][-1]["text"]

    @pytest.mark.parametrize(
        ("result", "unit_type", "expected", "unexpected"),
        [
            # The nightly delivery run that failed one step on 2026-08-06 at
            # 10:02 and was announced as a service that «stopped and is no
            # longer running». It had not stopped.
            ("exit-code", "oneshot", "مهمة مجدولة فشلت", "توقفت"),
            # An always-on unit that really did give up: systemd has stopped
            # restarting it and recovery is a command, so send the command.
            ("start-limit-hit", "notify", "استنفدت محاولات التشغيل",
             "مهمة مجدولة"),
            ("exit-code", "notify", "خدمة توقفت ولم تعد تشتغل", "مهمة"),
            # Nothing known about the unit — say so rather than guess.
            ("", "", "أبلغت عن فشل", "توقفت ولم تعد"),
        ],
    )
    def test_the_headline_follows_what_systemd_reports(
        self, sink: dict[str, Any], tmp_path: Path,
        result: str, unit_type: str, expected: str, unexpected: str,
    ) -> None:
        """The OnFailure path, driven through a stub `systemctl` so every
        branch is proven on any host — including a CI container that has no
        systemd at all."""
        shim = tmp_path / "bin"
        shim.mkdir()
        (shim / "systemctl").write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            f'  *"-p Result"*) printf "%s" "{result}" ;;\n'
            f'  *"-p Type"*) printf "%s" "{unit_type}" ;;\n'
            "esac\n"
        )
        (shim / "systemctl").chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "SERVICE_RESULT"}
        env["PATH"] = f"{shim}:{env['PATH']}"
        done = subprocess.run(  # noqa: S603 — fixed argv, our own script
            [str(SCRIPTS / "alert_unit_failure.sh"), "career-unit.service"],
            capture_output=True, text=True,
            env={**env, **sink["env"]}, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        text = sink["posts"][-1]["text"]
        assert expected in text, text
        assert unexpected not in text, text
        if result == "start-limit-hit":
            # Recovery is manual by design (career-worker.service) — the
            # message has to carry the two commands, on their own line.
            assert "systemctl reset-failed career-unit.service" in text

    def test_arabic_lines_stay_direction_pure(self, sink: dict[str, Any]) -> None:
        env = {**os.environ, **sink["env"], "SERVICE_RESULT": "watchdog"}
        subprocess.run(  # noqa: S603 — fixed argv, our own script
            [str(SCRIPTS / "alert_unit_failure.sh"), "career-verify-restore.service"],
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )
        arabic = re.compile(r"[؀-ۿ]")
        latin = re.compile(r"[A-Za-z0-9]")
        for line in sink["posts"][-1]["text"].splitlines():
            assert not (arabic.search(line) and latin.search(line)), line

    def test_the_token_never_reaches_the_command_line(self) -> None:
        """/proc/<pid>/cmdline is world-readable and `ps` prints it. Both
        scripts used to pass https://…/bot<TOKEN>/sendMessage as a curl
        ARGUMENT, publishing the bot token to anyone with a shell on the host
        for as long as the request lasted. It rides in on stdin now."""
        for name in ("alert_unit_failure.sh", "post_boot_report.sh"):
            source = (SCRIPTS / name).read_text()
            assert "-K -" in source, name
            invocations = [
                line for line in source.splitlines()
                if "curl " in line and not line.lstrip().startswith("#")
            ]
            assert invocations, name
            for line in invocations:
                assert "TOKEN" not in line, f"{name}: {line.strip()}"

    def test_the_two_scripts_share_one_sender_verbatim(self) -> None:
        """Two copies of a subtle escaping routine is one copy that rots."""
        def sender(name: str) -> str:
            source = (SCRIPTS / name).read_text()
            start = source.index("send_telegram() {")
            return source[start:source.index("\n}\n", start)]
        assert sender("alert_unit_failure.sh") == sender("post_boot_report.sh")


class TestPostBootReport:
    """This script has NEVER executed: `journalctl -u career-post-boot` is
    empty for the whole life of the journal, because the unit was installed
    after the last boot. Proving it works therefore cannot mean rebooting a
    server that is serving a customer — so the settle time, the health URL and
    the Telegram endpoint are all overridable, and the proof is this test."""

    def test_it_reports_and_says_so(self, sink: dict[str, Any]) -> None:
        done = _run_script("post_boot_report.sh", sink)
        assert done.returncode == 0, done.stderr
        text = sink["posts"][-1]["text"]
        assert "السيرفر رجع بعد إعادة التشغيل" in text
        assert "career-worker" in text

    def test_a_telegram_failure_is_a_UNIT_failure(self, sink: dict[str, Any]) -> None:
        """It used to `curl ... >/dev/null 2>&1` and exit 0 whatever happened:
        a report nobody received looked exactly like a report delivered, so
        OnFailure= could never fire for the one thing this unit exists to do."""
        _Sink.status = 500
        done = _run_script("post_boot_report.sh", sink)
        assert done.returncode != 0
        assert "telegram" in done.stderr.lower()

    def test_missing_credentials_are_a_UNIT_failure(self, sink: dict[str, Any]) -> None:
        env = dict(sink["env"])
        env["CAREER_ENV_FILE"] = "/nonexistent/env"
        done = subprocess.run(  # noqa: S603 — fixed argv, our own script
            [str(SCRIPTS / "post_boot_report.sh")], capture_output=True, text=True,
            env={**os.environ, **env}, timeout=180, check=False,
        )
        assert done.returncode != 0

    def test_arabic_lines_stay_direction_pure(self, sink: dict[str, Any]) -> None:
        _run_script("post_boot_report.sh", sink)
        arabic = re.compile(r"[؀-ۿ]")
        latin = re.compile(r"[A-Za-z0-9]")
        for line in sink["posts"][-1]["text"].splitlines():
            assert not (arabic.search(line) and latin.search(line)), line


class TestShellcheckStillPasses:
    def test_ops_scripts_are_clean(self) -> None:
        binary = shutil.which("shellcheck")
        if binary is None:
            # CI installs it; a local run without it once let ten days of red
            # CI go unnoticed (scripts/gate.sh).
            pytest.skip("shellcheck not installed")
        done = subprocess.run(  # noqa: S603 — resolved path, fixed argv
            [binary, str(SCRIPTS / "alert_unit_failure.sh"),
             str(SCRIPTS / "post_boot_report.sh")],
            capture_output=True, text=True, check=False,
        )
        assert done.returncode == 0, done.stdout


# ── what IS, not what SHOULD be ──────────────────────────────────────────────
#
# Everything above this line reads `ops/systemd/` out of the REPOSITORY. On
# 2026-08-06 that was enough to certify a correct `career-worker.service` —
# Type=notify, WatchdogSec=300, ExecStopPost= — with 44 green tests, while
# /etc/systemd/system held the copy from 2026-07-31 and the host ran a nine-hour
# wedge with `systemctl` reporting active / Result=success / NRestarts=0 and no
# alert anywhere. Not one assertion in this file could have been red. A suite
# that reads what SHOULD be true will certify a correct file forever while the
# machine runs the old one.
#
# So this section reads the machine.

#: Where a deploy puts unit files. Overridable ONLY so the drift check can
#: itself be proven against a synthetic good host and a synthetic bad one —
#: which is impossible on a single real machine, and a drift check nobody
#: proved is how this file got into trouble in the first place.
ETC_DEFAULT = Path("/etc/systemd/system")
#: systemd's own marker that it is PID 1 here. `which systemctl` is not enough:
#: the binary exists in plenty of containers where nothing is running.
RUN_MARKER_DEFAULT = Path("/run/systemd/system")

#: Seconds a unit file may sit on disk with systemd not yet reloaded before
#: «a deploy is in progress» stops being a believable explanation. A deploy is
#: `cp` then `daemon-reload` then `restart`, seconds apart; fifteen minutes is
#: two orders of magnitude of slack. Past it, «not reloaded» is not a deploy in
#: flight, it is a deploy that stopped halfway — which is EXACTLY the shape of
#: the 2026-08-06 incident and must be red.
DEPLOY_GRACE_S = 900

#: live property ← repo directive, with the comparison each one needs.
#: `None` for the default means «ask the manager», because DefaultTimeoutStartSec
#: and friends are configurable and hard-coding 90s here would invent drift on
#: a host that configured them otherwise.
SERVICE_PROPERTIES: tuple[tuple[str, str, str | None, str], ...] = (
    ("Type", "Type", "simple", "text"),
    ("Restart", "Restart", "no", "text"),
    ("WatchdogUSec", "WatchdogSec", "0", "watchdog"),
    ("RestartUSec", "RestartSec", None, "time"),
    # Default resolved per unit — see _timeout_start_default().
    ("TimeoutStartUSec", "TimeoutStartSec", "", "time"),
    ("StartLimitIntervalUSec", "StartLimitIntervalSec", None, "time"),
    ("StartLimitBurst", "StartLimitBurst", None, "int"),
    ("OnFailure", "OnFailure", "", "units"),
    ("ExecStopPost", "ExecStopPost", "", "exec"),
)

_MANAGER_DEFAULT_OF = {
    "RestartUSec": "DefaultRestartUSec",
    "TimeoutStartUSec": "DefaultTimeoutStartUSec",
    "StartLimitIntervalUSec": "DefaultStartLimitIntervalUSec",
    "StartLimitBurst": "DefaultStartLimitBurst",
}

_EXEC_PREFIXES = "-@:+!"


def _show(systemctl: str, properties: list[str], unit: str | None = None) -> dict[str, str]:
    """`systemctl show` → dict. Properties systemd omits come back absent, not
    empty, so callers must use .get() with the documented default."""
    argv = [systemctl, "show"]
    if unit is not None:
        argv.append(unit)
    for prop in properties:
        argv += ["-p", prop]
    done = subprocess.run(  # noqa: S603 — resolved binary, fixed argv
        argv, capture_output=True, text=True, check=False, timeout=60,
    )
    out: dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            out[key] = value
    return out


def _expand(value: str, unit: str) -> str:
    """The specifiers these units actually use. `%n` is the full unit name —
    `OnFailure=career-alert@%n.service` is loaded as
    `career-alert@career-worker.service.service`, and comparing the raw strings
    would report drift on every correctly deployed host."""
    instance = unit.partition("@")[2].rpartition(".")[0]
    return (value.replace("%n", unit)
                 .replace("%N", unit.rpartition(".")[0])
                 .replace("%i", instance)
                 .replace("%%", "%"))


#: An Exec* command, reduced to the three things a unit file actually asks for:
#: the binary systemd will execute, the argument vector it will hand it, and
#: whether a non-zero exit is allowed to pass. Everything else systemd prints
#: alongside them is runtime state.
_ExecRecord = tuple[str, tuple[str, ...], bool]

#: The fields systemd interleaves into every Exec* record that describe what the
#: process DID on its last run, not what the unit file asked for. They move on
#: their own — a restart changes `pid`, a stop changes `status` — so a
#: comparison that reads them reports drift for a host nobody touched.
_EXEC_RUNTIME_FIELDS = frozenset({"start_time", "stop_time", "pid", "code", "status"})

#: systemd's spellings of «the leading `-`», by property variant and version:
#: the plain `ExecStopPost=` prints `ignore_errors=yes|no`, the structured
#: `ExecStopPostEx=` prints it inside `flags=` as `ignore-failure`. Both are
#: accepted, and `ignore_exit_status` with them, because THIS check was red for
#: a real deploy on the strength of guessing one spelling and meeting another —
#: see TestExecPropertiesAreComparedOnMeaning for the incident.
_EXEC_IGNORE_KEYS = ("ignore_errors", "ignore_exit_status")
_EXEC_IGNORE_FLAG = "ignore-failure"

#: One `{ … }` record. systemd concatenates several when a unit carries several
#: lines of the same directive, so this is findall, never search.
_EXEC_RECORD_RE = re.compile(r"\{(.*?)\}", re.S)


def _exec_words(value: str) -> tuple[str, ...]:
    """Split a command line the way systemd does for the cases these units use.

    `shlex` because systemd honours quotes in Exec lines; the fallback because a
    live argv[] is re-joined by systemd WITHOUT re-quoting, so a pathological
    argument can come back unbalanced and an exception here would read as a
    crash rather than as drift.
    """
    try:
        return tuple(shlex.split(value))
    except ValueError:
        return tuple(value.split())


def _exec_repo_record(spec: str) -> tuple[_ExecRecord, ...]:
    """A repo `ExecStopPost=` line → the canonical record(s).

    The leading `-` is not decoration: without it a failing alert script turns
    the unit itself into a failure, and both unit comments say so. It is carried
    as a BOOLEAN rather than as systemd's text for it, which is the whole point
    of this pair of functions — see _exec_live_records().
    """
    body = spec.strip()
    if not body:
        return ()
    ignore = False
    argv0_given = False
    while body and body[0] in _EXEC_PREFIXES:
        ignore = ignore or body[0] == "-"
        # `@` means the token after the path is argv[0] rather than an argument.
        argv0_given = argv0_given or body[0] == "@"
        body = body[1:]
    words = _exec_words(body)
    if not words:
        return ()
    path = words[0]
    return ((path, words[1:] if argv0_given else words, ignore),)


def _exec_live_records(rendered: str) -> tuple[_ExecRecord, ...]:
    """systemd's rendering of an Exec* property → the same canonical record(s).

    systemd prints a command as a structured record:

        { path=… ; argv[]=… ; ignore_errors=yes ; start_time=… ; pid=… ; … }

    Only the first three fields are configuration. Comparing the printed string
    against a unit-file line can never work — they are two spellings of one
    fact — so both sides are parsed to (path, argv, ignore) and the comparison
    happens there.
    """
    records: list[_ExecRecord] = []
    for body in _EXEC_RECORD_RE.findall(rendered):
        fields: dict[str, str] = {}
        for field in body.split(" ; "):
            key, sep, value = field.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        records.append((
            fields.get("path", ""),
            _exec_words(fields.get("argv[]", "")),
            _exec_ignores_errors(fields),
        ))
    return tuple(records)


def _exec_ignores_errors(fields: dict[str, str]) -> bool:
    for key in _EXEC_IGNORE_KEYS:
        if key in fields:
            return fields[key] == "yes"
    return _EXEC_IGNORE_FLAG in fields.get("flags", "").split()


def _exec_understood(rendered: str) -> bool:
    """Did we recognise systemd's spelling of the ignore flag at all?

    A rename in some future systemd would make every `-` look absent and turn
    this check red for a correct host — which is exactly the failure being fixed
    here. TestExecPropertiesAreComparedOnMeaning asserts this against the real
    machine so the next rename fails with its own name on it instead of
    masquerading as ExecStopPost drift.
    """
    return all(
        any(key in body for key in _EXEC_IGNORE_KEYS) or "flags=" in body
        for body in _EXEC_RECORD_RE.findall(rendered)
    )


def _render_exec(records: tuple[_ExecRecord, ...]) -> str:
    """A record back in the spelling a human would type into the unit file.

    The fix for real Exec drift is always an edit to a unit file, so that is the
    form the drift message prints — `-/path/script.sh career-worker.service`
    rather than the eighty characters of runtime state systemd wraps it in.
    Display only: the comparison is on the records themselves.
    """
    if not records:
        return "<unset>"
    out = []
    for path, argv, ignore in records:
        prefix = "-" if ignore else ""
        if argv[:1] == (path,):
            out.append(" ".join((prefix + path, *argv[1:])).strip())
        else:
            out.append(" ".join((prefix + "@" + path, *argv)).strip())
    return " ; ".join(out)


def _timespan(spec: str) -> float:
    return float("inf") if spec.strip() == "infinity" else _seconds(spec)


def _timeout_start_default(unit_type: str, manager_default: str) -> str:
    """`TimeoutStartSec=` does not default to DefaultTimeoutStartSec everywhere.

    systemd.service(5): for `Type=oneshot` it defaults to `infinity`, and this
    host confirms it — career-backup, career-engine-nightly and
    career-restore-test all report TimeoutStartUSec=infinity while carrying no
    TimeoutStartSec= of their own. Comparing them against 90s would have
    printed «drift» for three units that are deployed exactly as written, and a
    drift check that cries wolf about correct units is a drift check the next
    person deletes.
    """
    return "infinity" if unit_type == "oneshot" else manager_default


def _compare(kind: str, repo: str, live: str) -> bool:
    if kind == "watchdog":
        # systemd has TWO spellings for «no watchdog armed»: `0` and
        # `infinity`. Which one `show` prints is an artifact of its internal
        # state, not of the unit file — measured on this host 2026-08-06,
        # career-post-boot.service reports `infinity` while
        # career-restore-test.service reports `0`, with identical
        # (absent) WatchdogSec= and both never started. Collapsing them costs
        # nothing: a unit file asking for 300s still mismatches both.
        return _timespan(repo.replace("infinity", "0")) == _timespan(
            live.replace("infinity", "0")
        )
    if kind == "time":
        return _timespan(repo) == _timespan(live)
    if kind == "int":
        return int(repo) == int(live)
    if kind == "units":
        return set(repo.split()) == set(live.split())
    if kind == "exec":
        # Both sides parsed to (path, argv, ignore) — see _exec_live_records().
        # The tuples, not their rendering: a comparison on _render_exec() output
        # would be a comparison on a display string, which is the class of
        # mistake this branch exists to have stopped making.
        return _exec_repo_record(repo) == _exec_live_records(live)
    return repo == live


def _for_humans(kind: str, repo: str, live: str) -> tuple[str, str]:
    """The pair of values a drift message should print.

    Raw for everything whose printed form IS its meaning; normalised for the
    kinds where systemd's rendering buries the difference — an operator reading
    this at 03:00 needs to see which script and which flag, not `pid=0 ;
    code=(null) ; status=0/0`.
    """
    if kind == "exec":
        return _render_exec(_exec_repo_record(repo)), _render_exec(_exec_live_records(live))
    return repo, live


def live_unit_drift(
    *,
    units_dir: Path = UNITS,
    etc: Path = ETC_DEFAULT,
    systemctl: str = "systemctl",
    run_marker: Path = RUN_MARKER_DEFAULT,
    now: float | None = None,
) -> tuple[list[str], list[str], str | None]:
    """Compare the LIVE host against `ops/systemd/`.

    Returns ``(drift, tolerated, skip_reason)``.

    Three questions, deliberately kept apart, because conflating them is what
    makes a drift check either useless or permanently red:

      * is the file on disk the repository's file?  — «has the deploy landed»
      * are systemd's loaded properties that file?  — «is the host RUNNING it»
      * is this even a machine that deploys career? — «is there anything to ask»

    The middle one is the only one that can be transiently false for an honest
    reason: `cp` has run and `daemon-reload` has not. systemd answers that
    itself with NeedDaemonReload=yes, and the answer is only believed while the
    file is younger than DEPLOY_GRACE_S — otherwise «mid-deploy» becomes the
    permanent excuse that the 31-July units would have used for six days.
    """
    now = time.time() if now is None else now
    resolved = shutil.which(systemctl) or (systemctl if Path(systemctl).is_file() else None)
    if resolved is None:
        return [], [], "no systemctl on this host — nothing live to read"
    if not run_marker.is_dir():
        return [], [], f"{run_marker} absent — systemd is not running here"

    repo_units = sorted(p for p in units_dir.iterdir()
                        if p.suffix in {".service", ".timer"})
    if not any((etc / p.name).exists() for p in repo_units):
        # A developer laptop, or a CI runner that happens to have systemd. Not
        # a deploy host: there is no claim to check. A host that has SOME of
        # these units is a different matter entirely — see below, that is the
        # 2026-08-06 signature and it is drift, never a skip.
        return [], [], f"no career unit installed in {etc} — not a deploy host"

    defaults = _show(resolved, sorted(set(_MANAGER_DEFAULT_OF.values())))
    drift: list[str] = []
    tolerated: list[str] = []

    for unit_file in repo_units:
        name = unit_file.name
        installed = etc / name
        if not installed.exists():
            drift.append(
                f"{name}: in the repository but NOT installed on this host — "
                f"cp {unit_file} {installed} && systemctl daemon-reload"
            )
            continue

        repo_directives = _directives_of(unit_file)
        file_directives = _directives_of(installed)
        landed = True
        for key in sorted(set(repo_directives) | set(file_directives)):
            if repo_directives.get(key, "") != file_directives.get(key, ""):
                landed = False
                drift.append(
                    f"{name}: the INSTALLED FILE differs — {key}: "
                    f"repo={repo_directives.get(key, '<unset>')!r} "
                    f"installed={file_directives.get(key, '<unset>')!r} "
                    f"(the deploy never landed; diff {installed} {unit_file})"
                )
        if not landed or name.endswith("@.service"):
            # A template has no loaded state of its own — `systemctl show
            # career-alert@.service` refuses the name — so the file comparison
            # above is the whole check for it.
            continue
        if unit_file.suffix != ".service":
            continue

        live = _show(resolved, [p for p, *_ in SERVICE_PROPERTIES]
                     + ["NeedDaemonReload", "LoadState"], name)
        if live.get("LoadState") != "loaded":
            drift.append(
                f"{name}: installed but LoadState={live.get('LoadState', '?')!r} — "
                "systemd has never read this file"
            )
            continue

        unit_type = repo_directives.get("Type", "simple")
        differences: list[str] = []
        for prop, directive, fallback, kind in SERVICE_PROPERTIES:
            if fallback is None:
                fallback = defaults[_MANAGER_DEFAULT_OF[prop]]
            elif prop == "TimeoutStartUSec":
                fallback = _timeout_start_default(
                    unit_type, defaults[_MANAGER_DEFAULT_OF[prop]]
                )
            repo_value = _expand(repo_directives.get(directive, fallback), name)
            live_value = live.get(prop, "")
            if not _compare(kind, repo_value, live_value):
                shown_repo, shown_live = _for_humans(kind, repo_value, live_value)
                differences.append(
                    f"{prop}: repo={shown_repo!r} live={shown_live!r}"
                )
        if not differences:
            continue

        stale = live.get("NeedDaemonReload") == "yes"
        age = now - installed.stat().st_mtime
        if stale and age < DEPLOY_GRACE_S:
            tolerated.append(
                f"{name}: file landed {age:.0f}s ago and systemd has not reloaded "
                f"yet — a deploy in flight, not drift ({'; '.join(differences)})"
            )
            continue
        why = ("systemd wants a daemon-reload that never came"
               if stale else "systemd is loaded with something else")
        drift.append(f"{name}: {why} — " + "; ".join(differences))

    return drift, tolerated, None


def _directives_of(path: Path) -> dict[str, str]:
    """`_directives()` by path rather than by repo name — the same parse has to
    run against /etc/systemd/system, which is the whole point of this section."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


#: The exact opening of the failure a REQUIRED-but-unaskable host check
#: produces. scripts/gate.sh matches on it to tell «this host has drifted» from
#: «nothing about any host was verified» — two red results that need different
#: sentences from a human — and TestTheShipAndHostGatesCannotDrift asserts the
#: two files still agree on the wording.
REQUIRE_LIVE_FAILURE = "CAREER_REQUIRE_LIVE_UNITS is set but"


def _require_live() -> bool:
    """A skip that hides a real drift is exactly how nine hours went unnoticed.
    The deploy gate sets this — `scripts/gate.sh --host`, which
    `scripts/deploy_preflight.sh` requires — so on the machines that deploy,
    «skipped» is not an available answer."""
    return os.environ.get("CAREER_REQUIRE_LIVE_UNITS", "") not in ("", "0")


class TestTheLiveHostRunsWhatTheRepositorySays:
    """The check the 2026-08-06 suite did not have.

    Design decisions, stated because each of them is a way to reintroduce the
    incident:

    SKIP vs FAIL. Skipping is honest in exactly one situation: there is no
    systemd to ask, or this machine has never had a career unit installed. Any
    OTHER shape is a failure — in particular a host that runs SOME career units
    and is missing others, which is not «not a deploy host», it is a deploy
    that stopped halfway, and it is what career-verify-restore.service is doing
    right now. And because a skip can still hide something, CAREER_REQUIRE_LIVE_UNITS=1
    turns every skip in this class into a failure for the hosts that deploy.

    DEPLOYS IN FLIGHT. Between `cp` and `daemon-reload` the loaded properties
    are legitimately stale, and a check that goes red there teaches people to
    ignore it. systemd says so itself with NeedDaemonReload=yes, and that
    excuse is honoured only while the installed file is younger than
    DEPLOY_GRACE_S — otherwise «mid-deploy» is the alibi a six-day-old
    half-deploy would have used.

    WHICH PROPERTY. «drift» is not actionable at 03:00. Every message names the
    property, the repository's value and the machine's value, and the missing
    ones name the command that fixes them.

    WHICH GATE. This test answers a question about the MACHINE, so it carries
    `@pytest.mark.livehost` and its verdict is not the push-safety verdict:
    `scripts/gate.sh` runs it, prints its result in a section of its own, and
    then decides push-safety from the rest of the suite. That is not a demotion
    — `scripts/gate.sh --host` and `scripts/deploy_preflight.sh` treat exactly
    this test as blocking, and it is RED on this host today. The alternative,
    letting host drift fail the commit gate, means nobody can commit until
    somebody deploys, and the reliable end of that story is this test being
    deleted by whoever needs to ship at 02:00.
    """

    @pytest.mark.livehost
    def test_no_unit_property_has_drifted(self) -> None:
        drift, tolerated, skip = live_unit_drift()
        if skip is not None:
            if _require_live():
                pytest.fail(f"{REQUIRE_LIVE_FAILURE}: {skip}")
            pytest.skip(skip)
        assert not drift, (
            "the machine is not running the units in this repository:\n  "
            + "\n  ".join(drift)
            + ("\n(tolerated, deploy in flight: " + "; ".join(tolerated) + ")"
               if tolerated else "")
        )

    def test_a_skip_is_never_available_where_it_matters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch has to be real, or the paragraph above is decoration."""
        assert not _require_live()
        monkeypatch.setenv("CAREER_REQUIRE_LIVE_UNITS", "1")
        assert _require_live()
        # …and «0» must read as off, or a deploy script that sets it to 0 to
        # disable the gate would silently be enabling it.
        monkeypatch.setenv("CAREER_REQUIRE_LIVE_UNITS", "0")
        assert not _require_live()


# ── the two gates, and why the selection between them cannot be a list ───────
#
# One suite, two questions:
#
#   «is this code safe to push»   — repository truth. scripts/gate.sh exits on
#                                   this and nothing else.
#   «is this machine running it»  — host truth. scripts/gate.sh PRINTS it in a
#                                   section of its own; scripts/gate.sh --host
#                                   and scripts/deploy_preflight.sh exit on it.
#
# The obvious implementation of that split is the dangerous one. Deselecting
# the host tests so they vanish from the commit gate reproduces the 2026-08-06
# incident in a new place: a green gate that is perfectly compatible with a
# nine-hour unalerted outage. So the ship gate still RUNS them and still shows
# the verdict; it just does not let that verdict decide push-safety.
#
# What routes a test to the host gate is `@pytest.mark.livehost`, and the
# question this section exists to answer is: who guarantees the next live-host
# test gets it? Not a list in scripts/gate.sh — this repository has been bitten
# three times this week by a hand-maintained list of names, and a live-host
# test that is missing from such a list is invisible in BOTH gates, which is
# worse than either failure alone.
#
# So the marker is derived instead of remembered. There are exactly two ways
# into the live machine from this suite — ETC_DEFAULT and RUN_MARKER_DEFAULT —
# and a test reaches the machine if it reaches one of them, directly or through
# any chain of helpers. That is computed from the source below, and a test that
# reaches the machine without the marker is a FAILURE of the ship gate, so the
# hole closes on the commit that opens it.

#: The marker that routes a test to the host gate. Registered in pyproject.toml
#: (with --strict-markers, so a typo cannot silently select nothing).
LIVE_MARKER = "livehost"

#: Every door to the live machine. A function that takes one of these as a
#: parameter default is a «gateway»: harmless when the caller supplies its own
#: path, a machine read when the caller does not.
HOST_DOORS = frozenset({"ETC_DEFAULT", "RUN_MARKER_DEFAULT"})

#: A path typed out by hand bypasses the doors and reads the machine anyway, so
#: it counts too. Derived FROM the doors — spelling the prefixes out as string
#: literals here would make the scanner flag itself, which is the correct answer
#: to the wrong question and cost an hour to work out.
HOST_PATH_PREFIXES = tuple(sorted(
    f"/{Path(globals()[name]).parts[1]}/" for name in HOST_DOORS
))

TESTS_DIR = REPO / "tests"


def _mark_name(expr: ast.expr) -> str | None:
    """`pytest.mark.foo` / `pytest.mark.foo(...)` → "foo"; anything else → None."""
    target = expr.func if isinstance(expr, ast.Call) else expr
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    parts.reverse()
    if parts[:2] == ["pytest", "mark"] and len(parts) > 2:
        return parts[2]
    return None


def _decorator_marks(node: ast.AST) -> set[str]:
    return {name for dec in getattr(node, "decorator_list", [])
            if (name := _mark_name(dec)) is not None}


def _module_marks(module: ast.Module) -> set[str]:
    """`pytestmark = pytest.mark.x` / `= [pytest.mark.x, ...]` at module level."""
    marks: set[str] = set()
    for stmt in module.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            continue
        values = (stmt.value.elts if isinstance(stmt.value, ast.List | ast.Tuple)
                  else [stmt.value])
        marks |= {name for v in values if (name := _mark_name(v)) is not None}
    return marks


_Func = ast.FunctionDef | ast.AsyncFunctionDef


def _body_statements(func: _Func) -> list[ast.stmt]:
    """The body without its docstring — every prose mention of
    /etc/systemd/system in this file would otherwise read as a machine access."""
    body = list(func.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _host_parameters(func: _Func) -> set[str]:
    """Parameters whose DEFAULT is a door — i.e. what a caller must override to
    keep this function off the machine."""
    args = func.args
    positional = args.posonlyargs + args.args
    pairs: list[tuple[ast.arg, ast.expr | None]] = [
        *zip(positional[len(positional) - len(args.defaults):], args.defaults, strict=True),
        *zip(args.kwonlyargs, args.kw_defaults, strict=True),
    ]
    return {arg.arg for arg, default in pairs
            if isinstance(default, ast.Name) and default.id in HOST_DOORS}


def _test_tree_functions() -> list[tuple[str, _Func, set[str]]]:
    """(nodeid, function, effective marks) for every function under tests/.

    Every function, not only test functions: the chain from a test to the
    machine can run through any number of helpers, and following it is the
    whole point.
    """
    found: list[tuple[str, _Func, set[str]]] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        base = str(path.relative_to(REPO))
        inherited = _module_marks(module)

        def walk(body: list[ast.stmt], prefix: str, marks: set[str]) -> None:
            for stmt in body:
                if isinstance(stmt, ast.ClassDef):
                    walk(stmt.body, f"{prefix}{stmt.name}::",
                         marks | _decorator_marks(stmt))
                elif isinstance(stmt, _Func):
                    found.append((f"{prefix}{stmt.name}", stmt,
                                  marks | _decorator_marks(stmt)))

        walk(module.body, f"{base}::", inherited)
    return found


def _functions_that_reach_the_machine() -> set[str]:
    """Names of every function under tests/ that reads the live host.

    Seeded from the doors and closed under «calls», so a helper three levels
    down still marks its callers. Deliberately keyed on the bare name: two
    modules with the same helper name resolve to «both», which over-marks. That
    is the safe direction — an extra test in the host gate costs nothing, a
    missing one costs nine hours.
    """
    functions = _test_tree_functions()
    gateways = {name.rpartition("::")[2]: _host_parameters(func)
                for name, func, _ in functions if _host_parameters(func)}

    def touches(func: _Func) -> bool:
        for stmt in _body_statements(func):
            for sub in ast.walk(stmt):
                # a door named outright
                if isinstance(sub, ast.Name) and sub.id in HOST_DOORS:
                    return True
                # a hardcoded host path, bypassing the doors entirely
                if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        and sub.value.startswith(HOST_PATH_PREFIXES)):
                    return True
                if not isinstance(sub, ast.Call):
                    continue
                callee = (sub.func.id if isinstance(sub.func, ast.Name)
                          else sub.func.attr if isinstance(sub.func, ast.Attribute)
                          else "")
                if callee not in gateways:
                    continue
                supplied = {kw.arg for kw in sub.keywords}
                if None in supplied:
                    # `f(**somedict)`: nobody can tell from here whether the
                    # dict carries `etc`. Unknowable reads as «live».
                    return True
                if not gateways[callee] <= supplied:
                    return True
        return False

    live = {name.rpartition("::")[2] for name, func, _ in functions if touches(func)}
    changed = True
    while changed:
        changed = False
        for name, func, _ in functions:
            short = name.rpartition("::")[2]
            if short in live:
                continue
            called = {
                sub.func.id if isinstance(sub.func, ast.Name) else sub.func.attr
                for stmt in _body_statements(func) for sub in ast.walk(stmt)
                if isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name | ast.Attribute)
            }
            if called & live:
                live.add(short)
                changed = True
    return live


class TestTheShipAndHostGatesCannotDrift:
    """The selection between the two gates, asserted rather than trusted."""

    def test_every_test_that_reads_the_machine_carries_the_marker(self) -> None:
        """Add a live-host test tomorrow and forget the marker: this goes red
        today, in the ship gate, naming the test — instead of the new check
        quietly running in the commit gate, blocking every push for a reason
        that is not about the code, and being deleted by Friday."""
        live = _functions_that_reach_the_machine()
        unmarked = [
            nodeid for nodeid, func, marks in _test_tree_functions()
            if func.name.startswith("test_")
            and func.name in live
            and LIVE_MARKER not in marks
        ]
        assert not unmarked, (
            f"these tests read the LIVE machine but are not @pytest.mark.{LIVE_MARKER}, "
            "so scripts/gate.sh would judge push-safety by the state of this host:\n  "
            + "\n  ".join(unmarked)
        )

    def test_the_host_gate_is_not_empty(self) -> None:
        """«Nothing selected» is the shape a deleted alarm takes. scripts/gate.sh
        treats an empty host selection as RED too; this makes it a repository
        failure as well, so both gates notice."""
        marked = [nodeid for nodeid, func, marks in _test_tree_functions()
                  if func.name.startswith("test_") and LIVE_MARKER in marks]
        assert marked, (
            f"no test carries @pytest.mark.{LIVE_MARKER} — the host gate now "
            "certifies an empty set, which is how a nine-hour outage passes"
        )

    def test_the_doors_still_open_onto_the_real_machine(self) -> None:
        """The derivation above is only as true as these two constants. Point
        them at a tmp_path and every live-host test becomes a synthetic one
        while the marker discipline still looks enforced.

        Looked up by name rather than referenced, and the paths assembled from
        segments, so that asserting this does not itself read as a machine
        access to the scan above — the check has to be exempt from its own rule
        without anyone writing down an exemption.
        """
        assert {name: globals()[name] for name in sorted(HOST_DOORS)} == {
            "ETC_DEFAULT": Path("/", "etc", "systemd", "system"),
            "RUN_MARKER_DEFAULT": Path("/", "run", "systemd", "system"),
        }
        # …and the hand-typed-path net still covers both of them.
        assert len(HOST_PATH_PREFIXES) == len(HOST_DOORS)
        assert all(str(globals()[name]).startswith(HOST_PATH_PREFIXES)
                   for name in HOST_DOORS)

    def test_the_marker_is_registered_and_typos_are_fatal(self) -> None:
        import tomllib

        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        options = config["tool"]["pytest"]["ini_options"]
        assert any(m.split(":")[0] == LIVE_MARKER for m in options.get("markers", []))
        # Without this, `-m livehsot` selects nothing and exits 0 on some
        # invocations — a green host gate that ran no host check.
        assert "--strict-markers" in str(options.get("addopts", ""))

    def test_the_ship_gate_still_runs_and_reports_the_host_checks(self) -> None:
        """The failure mode of this whole design is someone «simplifying»
        gate.sh by dropping the host section. Then the commit gate is green,
        says nothing, and is once again compatible with a wedged worker."""
        gate = (REPO / "scripts" / "gate.sh").read_text(encoding="utf-8")
        assert f'-m "not {LIVE_MARKER}"' in gate, (
            "scripts/gate.sh no longer excludes the host tests from the push "
            "verdict — host drift will block every commit again"
        )
        assert f'-m "{LIVE_MARKER}"' in gate, (
            "scripts/gate.sh no longer RUNS the host tests — their verdict is "
            "now absent from the commit gate, which is the 2026-08-06 shape"
        )
        assert "CAREER_REQUIRE_LIVE_UNITS=1" in gate, (
            "scripts/gate.sh --host no longer forbids a skip, so a host with no "
            "systemd would certify itself as deployed"
        )
        assert REQUIRE_LIVE_FAILURE in gate, (
            "scripts/gate.sh can no longer tell «this host drifted» from "
            f"«no host was checked»: it matches on {REQUIRE_LIVE_FAILURE!r} and "
            "this file stopped saying it"
        )

    def test_the_deploy_gate_requires_the_host_checks(self) -> None:
        preflight = REPO / "scripts" / "deploy_preflight.sh"
        assert preflight.exists(), "the deploy gate is gone"
        assert os.access(preflight, os.X_OK), f"{preflight} is not executable"
        body = preflight.read_text(encoding="utf-8")
        assert "gate.sh" in body and "--host" in body, (
            "deploy_preflight.sh no longer delegates to the host gate — the "
            "deploy path can now certify a machine nobody checked"
        )


# A synthetic host, so the drift check is itself checked.
#
# The check above can only ever report ONE verdict on this machine, and today
# that verdict is red. A checker that has never been seen to go green is not
# known to be a checker — it might be a function that always fails, which is
# how «we have a drift test» becomes «we have a muted drift test». So the same
# code is driven against a fabricated good host and a fabricated bad one.

_SYSTEMCTL_SHIM = '''#!/usr/bin/env python3
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[0] + ".json").read_text())
args = sys.argv[1:]
unit, props, i = None, [], 1
while i < len(args):
    if args[i] == "-p":
        props.append(args[i + 1]); i += 2
    else:
        unit = args[i]; i += 1
table = data["__manager__"] if unit is None else data.get(unit, {})
for prop in props:
    if prop in table:
        print(f"{prop}={table[prop]}")
'''

_MANAGER = {
    "DefaultTimeoutStartUSec": "1min 30s",
    "DefaultRestartUSec": "100ms",
    "DefaultStartLimitIntervalUSec": "10s",
    "DefaultStartLimitBurst": "5",
}

#: What `systemctl show career-worker` prints on a host that IS running the
#: repository's unit. Hand-written on purpose: deriving it from the same
#: normalisers the check uses would make the green case prove nothing.
#:
#: CORRECTED 2026-08-07, and the correction is the whole lesson. The
#: ExecStopPost line below used to read `ignore_exit_status=yes` — a spelling no
#: systemd has ever printed. Hand-writing the fixture was right; hand-writing it
#: from imagination was not. This table said the deploy was fine while the real
#: machine reported `ignore_errors=yes` and the gate refused, so the fixture
#: certified the very bug it existed to catch. Every value here is now copied
#: verbatim from `systemctl show career-worker.service` on the live host
#: (systemd 255.4-1ubuntu8.16), runtime fields included, precisely because those
#: are what the parser must learn to ignore.
_WORKER_DEPLOYED = {
    "Type": "notify",
    "Restart": "always",
    "WatchdogUSec": "5min",
    "RestartUSec": "5s",
    "TimeoutStartUSec": "3min",
    "StartLimitIntervalUSec": "10min",
    "StartLimitBurst": "20",
    "OnFailure": "career-alert@career-worker.service.service",
    "ExecStopPost": (
        "{ path=/root/career/scripts/alert_unit_failure.sh ; "
        "argv[]=/root/career/scripts/alert_unit_failure.sh career-worker.service ; "
        "ignore_errors=yes ; start_time=[n/a] ; stop_time=[n/a] ; "
        "pid=0 ; code=(null) ; status=0/0 }"
    ),
    "NeedDaemonReload": "no",
    "LoadState": "loaded",
}

#: What it printed on 2026-08-06: the 31-July unit, still running.
_WORKER_JULY = {
    **_WORKER_DEPLOYED,
    "Type": "simple",
    "WatchdogUSec": "0",
    "TimeoutStartUSec": "1min 30s",
    "StartLimitIntervalUSec": "10s",
    "StartLimitBurst": "5",
    "ExecStopPost": "",
}


def _synthetic_host(
    tmp_path: Path, live: dict[str, dict[str, str]], *, installed: list[str],
    shipped: list[str] | None = None,
) -> dict[str, Any]:
    """A units dir, an /etc, and a `systemctl` that answers from a table.

    ``shipped`` is what the repository carries, ``installed`` what reached the
    machine. Keeping them separate is the whole point: their difference IS the
    fault class this section exists for.
    """
    units = tmp_path / "ops"
    etc = tmp_path / "etc"
    run = tmp_path / "run"
    for path in (units, etc, run):
        path.mkdir(exist_ok=True)
    for name in (shipped if shipped is not None else installed):
        body = (UNITS / name).read_text(encoding="utf-8")
        (units / name).write_text(body, encoding="utf-8")
        if name in installed:
            (etc / name).write_text(body, encoding="utf-8")
    shim = tmp_path / "systemctl"
    shim.write_text(_SYSTEMCTL_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    (tmp_path / "systemctl.json").write_text(
        __import__("json").dumps({"__manager__": _MANAGER, **live}), encoding="utf-8"
    )
    return {"units_dir": units, "etc": etc, "systemctl": str(shim), "run_marker": run}


def _synthetic_drift(
    host: dict[str, Any], *, now: float | None = None,
) -> tuple[list[str], list[str], str | None]:
    """`live_unit_drift` against a fabricated host — every host argument spelled
    out, on purpose.

    The tests below used to unpack that dict straight into `live_unit_drift`.
    A `**` unpack is
    indistinguishable, to any reader and to any static check, from a call that
    forgot one key and silently fell back to /etc/systemd/system. Naming the
    four arguments here means «this call cannot touch the machine» is a fact
    about the source, which is what TestTheShipAndHostGatesCannotDrift relies on
    to decide which gate a test belongs to without anyone maintaining a list.
    """
    return live_unit_drift(
        units_dir=host["units_dir"],
        etc=host["etc"],
        systemctl=host["systemctl"],
        run_marker=host["run_marker"],
        now=now,
    )


class TestTheDriftCheckItself:
    def test_a_correctly_deployed_host_is_green(self, tmp_path: Path) -> None:
        host = _synthetic_host(
            tmp_path, {"career-worker.service": _WORKER_DEPLOYED},
            installed=["career-worker.service"],
        )
        drift, tolerated, skip = _synthetic_drift(host)
        assert skip is None
        assert drift == [], drift
        assert tolerated == []

    def test_the_2026_08_06_host_is_red_and_names_every_property(
        self, tmp_path: Path
    ) -> None:
        """The exact machine state that certified a correct unit file for six
        days: the file in /etc is July's, and systemd is running July's."""
        host = _synthetic_host(
            tmp_path, {"career-worker.service": _WORKER_JULY},
            installed=["career-worker.service"],
        )
        july = (UNITS / "career-worker.service").read_text().replace(
            "Type=notify", "Type=simple"
        ).replace("WatchdogSec=300", "").replace(
            "ExecStopPost=-/root/career/scripts/alert_unit_failure.sh %n", ""
        )
        (Path(host["etc"]) / "career-worker.service").write_text(july)
        drift, _tolerated, skip = _synthetic_drift(host)
        assert skip is None
        report = "\n".join(drift)
        # «drift» is not actionable at 03:00 — the property, the repository's
        # value and the machine's value, or it is a shrug with a stack trace.
        for property_name in ("Type", "WatchdogSec", "ExecStopPost"):
            assert property_name in report, report
        assert "'notify'" in report and "'simple'" in report, report

    def test_a_partially_deployed_host_is_drift_and_never_a_skip(
        self, tmp_path: Path
    ) -> None:
        """The judgement call this whole class turns on.

        A machine with SOME career units installed and others missing is not
        «not a deploy host». It is a deploy that stopped halfway — which is what
        this machine is doing with career-verify-restore.service right now, and
        the reason the weekly verification has never once run. Answering «skip»
        here would hide precisely the class of fault that caused the outage.
        """
        host = _synthetic_host(
            tmp_path, {"career-worker.service": _WORKER_DEPLOYED},
            installed=["career-worker.service"],   # admin-bot deliberately absent
            shipped=["career-worker.service", "career-admin-bot.service"],
        )
        drift, _tolerated, skip = _synthetic_drift(host)
        assert skip is None, "a half-deployed host must never be skipped"
        assert any("career-admin-bot.service" in line and "NOT installed" in line
                   for line in drift), drift
        # …and it must hand over the command, not a diagnosis.
        assert any("systemctl daemon-reload" in line for line in drift), drift

    def test_a_host_with_no_career_units_skips_honestly(self, tmp_path: Path) -> None:
        host = _synthetic_host(tmp_path, {}, installed=[])
        drift, _tolerated, skip = _synthetic_drift(host)
        assert skip is not None and "not a deploy host" in skip
        assert drift == []

    def test_no_systemd_skips_rather_than_inventing_a_verdict(
        self, tmp_path: Path
    ) -> None:
        host = _synthetic_host(
            tmp_path, {"career-worker.service": _WORKER_DEPLOYED},
            installed=["career-worker.service"],
        )
        host["run_marker"] = tmp_path / "no-such-dir"
        _drift, _tolerated, skip = _synthetic_drift(host)
        assert skip is not None and "systemd is not running" in skip

    def test_a_deploy_in_flight_is_tolerated_not_failed(self, tmp_path: Path) -> None:
        """`cp` has run, `daemon-reload` has not. systemd says so itself.

        A check that goes red in the four seconds between those two commands is
        a check every deploy learns to ignore, and an ignored check is the
        muted channel again.
        """
        live = {"career-worker.service": {**_WORKER_JULY, "NeedDaemonReload": "yes"}}
        host = _synthetic_host(
            tmp_path, live, installed=["career-worker.service"],
        )
        drift, tolerated, _skip = _synthetic_drift(host)
        assert drift == [], drift
        assert any("deploy in flight" in line for line in tolerated), tolerated

    def test_an_ABANDONED_deploy_is_drift(self, tmp_path: Path) -> None:
        """The same state, fifteen minutes later, is not an excuse any more.

        Without this bound «NeedDaemonReload=yes» becomes the permanent alibi:
        the file is right, so the file check is green, and systemd is running
        something else forever. That is a six-day-old half-deploy wearing the
        word «in progress».
        """
        live = {"career-worker.service": {**_WORKER_JULY, "NeedDaemonReload": "yes"}}
        host = _synthetic_host(
            tmp_path, live, installed=["career-worker.service"],
        )
        drift, tolerated, _skip = _synthetic_drift(
            host, now=time.time() + DEPLOY_GRACE_S + 60,
        )
        assert tolerated == []
        assert any("daemon-reload that never came" in line for line in drift), drift

    def test_a_drop_in_that_overrides_the_unit_is_caught(self, tmp_path: Path) -> None:
        """The file matches and systemd still runs something else.

        `/etc/systemd/system/career-worker.service.d/*.conf` can turn off the
        watchdog without touching a byte of the file this repository ships, and
        a comparison of files alone would call that host correctly deployed.
        Reading the LOADED properties is the only thing that sees it.
        """
        live = {"career-worker.service": {**_WORKER_DEPLOYED, "WatchdogUSec": "0"}}
        host = _synthetic_host(
            tmp_path, live, installed=["career-worker.service"],
        )
        drift, _tolerated, _skip = _synthetic_drift(host)
        assert any("WatchdogUSec" in line and "loaded with something else" in line
                   for line in drift), drift


# ── Exec* properties: compared on what they MEAN ─────────────────────────────
#
# THE FALSE RED OF 2026-08-07. A correct deploy landed, systemd loaded both
# units exactly as written, and scripts/deploy_preflight.sh refused:
#
#   career-worker.service: systemd is loaded with something else — ExecStopPost:
#     repo='-/root/career/scripts/alert_unit_failure.sh career-worker.service'
#     live='{ path=/root/career/scripts/alert_unit_failure.sh ;
#             argv[]=/root/career/scripts/alert_unit_failure.sh career-worker.service ;
#             ignore_errors=yes ; start_time=[n/a] ; stop_time=[n/a] ;
#             pid=0 ; code=(null) ; status=0/0 }'
#
# Those two lines say the SAME THING. The unit file's leading `-` is what
# systemd reports as `ignore_errors=yes`; `%n` has been expanded to the unit
# name; and start_time/stop_time/pid/code/status are what the process did last
# time, not what the unit file asked for. Nothing had drifted.
#
# The checker was reading `ignore_exit_status=`, a field systemd does not print,
# so the flag came back `?` and every correctly deployed host was drift.
#
# WHY THAT IS WORTH MORE THAN A CORRECTED REGEX. A drift check that cries wolf
# about a correct machine is a drift check somebody deletes at 02:00 — this
# file's own history says so — but the failure mode of a lazy fix is worse.
# Loosening the comparison until the real host goes quiet (matching only the
# script name, or dropping ExecStopPost from SERVICE_PROPERTIES) would silence
# the three drifts this property exists to catch: a hook pointed at a DIFFERENT
# script, a hook that lost its `-` (so a Telegram outage would fail the service
# it was reporting on), and a hook handed a different argument (so the alert
# names the wrong unit). All three are tested below, in both directions.
#
# THE RULE, and it is the rule the rest of _compare() already follows: a
# property is compared on its MEANING, never on its rendering. `time` parses
# `5min` and `300` to the same number; `watchdog` collapses systemd's two
# spellings of «disarmed»; `units` compares sets. `exec` now parses both sides
# to (path, argv, ignore_errors) and compares THAT. What was rejected: a regex
# that strips the runtime tail — it would still have compared two spellings of
# one fact, and it would still have ignored argv entirely, which the old code
# did and which is why a changed argument was invisible.
#
# NOT CHANGED, checked and left alone: WatchdogUSec, RestartUSec,
# TimeoutStartUSec and StartLimitIntervalUSec are already compared on meaning
# through _seconds(), which reads the `5min` / `3min` / `10min` this host prints;
# StartLimitBurst is compared as an int; OnFailure as a set after %n expansion.
# ExecStopPost was the only structured record among the properties this checker
# reads. (ExecStart is structured too, and is NOT read here — adding it is a
# separate decision, not a bug fix.)


class TestExecPropertiesAreComparedOnMeaning:
    """Both directions. Neither half is worth anything without the other."""

    #: Copied verbatim from `systemctl show career-worker.service -p ExecStopPost`
    #: on the live host, 2026-08-07, systemd 255.4-1ubuntu8.16 — the exact string
    #: that produced the false red.
    LIVE = (
        "{ path=/root/career/scripts/alert_unit_failure.sh ; "
        "argv[]=/root/career/scripts/alert_unit_failure.sh career-worker.service ; "
        "ignore_errors=yes ; start_time=[n/a] ; stop_time=[n/a] ; "
        "pid=0 ; code=(null) ; status=0/0 }"
    )

    def _repo(self, unit: str = "career-worker.service") -> str:
        """The repository's own ExecStopPost=, %n expanded as systemd expands it."""
        return _expand(_directives(unit)["ExecStopPost"], unit)

    # ── direction one: the correct host must go quiet ────────────────────────

    def test_the_deployed_hook_is_not_drift(self) -> None:
        """The false red, reproduced from its own two strings and now green."""
        assert _compare("exec", self._repo(), self.LIVE)

    def test_both_units_agree_with_what_the_machine_printed(self) -> None:
        for unit in LOOP_UNITS:
            live = self.LIVE.replace("career-worker.service", unit)
            assert _compare("exec", self._repo(unit), live), unit

    def test_runtime_state_is_not_configuration(self) -> None:
        """pid/code/status move on their own — a restart is not a deploy.

        This is the half a «strip the tail» fix would also have got right, and
        it is kept because it is the reason the tail must be parsed rather than
        pattern-matched: the running unit and the idle one print different
        values here and are the same configuration.
        """
        running = (
            "{ path=/root/career/scripts/alert_unit_failure.sh ; "
            "argv[]=/root/career/scripts/alert_unit_failure.sh career-worker.service ; "
            "ignore_errors=yes ; start_time=[Fri 2026-08-07 18:48:34 CEST] ; "
            "stop_time=[Fri 2026-08-07 18:48:34 CEST] ; pid=1796574 ; "
            "code=exited ; status=0 }"
        )
        assert _compare("exec", self._repo(), running)
        assert _exec_live_records(running) == _exec_live_records(self.LIVE)

    def test_every_spelling_of_the_leading_dash_is_understood(self) -> None:
        """systemd prints the `-` three different ways depending on which
        property you ask for and which version answers. Guessing one of them is
        the entire bug, so all three are read."""
        head = ("{ path=/x.sh ; argv[]=/x.sh a ; ")
        for spelling in ("ignore_errors=yes", "ignore_exit_status=yes",
                         "flags=ignore-failure"):
            assert _exec_live_records(head + spelling + " ; pid=0 }")[0][2], spelling
        for spelling in ("ignore_errors=no", "ignore_exit_status=no", "flags="):
            assert not _exec_live_records(head + spelling + " ; pid=0 }")[0][2], spelling

    # ── direction two: real drift must still be caught ───────────────────────

    def test_a_hook_pointed_at_a_different_script_is_drift(self) -> None:
        live = self.LIVE.replace("alert_unit_failure.sh", "alert_unit_failure.sh.bak")
        assert not _compare("exec", self._repo(), live)

    def test_a_hook_that_lost_its_leading_dash_is_drift(self) -> None:
        """The one that costs the most and shows the least: the unit still has
        its alert, and a Telegram outage now turns the alerting into a failure
        of the service it was reporting on."""
        live = self.LIVE.replace("ignore_errors=yes", "ignore_errors=no")
        assert not _compare("exec", self._repo(), live)
        # …and via the other spelling, so the `flags=` reader is held to it too.
        assert not _compare("exec", self._repo(),
                            self.LIVE.replace("ignore_errors=yes", "flags="))

    def test_a_changed_argument_is_drift(self) -> None:
        """What the OLD code could not see at all: it compared the path and the
        flag and threw argv away, so a hook alerting under the WRONG UNIT NAME
        read as correctly deployed."""
        live = self.LIVE.replace(
            "alert_unit_failure.sh career-worker.service ;",
            "alert_unit_failure.sh career-admin-bot.service ;",
        )
        assert not _compare("exec", self._repo(), live)

    def test_a_dropped_argument_is_drift(self) -> None:
        live = self.LIVE.replace("alert_unit_failure.sh career-worker.service ;",
                                 "alert_unit_failure.sh ;")
        assert not _compare("exec", self._repo(), live)

    def test_a_hook_that_is_gone_entirely_is_drift(self) -> None:
        assert not _compare("exec", self._repo(), "")

    def test_a_binary_swapped_under_an_unchanged_argv_is_drift(self) -> None:
        """Why `path` is in the record and in the rendering, though it is
        USUALLY just argv[0] repeated.

        With systemd's `@` prefix the two come apart: `@/bin/A x` and `@/bin/B x`
        have the identical argv `('x',)` and run different binaries. A record of
        (argv, ignore) alone — the tempting simplification, since path is
        redundant in every unit this repository ships — would call those two the
        same deploy. So path is compared, and `_render_exec` prints the `@` back:
        drop it from the RENDERING and the message shows `repo=X live=X` for two
        configurations that genuinely differ, which reads as a broken tool.
        """
        assert _exec_repo_record("@/bin/A x") != _exec_repo_record("@/bin/B x")
        assert _render_exec(_exec_repo_record("@/bin/A x")) != \
               _render_exec(_exec_repo_record("@/bin/B x"))
        # and the `@` rendering is a UNIT-FILE line, not a systemd one: it parses
        # straight back to the record it came from, so both halves of a drift
        # message are written in the language the operator edits.
        live = _exec_live_records(
            "{ path=/a/alert.sh ; argv[]=/a/alert.sh.bak career-worker.service ; "
            "ignore_errors=no ; pid=0 }"
        )
        assert _exec_repo_record(_render_exec(live)) == live

    def test_a_second_hook_nobody_shipped_is_drift(self) -> None:
        """systemd concatenates records, so an extra ExecStopPost= injected by a
        drop-in appears as `{ … } { … }` — and must not read as the one the
        repository ships."""
        live = self.LIVE + (" { path=/usr/bin/curl ; argv[]=/usr/bin/curl http://x ; "
                            "ignore_errors=no ; pid=0 }")
        assert not _compare("exec", self._repo(), live)
        assert len(_exec_live_records(live)) == 2

    # ── the message a human has to act on ────────────────────────────────────

    def test_the_drift_message_names_the_property_and_both_values(
        self, tmp_path: Path
    ) -> None:
        """Through the real `live_unit_drift`, not the comparison alone.

        The old message pasted eighty characters of runtime state into the
        operator's terminal and left them to spot which field mattered. Both
        values are now printed in the spelling of the unit file — which is the
        form the FIX takes.
        """
        live = {"career-worker.service": {
            **_WORKER_DEPLOYED,
            # both the `-` and the script: one message, two faults, and the
            # operator must be able to see both without opening anything.
            "ExecStopPost": self.LIVE.replace("ignore_errors=yes", "ignore_errors=no")
                                     .replace("alert_unit_failure.sh",
                                              "alert_unit_failure.sh.bak"),
        }}
        host = _synthetic_host(tmp_path, live, installed=["career-worker.service"])
        drift, _tolerated, _skip = _synthetic_drift(host)
        line = next((x for x in drift if "ExecStopPost" in x), "")
        assert line, drift
        # the property, so nobody has to diff two blobs to find it
        assert "ExecStopPost" in line
        # what the repository asks for, as a unit-file line
        assert "repo='-/root/career/scripts/alert_unit_failure.sh career-worker.service'" in line
        # what the machine has, in the same spelling: no `-`, wrong script
        assert "live='/root/career/scripts/alert_unit_failure.sh.bak career-worker.service'" in line
        # and none of the runtime noise that made the old message unreadable
        assert not any(field in line for field in _EXEC_RUNTIME_FIELDS), line

    def test_a_correct_hook_is_absent_from_that_message_entirely(
        self, tmp_path: Path
    ) -> None:
        """The green direction through the same path: the fixture host is
        deployed correctly, so ExecStopPost must not appear in the drift list at
        all. Without this, «no drift» could mean «the exec branch never ran»."""
        host = _synthetic_host(
            tmp_path, {"career-worker.service": _WORKER_DEPLOYED},
            installed=["career-worker.service"],
        )
        drift, _tolerated, _skip = _synthetic_drift(host)
        assert not any("ExecStopPost" in line for line in drift), drift

    # ── and the machine itself ───────────────────────────────────────────────

    @pytest.mark.livehost
    def test_this_machine_spells_the_ignore_flag_a_way_we_know(self) -> None:
        """A guard against the NEXT rename.

        If some future systemd prints the flag under a fourth name, every `-`
        would look absent and this check would go red for a correct host all
        over again — but with the same misleading message as 2026-08-07. This
        test fails first, and says what it actually is.
        """
        if not RUN_MARKER_DEFAULT.is_dir():
            pytest.skip("systemd is not running here — nothing to read")
        systemctl = shutil.which("systemctl")
        if systemctl is None:
            pytest.skip("no systemctl on this host")
        for unit in LOOP_UNITS:
            rendered = _show(systemctl, ["ExecStopPost"], unit).get("ExecStopPost", "")
            if not rendered.strip():
                continue  # not installed here; the drift check is what says so
            assert _exec_understood(rendered), (
                f"{unit}: systemd no longer spells the ignore flag any of "
                f"{_EXEC_IGNORE_KEYS + (_EXEC_IGNORE_FLAG,)} — teach "
                f"_exec_ignores_errors() the new name before believing any "
                f"ExecStopPost drift this run reports.\n  {rendered}"
            )


# ── the alert that must not become a storm ───────────────────────────────────


def _alert_constant(name: str) -> int:
    """A constant read out of the shell script, so the arithmetic below is
    asserted against the value that actually ships and not a copy of it."""
    source = (SCRIPTS / "alert_unit_failure.sh").read_text(encoding="utf-8")
    found = re.search(rf"^{name}=(\d+)$", source, re.M)
    assert found, f"{name} is not defined in alert_unit_failure.sh"
    return int(found.group(1))


def _kill_period(unit: str, deadline: str) -> float:
    directives = _directives(unit)
    return _seconds(directives[deadline]) + _seconds(directives["RestartSec"])


class TestTheAlertDoesNotBecomeAStorm:
    """The decision, and the arithmetic that keeps the comment honest.

    Under the new units this exact wedge DOES page — and never stops. The
    units are deliberately built so a wedge can never exhaust the start limit,
    so systemd restarts a permanently wedged worker every WatchdogSec+RestartSec
    forever and every one of those restarts reaches ExecStopPost=.

    THE ALTERNATIVE, REJECTED: retune StartLimitBurst/StartLimitIntervalSec so
    the wedge escalates to start-limit-hit and the unit STAYS DOWN — one
    message instead of 283. Rejected because Restart=always is the only
    unattended recovery this host has, and the wedges that heal on restart (the
    2026-08-04 Postgres bounce healed in 100s with nobody awake) would become
    outages lasting until a human types `systemctl reset-failed`; and because
    the same two knobs govern the hung-start verdict, so a Postgres merely slow
    to come back at boot would permanently disable the worker. The storm is a
    property of the notification, so it is fixed in the notification.
    """

    def test_the_storm_is_real_and_this_is_its_size(self) -> None:
        period = _kill_period("career-worker.service", "WatchdogSec")
        assert period == 305, "the numbers below are derived from this one"
        per_day = 86400 / period
        assert per_day > 280, per_day     # ≈283 identical Telegram messages

    def test_the_units_still_refuse_to_escalate_a_wedge(self) -> None:
        """The rejected alternative, asserted as rejected.

        If someone later retunes the start limit so a wedge CAN exhaust it,
        this test goes red and they have to come and read the paragraph above
        rather than discover the consequence on a Friday night — a worker that
        stops retrying after a transient database outage.
        """
        for unit in LOOP_UNITS:
            directives = _directives(unit)
            burst = int(directives["StartLimitBurst"])
            window = _seconds(directives["StartLimitIntervalSec"])
            for deadline in ("WatchdogSec", "TimeoutStartSec"):
                assert _kill_period(unit, deadline) * (burst - 1) > window, (
                    f"{unit}/{deadline}: the start limit is now reachable, so a "
                    "transient failure ends with systemd giving up. That was "
                    "considered and rejected — see TestTheAlertDoesNotBecomeAStorm."
                )

    def test_the_repeat_window_cuts_the_storm_to_a_human_volume(self) -> None:
        repeat = _alert_constant("REPEAT_S")
        assert 86400 / repeat <= 24, "still a storm"
        # …and it has to actually suppress something, or it is decoration.
        for unit, deadline in (("career-worker.service", "WatchdogSec"),
                               ("career-admin-bot.service", "WatchdogSec")):
            assert repeat >= 10 * _kill_period(unit, deadline), unit

    def test_the_incident_gap_is_derived_from_the_units_not_chosen(self) -> None:
        gap = _alert_constant("INCIDENT_GAP_S")
        longest = max(_kill_period(unit, deadline)
                      for unit in LOOP_UNITS
                      for deadline in ("WatchdogSec", "TimeoutStartSec"))
        # Two consecutive kills must never look like two incidents, or every
        # restart of an ongoing wedge reports itself as brand new.
        assert gap > 2 * longest, (gap, longest)
        # …and an incident must not be declared over while a reminder for it is
        # still pending, or «still broken» and «broke again» can both be true.
        assert gap < _alert_constant("REPEAT_S")

    # ---- the same arithmetic, executed ------------------------------------

    def _alert(self, sink: dict[str, Any], unit: str, result: str,
               now: int) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **sink["env"],
               "SERVICE_RESULT": result, "CAREER_ALERT_NOW": str(now)}
        done = subprocess.run(  # noqa: S603 — fixed argv, our own ops script
            [str(SCRIPTS / "alert_unit_failure.sh"), unit],
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        return done

    def test_a_permanent_wedge_sends_one_message_then_hourly_reminders(
        self, sink: dict[str, Any]
    ) -> None:
        """Thirteen watchdog kills, two messages — and the second one says how
        many it stands for."""
        t0 = 1_800_000_000
        period = int(_kill_period("career-worker.service", "WatchdogSec"))
        self._alert(sink, "career-worker.service", "watchdog", t0)
        assert len(sink["posts"]) == 1
        for k in range(1, 12):
            self._alert(sink, "career-worker.service", "watchdog", t0 + period * k)
        assert len(sink["posts"]) == 1, (
            "eleven further kills inside the repeat window must be silent — "
            f"got {len(sink['posts'])} messages"
        )
        self._alert(sink, "career-worker.service", "watchdog",
                    t0 + _alert_constant("REPEAT_S"))
        assert len(sink["posts"]) == 2
        text = sink["posts"][-1]["text"]
        assert "ما زال" in text, text          # STILL broken, not broken again
        assert "١٣" in text, text              # 13 kills, counted not guessed
        assert "٦٠" in text, text              # 60 minutes so far

    def test_a_failure_that_returns_after_quiet_says_AGAIN_not_still(
        self, sink: dict[str, Any]
    ) -> None:
        """The distinction the operator has to be able to make.

        «still broken» sends him to a journal that has been failing for hours;
        «broke again» sends him to the last few minutes and to whatever changed
        in between. Reporting the second as the first costs him the wrong hours.
        """
        t0 = 1_800_000_000
        self._alert(sink, "career-worker.service", "watchdog", t0)
        healed_for = _alert_constant("INCIDENT_GAP_S") + 600
        self._alert(sink, "career-worker.service", "watchdog", t0 + healed_for)
        assert len(sink["posts"]) == 2, "a NEW incident must never be suppressed"
        text = sink["posts"][-1]["text"]
        assert "من جديد" in text, text
        assert "ما زال" not in text, text
        assert "٢٥" in text, text     # 1500s of health = 25 minutes

    def test_a_different_verdict_on_the_same_unit_is_never_suppressed(
        self, sink: dict[str, Any]
    ) -> None:
        """A wedge and a hung start send him to different halves of the
        journal. Silencing one because the other just spoke would hide a change
        in the failure itself."""
        t0 = 1_800_000_000
        self._alert(sink, "career-worker.service", "watchdog", t0)
        self._alert(sink, "career-worker.service", "timeout", t0 + 5)
        assert len(sink["posts"]) == 2
        assert "لم تكمل الإقلاع" in sink["posts"][-1]["text"]

    def test_de_duplication_fails_OPEN(self, sink: dict[str, Any]) -> None:
        """If the state cannot be kept, the alert is sent anyway.

        Getting suppression wrong in this direction costs a noisy hour. Getting
        it wrong in the other direction costs nine silent ones, which is the
        incident this file exists because of.
        """
        blocker = Path(sink["env"]["CAREER_ALERT_STATE_DIR"]).parent / "blocker"
        blocker.write_text("not a directory")
        env = dict(sink["env"])
        env["CAREER_ALERT_STATE_DIR"] = str(blocker / "alerts")
        for _ in range(3):
            done = subprocess.run(  # noqa: S603 — fixed argv, our own script
                [str(SCRIPTS / "alert_unit_failure.sh"), "career-worker.service"],
                capture_output=True, text=True, timeout=60, check=False,
                env={**os.environ, **env, "SERVICE_RESULT": "watchdog"},
            )
            assert done.returncode == 0, done.stderr
        assert len(sink["posts"]) == 3, "silence is never the safe default here"

    def test_the_reminder_lines_stay_direction_pure(
        self, sink: dict[str, Any]
    ) -> None:
        """The reminder carries numbers, which is exactly where a bidi client
        scrambles a line. Arabic-Indic digits keep the line Arabic-only.

        Both shapes are checked, because they are two different sentences with
        two different numbers in them and only one of them is exercised by any
        other test. Reaching the «still» shape needs CONTINUOUS kills — jumping
        straight to the repeat window means the failure was quiet for an hour,
        which is «broke again» and rightly says so.
        """
        arabic = re.compile(r"[؀-ۿ]")
        latin = re.compile(r"[A-Za-z0-9]")
        t0 = 1_800_000_000
        period = int(_kill_period("career-worker.service", "WatchdogSec"))
        repeat = _alert_constant("REPEAT_S")

        for kill in range(0, repeat + period, period):
            self._alert(sink, "career-worker.service", "watchdog", t0 + kill)
        still = sink["posts"][-1]["text"]
        assert "ما زال" in still, still

        later = t0 + repeat + _alert_constant("INCIDENT_GAP_S") + 6000
        self._alert(sink, "career-worker.service", "watchdog", later)
        again = sink["posts"][-1]["text"]
        assert "من جديد" in again, again

        for text in (still, again):
            for line in text.splitlines():
                assert not (arabic.search(line) and latin.search(line)), line


# ── the timeout that makes a hang possible ───────────────────────────────────


class TestTheConnectDeadline:
    """`grep -rn connect_timeout src/career/` returned nothing.

    That absence is the concrete trigger for the hung-start path: libpq's
    default connect_timeout is zero — «wait forever» — and `report_environment()`
    opens a session BEFORE `sd_notify.ready()`. A Postgres that accepts the TCP
    connection and never completes the handshake (paused container, docker-proxy
    outliving its backend, an iptables DROP) blocks there indefinitely, which
    under Type=notify is a unit killed at TimeoutStartSec with the journal
    ending mid-boot-check and no reason in it.
    """

    def test_the_deadline_is_bounded_and_fits_the_start_budget(self) -> None:
        from career.db.session import CONNECT_TIMEOUT_S

        # libpq silently promotes anything below 2 to 2, so a smaller number
        # would be a lie in the source.
        assert CONNECT_TIMEOUT_S >= 2
        # It has to fail the boot check well before systemd kills the unit, or
        # the operator gets «did not finish starting» instead of a line naming
        # Postgres.
        for unit in LOOP_UNITS:
            assert CONNECT_TIMEOUT_S < _seconds(_directives(unit)["TimeoutStartSec"]) / 5

    def test_it_cannot_silently_fail_to_apply(self) -> None:
        """The factory is not enough, and this is the test that says why.

        Nine call sites in this repository build their engine with a bare
        `create_engine(settings.owner_database_url, future=True)` and never
        touch `engine_for` — scripts/run_worker_loop.py among them, which is
        the exact engine the hanging boot check opens. So the deadline is
        injected in the `do_connect` listener that is registered on the Engine
        CLASS, and this exercises precisely that path: a bare create_engine,
        against a socket that accepts and never speaks.
        """
        from sqlalchemy import create_engine

        import career.db.session as session_module

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        accepted: list[socket.socket] = []

        def swallow() -> None:
            while True:
                try:
                    accepted.append(server.accept()[0])
                except OSError:
                    return

        threading.Thread(target=swallow, daemon=True).start()
        engine = create_engine(
            f"postgresql+psycopg://career_app:x@127.0.0.1:{port}/career", future=True
        )
        started = time.monotonic()
        try:
            with pytest.raises(Exception):  # noqa: B017,PT011 — DBAPI-specific
                engine.connect()
        finally:
            elapsed = time.monotonic() - started
            engine.dispose()
            for conn in accepted:
                conn.close()
            server.close()
        limit = session_module.CONNECT_TIMEOUT_S
        assert elapsed < limit * 2, (
            f"a connection to a black-hole socket took {elapsed:.1f}s against a "
            f"{limit}s deadline — the deadline is not reaching libpq"
        )
        assert elapsed >= limit * 0.5, (
            f"gave up after {elapsed:.1f}s, far short of the {limit}s deadline — "
            "this test is proving something other than the timeout"
        )

    def test_the_listener_is_reachable_from_the_loops_that_hang(self) -> None:
        """The deadline arrives by import, so the import has to be asserted.

        `scripts/run_worker_loop.py` never mentions `career.db.session`. It gets
        the `do_connect` listener — and therefore the connect deadline and the
        role guard both — only because something it imports imports it. That is
        true today (verified: importing the loop's `career.*` imports puts
        career.db.session in sys.modules), and it is exactly the kind of truth a
        refactor deletes by accident, in a file nobody would think to re-check.
        If this ever goes red, the boot check is back on «wait forever» and the
        symptom is a unit killed at TimeoutStartSec with an empty journal.
        """
        import ast as ast_module
        import importlib
        import sys

        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        import career.db.session as session_module

        for script in LOOP_UNITS.values():
            tree = ast_module.parse((SCRIPTS / script).read_text())
            modules = sorted({
                node.module for node in ast_module.walk(tree)
                if isinstance(node, ast_module.ImportFrom)
                and node.module and node.module.startswith("career")
            })
            for module in modules:
                importlib.import_module(module)
            assert "career.db.session" in sys.modules, (
                f"{script}: nothing it imports pulls in career.db.session, so "
                "the do_connect listener is never registered in that process — "
                "every engine it builds is back on libpq's «wait forever»"
            )
        assert event.contains(
            Engine, "do_connect", session_module._guard_every_connection
        ), "the listener is not registered on the Engine class at all"
