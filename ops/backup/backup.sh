#!/usr/bin/env bash
# Encrypted off-server backup (whitepaper §15.14, PLAN P-B.5).
#
# restic encrypts client-side (AES-256) before anything leaves this server, so
# the destination (Backblaze B2) only ever holds ciphertext. Backs up a nightly
# pg_dump of the staging database plus the per-tenant object-storage volume.
# .env files are NEVER backed up — secrets do not leave the server.
#
# Config comes from an untracked env file (default /root/.config/career-backup/
# backup.env), which sets RESTIC_REPOSITORY, RESTIC_PASSWORD, the B2 keys, and
# the DB/container settings. Run: ops/backup/backup.sh
set -euo pipefail

ENV_FILE="${CAREER_BACKUP_ENV:-/root/.config/career-backup/backup.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "backup: env file not found: $ENV_FILE" >&2
  exit 2
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD must be set}"
PG_CONTAINER="${PG_CONTAINER:-career_staging-postgres-1}"
PG_OWNER="${DB_OWNER_USER:-career_owner}"
PG_DB="${DB_NAME:-career_staging}"
STORAGE_VOLUME="${STORAGE_VOLUME:-career_staging_storage_data}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-6}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "backup: repository = $RESTIC_REPOSITORY"

# Initialize the repo on first run (idempotent — ignore 'already initialized').
if ! restic cat config >/dev/null 2>&1; then
  echo "backup: initializing repository"
  restic init
fi

# 1. Custom-format pg_dump to a temp file first (so a dump failure aborts BEFORE
#    we upload anything — set -e catches it), then stream it into restic with a
#    STABLE filename so the restore test can address it deterministically.
DUMP="$WORK/${PG_DB}.dump"
echo "backup: dumping $PG_DB"
docker exec "$PG_CONTAINER" pg_dump -U "$PG_OWNER" -Fc "$PG_DB" > "$DUMP"
echo "backup: dump size $(du -h "$DUMP" | cut -f1)"
restic backup --stdin --stdin-filename "${PG_DB}.dump" --tag staging --tag db < "$DUMP"

# 2. Back up the REAL per-tenant object storage. The host services (worker,
#    nightly, admin bot) write to STORAGE_ROOT on the host filesystem — the
#    docker volume only serves the compose api container. Audit fix: the
#    volume alone was backed up while every real CV/upload lived on the host
#    path, so tenant files had never actually been protected.
STORAGE_ROOT="${STORAGE_ROOT:-/root/career/data}"
if [[ -d "$STORAGE_ROOT" ]]; then
  echo "backup: backing up host storage root $STORAGE_ROOT"
  restic backup --tag staging --tag storage "$STORAGE_ROOT"
fi
if docker volume inspect "$STORAGE_VOLUME" >/dev/null 2>&1; then
  STORAGE_PATH="$(docker volume inspect -f '{{.Mountpoint}}' "$STORAGE_VOLUME")"
  if [[ -d "$STORAGE_PATH" ]]; then
    echo "backup: backing up storage volume"
    restic backup --tag staging --tag storage-volume "$STORAGE_PATH"
  fi
fi

# 4. Retention: prune old snapshots.
echo "backup: applying retention ($KEEP_DAILY d / $KEEP_WEEKLY w / $KEEP_MONTHLY m)"
restic forget \
  --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" \
  --prune

echo "backup: done"
restic snapshots --compact | tail -5
