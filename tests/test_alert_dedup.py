"""The de-duplication in `scripts/alert_unit_failure.sh`, driven for real.

The script's stated promise is that everything about suppression FAILS OPEN:
any doubt sends the alert, because a noisy hour costs less than a silent one.
Two ways it broke that promise were found by driving the shipped script — not
by reading it — and both are pinned here the same way they were found: the real
`scripts/alert_unit_failure.sh`, in a real subprocess, with a real state
directory. Nothing in this file re-implements the decision it is testing. A
test that models a script instead of running it proves only that two authors
agreed, which is how a claim about `systemctl` became a lie in this repository
on 2026-08-06.

HOW «WOULD HAVE SENT» IS OBSERVED, without ever sending.

Most tests here point the script at an env file that is shaped like the real
one but carries NO Telegram keys. The script then reaches its credential check
only when it has decided to speak, and says so:

    decided to send   → exit 1, «telegram admin credentials absent» on stderr
    suppressed        → exit 0, silence

That is a stronger signal than a mocked sender, because it is produced by the
same code path the operator's phone is on, and it cannot accidentally reach
Telegram. `_sent()` refuses to read any other exit-1 as a send, so a syntax
error in the script can never be scored as an alert.

The few tests that need the WORDS also run the real sender, against a local
HTTP server standing in for api.telegram.org.
"""

from __future__ import annotations

import http.server
import os
import re
import subprocess
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "alert_unit_failure.sh"

UNIT = "career-worker.service"
#: WatchdogSec 300 + RestartSec 5 in career-worker.service: one kill-and-restart
#: of a permanently wedged worker. The unit side of this arithmetic (that the
#: start limit is deliberately unreachable, so the wedge really does restart
#: forever) is asserted against the unit files in tests/test_ops_watchdogs.py;
#: here it is the INPUT to the storm the notification has to survive.
KILL_PERIOD_S = 305


def _constant(name: str) -> int:
    """Read a constant out of the script, so the arithmetic below is asserted
    against the value that ships and not against a copy of it."""
    found = re.search(rf"^{name}=(\d+)$", SCRIPT.read_text(encoding="utf-8"), re.M)
    assert found, f"{name} is not defined in {SCRIPT.name}"
    return int(found.group(1))


REPEAT_S = _constant("REPEAT_S")
INCIDENT_GAP_S = _constant("INCIDENT_GAP_S")


def _sent(done: subprocess.CompletedProcess[str]) -> bool:
    """Did this invocation decide to alert?

    Exit 1 counts as «yes» ONLY when it is the credential check talking. Any
    other non-zero exit is a broken script being scored as a working alert,
    which is the failure this whole file exists to prevent.
    """
    if done.returncode == 0:
        return False
    assert done.returncode == 1, done.stderr
    assert "credentials absent" in done.stderr, (
        f"exit {done.returncode} for a reason that is not «decided to send»:\n{done.stderr}"
    )
    return True


