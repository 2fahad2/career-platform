"""The nightly-run entry point (whitepaper §06) — the systemd timer target.

Thin composition over the tested engine: Settings → owner engine →
``HttpSearchApiClient`` (D13) + ``PythonJobSpyClient`` + ``UrllibPageFetcher``
→ :func:`career.engine.run.run_nightly`. Flags follow D9: ``--digest-only``
is the DEFAULT (no send path exists in the engine at all — delivery is C7's)
and has an explicit ``--no-digest-only`` off form; no flag governs two
effects. The report goes to stdout as JSON with TEN codes instead of tenant
UUIDs (journal = same no-PII discipline as the admin channel, §15.13).

Exit codes are honest and boring: 0 when the run produced a truthful outcome
(completed / partial / no_active_tenants — the report carries the details),
1 only for discovery_failed (every source down: nothing was discovered).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from career.config import get_settings
from career.db.models import Tenant
from career.engine.enrichment import UrllibPageFetcher
from career.engine.run import (
    DEFAULT_ENRICH_CAP,
    DEFAULT_MAX_PER_QUERY,
    DEFAULT_RETRIEVAL_CAP,
    RunReport,
    run_nightly,
)
from career.engine.sources import HttpSearchApiClient, PythonJobSpyClient
from career.logging_filters import install_secret_redaction

logger = logging.getLogger("career.engine.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_nightly",
        description="One nightly discovery/gate/rank run (whitepaper §06).",
    )
    parser.add_argument(
        "--digest-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="record the run as digest-only (D9). DEFAULT until C7 delivery "
             "exists; --no-digest-only is the explicit off form.",
    )
    parser.add_argument("--max-per-query", type=int, default=DEFAULT_MAX_PER_QUERY)
    parser.add_argument("--retrieval-cap", type=int, default=DEFAULT_RETRIEVAL_CAP)
    parser.add_argument("--enrich-cap", type=int, default=DEFAULT_ENRICH_CAP)
    parser.add_argument(
        "--tenant", type=uuid.UUID, action="append", default=None,
        help="scope the run to specific tenant id(s); repeatable",
    )
    parser.add_argument(
        "--include-weekend",
        action="store_true",
        help="manual canary runs only — deliver on a Riyadh weekend too",
    )
    parser.add_argument(
        "--deliver",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the delivery phase after discovery (Sun-Thu, skipped "
             "automatically without WhatsApp credentials); --no-deliver is "
             "the explicit off form (D9).",
    )
    return parser


def summarize(
    report: RunReport, tenant_codes: dict[uuid.UUID, str]
) -> dict[str, Any]:
    """The stdout/journal shape: status honest, tenants keyed by TEN code."""
    return {
        "run_id": str(report.run_id),
        "status": report.status,
        "counts": report.counts,
        "tenants": {
            tenant_codes.get(tenant_id, str(tenant_id)): payload
            for tenant_id, payload in report.per_tenant.items()
        },
    }


def exit_code_for(status: str) -> int:
    return 1 if status == "discovery_failed" else 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — thin
    # composition over tested parts; exercised live by the C6 exit gate.
    install_secret_redaction()
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)

    settings = get_settings()
    if not settings.searchapi_api_key:
        logger.error("SEARCHAPI_API_KEY is empty — refusing a blind run")
        return 2

    engine = create_engine(settings.owner_database_url, future=True)
    try:
        with Session(engine) as session:
            report = run_nightly(
                session,
                searchapi=HttpSearchApiClient(settings.searchapi_api_key),
                jobspy_client=PythonJobSpyClient(),
                fetcher=UrllibPageFetcher(),
                now=datetime.now(UTC),
                tenant_ids=args.tenant,
                digest_only=args.digest_only,
                max_per_query=args.max_per_query,
                retrieval_cap=args.retrieval_cap,
                enrich_cap=args.enrich_cap,
            )
            codes = {
                row.id: row.code
                for row in session.execute(
                    select(Tenant.id, Tenant.code).where(
                        Tenant.id.in_(list(report.per_tenant))
                    )
                ).all()
            } if report.per_tenant else {}
    finally:
        engine.dispose()

    summary = summarize(report, codes)

    # ── the delivery phase (C7): engine result → CV → WhatsApp → close ──
    if args.deliver and settings.whatsapp_access_token and report.per_tenant:
        from career.cv.daily_run import DailyDeps, run_daily_delivery
        from career.cv.generate import AnthropicLlmClient
        from career.storage import FilesystemStorageAdapter
        from career.whatsapp.client import HttpWhatsAppClient

        class _JournalAdmin:
            def send_admin(self, text: str) -> str:
                logger.info("ADMIN: %s", text)
                return "journal"

        storage = FilesystemStorageAdapter(settings.storage_root)
        deps = DailyDeps(
            storage=storage,
            whatsapp_client=HttpWhatsAppClient(
                settings.whatsapp_access_token,
                settings.whatsapp_phone_number_id,
                storage=storage,
            ),
            admin_client=_JournalAdmin(),
            llm=AnthropicLlmClient(api_key=settings.anthropic_api_key),
        )
        engine2 = create_engine(settings.owner_database_url, future=True)
        try:
            with Session(engine2) as session:
                states = run_daily_delivery(
                    session, report=report, deps=deps,
                    now=datetime.now(UTC),
                    include_weekend=args.include_weekend,
                )
                # read INSIDE the session — the rows expire on close
                summary["delivery"] = {
                    str(tid): state.state for tid, state in states.items()
                }
                session.commit()
        finally:
            engine2.dispose()

    print(json.dumps(summary, ensure_ascii=False, default=str))
    return exit_code_for(report.status)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
