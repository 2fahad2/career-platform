# Career Platform Constitution

> **This file is a BRIDGE, not the source of truth.** The real constitution of
> this repository predates spec-kit and always wins:
>
> 1. `CLAUDE.md` — the working constitution (Arabic).
> 2. `docs/WHITEPAPER.html` v1.1 — the full specification; its §15 constants
>    beat everything.
> 3. `docs/CHANGELOG-v1.1.md` — approved amendments (override the whitepaper
>    where they conflict).
> 4. `docs/DEVIATIONS.md` — the numbered deviation register (D1…D14).
>
> Every /speckit-* command MUST read those four before producing anything, and
> any conflict resolves in their favor.

## Core Principles

### I. Document-Before-Code (NON-NEGOTIABLE)
No behavior change lands before the decision is recorded in the whitepaper /
CHANGELOG / DEVIATIONS in its own `docs:` commit. Spec-kit artifacts
(`specs/**`) are working drafts; a feature is only "specified" once its
decision is reflected in the canonical Arabic docs above.

### II. The §15 Constants Are Absolute
Fifteen locked constants (CLAUDE.md summary): no auto-apply automation ever;
every customer send passes the gate structurally; atomic ledger writes;
idempotent paid-only subscription creation; CV claims only from the confirmed
achievement bank + forbidden_claims honored; binding validation before any
document send; atomic PDF publishing; PII-free LLM calls; untrusted job
descriptions; forced RLS + cross-tenant attack tests in CI; worker re-verifies
ownership from DB; one honest day-state per tenant/day; no PII in logs or the
admin channel (TEN-#### codes only); encrypted, restore-tested off-server
backups; the document precedes the code.

### III. Tests Are the Gate
pytest against the disposable `career_test` DB only (never staging). A unit is
done when a FRESH full-suite run prints "N passed" (result file deleted before
the run), plus clean `ruff` and `mypy src/ + scripts/`. Commits are gated on
that fresh evidence; conventional commits in English; every commit pushed to
GitHub immediately (standing order).

### IV. Arabic Customer Surface, Saudi Dialect
All customer-facing texts are Arabic; conversational copy is Saudi colloquial
(no Gulf-neighbor or Levantine markers — e.g. «مرة» not «وايد»). Bidi
discipline: never mix Arabic and Latin on one line; English/code/URLs live on
their own lines. Operator-facing docs are Arabic; code/comments English.

### V. Explicit Owner Approval for the Irreversible
Forbidden without Fahad's explicit fresh permission: real messages to
non-test numbers, data deletion, purchases/payments, provider changes,
secrets exposure. Product/architecture decisions require his sign-off — the
spec draft is presented to him before implementation.

## Workflow Mapping (spec-kit → this repo)

- `/speckit-constitution` → edits THIS bridge only; the Arabic constitution
  changes via a `docs:` commit to CLAUDE.md/whitepaper as always.
- `/speckit-specify` + `/speckit-clarify` → draft under `specs/`; the accepted
  outcome must be distilled into a CHANGELOG entry (and a DEVIATIONS row if it
  bends the whitepaper) before `/speckit-implement`.
- `/speckit-plan` → must reuse the locked stack: Python 3.11+/FastAPI/
  PostgreSQL 16 RLS/Redis/SQLAlchemy+Alembic/pytest; Anthropic-only LLM calls;
  StorageAdapter for files. No new providers without owner approval.
- `/speckit-tasks` → tasks follow the phase discipline (C1→C9 exit criteria)
  and the gated-commit test rule above.
- `/speckit-implement` → respects everything here; never marks work complete
  without the fresh full-suite gate.

## Governance

This bridge amends nothing by itself. Amendments happen in the canonical
Arabic docs first (owner-approved), then this file is synced to match.

**Version**: 1.0.0 | **Ratified**: 2026-07-20 | **Last Amended**: 2026-07-20