class Alerts:
    """One unit's alert path, with its own state directory and its own clock.

    `speaks` selects how «decided to send» is read back. False is the
    credential-less harness of the module docstring — exit 1 at the credential
    check. True is the sink: the message really is composed and posted, so the
    signal is a successful exit AND a new message on the wire.
    """

    def __init__(self, state_dir: Path, env_file: Path, *, speaks: bool = False) -> None:
        self.state_dir = state_dir
        self.speaks = speaks
        self._seen = 0
        self.env: dict[str, str] = {
            "CAREER_ENV_FILE": str(env_file),
            "CAREER_ALERT_STATE_DIR": str(state_dir),
        }

    def _decided(self, done: subprocess.CompletedProcess[str]) -> bool:
        if not self.speaks:
            return _sent(done)
        assert done.returncode == 0, done.stderr
        spoke = len(_Telegram.texts) > self._seen
        self._seen = len(_Telegram.texts)
        return spoke

    def _env(self, now: int, result: str) -> dict[str, str]:
        # SERVICE_RESULT set is «asked by ExecStopPost=» — the watchdog path.
        # It is always set here, so no test in this file ever runs the
        # OnFailure= branch and nothing consults the live systemd.
        return {**os.environ, **self.env,
                "SERVICE_RESULT": result, "CAREER_ALERT_NOW": str(now)}

    def spawn(self, now: int, *, unit: str = UNIT,
              result: str = "watchdog") -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 — fixed argv, our own ops script
            [str(SCRIPT), unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self._env(now, result),
        )

    def fire(self, now: int, *, unit: str = UNIT, result: str = "watchdog") -> bool:
        done = subprocess.run(  # noqa: S603 — fixed argv, our own ops script
            [str(SCRIPT), unit], capture_output=True, text=True, timeout=60,
            check=False, env=self._env(now, result),
        )
        return self._decided(done)

    def stamps(self) -> dict[str, list[int]]:
        """Every stamp the script has written, as integers. Read off the disk
        the script wrote to — the key is whatever it chose to call it."""
        out: dict[str, list[int]] = {}
        for path in sorted(self.state_dir.glob("*")):
            if path.suffix == ".lock" or not path.is_file():
                continue
            out[path.name] = [int(field) for field in path.read_text().split()]
        return out

    def kills(self) -> int:
        """The counter the reminder quotes to the operator
        («مرات التعطل منذ أول إنذار»)."""
        stamps = self.stamps()
        assert len(stamps) == 1, stamps
        return next(iter(stamps.values()))[3]


class _Telegram(http.server.BaseHTTPRequestHandler):
    """api.telegram.org, on localhost."""

    texts: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        _Telegram.texts.append(urllib.parse.parse_qs(body).get("text", [""])[0])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture()
def alerts(tmp_path: Path) -> Alerts:
    """No Telegram credentials: «decided to send» surfaces as exit 1."""
    env_file = tmp_path / "env"
    # Shaped like the real file, including the JSON value that once broke this
    # script when the env file was still `source`d — and deliberately without
    # TELEGRAM_ADMIN_BOT_TOKEN / TELEGRAM_ADMIN_CHAT_ID.
    env_file.write_text('SALLA_PRODUCT_CATALOG={"basic": "1", "pro": "2"}\n',
                        encoding="utf-8")
    return Alerts(tmp_path / "state", env_file)


@pytest.fixture()
def spoken(tmp_path: Path) -> Iterator[Alerts]:
    """The same, but the message is actually sent — to a local socket."""
    _Telegram.texts = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Telegram)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[0], server.server_address[1]
    env_file = tmp_path / "env"
    env_file.write_text(
        "TELEGRAM_ADMIN_BOT_TOKEN=123456789:FAKEfake0000000000000000000000\n"
        "TELEGRAM_ADMIN_CHAT_ID=99999999\n"
        'SALLA_PRODUCT_CATALOG={"basic": "1", "pro": "2"}\n',
        encoding="utf-8",
    )
    talker = Alerts(tmp_path / "state", env_file, speaks=True)
    talker.env["CAREER_TELEGRAM_API"] = f"http://{host}:{port}"
    yield talker
    server.shutdown()


def texts() -> list[str]:
    return _Telegram.texts


class TestTheHarnessItself:
    """Before anything is proven with it, the instrument is proven.

    If «would have sent» were ever silently indistinguishable from «suppressed»,
    every assertion below would pass against a script that says nothing.
    """

    def test_a_decision_to_send_is_visible_and_a_suppression_is_not(
        self, alerts: Alerts
    ) -> None:
        assert alerts.fire(1_800_000_000) is True
        assert alerts.fire(1_800_000_005) is False

    def test_nothing_here_can_reach_a_real_telegram(self, alerts: Alerts) -> None:
        """The env file has no credentials at all, so the sender is never
        reached — the reason this file can be run on the live host."""
        env_file = Path(alerts.env["CAREER_ENV_FILE"]).read_text(encoding="utf-8")
        assert "TELEGRAM" not in env_file, env_file


