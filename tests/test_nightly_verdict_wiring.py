"""The nightly's verdict, tested where it is actually assembled.

Two guards live here rather than beside their neighbours in
`test_engine_cli_exit.py`, because both of them are about `main()` and
`main()` is the one function in `career.engine.cli` that no test ever ran.
It carries `# pragma: no cover — thin`, and «thin» was true of the code and
false of the risk: the four live nights of 21, 22 and 23 July and 2 August
2026 that closed a real customer WHATSAPP_FAILED and exited 0 were not a bug
in `exit_code_for` and not a bug in `summarize_delivery`. Both were correct.
The bug was the wiring between them, and the tests written for the fix
re-implemented that wiring in their own bodies:

    verdict = cli.exit_code_for(
        "completed",
        [*summary["delivery"].values(),
         *(row["state"] for row in summary["delivery_expired"])],
    )

That list comprehension is a verbatim copy of four lines inside `main()`. It
passes whether or not `main()` contains them — it would have passed on 20 July,
against the code that then failed a paying customer four times. A test that
would pass against the code it was written to condemn is not evidence.

So the composition is not copied here. It is LIFTED OUT OF `main()` AND RUN:
the assignment is read from the source, compiled, and evaluated against the
real output of `summarize_delivery`. Delete the expired half from `main()` and
this fails; edit it to read the wrong key and this fails.

What this still cannot do is run `main()` itself — it opens two owner engines,
calls SearchAPI, Anthropic and Meta, and sweeps subscriptions. The change that
would let a test call the real thing is small and belongs in the source:

    def closed_states_for_verdict(summary: dict[str, Any]) -> list[str]:
        \"\"\"Every tenant-day this run closed — tonight's, and the previous
        days the stale-bundle sweep finished off.\"\"\"
        return [
            *summary.get("delivery", {}).values(),
            *(row["state"] for row in summary.get("delivery_expired", [])),
        ]

with `main()` reduced to `exit_code_for(report.status,
closed_states_for_verdict(summary), delivery_phase=…)`. Then this file tests a
function instead of an expression, and the AST reading below can go away. That
is an edit to `src/`, which this pass does not make.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import date

import pytest

from career.engine import cli

_CLI_SOURCE = pathlib.Path(cli.__file__)

#: The local in `main()` that holds every day-state the verdict is computed
#: over. Named here so the failure message can say what to do when it moves.
_COMPOSITION_LOCAL = "closed_states"


def _composition_from_main() -> ast.expr:
    """The right-hand side of `closed_states = …`, taken out of `main()`.

    Reading source rather than importing a function is not the shape anybody
    wants. It is the shape available: the composition is an expression inside
    an untestable function, and the alternative on offer — copying it into the
    test — is what produced a green suite over four failed nights.
    """
    tree = ast.parse(_CLI_SOURCE.read_text(encoding="utf-8"), filename=str(_CLI_SOURCE))
    main = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "main"
        ),
        None,
    )
    assert main is not None, "career.engine.cli.main is gone — read this file's docstring"
    for node in ast.walk(main):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == _COMPOSITION_LOCAL
        ):
            return node.value
    pytest.fail(
        f"main() no longer assigns `{_COMPOSITION_LOCAL}`. If the composition "
        "was extracted into a named function — the right move, see this "
        "file's docstring — import and test that function directly and delete "
        "this reader. If it was merely renamed, rename _COMPOSITION_LOCAL. If "
        "it was inlined into the exit_code_for call, put it back: the four "
        "nights of 21/22/23 July and 2 August 2026 exited 0 because nothing "
        "could see this expression."
    )


def _run_composition(summary: dict) -> list[str]:
    """Evaluate `main()`'s own expression over a summary. No copy, no stand-in."""
    node = ast.Expression(body=_composition_from_main())
    ast.fix_missing_locations(node)
    return list(eval(compile(node, str(_CLI_SOURCE), "eval"), {"summary": summary}))  # noqa: S307


def _summary(*, served: dict, expired: list) -> dict:
    """A real `summarize_delivery` output, so both ends of the wire are real."""
    codes = {}
    states = {}
    for code, state in served.items():
        tid = uuid.uuid4()
        codes[tid] = code
        states[tid] = state
    expired_rows = []
    for code, day, state in expired:
        tid = uuid.uuid4()
        codes[tid] = code
        expired_rows.append((tid, day, state))
    return cli.summarize_delivery(
        tenant_codes=codes,
        intended=list(states),
        states=states,
        expired=expired_rows,
    )


# ── the wiring that was broken ───────────────────────────────────────────────


def test_main_computes_its_verdict_over_both_delivery_channels() -> None:
    """LIVE EVIDENCE. `expire_stale_held_deliveries` stamps the day state at
    01:3x UTC — the NEXT morning's sweep, not the failed day's run — so those
    tenants never appear in tonight's `states` map. Reading only
    `summary["delivery"]` therefore saw an empty dict and called the night
    clean, four times, while a real customer received nothing.

    The list is built by `main()`'s own code here, not by this test's."""
    summary = _summary(
        served={},
        expired=[("TEN-0002", date(2026, 8, 4), "WHATSAPP_FAILED")],
    )
    assert summary["delivery"] == {}          # the empty dict that lied
    assert _run_composition(summary) == ["WHATSAPP_FAILED"]
    assert cli.exit_code_for(
        "completed", _run_composition(summary), delivery_phase=cli.PHASE_RAN,
    ) == cli.EXIT_DELIVERY_FAILED


