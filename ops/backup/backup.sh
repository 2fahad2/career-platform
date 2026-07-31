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
# The env file is chosen at runtime, so shellcheck cannot follow it. The
# directive must sit immediately above the `source` STATEMENT — on the old
# one-liner it bound to `set -a` instead and SC1090 still fired, which failed
# CI on every push for ten days while nothing ran it locally.
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD must be set}"
PG_CONTAINER="${PG_CONTAINER:-career_staging-postgres-1}"
PG_OWNER="${DB_OWNER_USER:-career_owner}"
PG_DB="${DB_NAME:-career_staging}"
STORAGE_VOLUME="${STORAGE_VOLUME:-career_staging_storage_data}"
REPO_ROOT="${REPO_ROOT:-/root/career}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-6}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# systemd starts this unit with no HOME, so restic could not locate a cache dir
# and re-fetched repository metadata from B2 on every single run. Harmless in
# isolation, slow and chatty over a year of nightlies.
export HOME="${HOME:-/root}"

echo "backup: repository = $RESTIC_REPOSITORY"

# Initialize the repo on first run (idempotent — ignore 'already initialized').
if ! restic cat config >/dev/null 2>&1; then
  echo "backup: initializing repository"
  restic init
fi

# Clear STALE locks (owner process gone / lock expired) — never live ones;
# plain `unlock` refuses to touch a lock whose process is still running.
# Found the hard way: an interrupted manual restic run left a lock behind on
# 30 Jul, and every nightly since then uploaded its snapshots and then died at
# `forget --prune` with exit 1 — retention silently stopped while the unit sat
# in `failed`. A backup job must not be one killed terminal away from that.
restic unlock >/dev/null 2>&1 || true

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
else
  # §15.12: a missing tenant-storage root is a FAILURE, not a silent skip
  echo "backup: FAILED — storage root missing: $STORAGE_ROOT" >&2
  exit 1
fi
if docker volume inspect "$STORAGE_VOLUME" >/dev/null 2>&1; then
  STORAGE_PATH="$(docker volume inspect -f '{{.Mountpoint}}' "$STORAGE_VOLUME")"
  if [[ -d "$STORAGE_PATH" ]]; then
    echo "backup: backing up storage volume"
    restic backup --tag staging --tag storage-volume "$STORAGE_PATH"
  fi
fi

# 3. Recoverable NON-SECRET host configuration.
#    The dump and the tenant files are only half a recovery: on a fresh host
#    nothing knows how to terminate TLS or what to run. The Caddyfile lives
#    ONLY in /etc/caddy — it is not in git, and the repository map claimed a
#    root `Caddyfile` that had never existed, so losing the server lost the
#    only copy. The installed unit files can also drift from the copies in
#    ops/systemd/, and it is the installed ones that were actually running.
#    Both classes are secret-free by construction: a unit names an
#    EnvironmentFile, it never contains that file's values.
#    Packed as ONE tarball under a stable name so the restore path is as
#    deterministic as the DB dump:
#      restic dump --tag config latest career-config.tar.gz
CONFIG_DIR="$WORK/config"
CONFIG_TAR="$WORK/career-config.tar.gz"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
mkdir -p "$CONFIG_DIR/caddy" "$CONFIG_DIR/systemd"

if [[ -f "$CADDYFILE" ]]; then
  cp "$CADDYFILE" "$CONFIG_DIR/caddy/Caddyfile"
else
  # §15.12 again: the gateway config is part of the service, not an optional
  # extra. Missing means the operator must know NOW, not on rebuild night.
  echo "backup: FAILED — Caddyfile missing: $CADDYFILE" >&2
  exit 1
fi

UNIT_COUNT=0
for unit in "$UNIT_DIR"/career-*.service "$UNIT_DIR"/career-*.timer; do
  [[ -e "$unit" ]] || continue
  cp "$unit" "$CONFIG_DIR/systemd/"
  UNIT_COUNT=$((UNIT_COUNT + 1))
done
if [[ "$UNIT_COUNT" -eq 0 ]]; then
  echo "backup: FAILED — no career-* units found in $UNIT_DIR" >&2
  exit 1
fi

# The manifest answers the questions a rebuild asks and nothing else. Every
# line here is a NAME, a VERSION or a COUNT — never a credential value. The
# env section deliberately lists KEY NAMES ONLY (`grep -oE '^KEY='`), because
# the one thing a 3am rebuild cannot guess is WHICH keys .env.staging needs;
# the values themselves are the owner's to restore (see the DR runbook).
{
  echo "# career-platform host manifest — $(date -Is)"
  echo "# NO SECRETS: names, versions and counts only. Never values."
  echo
  echo "[host]"
  echo "hostname = $(hostname)"
  echo "os       = $(grep -m1 '^PRETTY_NAME=' /etc/os-release 2>/dev/null |
    cut -d= -f2- | tr -d '"' || echo unknown)"
  echo "kernel   = $(uname -r)"
  echo "timezone = $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
  echo "addresses:"
  ip -4 -o addr show scope global 2>/dev/null | awk '{print "  " $2 " " $4}' || true
  echo
  echo "[versions]"
  echo "docker  = $(docker --version 2>/dev/null || echo absent)"
  echo "compose = $(docker compose version 2>/dev/null || echo absent)"
  echo "caddy   = $(caddy version 2>/dev/null | head -1 || echo absent)"
  echo "restic  = $(restic version 2>/dev/null || echo absent)"
  echo "python  = $(python3 --version 2>/dev/null || echo absent)"
  echo
  echo "[code]"
  # Sanitized: if the remote URL ever carries an embedded token, strip it.
  echo "git_remote = $(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null |
    sed -E 's#://[^/@]*@#://#' || echo unknown)"
  echo "git_branch = $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  echo "git_commit = $(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "alembic_head_in_db = $(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$PG_DB" \
    -tAc 'SELECT version_num FROM alembic_version' 2>/dev/null || echo unknown)"
  echo
  echo "[units]"
  systemctl list-unit-files 'career*' --no-legend 2>/dev/null || true
  echo
  echo "[docker]"
  echo "volumes:"
  docker volume ls --format '  {{.Name}}' 2>/dev/null || true
  echo "containers:"
  docker ps --format '  {{.Names}} {{.Image}} {{.Ports}}' 2>/dev/null || true
  echo
  echo "[env-key-names]"
  echo "# .env.staging is NEVER backed up. These are the key NAMES the rebuild"
  echo "# must refill from the owner's password manager / provider dashboards."
  grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$REPO_ROOT/.env.staging" 2>/dev/null |
    tr -d '=' | sed 's/^/  /' || echo "  (.env.staging not readable)"
} > "$CONFIG_DIR/MANIFEST.txt"

echo "backup: config bundle — Caddyfile + $UNIT_COUNT unit(s) + manifest"
tar -czf "$CONFIG_TAR" -C "$WORK" config
restic backup --stdin --stdin-filename career-config.tar.gz --tag staging --tag config < "$CONFIG_TAR"

# 4. Retention: prune old snapshots.
echo "backup: applying retention ($KEEP_DAILY d / $KEEP_WEEKLY w / $KEEP_MONTHLY m)"
restic forget \
  --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" \
  --prune

echo "backup: done"
restic snapshots --compact | tail -5