class TestABackwardClockMustNotSilenceTheChannel:
    """DEFECT 1 — a backward NTP step used to mute a unit for Δ + REPEAT_S.

    `quiet = NOW - p_kill` went negative when the wall clock stepped back. A
    negative number is not greater than INCIDENT_GAP_S, so the script took the
    «still broken» branch; `NOW - p_sent >= REPEAT_S` was then negative too, so
    SEND=0. Proven against the shipped script:

        t=4700  WOULD SEND          stamp: 4700 4700 4700 1
        t=4000  suppressed (rc=0)   ← clock stepped back
        t=100   suppressed (rc=0)   ← larger backward step

    Every one of those suppressed lines is a NEW watchdog kill of a wedged
    worker that the operator was never told about, on a host whose clock is
    disciplined by NTP and whose stamps live in tmpfs across an unknown skew.
    A file whose header says it fails open cannot have a path where doubt about
    the clock produces silence.

    THE FIX, and the alternative that was rejected. A monotonic source
    (/proc/uptime, CLOCK_BOOTTIME) is immune to the step by construction, and
    /run being tmpfs means a stamp never outlives the boot its uptime is
    measured from — so it would work. It was rejected because the durations in
    the message are wall-clock sentences the operator reads against his own
    journal, because it would need a second clock injection point beside
    CAREER_ALERT_NOW (used by tests/test_ops_watchdogs.py), and because it
    answers only half the question: a FORWARD step still has to be handled, and
    it already is — a large forward jump reads as quiet and sends «broke again».
    So the clock is sanity-checked instead: a negative interval is not a
    duration, it is evidence that the clock moved, and evidence of a moved clock
    is doubt, and doubt sends.
    """

    def test_the_exact_sequence_that_was_proven(self, alerts: Alerts) -> None:
        assert alerts.fire(4700) is True
        assert alerts.fire(4000) is True, (
            "a watchdog kill went unreported because the clock stepped back 700s"
        )
        assert alerts.fire(100) is True, (
            "a larger backward step silenced it too — the suppression window now "
            "outlives the failure"
        )

    def test_a_step_back_does_not_extend_the_quiet_window(self, alerts: Alerts) -> None:
        """The size of the damage, asserted.

        Before the fix, a backward step of Δ bought silence for Δ + REPEAT_S of
        real time: the stamp's `p_sent` sits Δ seconds in the future, so the
        hourly reminder cannot come due until the clock has caught up AND run
        another hour. Here Δ is a modest half hour and the whole repeat window
        is walked afterwards; not one of those kills may be lost.
        """
        t0 = 1_800_000_000
        step = 1800
        assert alerts.fire(t0) is True
        assert alerts.fire(t0 + KILL_PERIOD_S) is False   # ordinary suppression
        assert alerts.fire(t0 - step) is True, "the clock moved; that is doubt"
        for k in range(1, 6):
            # …and the kills that follow, still inside the (rebased) repeat
            # window, are suppressed again: the fix must not turn one clock step
            # into a permanent storm.
            assert alerts.fire(t0 - step + KILL_PERIOD_S * k) is False

    def test_it_self_heals_onto_the_new_clock(self, alerts: Alerts) -> None:
        """One alert for the step, then ORDINARY service on the new timeline.

        A four-hour NTP correction lands mid-wedge, and the wedge keeps killing
        every 305s. The step itself speaks once; the next twelve kills are the
        normal storm cap doing its job, and the reminder comes due exactly one
        REPEAT_S after the step — not one REPEAT_S after the old, abandoned
        timeline, which is the arithmetic that used to swallow four extra hours.
        """
        t0 = 1_800_000_000
        assert alerts.fire(t0) is True
        stepped = t0 - 4 * 3600
        assert alerts.fire(stepped) is True
        sends = sum(alerts.fire(stepped + KILL_PERIOD_S * k) for k in range(1, 13))
        assert sends == 1, (
            f"{sends} of the twelve kills after the step spoke — the step must "
            "cost one message, not a storm and not an hour of silence"
        )
        assert alerts.kills() == 13, "the re-based incident lost count"

    def test_each_verdict_re_bases_on_its_own_stamp(self, alerts: Alerts) -> None:
        """The state is keyed on unit AND verdict, and a clock step must not
        smear one key's timeline onto the other's — the wedge and the hung start
        send the operator to different halves of the journal.

        The clock is the host's, so a step is felt by both keys; what must stay
        separate is what each key does about it.
        """
        t0 = 1_800_000_000
        assert alerts.fire(t0, result="watchdog") is True
        assert alerts.fire(t0 + 5, result="timeout") is True
        assert alerts.fire(t0 - 900, result="watchdog") is True
        assert alerts.fire(t0 - 895, result="timeout") is True
        # …and each is then suppressed on its own re-based clock.
        assert alerts.fire(t0 - 890, result="watchdog") is False
        assert alerts.fire(t0 - 885, result="timeout") is False
        assert alerts.stamps() == {
            f"{UNIT}.watchdog": [t0 - 900, t0 - 890, t0 - 900, 2],
            f"{UNIT}.timeout": [t0 - 895, t0 - 885, t0 - 895, 2],
        }

    def test_the_message_never_reports_a_negative_duration(
        self, spoken: Alerts
    ) -> None:
        """Fail-open is necessary and not sufficient.

        The cheapest fix — let a negative `quiet` fall through to «broke again»
        — sends the alert and then tells the operator he enjoyed «مدة السلامة
        قبل هذا العطل بالدقائق: -٣٠». That is a wrong sentence about his own
        system (it did not heal; the clock moved) with a Latin minus sign
        sitting inside an Arabic line, which is exactly where his client
        scrambles the reading order. It must not claim «still broken» either:
        the elapsed-time numbers that sentence carries are the ones the clock
        just invalidated.
        """
        t0 = 1_800_000_000
        assert spoken.fire(t0) is True
        assert spoken.fire(t0 - 1800) is True
        assert len(texts()) == 2, texts()
        message = texts()[-1]
        assert not re.search(r"-[٠-٩]", message), message
        assert "ما زال" not in message, message
        arabic = re.compile(r"[؀-ۿ]")
        latin = re.compile(r"[A-Za-z0-9]")
        for line in message.splitlines():
            assert not (arabic.search(line) and latin.search(line)), line

    def test_no_field_of_a_stamp_may_be_in_the_future(self, spoken: Alerts) -> None:
        """All three timestamps are checked, not only the one that decides
        suppression.

        A stamp whose kill time is sane but whose INCIDENT START is in the
        future — the shape a step leaves behind if only `p_kill` is looked at —
        reaches the «still broken» reminder and prints «مستمر منذ دقائق عددها:
        -٦٠». The minus sign alone scrambles an Arabic line in his client, and
        the sentence is false besides.
        """
        t0 = 1_800_000_000
        assert spoken.fire(t0) is True
        stamp = spoken.state_dir / f"{UNIT}.watchdog"
        stamp.write_text(f"{t0 + 4 * 3600} {t0} {t0 - REPEAT_S} 7\n", encoding="utf-8")
        assert spoken.fire(t0 + 60) is True
        message = texts()[-1]
        assert not re.search(r"-[٠-٩]", message), message
        assert "ما زال" not in message, message


