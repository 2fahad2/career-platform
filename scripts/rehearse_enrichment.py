"""Live rehearsal of the F-ENRICH nudge (owner-approved, 29 July).

Why a script and not a test: the enrichment feature has never run against the
real Graph API, the real Claude examples writer, or a real human replying in
dialect. Everything downstream of the opening message is already wired into
the running worker — this script only fires the arming step the nightly run
would fire, so the rest of the journey happens exactly as a customer's would.

Safety:
* Refuses unless the 24h window is OPEN (Meta rejects free-form otherwise).
* Refuses if an enrichment session is already open, or the role was already
  asked (the once-ever ledger is respected — no rehearsal privilege).
* Targets a REAL role, so a confirmed answer is genuine profile enrichment,
  not test pollution to clean up afterwards.

Run:  .venv/bin/python scripts/rehearse_enrichment.py [--role-title "IT Specialist"]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from career.config import get_settings
from career.db.models import CustomerChannel, OnboardingSession, ProfileFact
from career.onboarding import enrichment as enr
from career.onboarding.achievement_render import AnthropicExamplesWriter
from career.onboarding.confirmation import BANK_STATUSES
from career.storage import FilesystemStorageAdapter
from career.whatsapp.client import HttpWhatsAppClient
from career.whatsapp.delivery import record_out
from career.whatsapp.window import WindowState, window_state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role-title", default=None,
                    help="target role title; default = fewest achievements")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.owner_database_url, future=True)
    now = datetime.now(UTC)

    with Session(engine) as session:
        journey = session.execute(
            select(OnboardingSession).where(OnboardingSession.state == "ACTIVE")
        ).scalars().first()
        if journey is None:
            print("no ACTIVE journey — nothing to rehearse")
            return 1
        channel = session.get(CustomerChannel, journey.channel_id)
        if channel is None:
            print("journey has no channel")
            return 1

        state = window_state(last_inbound_at=channel.last_inbound_at,
                             opt_out_at=channel.opt_out_at, now=now)
        if state is not WindowState.OPEN:
            print(f"window is {state.name} — send ANY WhatsApp message to the "
                  "service number first, then re-run")
            return 2

        context = dict(journey.context or {})
        if (context.get("enrichment") or {}).get("open"):
            print("an enrichment session is already open — finish or wait for "
                  "the 72h auto-close")
            return 3

        roles = list(session.execute(
            select(ProfileFact).where(
                ProfileFact.tenant_id == journey.tenant_id,
                ProfileFact.category == "experience",
                ProfileFact.status.in_(sorted(BANK_STATUSES)),
            )
        ).scalars().all())
        if not roles:
            print("no confirmed roles")
            return 1

        def count(f: ProfileFact) -> int:
            return len((f.payload or {}).get("achievements") or [])

        if args.role_title:
            picked = next((r for r in roles
                           if (r.payload or {}).get("title") == args.role_title), None)
            if picked is None:
                print(f"role not found: {args.role_title}")
                return 1
        else:
            picked = min(roles, key=count)

        title = (picked.payload or {}).get("title")
        print(f"target role: {title} ({count(picked)} achievements)")
        if args.dry_run:
            print("dry run — nothing sent")
            return 0

        armed = enr.enqueue_enrichment(
            session, tenant_id=journey.tenant_id, role_fact_id=picked.id,
            journey_context=context, trigger="live_rehearsal", now=now,
        )
        if not armed:
            print("already asked once (ledger) — the once-ever rule holds")
            return 4

        writer = (AnthropicExamplesWriter(api_key=settings.anthropic_api_key)
                  if settings.anthropic_api_key else None)
        examples = enr.prepare_examples(
            session, role_fact_id=picked.id, writer=writer
        )
        if examples:
            context["enrichment"]["examples"] = examples
        journey.context = context

        storage = FilesystemStorageAdapter(settings.storage_root)
        wa = HttpWhatsAppClient(settings.whatsapp_access_token,
                                settings.whatsapp_phone_number_id, storage=storage)

        opening = enr.opening_message(session, role_fact_id=picked.id)
        mid = wa.send_interactive(channel.phone_e164, opening, enr.OPENING_BUTTONS)
        record_out(session, tenant_id=journey.tenant_id, channel_id=channel.id,
                   kind="interactive", wa_message_id=mid, now=now)
        print(f"opening sent ({mid})")

        if examples:
            from career.onboarding.achievement_render import format_examples_message

            mid2 = wa.send_text(channel.phone_e164, format_examples_message(examples))
            record_out(session, tenant_id=journey.tenant_id,
                       channel_id=channel.id, kind="text",
                       wa_message_id=mid2, now=now)
            print(f"examples sent ({len(examples)}) ({mid2})")
        else:
            print("no examples (writer unavailable or all scrubbed) — "
                  "the open question stands alone")

        session.commit()
    print("\nrehearsal armed. Reply on WhatsApp — the live worker handles the "
          "rest: render → grounding guard → confirm buttons → bank.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
