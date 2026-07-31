#!/usr/bin/env bash
# The full commit gate, identical in ORDER to .github/workflows/ci.yml.
#
# Why this exists: for ten days CI was red at «Shellcheck ops scripts» and
# nobody noticed, because shellcheck was not installed locally — so pytest,
# the alembic drift check and the RLS attack tests never ran on any push
# while every local run looked green. A gate that only lives in the cloud is
# a gate you find out about late. Run this before pushing.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

fail=0
step() {
  printf '\n▶ %s\n' "$1"; shift
  if "$@"; then printf '  ✓ pass\n'; else printf '  ✗ FAIL\n'; fail=1; fi
}

step "ruff"        .venv/bin/ruff check src tests scripts
step "mypy src"    .venv/bin/python -m mypy src/
step "mypy scripts" .venv/bin/python -m mypy scripts/ --explicit-package-bases
if command -v shellcheck >/dev/null 2>&1; then
  # scripts/*.sh too: the recovery helpers are exactly the code that must not
  # rot, because nobody runs them until the night everything is already wrong.
  step "shellcheck" shellcheck ops/backup/*.sh scripts/*.sh
else
  printf '\n▶ shellcheck\n  ! not installed — CI WILL still run it (apt-get install shellcheck)\n'
  fail=1
fi
step "lockfile"    test -s requirements.lock

DB_OWNER_PASSWORD=$(grep '^DB_OWNER_PASSWORD=' .env.staging | cut -d= -f2)
DB_PASSWORD=$(grep '^DB_PASSWORD=' .env.staging | cut -d= -f2)
REDIS_PASSWORD=$(grep '^REDIS_PASSWORD=' .env.staging | cut -d= -f2)
export DB_OWNER_PASSWORD DB_PASSWORD REDIS_PASSWORD
export DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=career_test \
  REDIS_HOST=127.0.0.1 REDIS_PORT=6380 \
  SALLA_WEBHOOK_SECRET=test_secret_123 WHATSAPP_APP_SECRET=wa_app_secret_123 \
  WHATSAPP_VERIFY_TOKEN=wa_verify_123 CI_REQUIRE_DB=1

step "alembic drift" .venv/bin/alembic check
step "pytest"        .venv/bin/python -m pytest tests/ -q

if [[ $fail -eq 0 ]]; then
  printf '\n✅ gate green — safe to push\n'
else
  printf '\n❌ gate RED — do not push\n'
fi
exit $fail
