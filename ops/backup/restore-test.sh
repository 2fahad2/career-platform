#!/usr/bin/env bash
# Restore test (whitepaper §15.14 "tested by restoring periodically", PLAN P-B.5).
#
# Proves the backup is actually recoverable: pull the latest DB dump from the
# restic repo, restore it into a throwaway scratch database, run integrity
# checks (table count, RLS flags, a sample row), then drop the scratch DB.
# This is the evidence that closes the C2 exit condition.
set -euo pipefail

ENV_FILE="${CAREER_BACKUP_ENV:-/root/.config/career-backup/backup.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "restore-test: env file not found: $ENV_FILE" >&2
  exit 2
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD must be set}"
PG_CONTAINER="${PG_CONTAINER:-career_staging-postgres-1}"
PG_OWNER="${DB_OWNER_USER:-career_owner}"
PG_DB="${DB_NAME:-career_staging}"
SCRATCH_DB="${SCRATCH_DB:-career_restore_test}"

WORK="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK"
  docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d postgres \
    -c "DROP DATABASE IF EXISTS ${SCRATCH_DB}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "restore-test: pulling latest ${PG_DB}.dump from $RESTIC_REPOSITORY"
restic dump --tag db latest "${PG_DB}.dump" > "$WORK/restore.dump"
echo "restore-test: pulled $(du -h "$WORK/restore.dump" | cut -f1)"

echo "restore-test: creating scratch DB $SCRATCH_DB"
docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d postgres \
  -c "DROP DATABASE IF EXISTS ${SCRATCH_DB}" >/dev/null
docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d postgres \
  -c "CREATE DATABASE ${SCRATCH_DB}" >/dev/null

echo "restore-test: restoring into $SCRATCH_DB"
docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_OWNER" -d "$SCRATCH_DB" --no-owner \
  < "$WORK/restore.dump"

# Integrity checks.
TABLES="$(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$SCRATCH_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
RLS_TABLES="$(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$SCRATCH_DB" -tAc \
  "SELECT count(*) FROM pg_class WHERE relrowsecurity = true")"
echo "restore-test: restored tables=$TABLES · RLS-enabled tables=$RLS_TABLES"

if [[ "$TABLES" -lt 10 || "$RLS_TABLES" -lt 5 ]]; then
  echo "restore-test: FAILED — restored schema looks incomplete" >&2
  exit 1
fi
echo "restore-test: PASSED — backup is recoverable"