class TestTheStateUpdateIsAtomic:
    """DEFECT 2 — read/modify/write with no lock.

    Eight simultaneous invocations of the same unit and verdict in the same
    second, against the shipped script:

        sends: 3 of 8      stamp: 200000 200000 200000 2   ← KILLS says 2

    The send direction is fail-open (extra messages, never silence), so this is
    the milder of the two. And it is HARD to reach in production, which is said
    plainly here rather than dressed up: systemd serialises one unit's
    ExecStopPost= hooks, so the eight racing copies below are not a shape the
    worker produces on its own. What it takes is two units failing into the same
    key — or, more realistically, a human running the script by hand while the
    unit is flapping.

    What is NOT fail-open is the counter. `KILLS` is the number the reminder
    quotes to the operator as «مرات التعطل منذ أول إنذار», and it is his only
    measure of how hard the thing is failing. A lost update makes it say 2 after
    eight kills — an under-count, in the reassuring direction, in the one line
    that is supposed to tell him this is getting worse. `flock` on the stamp
    removes both at once and costs nothing on the uncontended path.
    """

    def _burst(self, alerts: Alerts, now: int, count: int = 8) -> int:
        running = [alerts.spawn(now) for _ in range(count)]
        return sum(_sent(subprocess.CompletedProcess(
            proc.args, proc.wait(timeout=60), *proc.communicate(timeout=60),
        )) for proc in running)

    def test_a_simultaneous_first_burst_speaks_once_and_counts_all_of_them(
        self, alerts: Alerts
    ) -> None:
        sends = self._burst(alerts, 200_000)
        assert alerts.kills() == 8, (
            f"eight kills were recorded as {alerts.kills()} — the reminder will "
            "under-report the failure rate to the operator"
        )
        assert sends == 1, f"{sends} of 8 raced past the first-alert decision"

    def test_a_burst_after_the_first_alert_is_still_silent(
        self, alerts: Alerts
    ) -> None:
        """The half that was already correct, kept correct.

        Once a stamp exists, all eight read it and all eight suppress — the
        suppression path never had the race. A lock that made this noisy would
        be trading one defect for the storm the whole mechanism exists to stop.
        """
        assert alerts.fire(200_000) is True
        assert self._burst(alerts, 200_100) == 0
        assert alerts.kills() == 9

    def test_a_burst_never_loses_the_incident_start(self, alerts: Alerts) -> None:
        """`FIRST_TS` is what «مستمر منذ دقائق عددها» is measured from. A lost
        update there would re-date the incident to the moment of the race and
        report an hours-old outage as minutes old."""
        t0 = 1_800_000_000
        assert alerts.fire(t0) is True
        self._burst(alerts, t0 + KILL_PERIOD_S)
        first, _kill, _sent_at, _kills = next(iter(alerts.stamps().values()))
        assert first == t0, "the incident lost its own start time"