def test_a_tenant_that_was_both_expired_and_served_contributes_twice() -> None:
    """Yesterday expired at 04:30, today delivered a minute later. Keyed by
    tenant the two days overwrite each other and the loser is always the
    failure — so the composition must carry both, not merge them."""
    summary = _summary(
        served={"TEN-0002": "DELIVERED"},
        expired=[("TEN-0002", date(2026, 8, 4), "WHATSAPP_FAILED")],
    )
    assert sorted(_run_composition(summary)) == ["DELIVERED", "WHATSAPP_FAILED"]
    assert cli.exit_code_for(
        "completed", _run_composition(summary), delivery_phase=cli.PHASE_RAN,
    ) == cli.EXIT_DELIVERY_FAILED


def test_an_ordinary_good_night_still_exits_zero_through_the_same_path() -> None:
    """The other side of the line, through `main()`'s expression as well: a
    guard that only ever proves «this fails» gets muted the first Friday it
    pages for a Riyadh weekend."""
    served_only = _summary(served={"TEN-0002": "DELIVERED"}, expired=[])
    assert _run_composition(served_only) == ["DELIVERED"]
    assert cli.exit_code_for(
        "completed", _run_composition(served_only), delivery_phase=cli.PHASE_RAN,
    ) == cli.EXIT_OK

    quiet = _summary(served={}, expired=[])
    assert _run_composition(quiet) == []
    assert cli.exit_code_for(
        "completed", _run_composition(quiet), delivery_phase=cli.PHASE_RAN,
    ) == cli.EXIT_OK


def test_the_composition_survives_a_summary_missing_a_channel() -> None:
    """`main()` reads both keys with `.get`. An older summary shape — or a
    partial one built on an early return — must not raise inside the last four
    lines of the nightly, because a traceback there is a run that already did
    all its work and then reports nothing at all."""
    assert _run_composition({}) == []
    assert _run_composition({"delivery": {"TEN-0002": "DELIVERED"}}) == ["DELIVERED"]


# ── the exit vocabulary ──────────────────────────────────────────────────────


def _declared_exit_codes() -> dict[str, int]:
    return {
        name: value
        for name, value in vars(cli).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }


def test_every_exit_code_is_a_different_number() -> None:
    """This replaces `test_the_missing_token_gets_its_own_runbook_number`,
    which asserted `EXIT_NO_WHATSAPP_TOKEN not in (EXIT_OK,
    EXIT_DISCOVERY_FAILED, EXIT_NO_SEARCH_KEY, EXIT_DELIVERY_FAILED)`.

    That is `4 not in (0, 1, 2, 3)` — true of the numbers, and it names four of
    the five constants by hand, so the fifth and sixth exit code somebody adds
    are outside it by construction. Two codes pointing at one runbook entry is
    the failure it was reaching for; pairwise over everything declared is that
    property, and it keeps holding as the vocabulary grows.
    """
    codes = _declared_exit_codes()
    assert len(codes) >= 5, f"only {sorted(codes)} declared — did they move?"
    collisions = {
        value: sorted(n for n, v in codes.items() if v == value)
        for value in set(codes.values())
        if list(codes.values()).count(value) > 1
    }
    assert not collisions, (
        f"two exit codes share one number: {collisions} — `systemctl status` "
        "shows the operator the number, and one number must send him to one "
        "runbook entry"
    )


def test_every_exit_code_is_reachable() -> None:
    """A number nothing returns is a runbook entry nobody will ever need, and
    it makes the vocabulary look larger than the product's honesty actually is.

    Four are driven through `exit_code_for`. `EXIT_NO_SEARCH_KEY` is returned
    by `main()` before any of the run happens — the one code that cannot be
    reached through the verdict function — so it is read out of `main()`'s
    source, the same way the composition above is.
    """
    produced = {
        cli.exit_code_for("completed", ["DELIVERED"], delivery_phase=cli.PHASE_RAN),
        cli.exit_code_for("discovery_failed", []),
        cli.exit_code_for("completed", ["WHATSAPP_FAILED"]),
        cli.exit_code_for("completed", [], delivery_phase=cli.PHASE_NO_CREDENTIALS),
    }
    tree = ast.parse(_CLI_SOURCE.read_text(encoding="utf-8"))
    returned_names = {
        node.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }
    unreachable = sorted(
        name
        for name, value in _declared_exit_codes().items()
        if value not in produced and name not in returned_names
    )
    assert not unreachable, (
        f"these exit codes are declared and never returned: {unreachable}"
    )


def test_every_exit_code_is_explained_where_it_is_declared() -> None:
    """«Its own runbook number» — the promise in the old test's name.

    There is no document in `docs/` that lists the nightly's exit codes; the
    comments around the declarations in `cli.py` are the whole runbook, and
    they are good ones. So this asserts what actually exists: every code is
    introduced by a comment, in the file, where the person reading
    `systemctl status` at 05:00 will find it. The day a real runbook section
    is written, point this at that file instead.
    """
    lines = _CLI_SOURCE.read_text(encoding="utf-8").splitlines()
    for name in _declared_exit_codes():
        index = next(
            (i for i, line in enumerate(lines) if line.startswith(f"{name} = ")), None
        )
        assert index is not None, f"{name} is not declared at module level"
        block = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].strip():
            block.append(lines[cursor])
            cursor -= 1
        assert any(line.lstrip().startswith("#") for line in block), (
            f"{name} has no comment above it — the number reaches the operator "
            "with nothing attached to it"
        )
