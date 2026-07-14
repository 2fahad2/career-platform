# Career Platform

[![CI](https://github.com/2fahad2/career-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/2fahad2/career-platform/actions/workflows/ci.yml)

Paid personal job-search assistant for the Saudi market (WhatsApp + Salla).
It searches daily, filters through each customer's private gate, generates a
tailored English CV per selected opportunity, and delivers it over WhatsApp so
the customer applies themselves. **A tool — not a recruitment agency.**

> **Source of truth:** [`docs/WHITEPAPER.html`](docs/WHITEPAPER.html) +
> [`docs/CHANGELOG-v1.1.md`](docs/CHANGELOG-v1.1.md). The whitepaper precedes the
> code (invariant §15.15). Read [`CLAUDE.md`](CLAUDE.md) before any change.

## Stack (locked)

Python 3.11+ · FastAPI · PostgreSQL 16 (+ Row-Level Security) · Redis ·
SQLAlchemy + Alembic · Docker Compose · pytest. All LLM calls go through the
Anthropic API only.

## Environments

Two fully isolated environments run as separate Compose projects — **separate
databases, secrets, volumes, and ports**. They share nothing.

- **staging** — this Contabo EU server; staging + friends alpha only.
- **production** — moves to a Saudi server before wave 2 (an exit gate).

## Layout

```
src/career/          Application package
  config.py          Settings from the environment
  main.py            FastAPI app + /health (DB + Redis probes)
  db/                SQLAlchemy base, tenant-scoped session, models
  storage/           StorageAdapter (S3-compatible) + filesystem impl
migrations/          Alembic (env + versions)
tests/               pytest — incl. adversarial cross-tenant RLS test
docs/                Whitepaper, changelog, PROGRESS.md, policies/
docker-compose.staging.yml / docker-compose.production.yml
```

## Quick start (staging)

```bash
cp .env.staging.example .env.staging     # then fill real secrets (never commit)
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d
docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm api alembic upgrade head
curl -fsS localhost:${API_PUBLISH_PORT:-8001}/health
```

## Tests

```bash
pip install -e ".[dev]"
pytest            # cross-tenant RLS isolation, etc. (needs a Postgres 16)
```

## Progress

Phase status and exit-condition tracking live in
[`docs/PROGRESS.md`](docs/PROGRESS.md). Work proceeds C0 → C9 per whitepaper §13;
a phase does not open until the previous one's exit condition holds.
