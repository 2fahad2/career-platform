"""The one classification in ``scripts/verify_restore.sh`` that has been wrong
three times, asked of the script's own code.

It reads what systemd LOADED for the watchdog alert hook and decides whether the
hook's leading ``-`` is there. Every previous failure was the same shape: a
pattern that was *reasoned about* instead of *run against a host*, so the check
answered confidently and wrongly.

  * it matched ``ignore_exit_status=``, a field systemd has never printed, so
    every host fell through to «loaded WITHOUT the leading -» — the exact
    opposite of the truth, on a correctly deployed machine (2026-08-07);
  * the repair added the two real spellings but left the fall-through pointing
    at the same accusing branch, while the comment above it claimed an
    unrecognised spelling now landed on a «could not tell» verdict. Comment and
    code disagreed, and the comment was the one a half-awake operator would
    believe at 05:00 on a Sunday;
  * and the whole thing was a single ``case`` over the concatenated property
    value while systemd prints one ``{ … }`` record PER ENTRY. The globs spanned
    record boundaries, so with two entries the verdict depended on the order
    they were written in — and one order returned PASS for a hook that was
    missing its ``-``. A false PASS about the single fatality this check exists
    to catch (2026-08-08).

So this drives the REAL classifier: it sources ``scripts/verify_restore.sh``
with ``VERIFY_RESTORE_LIB_ONLY=1`` — which defines the functions and stops
before any check touches the host — and calls ``classify_alert_hook``. Nothing
here re-implements or re-types the classification; a test that models the script
instead of running it would have passed on all three bugs.

Every input string is copied verbatim from ``systemctl show`` on this host
(systemd 255.4-1ubuntu8.16, 2026-08-08). The point is that they are evidence,
not paraphrase.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_restore.sh"
#: The script's own shebang is `#!/usr/bin/env bash` and the classifier uses
#: bash-only syntax — running it under `sh` would silently test a different
#: language, so the interpreter is resolved rather than assumed.
BASH = shutil.which("bash") or "/bin/bash"

UNIT = "career-worker.service"

#: One `ExecStopPost` record for the alert hook, exactly as
#: `systemctl show career-worker.service -p ExecStopPost` prints it on this host
#: with the leading `-` present in the unit file.
ARMED = (
    "{ path=/root/career/scripts/alert_unit_failure.sh ; "
    "argv[]=/root/career/scripts/alert_unit_failure.sh career-worker.service ; "
    "ignore_errors=yes ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
    "code=(null) ; status=0/0 }"
)
#: The same record as systemd prints it when the `-` is NOT there. Not invented:
#: it is what every un-prefixed Exec* line on this host shows.
NO_DASH = ARMED.replace("ignore_errors=yes", "ignore_errors=no")
#: `-p ExecStopPostEx`, the newer property, both ways. Also read off the host —
#: `ExecStopPostEx` for career-worker.service says `flags=ignore-failure` today.
ARMED_EX = ARMED.replace("ignore_errors=yes", "flags=ignore-failure")
NO_DASH_EX = ARMED.replace("ignore_errors=yes", "flags=")

#: The 2026-08-07 bug, preserved as an input: a field name this script does not
#: know. Any future systemd rename looks exactly like this to the check.
UNKNOWN_SPELLING = ARMED.replace("ignore_errors=yes", "ignore_exit_status=1")

#: A SECOND, unrelated `ExecStopPost=` entry that legitimately carries its `-`.
#: This is the whole 2026-08-08 defect: nothing about this entry says anything
#: about the alert hook, but its `ignore_errors=yes` sat in the same string.
OTHER_ARMED = (
    "{ path=/usr/bin/systemd-notify ; "
    "argv[]=/usr/bin/systemd-notify --status=stopped ; "
    "ignore_errors=yes ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
    "code=(null) ; status=0/0 }"
)


def _show(*records: str, prop: str = "ExecStopPost", load: str = "loaded") -> str:
    """Assemble the exact bytes `systemctl show -p …` would print.

    One property line per Exec entry, in the order they are declared, and
    `LoadState=` last — the layout observed on this host.
    """
    lines = [f"{prop}={record}" for record in records]
    if load is not None:
        lines.append(f"LoadState={load}")
    return "\n".join(lines)


def _verdict(show: str, unit: str = UNIT) -> tuple[int, str]:
    """Run the real function with `pass`/`bad` stubbed, and report what it said.

    The exit code matters as much as the text: `bad` is what makes
    career-verify-restore.timer page on Sunday morning, so «could not tell»
    being non-zero is part of the behaviour under test, not decoration.
    """
    program = f"""
