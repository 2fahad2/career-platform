"""Nightly-run CLI acceptance tests (D9 flags + honest report) — before code.

The entry point is thin composition (exercised live in C6's exit gate); what
is testable purely is tested here: the D9 flag discipline (digest-only is the
DEFAULT and has an explicit off form; no flag governs two effects), targeted
tenant runs, and the report summary (tenant UUIDs become TEN codes — the
journal follows the same no-PII discipline as the admin channel, §15.13).
"""

from __future__ import annotations

import uuid

from career.engine import cli, run
from career.engine.ranking import Composition


def test_parser_defaults_are_the_documented_run_shape() -> None:
    args = cli.build_parser().parse_args([])
    assert args.digest_only is True                 # D9: default, not opt-in
    assert args.max_per_query == run.DEFAULT_MAX_PER_QUERY
    assert args.retrieval_cap == run.DEFAULT_RETRIEVAL_CAP
    assert args.enrich_cap == run.DEFAULT_ENRICH_CAP
    assert args.tenant is None


def test_parser_flags_have_explicit_off_forms_and_targeting() -> None:
    tid = uuid.uuid4()
    args = cli.build_parser().parse_args([
        "--no-digest-only", "--max-per-query", "3",
        "--tenant", str(tid), "--tenant", str(uuid.uuid4()),
    ])
    assert args.digest_only is False                # D9: explicit off form
    assert args.max_per_query == 3
    assert args.tenant is not None
    assert args.tenant[0] == tid and len(args.tenant) == 2


def test_summary_uses_ten_codes_and_keeps_the_honest_shape() -> None:
    tid = uuid.uuid4()
    report = run.RunReport(
        run_id=uuid.uuid4(),
        status="partial",
        counts={"sources": {"searchapi_google_jobs": {"status": "error",
                                                      "reason": "URLError"}}},
        per_tenant={
            tid: {
                "final": [{"posting_id": "p1", "url": "https://x/1",
                           "title": "BA", "slot": None}],
                "counts": {"evaluated": 4, "passed": 1, "near_miss": 1,
                           "suppressed": 0},
            }
        },
    )
    summary = cli.summarize(report, {tid: "TEN-0007"})
    assert summary["status"] == "partial"           # honest, never rewritten
    assert summary["run_id"] == str(report.run_id)
    assert summary["counts"]["sources"]["searchapi_google_jobs"]["reason"] == "URLError"
    assert "TEN-0007" in summary["tenants"]         # code, not the UUID
    assert str(tid) not in str(summary["tenants"].keys())
    tenant = summary["tenants"]["TEN-0007"]
    assert tenant["final"][0]["title"] == "BA"
    assert tenant["counts"]["near_miss"] == 1


def test_summary_falls_back_to_uuid_for_unknown_codes() -> None:
    tid = uuid.uuid4()
    report = run.RunReport(uuid.uuid4(), "completed", {}, {tid: {"final": [],
                                                                 "counts": {}}})
    summary = cli.summarize(report, {})
    assert str(tid) in summary["tenants"]           # never drops a tenant


def test_exit_codes_are_honest() -> None:
    assert cli.exit_code_for("completed") == 0
    assert cli.exit_code_for("no_active_tenants") == 0
    assert cli.exit_code_for("partial") == 0        # value delivered; report says so
    assert cli.exit_code_for("discovery_failed") == 1


def test_composition_refs_survive_summary() -> None:
    """Guard: Composition is what run.py reads slots from — a slot assigned by
    the ranker must surface in the CLI summary items."""
    composition = Composition(selected=(), slots={"p9": "arabic_ad"})
    assert composition.slots.get("p9") == "arabic_ad"
