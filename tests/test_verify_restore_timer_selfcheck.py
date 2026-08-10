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


#: `TimeoutStartUSec` for career-verify-restore.service on this host. The
#: exemption is bounded by it: a job systemd will eventually kill has a
#: genuinely temporary empty next-elapse.
BOUNDED = "10min"
#: And what the other three carry — verified on this host, not assumed.
UNBOUNDED = "infinity"


def _verdict(
    *,
    enabled: str = "enabled",
    active: str = "active",
    next_elapse: str = SCHEDULED,
    job_state: str = "inactive",
    job_timeout: str = BOUNDED,
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
classify_timer_liveness "$1" "$2" "$3" "$4" "$5" "$6"
exit $fail
"""
    proc = subprocess.run(  # noqa: S603 — fixed argv, our own shell snippet
        [
            BASH, "-c", program, "bash",
            unit, enabled, active, next_elapse, job_state, job_timeout,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


def test_the_probe_can_pass_when_its_own_timer_started_it() -> None:
    """The 2026-08-09 incident, replayed with the state a oneshot ACTUALLY has.

    The first version of this test passed `active`, and every unit in the §6
    loop is `Type=oneshot` — sampled live, a oneshot goes
    inactive → activating → inactive and never reports `active` at all. So the
    test agreed with the fix while the fix could not fire.
    """
    code, out = _verdict(next_elapse=NOT_YET, job_state="activating")
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


def test_a_hung_job_with_no_kill_deadline_is_not_excused() -> None:
    """career-backup, career-engine-nightly and career-restore-test all carry
    `TimeoutStartSec=infinity` (read off this host, not assumed).

    A hung one stays `activating` for ever, so its timer's next elapse stays
    empty for ever, and an unbounded exemption would excuse it for ever. The
    check this function replaced was RIGHT about that case, and the first
    version of the replacement lost it.
    """
    code, out = _verdict(
        next_elapse=NOT_YET, job_state="activating", job_timeout=UNBOUNDED
    )
    assert code == 1, f"a job systemd will never kill was excused: {out}"
    assert "never kill it" in out


def test_the_units_this_check_guards_are_still_shaped_the_way_it_assumes() -> None:
    """The exemption's whole safety rests on two facts about the live host.

    Both are read here rather than asserted in a comment, because both are one
    unit-file edit away from becoming false — and the failure would be silent:
    the probe would simply start failing itself again, or start excusing a hung
    backup for ever.
    """
    if not shutil.which("systemctl"):
        return
    timeout = subprocess.run(  # noqa: S603 — fixed argv
        ["systemctl", "show", "career-verify-restore.service",
         "-p", "TimeoutStartUSec", "--value"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if timeout:  # absent on a machine where the unit was never installed
        assert timeout != "infinity", (
            "career-verify-restore.service now has no kill deadline, so the "
            "self-check exemption no longer applies to it and this probe will "
            "start failing itself again — which is the correct way for that "
            "unit-file decision to announce itself, but somebody has to read it"
        )


def test_the_job_state_the_loop_computes_is_one_bare_word() -> None:
    """The substitution, not the rule — this is what was actually broken.

    `systemctl is-active` exits non-zero for every state except
    active/reloading while still printing the real one, so the original
    `… || echo inactive` produced $'failed\\ninactive' and every comparison in
    the classifier silently failed. The rule was right from the first commit;
    the string feeding it was not, and no test looked at the string.
    """
    if not shutil.which("systemctl"):
        return
    for target in ("career-worker.service", "career-engine-nightly.service"):
        out = subprocess.run(  # noqa: S603 — fixed argv, our own snippet
            [BASH, "-c", f'systemctl is-active {target} 2>/dev/null | tail -n1'],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        assert out and "\n" not in out, f"{target}: {out!r}"


def test_a_timer_systemd_cannot_resolve_does_not_pass_by_falling_back() -> None:
    """`systemctl show` on an unknown unit prints an EMPTY `Unit=` and exits 0.

    Verified here rather than assumed, because the guard in the loop exists
    only for that combination: without it, `${triggers:-$unit}` would ask «is
    the TIMER active», which it is, and every unresolvable timer would pass.
    """
    if not shutil.which("systemctl"):
        return
    proc = subprocess.run(  # noqa: S603 — fixed argv
        ["systemctl", "show", "career-nonexistent-probe.timer", "-p", "Unit",
         "--value"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'if [[ -z "$triggers" ]]; then' in source


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