VERIFY_RESTORE_LIB_ONLY=1
export VERIFY_RESTORE_LIB_ONLY
source {SCRIPT}
pass() {{ printf 'PASS|%s\\n' "$1"; }}
bad()  {{ printf 'BAD|%s\\n' "$1"; fail=1; }}
fail=0
classify_alert_hook "$1" "$2"
exit $fail
"""
    # Fixed argv, no `shell=True`, absolute interpreter. The variable parts are
    # passed as $1/$2 rather than interpolated into the program text — which is
    # also what makes this a faithful stand-in for `systemctl show`: the
    # classifier must handle the property value as data, not as code.
    proc = subprocess.run(  # noqa: S603 — fixed argv, our own shell snippet
        [BASH, "-c", program, "bash", unit, show],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not proc.stderr, proc.stderr
    return proc.returncode, proc.stdout.strip()


def test_sourcing_the_script_runs_no_check() -> None:
    """The seam the rest of this file stands on.

    `VERIFY_RESTORE_LIB_ONLY=1` must define the functions and touch nothing:
    this file is otherwise nine sections of docker/psql/systemctl/curl against
    the live host, and a test suite that ran them would be a test suite that
    inspects production every time it runs.
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv
        [BASH, "-c", f'VERIFY_RESTORE_LIB_ONLY=1 source {SCRIPT}; declare -F classify_alert_hook'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "classify_alert_hook" in proc.stdout
    # No section banner, no ✓, no ✗ — nothing ran.
    assert "▶" not in proc.stdout, proc.stdout
    assert "✓" not in proc.stdout and "✗" not in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    ("record", "prop"),
    [(ARMED, "ExecStopPost"), (ARMED_EX, "ExecStopPostEx")],
    ids=["ExecStopPost", "Ex"],
)
def test_the_deployed_shape_passes(record: str, prop: str) -> None:
    """The regression that started it: this host is correct and must be told so."""
    code, out = _verdict(_show(record, prop=prop))
    assert code == 0, out
    assert out.startswith("PASS|"), out
    assert "armed" in out


@pytest.mark.parametrize(
    ("record", "prop"),
    [(NO_DASH, "ExecStopPost"), (NO_DASH_EX, "ExecStopPostEx")],
    ids=["ExecStopPost", "Ex"],
)
def test_a_real_missing_dash_is_accused_by_name(record: str, prop: str) -> None:
    """The accusation is still made — but only on systemd's own «no»."""
    code, out = _verdict(_show(record, prop=prop))
    assert code == 1, out
    assert "WITHOUT the leading" in out
    # An accusation without the repair is a page at 05:00 and nothing to do.
    assert f"ops/systemd/{UNIT}" in out
    assert "daemon-reload" in out


@pytest.mark.parametrize(
    ("order", "records"),
    [("hook-first", (NO_DASH, OTHER_ARMED)), ("hook-last", (OTHER_ARMED, NO_DASH))],
    ids=["hook-first", "hook-last"],
)
def test_a_neighbouring_entry_cannot_vouch_for_the_hook(
    order: str, records: tuple[str, ...]
) -> None:
    """The false PASS, both ways round.

    Two `ExecStopPost=` entries: the alert hook missing its `-`, and an
    unrelated entry that has one. Before 2026-08-08 the classifier globbed the
    whole concatenated value, so `*alert_unit_failure.sh*ignore_errors=yes*`
    matched the hook's path in one record against the OTHER record's
    `ignore_errors=yes`, and `hook-first` printed

        PASS|career-worker.service: watchdog alert hook armed and non-fatal

    while `hook-last` correctly accused. A check whose verdict depends on the
    order entries were typed in is not a check, and the order that PASSes is the
    one that certifies the fatality. Both orders must accuse.
    """
    code, out = _verdict(_show(*records))
    assert code == 1, f"{order}: FALSE PASS — {out}"
    assert "WITHOUT the leading" in out, f"{order}: {out}"


def test_a_neighbouring_entry_cannot_incriminate_the_hook_either() -> None:
    """The mirror image: a healthy hook beside a fatal unrelated entry.

    The old glob could reach across the boundary in this direction too and
    accuse a unit file that was already right — the 2026-08-07 false alarm, with
    a different cause.
    """
    other_fatal = OTHER_ARMED.replace("ignore_errors=yes", "ignore_errors=no")
    for records in ((ARMED, other_fatal), (other_fatal, ARMED)):
        code, out = _verdict(_show(*records))
        assert code == 0, out
        assert out.startswith("PASS|"), out


def test_an_unknown_flag_spelling_says_it_could_not_tell() -> None:
    """The fix. Before it, this input produced «loaded WITHOUT the leading -»
    about a hook whose state the script had no information about at all — a
    diagnosis invented from a fall-through, which is how a real Sunday morning
    gets spent chasing a unit file that was already right."""
    code, out = _verdict(_show(UNKNOWN_SPELLING))
    assert "WITHOUT the leading" not in out, (
        "an unrecognised flag spelling is being reported as a proven missing "
        f"'-': {out}"
    )
    assert "CANNOT VERIFY" in out, out
    # Loud, in the only way this script has: a blind check is not a green one,
    # and the weekly unattended run must page rather than print a shrug.
    assert code == 1, out
    # A verdict that shrugs is only useful if it says what to do next.
    assert f"systemctl show {UNIT}" in out
    assert "scripts/verify_restore.sh" in out


