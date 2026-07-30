"""Rehearse the 49-SAR analysis product end to end — purchase to report.

The funnel is the acquisition door: a prospect pays 49 SAR, uploads a CV, and
must receive a real Arabic report within minutes. Every part is covered by
unit tests, but the WHOLE journey had never been driven in one go, which is
exactly the shape of gap the 29-July live rehearsal exposed elsewhere.

This drives it with fake boundaries on the disposable career_test DB:
paid webhook → provision → activation → consent → path → CV upload →
extraction → deterministic scoring → Arabic PDF report + WhatsApp summary.

Run:  scripts/drill_cv_analysis.sh
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from career.config import get_settings


def main() -> int:
    settings = get_settings()
    if not settings.db_name.endswith("_test"):
        print(f"REFUSING: DB_NAME={settings.db_name!r} is not a *_test DB")
        return 3

    print("CV-analysis funnel rehearsal (49-SAR product)")
    # The E2E journey already exists as a test authority; drive THAT rather
    # than a parallel copy, so the drill can never drift from what CI proves.
    import pytest

    code = pytest.main([
        "-q", "--no-header",
        "tests/test_funnel_flow.py::test_purchase_to_report_in_one_conversation",
        "tests/test_funnel_flow.py::test_upgrade_relinks_and_opens_half_ready_onboarding",
    ])
    if code != 0:
        print("  ✗ the funnel journey did not complete")
        return 1
    print("  ✓ purchase → report landed in one conversation")
    print("  ✓ an upgrade from the same phone inherits the funnel tenant")
    print("rehearsal passed — the 49-SAR door works end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
