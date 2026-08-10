"""The §6 timer check could not pass when run by the thing it was checking.

On 2026-08-09 at 04:00:06 ``career-verify-restore.service`` — started by
``career-verify-restore.timer`` — read that timer and reported:

    ✗ career-verify-restore.timer: enabled/active next='none' — it will never fire

``systemctl list-timers`` said the next run was the following Sunday, and it
was. systemd does not schedule a timer's next elapse while the unit that timer
triggers is still running, so ``NextElapseUSecRealtime`` is empty for exactly as
long as the job takes — and the job, here, is the reader.

The cost was not one wrong line. That failure is what put the whole nine-check
probe into ``failed``, and it is the unit whose entire purpose is to tell the
operator that the host has drifted from the repository. Seven of its eight other
complaints that morning were TRUE and unactioned (two units in the repository
and not installed, four installed copies differing from the repository). **A
report with a false line at the top is a report an operator learns to skip**,
which is the failure mode every guard in this repository is written against.

Like ``tests/test_verify_restore_alert_hook.py``, this drives the REAL function
by sourcing the script with ``VERIFY_RESTORE_LIB_ONLY=1``. Nothing here
re-implements the verdict: a test that modelled the rule instead of running it
would have agreed with the bug, because the bug WAS the rule.

Every input is a value observed on this host (systemd 255.4-1ubuntu8.16): the
microsecond stamp is a real ``NextElapseUSecRealtime``, and the empty string is
what the same property printed while the job ran.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_restore.sh"
#: The script is bash and the classifier uses bash-only syntax; running it under
#: `sh` would silently test a different language.
BASH = shutil.which("bash") or "/bin/bash"

TIMER = "career-verify-restore.timer"
#: A real `NextElapseUSecRealtime` from `systemctl show` on this host.
SCHEDULED = "1786852800000000"
#: And what the same property prints while the triggered job is running. Not
#: invented — it is the value that produced the 2026-08-09 false alarm.
NOT_YET = ""


def _verdict(
    *,
    enabled: str = "enabled",
    active: str = "active",
    next_elapse: str = SCHEDULED,
    job_state: str = "inactive",
    unit: str = TIMER,
) -> tuple[int, str]:
    """Run the real function with `pass`/`bad` stubbed; report verdict and code.

    The exit code carries as much as the text: `bad` is what fails the probe
    and pages the operator, so «this is not a failure» has to be visible as a
    zero, not only as friendlier wording.
    """
    program = f"""
VERIFY_RESTORE_LIB_ONLY=1
export VERIFY_RESTORE_LIB_ONLY
source {SCRIPT}
pass() {{ printf 'PASS|%s\\n' "$1"; }}
bad()  {{ printf 'BAD|%s\\n' "$1"; fail=1; }}
fail=0
classify_timer_liveness "$1" "$2" "$3" "$4" "$5"
exit $fail
"""
    proc = subprocess.run(  # noqa: S603 — fixed argv, our own shell snippet
        [BASH, "-c", program, "bash", unit, enabled, active, next_elapse, job_state],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


def test_the_probe_can_pass_when_its_own_timer_started_it() -> None:
    """The 2026-08-09 incident, replayed exactly."""
    code, out = _verdict(next_elapse=NOT_YET, job_state="active")
    assert code == 0, (
        "the probe still fails itself: a timer whose own job is running has no "
        f"next elapse BY DESIGN, and this verdict called it dead — {out}"
    )
    assert out.startswith("PASS|")
    # …and it must not pretend it read a schedule it never read.
    assert "not scheduled YET" in out
    assert "its own job is running" in out


def test_a_timer_with_no_schedule_and_no_running_job_still_fails() -> None:
    """The whole point of the check, and it is NOT weakened."""
    code, out = _verdict(next_elapse=NOT_YET, job_state="inactive")
    assert code == 1
    assert out.startswith("BAD|")
    assert "it will never fire" in out


def test_a_disabled_timer_fails_even_while_its_job_runs() -> None:
    """A hand-started job does not make a disabled timer a live schedule.

    This is the shape the exemption could most easily have swallowed: someone
    runs the service manually, the timer is disabled underneath, and «its job is
    running» would excuse it. It must not.
    """
    for enabled, active in (("disabled", "active"), ("enabled", "inactive")):
        code, out = _verdict(
            enabled=enabled, active=active, next_elapse=NOT_YET, job_state="active"
        )
        assert code == 1, f"{enabled}/{active} was excused: {out}"
        assert "it will never fire" in out


def test_a_scheduled_timer_passes_and_says_so_plainly() -> None:
    code, out = _verdict(next_elapse=SCHEDULED, job_state="inactive")
    assert code == 0
    assert "next run scheduled" in out
    # The unqualified pass must not carry the running-job caveat — the two
    # verdicts are different claims and an operator reads the difference.
    assert "not scheduled YET" not in out


def test_every_transient_job_state_systemd_prints_is_covered() -> None:
    """`is-active` has five non-dead answers; three of them are mid-run.

    Written as a table because the failure would be silent: a state this
    function does not know falls through to «it will never fire», which is the
    original bug with a different trigger.
    """
    for state in ("active", "activating", "reloading", "deactivating"):
        code, out = _verdict(next_elapse=NOT_YET, job_state=state)
        assert code == 0, f"job state {state!r} was treated as a dead timer: {out}"
    for state in ("inactive", "failed"):
        code, out = _verdict(next_elapse=NOT_YET, job_state=state)
        assert code == 1, f"job state {state!r} excused a timer with no schedule: {out}"


def test_the_loop_asks_systemd_which_unit_the_timer_triggers() -> None:
    """Not derived by swapping `.timer` for `.service`.

    `Unit=` is the property that decides what a timer starts, and a timer may
    name a service that is not its own basename. Deriving it would work on all
    five units here and break on the first one that does not follow the
    convention — a guard that is right by coincidence.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'systemctl show "$unit" -p Unit --value' in source
    assert "classify_timer_liveness" in source
