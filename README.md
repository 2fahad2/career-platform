# Career Platform

[![CI](https://github.com/2fahad2/career-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/2fahad2/career-platform/actions/workflows/ci.yml)

Paid personal job-search assistant for the Saudi market (WhatsApp + Salla).
Every night it discovers jobs, filters them through each customer's **private
gate**, generates a **tailored English CV with Claude** for every selected
opportunity, and delivers the bundle over WhatsApp so the customer applies
themselves. **A tool — never a recruitment agency, never auto-apply.**

> **Source of truth:** [`docs/WHITEPAPER.html`](docs/WHITEPAPER.html) +
> [`docs/CHANGELOG-v1.1.md`](docs/CHANGELOG-v1.1.md). The whitepaper precedes
> the code (invariant §15.15). Read [`CLAUDE.md`](CLAUDE.md) before any change.
> An Arabic file-by-file walkthrough lives in
> [`docs/ARCHITECTURE-AR.md`](docs/ARCHITECTURE-AR.md).

**Stack (locked):** Python 3.11+ · FastAPI · PostgreSQL 16 (+ RLS) · Redis ·
SQLAlchemy + Alembic · Docker Compose · pytest · Anthropic API only for LLM.

---

## Current status 📊

| | |
|---|---|
| 🧪 Test suite | **849 passing** (fresh-run gated commits, disposable `career_test` DB only — enforced by a hard guard) |
| 🗄️ Schema | migration `0018`, live-verified drift-free (host **and** deployed container) |
| 🔍 Last full audit | 2026-07-23, 42-agent adversarial sweep — all 5 critical + 20/23 major findings **fixed** (see `docs/AUDIT-2026-07-23.md`) |
| 🚀 Live | worker loop, admin watchtower bot, nightly engine timer (04:30 Riyadh) — real customer journey completed end-to-end incl. first real CV delivery |
| 🏗️ Phases | C1–C8 complete · C9 (production env + launch waves) gated on the Salla store go-live |

## System overview

```text
                 ┌─────────────────┐        ┌──────────────────────┐
  Salla store ──▶│  FastAPI intake │◀────── WhatsApp Cloud API
   (webhooks)    │  verify→dedupe  │        (inbound + receipts)
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │  PostgreSQL 16  │  Row-Level Security (FORCE)
                 │  webhook_events │  26 tenant-isolated tables
                 └────┬───────┬────┘
        polls         │       │          reads/writes
  ┌───────────────────┘       └───────────────────────┐
  ▼                                                   ▼
┌──────────────────────┐   04:30 Riyadh   ┌──────────────────────────┐
│  career-worker       │  ┌────────────▶  │  career-engine-nightly   │
│  conversation loop   │  │               │  discover → gate → rank  │
│  ├ Salla provisioning│  │ systemd timer │  → CV (Claude) → deliver │
│  ├ onboarding (C5)   │  │               │  → honest close (C7)     │
│  ├ funnel (C8)       │  │               └────────────┬─────────────┘
│  └ outcome buttons   │  │                            │
└──────────┬───────────┘  │                            ▼
           │              │               ┌──────────────────────────┐
           ▼              │               │  WhatsApp: job card +    │
┌──────────────────────┐  │               │  named CV PDF + buttons  │
│  career-admin-bot    │  │               └──────────────────────────┘
│  operator watchtower │──┘ alerts/summaries → Telegram (TEN codes only)
└──────────────────────┘
```

Three systemd services on the host share the database with the containerized
intake API. Every LLM call goes out **PII-free**; every customer-visible send
passes the gate **structurally**.

---

## The five data flows

**1 · Purchase → activation (C3/C4).**
Salla webhook → HMAC verify → dedupe-persist → the worker re-verifies the
order **via Salla API** (never trusts the payload) → tenant + subscription
provisioned **only on `payment = paid`** → activation token (stored as
SHA-256) → the customer sends the token on WhatsApp → channel bound to tenant.

**2 · Onboarding → ACTIVE (C5).**
Merged consent (one tap → three ledger events) → 14 Arabic questions with
progress counters → CV upload through a six-stage hardened pipeline → **PII
stripped**, then Claude extracts facts → the customer confirms each fact into
the **achievement bank** (rejected claims land in `forbidden_claims`) →
three-layer career-path assessment → versioned search policy → `ACTIVE`.

**3 · Nightly engine (C6).**
Query families derived **dynamically from active customers' approved paths** →
SearchAPI google_jobs + JobSpy (isolated fetchers, sanitized failures) →
same-run dedupe → shared pool upsert → cached enrichment behind SSRF guards →
**per-tenant gate** with a persisted decision log (answers "why was this sent
to me?") → suppression filter *before* the cap → deterministic ranking with
versioned rank traces.

**4 · CV + delivery + honest close (C7).**
Resolve or generate a CV per selected job (Claude tailoring chain with rule
fallbacks at every stage — the pipeline survives a dead LLM) → guards:
bank-vocabulary whitelist, invented-content, forbidden claims, Arabic-leak →
**atomic publish** (PDF + sha-bound sidecar) → `validate_cv_binding` is the
sole send authority → grouped WhatsApp delivery (card → document → outcome
buttons per job) → suppression ledger for delivered only → **exactly one of
eight honest day states** per tenant (incl. `SKIPPED_OPTED_OUT`). Identity is injected locally at
assembly — **no LLM call ever sees PII**.

**5 · Acquisition funnel (C8).**
29-SAR CV-analysis product → the same consent/upload/extraction authorities →
deterministic scoring via the same path-scoring engine → one-page Arabic RTL
PDF report + WhatsApp summary → an upgrade purchase from the same phone
**inherits** the funnel tenant (half-ready onboarding, nothing deleted).

**6 · Thin-role enrichment (F-ENRICH) 🪄.**
When a delivered day tailors a CV for a role with <2 confirmed achievements,
ONE friendly Saudi-colloquial nudge is armed (once-ever ledger, open-window
only, never blocks delivery) → the customer answers in dialect (or picks a
digit from a scrubbed examples menu) → Claude renders ONE English bullet →
a **deterministic cross-lingual grounding guard** rejects any number/entity
absent from the raw Arabic → the customer confirms (English + Arabic gloss)
→ only then does the bullet join the bank as `CUSTOMER_CONFIRMED`.

---

## Repository map

### Root

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Working constitution: sources of truth, locked stack, invariants digest, forbidden-without-permission list. |
| `pyproject.toml` | Dependencies + ruff (incl. security rules) + strict mypy configuration. |
| `docker-compose.staging.yml` | Staging stack: Postgres 16 (loopback :5433), Redis 7 (loopback :6380), api container. |
| `Dockerfile` | The api container image. |
| `alembic.ini` | Migrations entry point (owner role). |
| `.env.example` `.env.staging.example` `.env.production.example` | Complete templates for every setting; real `.env*` files are untracked. |
| `Caddyfile` | TLS termination; exposes only `/webhooks/*` and `/health`. |

### `src/career_core/` — pure kernel (no I/O, no DB)

| File | Purpose |
|------|---------|
| `gate.py` | Pure gate calculus: salary certainty buckets, company/role scores, the deterministic `gate_sort_key`. |
| `salary.py` | Salary parsing and classification from raw JD text. |
| `identity.py` | Canonical job identity (`joburl-v1`) used by publish, binding and suppression alike. |
| `sentences.py` | English sentence-quality gate: rejects incomplete or dangling summaries. |
| `ssrf.py` | Fail-closed SSRF guard (scheme/host/IP, safe resolver). |
| `urltools.py` | URL normalization + tracking-parameter stripping. |

### `src/career/` — application core

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app: webhook intake (verify → dedupe → 200) + `/health`. |
| `config.py` | Typed `Settings` for every env var; arms the exact-literal secret scrubber on load. |
| `logging_filters.py` | Secret redaction on every log record (patterns + registered literals). |
| `audit.py` | Append-only tenant audit-trail writer. |
| `tokens.py` | Activation tokens — raw shown once, only SHA-256 stored. |

### `src/career/db/`

| File | Purpose |
|------|---------|
| `base.py` | Declarative base with deterministic naming (identical DDL everywhere). |
| `models.py` | The full ORM schema (30+ tables) with per-table RLS notes; PDFs never in the DB. |
| `session.py` | Two-role discipline: `career_app` + transaction-local tenant GUC (fail-closed) vs owner sessions. |

### `src/career/webhooks/` + `src/career/queue/`

| File | Purpose |
|------|---------|
| `webhooks/intake.py` | Shared fast-intake with fingerprint dedupe. |
| `queue/adapter.py` | Redis queue adapter behind a protocol. |
| `queue/message.py` | Queue envelope (PII-free by contract). |
| `queue/outbox.py` | Transactional outbox relay. |

### `src/career/salla/` — commerce (C3)

| File | Purpose |
|------|---------|
| `signature.py` | Constant-time HMAC verification; fail-closed on missing secret. |
| `webhook.py` | Event parsing + intake. |
| `client.py` | Salla API client with the **paid-status whitelist** (unknown → never provision). |
| `provisioning.py` | Order → tenant + subscription + token; idempotent; **re-verifies via API**. |
| `subscriptions.py` | 11-state machine with an explicit transition table + event trail. |

### `src/career/whatsapp/` — messaging (C4)

| File | Purpose |
|------|---------|
| `signature.py` | Meta signature verification (fail-closed). |
| `webhook.py` | Meta handshake + inbound intake. |
| `client.py` | Injectable send boundary (text/document/template/interactive/media); errors carry codes, never bodies. |
| `window.py` | 24-hour window calculus + the operator evening-nudge predicate. |
| `adaptive.py` | Delivery planning: open → direct, closed → template-then-hold, opted-out → nothing. |
| `delivery.py` | Grouped-bundle execution with per-job failure isolation and honest statuses. |
| `templates.py` | Approved-template registry. |
| `inbound.py` | Inbound classification (activation / STOP / support / other). |
| `activation.py` + `activation_flow.py` | Token → tenant binding; checks **before** any mutation; the funnel-inheritance exception. |
| `worker.py` | Conversation worker: idempotency, DB-verified ownership, routing, outcome buttons, receipts. |

### `src/career/onboarding/` — C5

| File | Purpose |
|------|---------|
| `fsm.py` | 10-state forward-only journey + the single regression edge + reminder predicate. |
| `orchestrator.py` | Conversation brain: consent, 14 questions, batch confirmation, privacy commands, reminders. |
| `collection.py` | Question bank (labels ≤ 20 chars — WhatsApp cap) + answer parsing. |
| `consents.py` | Purpose-separated append-only consent ledger + fail-closed gate. |
| `upload.py` | Six-stage hardened upload pipeline (sniff → scan → inspect → sanitize → sandbox). |
| `extract_worker.py` | Sandbox child: rlimits before parsing, defusedxml, output caps. |
| `extraction.py` | PII strip → assert-absent → Claude structured extraction (results ≠ truth). |
| `confirmation.py` | Facts → achievement bank; rejections feed `forbidden_claims`. |
| `paths.py` | Three-layer career-path assessment with weak-fit override. |
| `policy.py` | Versioned search-policy builder + Arabic summary card. |
| `privacy.py` | Export / pause / resume / two-step delete honoring retention. |

### `src/career/engine/` — C6

| File | Purpose |
|------|---------|
| `run.py` | Nightly composition with honest statuses; the run row closes on every exit path. |
| `families.py` | Dynamic query families from active tenants' approved paths. |
| `sources.py` | SearchAPI + JobSpy adapters (per-query isolation, retries, HTML stripping, apply-routing). |
| `identity.py` | Same-run dedupe + cross-source merge rules. |
| `enrichment.py` | Once-per-posting enrichment behind pre/post-redirect SSRF checks. |
| `gate.py` | Per-tenant gate: strictness policies, region equivalence, decision log, near-miss capture. |
| `ranking.py` | Suppression before the cap, deterministic sort, slots, versioned traces, ledger writer. |
| `quota.py` | SearchAPI credit-watch decision. |
| `cli.py` | Timer entry point: run → alerts → credit watch → delivery phase. |

### `src/career/cv/` — C7

| File | Purpose |
|------|---------|
| `schemas.py` | Pydantic models: `MasterCV`, `TailoredCV`, `ContactInfo`. |
| `template.py` | v5 HTML template, byte-verbatim from the legacy reference, guarded by tests. |
| `render.py` | WeasyPrint: deterministic bytes, pinned fonts, exactly one A4 page. |
| `normalize.py` | Achievement bank → `MasterCV` normalization rules. |
| `enforce.py` | One-page enforcement: caps, pacing, JD-ranked fill — no invention. |
| `validate.py` | Pre-render validator: Arabic-leak (blocking) + structure + style; reason codes only. |
| `prompts.py` | §10 prompts byte-verbatim; literal `.replace` filling (format-injection-proof). |
| `generate.py` | Tailoring chain + every guard + rule fallbacks + the Anthropic client with token metering. |
| `publish.py` | Atomic pair publish; `validate_cv_binding` — the sole send authority; resolver; budget; quarantine. |
| `deliver.py` | Arabic job cards, display filenames, grouped bundles, outcome buttons. |
| `close.py` | Seven-state daily authority, admin summary, usage + cost rollups. |
| `daily_run.py` | Delivery-day orchestration: tenant isolation, stale-held expiry, honest closes, cost metering. |

### `src/career/funnel/` — C8

| File | Purpose |
|------|---------|
| `flow.py` | Purchase → consent → upload → report → DONE; reuses C5 authorities; reopen on repurchase. |
| `evaluation.py` | Deterministic five-score analysis via the same path engine; Arabic notes; upgrade steps. |
| `report.py` | One-page Arabic RTL PDF + WhatsApp summary. |

### `src/career/telegram/` — operator watchtower

| File | Purpose |
|------|---------|
| `admin.py` | Bot API client: sanitized sends, inline keyboards, long-poll, body-free errors. |
| `console.py` | Stateless router: operator allowlist, self-contained callbacks, PII-free readers. |
| `views.py` | Pure Arabic screen renderers — golden-tested. |
| `messages.py` | Small admin-message builders (TEN codes only). |

### `src/career/storage/` + `src/career/worker/`

| File | Purpose |
|------|---------|
| `storage/adapter.py` | `StorageAdapter` protocol (S3-compatible) + tenant key helpers. |
| `storage/filesystem.py` | Atomic implementation: temp → write → fsync → replace → dir fsync. |
| `worker/worker.py` | Generic queue-consumer scaffolding. |

### `migrations/versions/`

| Migration | Adds |
|-----------|------|
| `0001` | Tenants + documents — the RLS foundation. |
| `0002` | Outbox + idempotency ledger. |
| `0003` | Append-only audit events. |
| `0004` | Salla billing (plans, subscriptions, tokens, intake). |
| `0005` | WhatsApp channels + deliveries + messages. |
| `0006` | Onboarding schema (9 tables). |
| `0007` | Consent total ordering. |
| `0008` | Engine pool + runs + decisions + suppressions. |
| `0009` | Profile contact fields. |
| `0010` | Outcome events. |
| `0011` | Usage + costs + tenant day states. |
| `0012` | Funnel sessions. |
| `0013` | The `cv_analysis` plan row. |
| `0014` | Admin-bot cursor. |
| `0015` | Funnel-session DELETE grant (privacy erasure). |
| `0016` | `subscriptions.order_phone_e164` — zero-touch activation. |
| `0017` | 🆕 `role_enrichments` — the F-ENRICH once-ever ledger. |
| `0018` | `outbox_events` FORCE RLS (caught by the RLS meta-test). |

### `scripts/` — live runners

| Script | Purpose |
|--------|---------|
| `run_worker_loop.py` | Conversation service: WhatsApp + Salla + reminder sweep + window nudge + heartbeat. |
| `run_nightly.py` | Engine CLI wrapper (systemd target). |
| `run_admin_bot.py` | Watchtower long-poll + live health probes. |
| `ci_create_app_role.py` | Non-superuser app role for CI. |
| `demo_engine_tenants.py` | Demo tenants for live engine proofs. |

### `ops/`

| Path | Purpose |
|------|---------|
| `engine/systemd/career-worker.service` | Conversation loop (Restart=always). |
| `engine/systemd/career-engine-nightly.{service,timer}` | 04:30 Riyadh nightly (Persistent=true). |
| `engine/systemd/career-admin-bot.service` | Watchtower (isolated from the worker). |
| `backup/backup.sh` | Encrypted restic backup to B2: pg_dump + host storage root + volume. |
| `backup/restore-test.sh` | Monthly restore drill (tables + RLS assertions). |
| `backup/systemd/*` | Daily backup + monthly restore-test timers. |

### `docs/`

| File | Purpose |
|------|---------|
| `WHITEPAPER.html` | Product constitution: phases + exit conditions, the 15 invariants, pricing. |
| `CHANGELOG-v1.1.md` | Approved post-v1.0 decisions (override conflicting v1.0 text). |
| `DEVIATIONS.md` | Approved deviations D1–D13 with rationale. |
| `PLAN.md` | Full execution plan. |
| `PROGRESS.md` | Session-by-session state + exit gates + pending decisions. |
| `ADMIN_BOT_DESIGN.md` | Watchtower design + external-proposal evaluation. |
| `LEGACY_KNOWLEDGE.md` | Read-only reference from the proven personal engine. |
| `ARCHITECTURE-AR.md` | Arabic file-by-file walkthrough. |
| `policies/*.md` | Privacy / refund / terms. |

---

## The 15 engineering invariants

1. **No auto-apply code path** — ever, not even as an experiment.
2. Every production send passes the gate **structurally** — no flag bypasses it.
3. Delivered-ledger writes only after complete delivery, atomically.
4. Subscriptions only on `payment = paid`, re-verified via Salla API, idempotent.
5. Every CV claim traces to the confirmed achievement bank; `forbidden_claims` honored literally.
6. No document sent before `validate_cv_binding` returns ready; nothing from quarantine.
7. Atomic publish for every PDF/metadata pair.
8. LLM calls are PII-free — identity injected locally at assembly.
9. Job descriptions are untrusted input — the analysis model has no tools, secrets, or control.
10. RLS enabled + FORCE; adversarial cross-tenant tests in CI.
11. Workers re-verify ownership from the DB — never trust queue payloads.
12. One honest state of seven per customer/day — no silent success.
13. No PII in logs or the admin channel — TEN codes only; secret filters everywhere.
14. Backups encrypted, off-server, restore-tested periodically.
15. **The whitepaper precedes the code.**

## Security model

- **Tenant isolation** — 26 tables under `ENABLE + FORCE` RLS with fail-closed
  transaction-local GUC; adversarial tests (forged writes, cross-reads,
  no-context) run in CI.
- **Webhooks** — constant-time HMAC on both providers; empty secret verifies
  nothing; dedupe before processing.
- **Uploads** — magic-byte sniffing, structural inspection, metadata
  sanitization, rlimited sandbox extraction.
- **LLM boundary** — PII stripped and asserted-absent before any call;
  byte-verbatim prompts; literal placeholder filling; invented-content +
  forbidden-claims + Arabic-leak output guards; rule fallbacks everywhere.
- **Secrets** — untracked env files; redaction filters + registered literals
  on every logger; error types carry codes, never bodies.

## Operations

```text
career-worker.service          conversation loop (3s poll, Restart=always)
career-engine-nightly.timer    04:30 Asia/Riyadh, Persistent=true
career-admin-bot.service       operator console (long-poll)
career-backup.timer            daily encrypted backup -> Backblaze B2
career-restore-test.timer      monthly restore drill
```

The Telegram watchtower gives the operator daily summaries, per-tenant cards
(TEN codes only), platform health, business numbers, sanitized error tails,
and proactive alerts.

## Testing

```bash
# disposable DB — NEVER run pytest against staging
DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=career_test \
REDIS_HOST=127.0.0.1 REDIS_PORT=6380 \
SALLA_WEBHOOK_SECRET=test_secret_123 \
WHATSAPP_APP_SECRET=wa_app_secret_123 WHATSAPP_VERIFY_TOKEN=wa_verify_123 \
CI_REQUIRE_DB=1 python -m pytest tests/ -q
```

**849 tests**, zero skips with a full environment: adversarial RLS (plus a
catalog **meta-test** enforcing ENABLE+FORCE+policy on every tenant table),
webhook
idempotency, upload attack files, CV binding/quarantine, the seven-state
failure matrix, delivery failure isolation, golden Arabic renderers,
byte-verbatim template/prompt guards, full journey E2Es from raw Meta
payloads. CI: ruff → strict mypy → alembic upgrade + drift check → pytest
against real Postgres/Redis.

## Configuration

Every variable is documented in [`.env.example`](.env.example):

| Group | Keys |
|-------|------|
| Database | `DB_*` (app role) + `DB_OWNER_*` (migrations/system) |
| Salla | `SALLA_WEBHOOK_SECRET` · `SALLA_API_KEY` · `SALLA_PRODUCT_CATALOG` · `SALLA_TOKEN_EXPIRES_AT` |
| WhatsApp | `WHATSAPP_ACCESS_TOKEN` · `WHATSAPP_PHONE_NUMBER_ID` · `WHATSAPP_WABA_ID` · `WHATSAPP_APP_SECRET` · `WHATSAPP_VERIFY_TOKEN` |
| LLM / search | `ANTHROPIC_API_KEY` · `SEARCHAPI_API_KEY` |
| Operator | `TELEGRAM_ADMIN_BOT_TOKEN` · `TELEGRAM_ADMIN_CHAT_ID` · `CANARY_TEST_PHONE` |

## Quick start (staging)

```bash
cp .env.staging.example .env.staging     # fill real secrets (never commit)
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d
docker compose -f docker-compose.staging.yml --env-file .env.staging \
  run --rm api alembic upgrade head
curl -fsS localhost:${API_PUBLISH_PORT:-8001}/health
```

## Development workflow

1. **Docs before code** — product-behavior changes start with a standalone
   `docs:` commit to the whitepaper/changelog.
2. **Tests first** — every authority lands with its failure cases.
3. Conventional commits; code/comments/commits in English; customer-facing
   strings in Arabic.
4. `ruff` + strict `mypy` + `alembic check` clean before every commit.
5. Update `docs/PROGRESS.md` at session end.
