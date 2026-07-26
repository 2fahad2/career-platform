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
from career.db.models import DiscoveryRun, Tenant
from career.engine.enrichment import UrllibPageFetcher
from career.engine.run import RunReport, run_nightly
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
        default=None,
        help="record the run as digest-only (D9: no send, no ledgers). "
             "AUDIT ح-3: the default now FOLLOWS --deliver (delivering run "
             "⇒ not digest-only) so the record can never contradict what "
             "actually happened; pass the flag explicitly to override.",
    )
    # None ⇒ fall back to Settings (§06: caps live in configuration)
    parser.add_argument("--max-per-query", type=int, default=None)
    parser.add_argument("--retrieval-cap", type=int, default=None)
    parser.add_argument("--enrich-cap", type=int, default=None)
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

    # §05 lifecycle sweep BEFORE the engine: a just-expired subscription
    # must not seed tonight's query families.
    try:
        from career.salla.lifecycle import sweep_subscription_lifecycle
        from career.whatsapp.client import HttpWhatsAppClient as _WaClient

        lifecycle_wa = (
            _WaClient(settings.whatsapp_access_token,
                      settings.whatsapp_phone_number_id)
            if settings.whatsapp_access_token else None
        )
        with Session(engine) as session:
            sweep_subscription_lifecycle(
                session, now=datetime.now(UTC),
                whatsapp_client=lifecycle_wa,
            )
            session.commit()
    except Exception:  # noqa: BLE001 — the sweep never blocks the run
        logger.error("subscription lifecycle sweep failed", exc_info=True)

    try:
        with Session(engine) as session:
            report = run_nightly(
                session,
                searchapi=HttpSearchApiClient(settings.searchapi_api_key),
                jobspy_client=PythonJobSpyClient(),
                fetcher=UrllibPageFetcher(),
                now=datetime.now(UTC),
                tenant_ids=args.tenant,
                # ح-3: honest default — the record mirrors the actual intent
                digest_only=(args.digest_only if args.digest_only is not None
                             else not args.deliver),
                max_per_query=args.max_per_query or settings.engine_max_per_query,
                retrieval_cap=args.retrieval_cap or settings.engine_retrieval_cap,
                enrich_cap=args.enrich_cap or settings.engine_enrich_cap,
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

    class _JournalAdmin:
        def send_admin(self, text: str) -> str:
            logger.info("ADMIN: %s", text)
            return "journal"

    def _admin_client() -> Any:
        if settings.telegram_admin_bot_token and settings.telegram_admin_chat_id:
            from career.telegram.admin import HttpTelegramAdminClient

            return HttpTelegramAdminClient(
                settings.telegram_admin_bot_token,
                settings.telegram_admin_chat_id,
            )
        return _JournalAdmin()

    admin = _admin_client()
    if report.status in ("discovery_failed", "partial"):
        try:
            admin.send_admin(
                f"⚠️ التشغيلة الليلية: {report.status} — "
                f"counts={report.counts}"
            )
        except Exception:  # noqa: BLE001 — alerting never breaks the run
            logger.warning("admin alert failed", exc_info=True)

    # SearchAPI credit watch (trial reads all-zero → quota_alert stays quiet)
    try:
        import json as _json
        from urllib.request import urlopen

        from career.engine.quota import quota_alert

        with urlopen(
            "https://www.searchapi.io/api/v1/me?api_key="
            + settings.searchapi_api_key,
            timeout=20,
        ) as resp:
            account = _json.load(resp).get("account") or {}
        alert = quota_alert(account)
        if alert:
            admin.send_admin(alert)
    except Exception:  # noqa: BLE001 — credit watch never breaks the run
        logger.warning("searchapi credit check failed", exc_info=True)

    # ── the delivery phase (C7): engine result → CV → WhatsApp → close ──
    delivery_phase_ran = bool(
        args.deliver and settings.whatsapp_access_token and report.per_tenant
    )
    if args.digest_only is None and not delivery_phase_ran:
        # ح-3: intended to deliver but the phase never ran (no creds / no
        # tenants) — flip the record so it never claims sends that didn't
        # happen NOR a digest that actually delivered.
        try:
            with Session(engine) as session:
                run_row = session.get(DiscoveryRun, report.run_id)
                if run_row is not None:
                    run_row.digest_only = True
                    session.commit()
        except Exception:  # noqa: BLE001 — honesty patch must not kill the run
            logger.warning("digest-only backfill failed", exc_info=True)
    if delivery_phase_ran:
        from career.cv.daily_run import DailyDeps, run_daily_delivery
        from career.cv.generate import AnthropicLlmClient
        from career.onboarding.achievement_render import AnthropicExamplesWriter
        from career.storage import FilesystemStorageAdapter
        from career.whatsapp.client import HttpWhatsAppClient

        storage = FilesystemStorageAdapter(settings.storage_root)
        deps = DailyDeps(
            storage=storage,
            whatsapp_client=HttpWhatsAppClient(
                settings.whatsapp_access_token,
                settings.whatsapp_phone_number_id,
                storage=storage,
            ),
            admin_client=admin,
            llm=AnthropicLlmClient(api_key=settings.anthropic_api_key),
            examples_writer=AnthropicExamplesWriter(
                api_key=settings.anthropic_api_key
            ),
        )
        engine2 = create_engine(settings.owner_database_url, future=True)
        try:
            with Session(engine2) as session:
                canary_tid = None
                if settings.canary_test_phone:
                    from career.db.models import CustomerChannel

                    # live-bug fix: Meta stores the phone without «+»,
                    # settings carry it — match every spelling (phones.py)
                    from career.whatsapp.phones import phone_variants

                    canary_tid = session.execute(
                        select(CustomerChannel.tenant_id).where(
                            CustomerChannel.phone_e164.in_(
                                phone_variants(settings.canary_test_phone)
                            )
                        )
                    ).scalars().first()
                states = run_daily_delivery(
                    session, report=report, deps=deps,
                    now=datetime.now(UTC),
                    include_weekend=args.include_weekend,
                    canary_tenant_id=canary_tid,
                    canary_delay_seconds=3600.0,
                )
                # read INSIDE the session — the rows expire on close
                # AUDIT ك-17: TEN codes in the journal, never raw uuids
                dcodes = {
                    row.id: row.code
                    for row in session.execute(
                        select(Tenant.id, Tenant.code).where(
                            Tenant.id.in_(list(states))
                        )
                    ).all()
                } if states else {}
                summary["delivery"] = {
                    dcodes.get(tid, "TEN-????"): state.state
                    for tid, state in states.items()
                }
                session.commit()
        finally:
            engine2.dispose()

    print(json.dumps(summary, ensure_ascii=False, default=str))
    return exit_code_for(report.status)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
