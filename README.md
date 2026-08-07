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
| 🧪 Test suite | `pytest -q` prints the count — **do not restate it from memory** (see [Testing](#testing)); disposable `career_test` DB only, enforced by a hard guard |
| 🗄️ Schema | authored head vs deployed head — the two commands that produce them are in [Migrations](#migrationsversions), and neither binary is on `PATH`, so use them as written. **They did not match when this line was last measured**, and the numbers are deliberately not repeated here: run both. The code in this checkout selects `next_attempt_at` in a live path, so `0022` is the floor for the worker to run at all, and the authored head is further on. Do not deploy before the gap is closed and re-measured |
| 🔍 Last full audit | 2026-08-05, five-lens sweep — 16 P0 + 5 P1 fixed (`docs/CHANGELOG-v1.1.md` §24), then **three adversarial rounds** aimed at the fixes themselves, each of which found real regressions in the round before it. The 42-agent sweep of 2026-07-23 has its own record and its own status box — read it there rather than here (`docs/AUDIT-2026-07-23.md`); the box says stage 4 finished at 25/28 while a bullet below it still lists majors as remaining, and that disagreement has not been resolved |
| 🚀 Live | **`systemctl is-active` says `active` and the worker is processing nothing.** Every polling cycle raises `column webhook_events.next_attempt_at does not exist` — the code in the running unit is ahead of the staging schema (see 🗄️ Schema above), so no WhatsApp message and no Salla event is being consumed. Do not read this row as the health check; measure it: `journalctl -u career-worker --since '10 min ago' \| grep -c 'does not exist'` should be **0**, and a non-zero count means the migration has still not been applied. `Restart=always` is what keeps the unit *alive* through this, which is precisely why «active» is not an answer. Historically: real customer journey completed end-to-end incl. first real CV delivery; admin watchtower bot and the 11:00 Riyadh nightly timer are armed |
| 🖐️ Blocked on the owner | **Two items, both deployment and neither code.** (1) **The migration.** The authored head is ahead of the deployed one (🗄️ Schema), and the stop is deliberate rather than an oversight — the permission model refuses to point the migration tool at the staging database, so Fahad runs it himself or grants the tool a rule. `docs/RUNBOOK-DISASTER-RECOVERY.md` has the procedure, and the final migration **drops two tables**, so it belongs behind its own deploy gate. (2) **The Caddy gateway and the systemd units**, written here and not published, with `/etc` out of the tool's reach. Measure both rather than trusting this row: `diff -q /etc/caddy/Caddyfile ops/caddy/Caddyfile`, and `./scripts/deploy_preflight.sh` for the units. **Ordering is not optional: database first, then restart.** Both live services run **straight from this checkout** (`ExecStart` points at `/root/career/.venv/bin/python`), so a running process carries the code as it was *at boot* — nothing wired today is inside it — and restarting before the migration reproduces the 6 August incident exactly. |
| 🏗️ Phases | C1–C8 **code**-complete; C4 closed live on 2026-07-17 (activation bound an order to a number, adaptive delivery ran both ways, Meta receipts returned) · **C3 is still the one open exit condition, for a narrower reason than before** — Salla approved on 2026-08-07 and the real store, its three products and a live credential are wired (`grep -o 'SALLA_[A-Z_]*=' .env.staging` names the keys; the values are the answer), so the token is no longer expired — but **no riyal has been paid and no live refund has been tested**, and neither is closed by a successful connection · C9 gated on the store go-live |

## System overview

```text
                 ┌─────────────────┐        ┌──────────────────────┐
  Salla store ──▶│  FastAPI intake │◀────── WhatsApp Cloud API
   (webhooks)    │  verify→dedupe  │        (inbound + receipts)
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │  PostgreSQL 16  │  Row-Level Security (FORCE)
                 │  webhook_events │  every tenant table FORCEd
                 └────┬───────┬────┘
        polls         │       │          reads/writes
  ┌───────────────────┘       └───────────────────────┐
  ▼                                                   ▼
┌──────────────────────┐   11:00 Riyadh   ┌──────────────────────────┐
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
intake API. **They run from this working tree, not from an image** — their
`ExecStart` points straight at `/root/career/.venv/bin/python` and
`/root/career/scripts/…` — so a running process carries whatever the files said
when it started, and editing a file changes nothing until the unit restarts.
Restart order is not a preference: **migrate first, then restart**, or the
worker fails on every cycle against a schema it is ahead of. Every LLM call goes
out **PII-free**; every customer-visible send passes the gate **structurally**.

---

## The data flows

**1 · Purchase → activation (C3/C4).**
Salla webhook → HMAC verify → dedupe-persist → the worker re-verifies the
order **via Salla API** (never trusts the payload) → tenant + subscription
provisioned **only on `payment = paid`** → activation token (stored as
SHA-256) → the customer sends the token on WhatsApp → channel bound to tenant.

**2 · Onboarding → ACTIVE (C5).**
Merged consent (one tap → three ledger events) → 14 Arabic questions with
progress counters → CV upload through a six-stage hardened pipeline (size →
sniff → scan → inspect → sanitize → sandbox) → **PII
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
| `ops/caddy/Caddyfile` | The gateway config: TLS termination, exposing only `/webhooks/*` and `/health`. **Tracked here since 2026-08-07 and NOT yet deployed** — the live copy is `/etc/caddy/Caddyfile`, and `diff -q /etc/caddy/Caddyfile ops/caddy/Caddyfile` is the authority on whether they agree (they do not today). The tracked version adds an **access log**, which did not exist at all: during the 26-hour DNS outage a refused request — bad signature, disallowed path — left no trace in either direction, because only requests that become a `webhook_events` row are visible anywhere. Deploy steps are in the file's own header (`reload`, not `restart`). `backup.sh` still captures the live copy in the `config` snapshot and `docs/RUNBOOK-DISASTER-RECOVERY.md` restores it. |

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
| `audit.py` | Append-only tenant audit-trail writer, wired to the security, money and privacy events. |
| `tokens.py` | Activation tokens — raw shown once, only SHA-256 stored. |
| `arabic.py` | The single Arabic-folding authority, and a **leaf**: consent classification, the Meta-mandated STOP/RESUME reading, the standing privacy commands. Extracting it is what stopped folding a string from dragging in all of SQLAlchemy — the 569 → 57 measurement in `CHANGELOG-v1.1.md` §25 is `whatsapp/inbound.py`'s, and this line used to quote it as if it were `arabic.py`'s. Measure, don't quote: `PYTHONPATH=src .venv/bin/python -c "import sys, career.arabic; print(len(sys.modules))"` reads 54 here against a bare interpreter's 33 and `career.db.models`'s 540. The property that cannot drift with the interpreter is the one `tests/test_arabic.py` asserts: in a fresh process the only `career.*` module loaded is `career.arabic` itself. |
| `fingerprint.py` | Content fingerprint of the package source — what is actually RUNNING, compared against what was committed (32 commits once ran unnoticed behind the live container for four days). |
| `notify.py` | The sd_notify writer: `READY=1`, `WATCHDOG=1`, and the watchdog interval systemd advertises. No dependency and no failure mode — a missing socket means «not under systemd», so the loops run identically when started by hand. |

### `src/career/db/`

| File | Purpose |
|------|---------|
| `base.py` | Declarative base with deterministic naming (identical DDL everywhere). |
| `models.py` | The full ORM schema with per-table RLS notes; PDFs never in the DB. For the table count ask the schema, not this line — and use the venv interpreter, since there is no bare `python` on this host: `.venv/bin/python -c "from career.db.base import Base; import career.db.models; print(len(Base.metadata.tables))"`. |
| `session.py` | Two-role discipline: `career_app` + transaction-local tenant GUC (fail-closed) vs owner sessions. |

### `src/career/webhooks/`

| File | Purpose |
|------|---------|
| `webhooks/intake.py` | Shared fast-intake with fingerprint dedupe. `persist_deduped_event` takes **`signature_valid` as a required argument that refuses anything but `True`** (`UnverifiedWebhook`). It used to write the literal `True` into every row: a column named as a verdict and true by *construction*, held up only by both callers happening to verify first — call-site discipline a third intake would not inherit. That stopped being cosmetic once the column acquired readers that act on it: `salla/provisioning._verified` consults it before letting a payload deliver a live merchant credential, and `credential_is_still_consumable` again before keeping that body past retention. Verifying *inside* this function was considered and rejected — both endpoints must refuse a bad signature before they parse a body at all, so the check has to exist at the call site regardless; what was missing was that the **answer** never travelled with the body. |

> **The database IS the queue** (`docs/DEVIATIONS.md` D24). `queue/` and
> `worker/` were written, tested, and never had a producer or a consumer; they
> were deleted on 2026-08-06. Every inbound event becomes a `webhook_events`
> row at the door and the worker polls it, which is why the 3 August incident
> was **recoverable**: `scripts/replay_lost_events.py` could re-read the rows
> and refuse anything that might message a customer twice. A Redis list would
> have let the wrong consumer take them with nothing left to replay — the queue
> would have turned a recoverable incident into permanent loss.

### `src/career/salla/` — commerce (C3)

| File | Purpose |
|------|---------|
| `signature.py` | Constant-time HMAC verification; fail-closed on missing secret. |
| `webhook.py` | Event parsing + intake. |
| `client.py` | Salla API client with the **paid-status whitelist** (unknown → never provision). Read-only: `get_order` is the only call it makes, which is why nothing in this project can write the remaining-seat count back onto a store page (`docs/DEVIATIONS.md` D26 item 3). |
| `provisioning.py` | Order → tenant + subscription + token; idempotent; **re-verifies via API**; the triple match (product, amount, currency) refuses anything else. |
| `subscriptions.py` | 11-state machine with an explicit transition table + event trail. |
| `renewal.py` | The second payment **continues**: one subscription row per Salla order, the previous retired to `EXPIRED` — extending one row would erase the money trail a later refund has to find (§16, D18). |
| `lifecycle.py` | The paper's own schedule as one idempotent daily sweep: day-27/29 reminders → `GRACE` 48h → `EXPIRED` → recovery message after 7 days. |
| `activation_link.py` | The `wa.me` deep link with «تفعيل &lt;token&gt;» pre-filled — issued on demand, never echoed into a permanent transcript. |
| `seats.py` | The founding-seat counter the store page promises is honest («ما نبيع الكرسي رقم ٣١»). It is computed from real subscription rows and shown to the **operator on Telegram** — the store page's own number is still updated by hand. |
| `tokens.py` | The Salla credential: captured from the `app.store.authorize` webhook and refreshed **before** expiry by `scripts/refresh_salla_token.py` on a daily timer. It exists because the previous token simply reached its expiry with no renewal path — the one moment Salla ever offers a replacement was defined and deliberately not consumed, and sales stopped for nine days (CHANGELOG-v1.1 §29, §30). Salla's refresh tokens are **single-use**, so the script holds a lock: two exchanges racing each other make Salla revoke everything and require the merchant to reinstall the app. |

### `src/career/whatsapp/` — messaging (C4)

| File | Purpose |
|------|---------|
| `signature.py` | Meta signature verification (fail-closed). |
| `webhook.py` | Meta handshake + inbound intake. |
| `client.py` | Injectable send boundary (text/document/template/interactive/media); errors carry codes, never bodies. |
| `window.py` | 24-hour window calculus + the operator evening-nudge predicate. |
| `adaptive.py` | Delivery planning: open → direct, closed → template-then-hold, opted-out → nothing. |
| `delivery.py` | Grouped-bundle execution with per-job failure isolation and honest statuses. |
| `templates.py` | The template registry with its Meta CATEGORY (utility vs marketing — they are priced ~2.4× apart). Submitted is not approved: the nightly asks the live account which names are actually APPROVED and takes the cheapest, so a name PENDING at Meta degrades rather than silently billing at the marketing rate. |
| `inbound.py` | Inbound classification (activation / STOP / support / other) over the Arabic folding in `arabic.py`. |
| `activation_flow.py` | Token → tenant binding; checks **before** any mutation; the funnel-inheritance exception. A buyer whose channel is still opted out gets a **different welcome** — one message, not two, saying the subscription is running, that his own earlier stop is why nothing will arrive, and the single word that undoes it — plus a `paid_while_opted_out` ticket on the operator's screen. `opt_out_at` is pointedly NOT cleared: a payment does not override a standing compliance instruction. **Renewal is covered too, and by the same code rather than a second decision**: `salla/provisioning.provision_order`'s RENEWED branch calls `ticket_if_silenced` **inside the money's own transaction**, so the charge and the reason nobody knows about it commit together or not at all — and that is the *common* route, since a customer who silences us without cancelling his billing never passes through activation again. He is sent nothing, on purpose: the opted-out welcome could go to a silenced number because it answered a message he had just typed, and a renewal is a charge from the storefront, so the same send would be us breaking his instruction to reassure ourselves. `_announce_renewal` no longer reports him with the ordinary shut-window sentence either — a shut window reopens the moment he writes, this one only he can lift — and it hands the operator the money decision explicitly. (A duplicate `activation.py` was deleted — D25: it stripped only `+` from the phone, so any number an operator typed with a space or a dash produced a dead `wa.me` link.) |
| `phones.py` | Phone-shape tolerance: Meta sends `from` without the leading `+` while operator settings are written with it, and every literal comparison silently missed. |
| `samples.py` | The «عينة» request — a prospect asking to see the work before paying, which the store copy promises literally. |
| `worker.py` | Conversation worker: idempotency, DB-verified ownership, routing, outcome buttons, receipts, retry budget + dead letter. Its **last** branch escalates a لمّاح+ customer's ordinary message to `promises/career_session.escalate_direct_message` — last, and not by keyword, because two live incidents came from command sets built on common Arabic nouns, and a keyword tight enough to be safe matches almost nothing a person writes. The branch turns on two **independent** facts, `wrote` and `spoke`, and on neither `landed` nor a tap: `landed` is a *delivery* event, and since `descend_pending_delivery` fires on any inbound while a bundle is held, the first message after a quiet day is precisely the one that lands one — so folding it into the condition dropped the single likeliest real question of the day. A **tap is never escalated as a rule**, because a tap can only have come from a card we sent and WhatsApp keeps cards tappable forever, so a stale one used to spend the customer's one open ticket. Recorded messages escalate too — voice, image and video (`_SPOKEN_MEDIA` → the `unreadable` noun changes the alert's *wording*, never whether it fires), because a voice note is the commonest thing a Saudi customer sends and a tier that answers it with «I can only read text» is selling a file format instead of access. That set is deliberately not «everything that is not text»: a sticker, a reaction, a location and a contact card are not messages anyone waits on an answer to, and a *document* is excluded because a file already pages the operator on every tier. `inbound_message_id` travels with the ticket so the queue says what he is waiting on, not just that he is. What the customer hears is deliberately unchanged: the escalation guarantees a human is *reached*, never that one is awake. |

### `src/career/onboarding/` — C5

| File | Purpose |
|------|---------|
| `fsm.py` | 10-state forward-only journey + the single regression edge + reminder predicate. |
| `orchestrator.py` | Conversation brain: consent, 14 questions, batch confirmation, privacy commands, reminders. |
| `collection.py` | Question bank (labels ≤ 20 chars — WhatsApp cap) + answer parsing. |
| `consents.py` | Purpose-separated append-only consent ledger + fail-closed gate. |
| `upload.py` | Six-stage hardened upload pipeline (size → sniff → scan → inspect → sanitize → sandbox) + the clamd client, its three-way readiness state and the declared policy for a file no engine could read. |
| `extract_worker.py` | Sandbox child: rlimits before parsing, defusedxml, output caps. |
| `extraction.py` | PII strip → assert-absent → Claude structured extraction (results ≠ truth). |
| `confirmation.py` | Facts → achievement bank; rejections feed `forbidden_claims`. |
| `paths.py` | Three-layer career-path assessment with weak-fit override. |
| `policy.py` | Versioned search-policy builder + Arabic summary card. |
| `privacy.py` | Export / pause / resume / two-step delete honoring retention — what deserves deletion deserves delivery, and a structural test holds the two lists equal (D19). |
| `retention.py` | The 90-day retention promise with an owner at last: three published texts promised it and nothing ran it. |
| `enrichment.py` | Thin-role enrichment (F-ENRICH): post-activation, opportunity-triggered, once ever per role. |
| `achievement_render.py` | Colloquial Arabic → one grounded English bullet, behind `bullet_is_grounded` — a deterministic cross-lingual guard that rejects any number or entity absent from the raw Arabic. |
| `bullet_panel.py` | The quality panel: nothing reaches the customer on a single attempt (F-PANEL). |
| `intent.py` | «What does this customer WANT?» — the wider question that replaced «is this text an achievement?» (F-INTENT). |

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
| `cli.py` | Timer entry point: run → alerts → credit watch → delivery phase → §05 lifecycle sweep. The sweep runs **one transaction per customer** (`sweep_lifecycle_per_customer`), not one for the night: its idempotency *is* the `subscription_events` mark it writes beside every send, so a single exception used to roll back the marks of every customer already processed — Meta had been charged for those templates, the database recorded none of them, and the next night re-sent and re-paid for all of them. Scoping is by customer and not by subscription (`_scope_to_one_customer`, via `with_loader_criteria`): the sweep asks a within-tenant question — «is there a later live row for this customer» — and a per-subscription scope silently answers no, walking a renewed customer through his previous period again. The boundary is the customer and not the *step*, because committing between the ACTIVE→GRACE transition and the renewal reminder it triggers would lose the reminder rather than retry it. What remains non-transactional is said out loud: a template already sent and then rolled back is sent again — that residue is not removed, it is bounded to the one customer whose transaction failed. A sweep that survived a customer's failure exits `EXIT_LIFECYCLE_FAILED` (`grep -n '^EXIT_' src/career/engine/cli.py` for the full set). |

### `src/career/cv/` — C7

| File | Purpose |
|------|---------|
| `schemas.py` | Pydantic models: `MasterCV`, `TailoredCV`, `ContactInfo`. |
| `template.py` | The v5 CV HTML template, byte-verbatim from the legacy reference, guarded by a test that re-extracts it. (The cover-letter template went with its renderer — D23.) |
| `render.py` | WeasyPrint with pinned fonts, exactly one A4 page. Same input → the same **document**, not the same bytes: PDF bytes depend on the font stack and fontconfig cache state, so nothing may key on a PDF hash for identity. |
| `normalize.py` | Achievement bank → `MasterCV` normalization rules. |
| `enforce.py` | One-page enforcement: caps, pacing, JD-ranked fill — no invention. |
| `validate.py` | Pre-render validator: Arabic-leak (blocking) + structure + style; reason codes only. |
| `prompts.py` | §10 prompts byte-verbatim; literal `.replace` filling (format-injection-proof). |
| `generate.py` | Tailoring chain + every guard + rule fallbacks + the Anthropic client with token metering. |
| `publish.py` | Atomic pair publish; `validate_cv_binding` — the sole send authority; resolver; budget; quarantine. |
| `deliver.py` | Arabic job cards, display filenames, grouped bundles, outcome buttons. |
| `close.py` | The daily-state authority (`DAILY_STATES` is the vocabulary — see invariant 12), sole writer of the day-state table, admin summary, usage + cost rollups with refunded/cancelled/charged-back revenue excluded. |
| `daily_run.py` | Delivery-day orchestration: tenant isolation, stale-held expiry, honest closes, cost metering. |
| `outcome_followup.py` | The outcome question 14 days after an application — once ever, free-window only, never a paid template. The only measure the product is judged by. |

### `src/career/funnel/` — C8

| File | Purpose |
|------|---------|
| `flow.py` | Purchase → consent → upload → report → DONE; reuses C5 authorities; reopen on repurchase. |
| `evaluation.py` | Deterministic five-score analysis via the same path engine; Arabic notes; upgrade steps. |
| `report.py` | One-page Arabic RTL PDF + WhatsApp summary. |

### `src/career/telegram/` — operator watchtower

> **Every line in this package is direction-pure, and that is enforced structurally** by
> `tests/test_alert_direction_purity.py`. Fahad reads these alerts on a client that **reverses any
> line mixing Arabic with Latin letters or digits**, so «↻ تجديد TEN-0002 — الفترة الجديدة تنتهي»
> arrived scrambled on every single renewal, with the TEN code — the only part that says *whose*
> renewal it is — as the piece that moved. This repo had fixed that defect by hand at least three
> times and it came back three times, because each fix was a private assertion about one string in
> one test file, and a string nobody remembered to list was a string nobody checked.
>
> **The guard takes no list of strings: it reads the source.** It parses `admin.py` to learn which
> methods carry a `text` parameter — those *are* the operator channel, so a fifth one added
> tomorrow is covered without editing the guard — then resolves each text argument backwards to
> the strings that can reach it, promoting any thin wrapper that forwards its own parameter into a
> sink (`salla/provisioning._alert`, `promises/guarantee._alert`, `promises/career_session`) to
> being a sink itself. This whole package is in scope by construction. Customer-facing Arabic is
> deliberately **out** of scope — it mixes scripts on purpose («أرسل سيرتك كملف PDF أو Word») and
> is read on the customer's own client, and a guard that flagged it would be switched off by
> lunch. Three of its five tests check the *guard* rather than the tree, including one that proves
> it can still see a violation at all.

| File | Purpose |
|------|---------|
| `admin.py` | Bot API client: sanitized sends, inline keyboards, long-poll, body-free errors. **The bidi guard reads this file to derive what counts as an operator-facing sink** — its `text`-carrying methods define the channel. |
| `console.py` | Stateless router: operator allowlist, self-contained callbacks, PII-free readers. The customer card's refund line is **computed** — `refund_deduction_sar` + `completed_sessions_count` scoped to the subscription row being displayed, with that period named on screen — rather than the constant it printed under a comment claiming it was computed. Naming the period is half the fix: «150 will be deducted» is not an answer without «from the refund of which order», and this card can show a session from a different period than the current one. Its `_TICKET_KIND_AR` is the only place a `support_events` kind is ever turned into words a human reads — this line used to say «its tickets **screen** is the only place», and that stopped being true the day `release_forgotten_tickets` shipped: the map acquired a reader that is not the screen at all — the 48-hour release alert, which pages from `run_worker_loop` while the operator sleeps. (The three dedupes elsewhere — `funnel/flow.py`, `promises/career_session.py`, `whatsapp/activation_flow.py` — match a kind against their own constant in a `WHERE` clause and never render one, which is why they were never what this sentence was about.) It labels each kind in the words of what happened to the customer — including `paid_while_opted_out` («💸 دفع أو جدّد وهو موقف الرسائل»), which nothing else says: the night closes such a customer `SKIPPED_OPTED_OUT`, an honest state that exits zero, so every other screen reads a charge against an undeliverable service as the customer's own choice. |
| `views.py` | Pure Arabic screen renderers — golden-tested; carries the health screen (deploy drift, scanner state) and the لمّاح+ star. |
| `messages.py` | Small admin-message builders (TEN codes only). |
| `weekly_report.py` | The Sunday operator report — a pure formatter; **the owed week is durable** (`0023`), so a restart cannot re-send it and an outage cannot lose it. This report goes to the OPERATOR: the customer-facing «تقرير أسبوعي» in the whitepaper's 449 tier is an entitlement nothing reads, and the name collision is what hid it. |

### `src/career/storage/`

| File | Purpose |
|------|---------|
| `storage/adapter.py` | `StorageAdapter` protocol (S3-compatible) + tenant key helpers. |
| `storage/filesystem.py` | Atomic implementation: temp → write → fsync → replace → dir fsync. |

### `src/career/promises/` — what the pages sell, kept

| File | Purpose |
|------|---------|
| `guarantee.py` | The 72-hour start guarantee, anchored on **onboarding completion** (renewal gives every period a new row, so anchoring there would restart the promise at each renewal); «the first opportunity arrived» means a day state with `delivered > 0`. Refunds stay in the owner's hands. |
| `career_session.py` | The لمّاح+ career session: a state ledger, one per period enforced by a partial unique index, escalating to a ticket after 24 hours. The trigger is the operator's — the page sells a human. Also the tier's **direct line** (`escalate_direct_message`) and the **refund deduction** (`refund_deduction_sar`, `completed_sessions_count`) the refund page publishes; the deduction is scoped to one subscription because a customer in his fourth period would otherwise have had four sessions deducted from one order's refund. Both return a named outcome rather than a boolean — «not on that tier», «lapsed» and «already has a ticket open» are three different answers, and a caller that cannot tell them apart cannot log the truth about any of them. |
| `price_lock.py` | The founder price lock: consulted only inside the amount-mismatch branch, one exact amount (never a ceiling), only **below** today's price, lapsing after the continuity window the store page itself states. |

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
| `0019` | `usage_events` prompt-cache token columns (§14 cost coverage). |
| `0020` | لمّاح pricing: one pass at 199, `basic` retired from sale (kept — subscriptions point at it). |
| `0021` | RLS fails closed + a named cross-tenant sweep capability (FORCE was decorative while every long-running process opened an owner engine). |
| `0022` | `webhook_events` retry budget + dead letter for the WhatsApp worker (any exception used to be terminal, and nothing ever selects `failed`). |
| `0023` | `admin_bot_state.weekly_report_sent_for` — the owed week, durable; it was a process local, so a Sunday restart re-sent the report and an outage lost the week. |
| `0024` | `webhook_events` tenant subject + raw-body redaction date — the table had no tenant column, so raw bodies carrying names and message text were in no §12 deletion, export or pruning path. |
| `0025` | Delivery-guarantee ledger — the 72-hour start promise, anchored on onboarding completion. |
| `0026` | `career_sessions` + the partial unique index that makes «one per period» a constraint rather than a date calculation. |
| `0027` | `price_locks` — the founder's exact amount, with the continuity window the store page states. |
| `0028` | `delivery_messages.channel_id` optional — the two lifecycle templates that are sent before any channel row exists were billable and unrecordable, so `close.whatsapp_spend` under-counted the WhatsApp bill (100% of it for a paid order that never activates). |
| `0029` | Drops `outbox_events` + `processed_messages` — the queue subsystem is gone (D24), and it rebuilds `app.tenants_with_pending_work()` first, because a LANGUAGE-sql body carries no catalog dependency and would have started raising at the next retention sweep instead of failing the drop. |

**Authored is not applied.** Ask both sides rather than reading the last row —
and note the *form* of each command, because neither `alembic` nor `psql` is on
this host's `PATH`: the first lives only in the venv, the second only inside the
Postgres container. A command that errors is worse than no command here, since
the whole point of the section is that the command IS the number.

```bash
.venv/bin/alembic heads                                  # authored head
docker exec career_staging-postgres-1 \
  psql -U career_owner -d career_staging \
  -tAc 'SELECT version_num FROM alembic_version'         # deployed head
```

Neither answer is written down here on purpose — a number in this file is a
number somebody has to remember to change, and the last two that were frozen
into it were both stale within a day. **They disagreed when this paragraph was
last measured**, and the code in this checkout queries columns the later
migrations add (`worker.py` selects `next_attempt_at`, which is `0022`).
**Migrate before deploying, or the polling loop fails on every cycle** — which
is not a hypothetical: it is what happened for nine hours on 2026-08-06. Note
also that `0029` DROPS two tables, so it is not part of a routine catch-up and
needs its own decision.

### `scripts/` — live runners

| Script | Purpose |
|--------|---------|
| `run_worker_loop.py` | Conversation service: WhatsApp + Salla + reminder sweep + window nudge + heartbeat — and, last in the hourly block, the **forgotten-ticket sweep** (`sweep_forgotten_tickets` → `telegram/console.release_forgotten_tickets`). That one is here and not on the console because the operator who stopped opening the watchtower is exactly the operator who forgot the ticket, and a ticket nobody closes mutes that customer's direct line for as long as it stays open. |
| `run_nightly.py` | Engine CLI wrapper (systemd target). |
| `run_admin_bot.py` | Watchtower long-poll + live health probes. |
| `ci_create_app_role.py` | Non-superuser app role for CI. |
| `demo_engine_tenants.py` | Demo tenants for live engine proofs. |
| `replay_lost_events.py` | Recovery for events a broken sweep marked `ignored`: dry-run by default, compares every candidate against `delivery_messages` and refuses anything that could message a customer twice. Written for the 3 August incident. |
| `refresh_salla_token.py` | Renews the Salla credential inside its expiry margin, on the `career-salla-token` timer. Salla's refresh tokens are single-use, so it holds a lock — two exchanges racing each other make Salla revoke everything and force the merchant to reinstall the app. |
| `consume_stored_authorize.py` | Applies an `app.store.authorize` webhook still sitting unread in `webhook_events`. Two situations leave one behind, both live history: the event was not handled at all before CHANGELOG §29, and since 2026-08-07 a credential from a merchant we cannot prove is ours is refused rather than swallowed. |
| `gate.sh` | CI's exact sequence — lint, types, migration drift, pytest. |
| `deploy_preflight.sh` | The other half of the 2026-08-06 split: `gate.sh` answers «is this code safe to push» and deliberately ignores host drift, because a commit gate that stays red until somebody deploys blocks everyone and gets ignored. This one answers «may this machine be called deployed» — exit 0 only if `/etc/systemd/system` holds this repository's units **and** systemd has actually loaded them. |
| `alert_unit_failure.sh` | The operator's «a unit stopped doing its job» message, with two callers. `ExecStopPost=` on the two always-on loops is the primary one — systemd hands it `$SERVICE_RESULT` and it speaks only for `watchdog`/`timeout`, staying silent for the ordinary restart of a deploy. `OnFailure=career-alert@%n` is the secondary one and reaches only the oneshots, which are the units that can actually enter `failed`. |

### `ops/`

| Path | Purpose |
|------|---------|
| `systemd/career-worker.service` | Conversation loop, `Type=notify` + `WatchdogSec=300`. `Restart=always` kept it *alive* through both silent incidents — 34 failed cycles against a restarting Postgres on 4 August, then nine hours of every cycle failing on a column staging does not have on 6 August — with `systemctl` reporting active throughout. `READY=1` once after the boot checks and `WATCHDOG=1` at the **end of each completed cycle** is what makes «a cycle finished» observable rather than «the process exists»; `ExecStopPost=` carries the alert because a watchdog kill restarts and by arithmetic never reaches `failed`. The file's own comments hold the measurements and the deploy order. |
| `systemd/career-engine-nightly.{service,timer}` | 11:00 Riyadh nightly (Persistent=true) — the timer file itself is the authority and carries the why. |
| `systemd/career-admin-bot.service` | Watchtower (isolated from the worker); the same `Type=notify` + watchdog + `ExecStopPost=` wiring, at `WatchdogSec=240` read off this bot's own long-poll numbers rather than copied from the worker's. |
| `systemd/career-backup.{service,timer}` `systemd/career-restore-test.{service,timer}` | Daily backup + monthly restore drill. |
| `systemd/career-alert@.service` | `OnFailure=` handler: tells the operator a unit stayed down. It could not fire at all until 2026-08-06 — `RestartSec=5` against the default 10-second window allowed two starts, so the rate limit was never exhausted and no always-on unit ever reached `failed`. `tests/test_ops_watchdogs.py` recomputes that arithmetic, **from the unit files in this repository** — that check does not read the host, which is why the deployed copies could stay the old ones for nine hours on 2026-08-06 with the suite green. The host is now read by a separate check in the same file (`TestTheLiveHostRunsWhatTheRepositorySays`), comparing `ops/systemd/` against the installed file AND against systemd's loaded properties, with a bounded grace for a deploy between `cp` and `daemon-reload`. **It is red on this machine today** — see [Operations](#operations) for what has drifted. |
| `systemd/career-post-boot.service` | Post-reboot self-report: the server says what came back up. |
| `systemd/career-verify-restore.{service,timer}` | Independent verification that the backup a restore drill produced is the one that would actually be restored. **Authored here and not installed on this host** — `systemctl is-enabled career-verify-restore.timer` answers `not-found`, so the verification described in this row is not running. |
| `systemd/career-salla-token.{service,timer}` | 09:00 Riyadh daily: renew the Salla credential before it expires, not after. Daily rather than weekly because the script only acts inside a margin of a few days, and daily is what turns that margin into a retry budget — a Salla outage or a host that was off costs nothing. 09:00 because the only recovery that exists when this job gives up is Fahad reinstalling the app on his store, and an alert next to the 03:30 backup is one he reads after the credential is dead. **Authored here and not installed on this host either** — so today the renewal does not run by itself. |
| `backup/backup.sh` | Encrypted restic backup to B2: pg_dump + host storage root + volume + host config bundle. |
| `backup/restore-test.sh` | Monthly restore drill (tables, RLS, a real tenant file, the config bundle). |

### `docs/`

| File | Purpose |
|------|---------|
| `WHITEPAPER.html` | Product constitution: phases + exit conditions, the 15 invariants, pricing. |
| `CHANGELOG-v1.1.md` | Approved post-v1.0 decisions (override conflicting v1.0 text). |
| `DEVIATIONS.md` | Every approved deviation from the legacy reference, with its reason and its approver. Numbered `D1…`; `grep -c '^| D\|^## D' docs/DEVIATIONS.md` counts them, so no count lives here to go stale. |
| `PLAN.md` | Full execution plan. |
| `PROGRESS.md` | Session-by-session state + exit gates + pending decisions. |
| `PRODUCTS-SHEET.md` `STORE-PAGES-AR.md` | What gets pasted into Salla, verbatim. Guarded by `tests/test_docs_truth.py`: a retired price fails CI, and so does a **new or reworded line on a product card** — bullet, headline or prose, compared whole rather than as a substring — every promise on the three plan cards is registered against the code that makes it true, so a reworded sale of an unbuilt feature fails by not being in the register rather than by being recognised. The register covers §6 of that sheet; the refund, privacy and founding blocks are prose and are not covered. **Read the `⛔ اقرأ قبل اللصق` block at the top of `STORE-PAGES-AR.md` before pasting anything**: it holds back the sentences the code does not currently keep, marks which of those have since shipped, and is not itself store copy. **It is measured by hand and can lag the tree, and it does today**: the 449 tier's direct line shipped on 2026-08-07 (see `whatsapp/worker.py` above) while that block still describes it as half-wired, because releasing a sentence is a separate act from wiring the code and belongs to whoever owns that file. Treat the block as the floor — never paste a sentence it holds back — and `docs/DEVIATIONS.md` D26 as the current judgement. Nothing there is a softened sale — no sales sentence in that file has been edited; the argument for that is in `docs/DEVIATIONS.md` D26. |
| `RUNBOOK-DISASTER-RECOVERY.md` | Restore from nothing: database, storage root, host config, the exact commands. |
| `ADMIN_BOT_DESIGN.md` | Watchtower design + external-proposal evaluation. |
| `LEGACY_KNOWLEDGE.md` | Read-only reference from the proven personal engine. |
| `ARCHITECTURE-AR.md` | Arabic file-by-file walkthrough. |
| `AUDIT-*.md` `CLOSURE-AUDIT-*.md` `BRAND-NAME-*.md` | Dated audit records — history, not instructions. |
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
11. Workers re-verify ownership from the DB — never trust a payload. There is
    no queue to trust any more (D24), and the live entry points never read a
    tenant id out of a request: it is derived from the phone, the order, or the
    active set, so a forged id is not rejected — it is **never read**. A ratchet
    test fails the day any entry point starts reading one.
12. One honest state per customer/day — no silent success. Seven computational
    states, plus the event-driven eighth (`SKIPPED_OPTED_OUT`, CHANGELOG §12).
    `career.cv.close.DAILY_STATES` is the vocabulary; nothing restates it.
13. No PII in logs or the admin channel — TEN codes only; secret filters everywhere.
14. Backups encrypted, off-server, restore-tested periodically.
15. **The whitepaper precedes the code.**

## Security model

- **Tenant isolation** — every table carrying `tenant_id` under
  `ENABLE + FORCE` RLS with a fail-closed transaction-local GUC; adversarial
  tests (forged writes, cross-reads, no-context) run in CI. The count is not
  maintained by hand — `tests/test_rls_meta.py` derives the table list from the
  catalog and fails CI if any of them loses ENABLE, FORCE or its policy.
  **Read D16/D20/D21 before trusting that sentence about the RUNNING system:**
  the three long-lived processes still open their engine as the owner role,
  which bypasses RLS by definition, so isolation in production rests today on
  the hand-written `WHERE tenant_id = …` in ~22 modules. Migration `0021`
  builds the database half (fail-closed + a narrow named sweep capability +
  audited break-glass) and is **authored, not applied**; moving the call sites
  is staged deliberately rather than shipped as a half-applied role change.
  And RLS here buys a forgotten predicate becoming a visible error — not
  containment of a compromised process: PostgreSQL 16 does not enforce
  `GRANT SET ON PARAMETER` for a custom GUC namespace, measured, not assumed.
- **Webhooks** — constant-time HMAC on both providers; empty secret verifies
  nothing; dedupe before processing.
- **Uploads** — magic-byte sniffing, structural inspection, metadata
  sanitization, rlimited sandbox extraction, and a malware engine (clamd over
  its unix socket) that is either **answering, unreachable, or absent** — the
  state is a light on the watchtower health screen, never an assumption. An
  upload nobody could scan is accepted or refused by a *declared* policy and
  its row says `unscanned`; it is never recorded as `clean`.
- **LLM boundary** — PII stripped and asserted-absent before any call;
  byte-verbatim prompts; literal placeholder filling; invented-content +
  forbidden-claims + Arabic-leak output guards; rule fallbacks everywhere.
- **Secrets** — untracked env files; redaction filters + registered literals
  on every logger; error types carry codes, never bodies.

## Operations

```text
career-worker.service          conversation loop (3s poll, Type=notify, 300s watchdog)
career-engine-nightly.timer    11:00 Asia/Riyadh, Persistent=true
career-admin-bot.service       operator console (long-poll)
career-backup.timer            daily encrypted backup -> Backblaze B2
career-restore-test.timer      monthly restore drill
career-post-boot.service       the server reports its own recovery after a reboot

career-salla-token.timer       renew the Salla credential before it expires
career-verify-restore.timer    independent check that the backup a drill made
                               is the one that would actually be restored
```

`systemctl list-timers 'career-*'` is the authority on what is armed; this
block says what each one is FOR. **The two are not the same question, and
today they give different answers**: `ops/systemd/` is the repository's intent,
`/etc/systemd/system/` is what the host runs. The always-on services are
DRIFTED on this host — the deployed copies are still `Type=simple`, with no
watchdog and no `ExecStopPost=` alert — and some units were never installed at
all, `career-salla-token.{service,timer}` among them since 2026-08-07. That is
a **half-finished deploy, not a missing feature**. Which units, on which side,
is measured and never quoted from here — a list in prose goes stale between a
deploy and a reader, and this one already did:

```bash
systemctl list-timers 'career-*' --all
for f in ops/systemd/*; do b=$(basename "$f"); \
  [ -e "/etc/systemd/system/$b" ] || { echo "NOT-DEPLOYED  $b"; continue; }; \
  diff -q "/etc/systemd/system/$b" "$f" >/dev/null || echo "DRIFTED       $b"; done
# and the same comparison as a test, including systemd's loaded properties:
.venv/bin/python -m pytest tests/test_ops_watchdogs.py -k drift -q
```

Installing them is a `/etc` write, which the tooling's permission classifier
refuses — so it waits on the owner's hand, alongside the staging migration and
`ops/caddy/Caddyfile`.

The Telegram watchtower gives the operator daily summaries, per-tenant cards
(TEN codes only), platform health, business numbers, sanitized error tails,
and proactive alerts.

## Testing

```bash
# disposable DB — NEVER run pytest against staging (a hard guard in
# tests/conftest.py refuses any DB whose name does not end in _test).
#
# The three passwords are NOT optional: without them every DB-backed test
# errors on connect. Read them out of the untracked .env.staging rather than
# retyping secrets — and read only those keys: `source`-ing the whole file
# hands its JSON values to bash as commands.
for k in DB_OWNER_PASSWORD DB_PASSWORD REDIS_PASSWORD; do
  export "$k=$(grep -m1 "^$k=" .env.staging | cut -d= -f2-)"
done
export DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=career_test
export REDIS_HOST=127.0.0.1 REDIS_PORT=6380
export SALLA_WEBHOOK_SECRET=test_secret_123
export WHATSAPP_APP_SECRET=wa_app_secret_123 WHATSAPP_VERIFY_TOKEN=wa_verify_123
export CI_REQUIRE_DB=1
.venv/bin/python -m pytest tests/ -q

# or simply, which runs CI's exact sequence including lint, types and drift:
./scripts/gate.sh

# How many tests are there? Ask, never remember. This README carried «944
# passing» for long enough that it was wrong by dozens in both directions,
# and a number nobody can regenerate is a number nobody corrects:
.venv/bin/python -m pytest --collect-only -q | tail -1   # collected count
ls tests/test_*.py | wc -l                               # test files
# (collected > `def test_` count: parametrize expands.)
```

Zero skips with a full environment: adversarial RLS (plus a
catalog **meta-test** enforcing ENABLE+FORCE+policy on every tenant table),
webhook
idempotency, upload attack files, CV binding/quarantine, the honest-state
failure matrix, delivery failure isolation, golden Arabic renderers,
byte-verbatim template/prompt guards, full journey E2Es from raw Meta
payloads. CI: ruff → strict mypy → alembic upgrade + drift check → pytest
against real Postgres/Redis.

## Configuration

Every variable is documented in [`.env.example`](.env.example):

| Group | Keys |
|-------|------|
| Database | `DB_*` (app role) + `DB_OWNER_*` (migrations/system) |
| Salla | `SALLA_WEBHOOK_SECRET` · `SALLA_API_KEY` · `SALLA_PRODUCT_CATALOG` · `SALLA_PRODUCT_PRICING` · `SALLA_STORE_ID` · `SALLA_STORE_URL` · `SALLA_TOKEN_EXPIRES_AT` · and the three the renewal needs — `SALLA_CLIENT_ID` · `SALLA_CLIENT_SECRET` · `SALLA_REFRESH_TOKEN`. This table is a summary and `.env.example` is the list, so `grep -o '^SALLA_[A-Z_]*' .env.example` settles any disagreement — **including one open today**: `salla/tokens.py` reads `SALLA_STORE_ID` (it is what makes a signed webhook provably ours) and the template does not carry it. |
| WhatsApp | `WHATSAPP_ACCESS_TOKEN` · `WHATSAPP_PHONE_NUMBER_ID` · `WHATSAPP_WABA_ID` · `WHATSAPP_APP_SECRET` · `WHATSAPP_VERIFY_TOKEN` |
| LLM / search | `ANTHROPIC_API_KEY` · `SEARCHAPI_API_KEY` |
| Operator | `TELEGRAM_ADMIN_BOT_TOKEN` · `TELEGRAM_ADMIN_CHAT_ID` · `CANARY_TEST_PHONE` |
| Upload scanning | `CV_SCAN_CLAMD_SOCKET` (empty = no engine, and the health screen says so) · `CV_SCAN_TIMEOUT_S` |

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