def test_a_missing_hook_is_still_its_own_verdict() -> None:
    """«Blind» must not swallow «absent» — they need different sentences.

    systemd omits an empty Exec array entirely, so a loaded unit with no
    ExecStopPost prints only `LoadState=loaded` (verified against caddy.service
    on this host).
    """
    code, out = _verdict(_show(load="loaded"))
    assert code == 1, out
    assert "NO ExecStopPost alert hook loaded" in out


def test_an_unanswerable_unit_is_not_reported_as_a_missing_hook() -> None:
    """A unit systemd does not have answers `LoadState=not-found` — with exit
    status 0, and with both Exec properties absent, which is byte-identical to
    a unit that genuinely has no hook.

    Saying «NO ExecStopPost alert hook loaded» there is a confident answer to a
    question that was never asked, and it sends the operator to edit a unit file
    instead of to find out why the unit is not loaded. Different failure,
    different sentence — still a failure.
    """
    code, out = _verdict(_show(load="not-found"), unit="career-worker-typo.service")
    assert code == 1, out
    assert "could not ask systemd" in out, out
    assert "not-found" in out, out
    assert "NO ExecStopPost alert hook loaded" not in out, out


def test_systemd_answering_nothing_at_all_is_the_same_verdict() -> None:
    """`systemctl show` printing nothing (manager unreachable in the weekly
    unattended run) must not read as «this unit has no alert hook»."""
    code, out = _verdict("")
    assert code == 1, out
    assert "could not ask systemd" in out, out
    assert "NO ExecStopPost alert hook loaded" not in out, out


def test_the_older_property_going_silent_cannot_hide_a_loaded_hook() -> None:
    """`ExecStopPost` is what this check reads today. If a future systemd stops
    answering it and only answers `ExecStopPostEx`, the hook is still loaded —
    the check must classify it, not report it missing."""
    code, out = _verdict(_show(ARMED_EX, prop="ExecStopPostEx"))
    assert code == 0, out
    assert "armed" in out


def test_the_five_verdicts_are_five_distinct_sentences() -> None:
    """Five inputs, five answers. A classifier whose branches collapse onto one
    message reads as green-or-broken and loses every distinction this test
    exists to protect."""
    said = {
        _verdict(_show(ARMED))[1],
        _verdict(_show(NO_DASH))[1],
        _verdict(_show(UNKNOWN_SPELLING))[1],
        _verdict(_show(load="loaded"))[1],
        _verdict(_show(load="not-found"))[1],
    }
    assert len(said) == 5, said


#: scripts/gate.sh greps its host-gate verdict for this exact prefix — a live
#: check that could not run must say so rather than skip quietly. Spelled out
#: here rather than imported so this file does not depend on another module's
#: internals; the ENV VAR and this sentence are the contract.
REQUIRE_LIVE_FAILURE = "CAREER_REQUIRE_LIVE_UNITS is set but"


def _require_live() -> bool:
    return os.environ.get("CAREER_REQUIRE_LIVE_UNITS", "") not in ("", "0")


@pytest.mark.livehost
@pytest.mark.parametrize("unit", ["career-worker.service", "career-admin-bot.service"])
def test_the_live_units_classify_as_armed(unit: str) -> None:
    """End to end on the machine this runs on: the bytes systemd prints TODAY,
    through the classifier, with nothing in between.

    Every fixture above is a copy of this output, and a copy is a claim about
    the past. This is the claim about the present — the check all three earlier
    bugs would have failed, and the reason this file cannot drift away from the
    host again.

    WHICH GATE. It reads the machine, so it carries `@pytest.mark.livehost` and
    the ship gate does not judge push-safety by it. On a machine that deploys
    (`CAREER_REQUIRE_LIVE_UNITS=1`) «could not read the host» is a failure, not
    a skip: an unaskable check is not a passing one.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        if _require_live():
            pytest.fail(f"{REQUIRE_LIVE_FAILURE}: there is no systemctl on this machine")
        pytest.skip("no systemd here — not the deployed host")
    probe = subprocess.run(  # noqa: S603 — fixed argv
        # The exact argv scripts/verify_restore.sh section 6 uses.
        [systemctl, "show", unit, "-p", "LoadState", "-p", "ExecStopPost", "-p", "ExecStopPostEx"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0 or "LoadState=loaded" not in probe.stdout:
        if _require_live():
            pytest.fail(f"{REQUIRE_LIVE_FAILURE}: systemd does not have {unit} loaded")
        pytest.skip(f"{unit} is not loaded here — not the deployed host")
    code, out = _verdict(probe.stdout, unit=unit)
    assert code == 0, f"live {unit}: {out}\n--- systemd said ---\n{probe.stdout}"
    assert out == f"PASS|{unit}: watchdog alert hook armed and non-fatal", out