class TestTheStormCapStillHolds:
    """The property both fixes had to leave standing.

    A permanently wedged worker restarts every 305s forever — 86400/305 ≈ 283
    ExecStopPost= invocations a day, every one of which used to be an identical
    Telegram. The cap is what makes the channel survivable, and a fail-open
    change is exactly the kind of change that quietly removes it.
    """

    def test_the_repeat_window_is_a_human_volume_and_still_suppresses(self) -> None:
        assert 86400 / REPEAT_S <= 24, "still a storm"
        assert REPEAT_S >= 10 * KILL_PERIOD_S, (
            "the window no longer covers ten kill cycles, so it suppresses "
            "almost nothing"
        )
        # An incident must not be declared over while its reminder is pending,
        # or «still broken» and «broke again» can both be true of one failure.
        assert 2 * KILL_PERIOD_S < INCIDENT_GAP_S < REPEAT_S

    def test_four_hours_of_wedge_is_four_messages(self, alerts: Alerts) -> None:
        """The arithmetic, executed against the script rather than asserted
        about it: 48 watchdog kills, four messages."""
        t0 = 1_800_000_000
        kills = list(range(0, 4 * 3600 + 1, KILL_PERIOD_S))
        assert len(kills) == 48
        sends = sum(alerts.fire(t0 + offset) for offset in kills)
        assert sends == 4, f"{len(kills)} kills produced {sends} messages"
        assert alerts.kills() == 48

    def test_the_reminder_still_says_STILL_and_still_carries_the_count(
        self, spoken: Alerts
    ) -> None:
        """Suppression is only tolerable because the message that ends it says
        what it stands for. Reaching «still» needs CONTINUOUS kills: jumping
        straight to the repeat window means the unit was quiet for an hour,
        which is «broke again» and rightly says so."""
        t0 = 1_800_000_000
        for offset in range(0, REPEAT_S + KILL_PERIOD_S, KILL_PERIOD_S):
            spoken.fire(t0 + offset)
        assert len(texts()) == 2, texts()
        message = texts()[-1]
        assert "ما زال" in message, message
        assert "١٣" in message, message      # thirteen kills, counted not guessed
        assert "٦١" in message, message      # 12 × 305s = 3660s, reported honestly

    def test_a_failure_that_returns_after_quiet_still_says_AGAIN(
        self, spoken: Alerts
    ) -> None:
        """The other sentence, and the one a clock fix is most likely to break:
        «broke again» is now one branch of a three-way decision instead of
        two."""
        t0 = 1_800_000_000
        assert spoken.fire(t0) is True
        assert spoken.fire(t0 + INCIDENT_GAP_S + 600) is True
        message = texts()[-1]
        assert "من جديد" in message, message
        assert "ما زال" not in message, message
        assert "٢٥" in message, message      # 1500s of health = 25 minutes

    def test_an_unreadable_state_directory_still_fails_open(
        self, alerts: Alerts, tmp_path: Path
    ) -> None:
        """No lock and no clock check may become a new way to go quiet: if the
        state cannot be kept at all, every kill speaks."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        alerts.env["CAREER_ALERT_STATE_DIR"] = str(blocker / "alerts")
        assert [alerts.fire(200_000 + n) for n in range(3)] == [True, True, True]

    def test_a_stamp_that_is_digits_but_not_a_NUMBER_does_not_wedge_the_cap(
        self, alerts: Alerts
    ) -> None:
        """The one bad-stamp shape that used not to self-heal.

        `008` is four ASCII digits, so it passes the «is this four integers»
        guard — and then bash reads it as octal, the arithmetic is a fatal
        expansion error, and the error unwinds the rest of the state block
        INCLUDING the write. The alert still goes out, so the direction was
        open; but the stamp was never replaced, so the next kill hit the same
        error, and the next. Every other garbage stamp is overwritten on first
        sight and the cap resumes; this one turned it off for that key until
        the next reboot — 283 messages a day, the storm the file exists to stop.

        It takes an outside writer to get there (this script only ever writes
        `date +%s`, which has no leading zero), which is the same standing as
        the other garbage-stamp cases: /run/career-alerts is root-owned tmpfs
        under umask 022 and the units carry no `User=`, so it is not a path a
        non-root process can take. It is a shape that must survive anyway.
        """
        assert alerts.fire(200_000) is True
        stamp = alerts.state_dir / f"{UNIT}.watchdog"
        stamp.write_text("008 008 008 1\n", encoding="utf-8")
        t0 = 1_800_000_000
        assert alerts.fire(t0) is True
        assert stamp.read_text().split()[:2] == [str(t0), str(t0)], (
            "the stamp was not rewritten, so this key's suppression is now "
            f"permanently off: {stamp.read_text()!r}"
        )
        assert alerts.fire(t0 + KILL_PERIOD_S) is False, "the cap never came back"

    def test_a_garbage_stamp_still_fails_open(self, alerts: Alerts) -> None:
        """Including a stamp whose fields are garbage in the new clock's eyes."""
        assert alerts.fire(200_000) is True
        stamp = next(iter(alerts.state_dir.glob(f"{UNIT}.watchdog")))
        for content in ("", "\n", "not a stamp at all\n", "1 2\n",
                        "1 2 3 4 5 6\n", "-1 -1 -1 -1\n", "x 200000 200000 1\n"):
            stamp.write_text(content, encoding="utf-8")
            assert alerts.fire(200_010) is True, f"silenced by: {content!r}"
