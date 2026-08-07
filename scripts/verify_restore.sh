#!/usr/bin/env bash
# Post-restore verification — the last step of docs/RUNBOOK-DISASTER-RECOVERY.md.
#
# A rebuild at 3am ends with the operator asking one question: "is it actually
# back?" Answering it by eye means remembering nine separate checks while tired,
# and the expensive failures are the quiet ones — the API answers /health while
# the migrations sit two revisions behind, or RLS came back disabled on a table,
# or the timers were copied but never enabled so nothing runs tomorrow night.
#
# Read-only by contract: this script inspects, it never starts, stops, migrates
# or writes anything. Exit 0 = every check passed. Exit 1 = at least one failed.
#
#   scripts/verify_restore.sh
#
# Since 2026-08-06 it also runs WEEKLY, unattended, from
# career-verify-restore.timer (Sunday 05:00 Riyadh) — because most of what it
# asks is a question about today rather than about a restore: is the backup
# chain still producing snapshots, are the render fonts still installed, is RLS
# still forced, is the database at head, are the units and timers still armed.
# The exit code is the whole interface: a non-zero exit puts the unit in
# `failed` and OnFailure pages the operator.
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-/root/career}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env.staging}"
PG_CONTAINER="${PG_CONTAINER:-career_staging-postgres-1}"
PG_OWNER="${DB_OWNER_USER:-career_owner}"
PG_DB="${DB_NAME:-career_staging}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/health}"
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL:-https://api.career-platform.net/health}"
STORAGE_ROOT="${STORAGE_ROOT:-$REPO_ROOT/data}"
MIN_TABLES="${MIN_TABLES:-30}"
MIN_RLS="${MIN_RLS:-20}"

fail=0
pass() { printf '  ✓ %s\n' "$1"; }
bad() {
  printf '  ✗ %s\n' "$1"
  fail=1
}
section() { printf '\n▶ %s\n' "$1"; }

section "1. secrets file present and locked down"
if [[ -s "$ENV_FILE" ]]; then
  # Count key NAMES only — this script never reads or prints a value.
  keys=$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")
  if [[ "$keys" -ge 30 ]]; then
    pass "$ENV_FILE has $keys keys"
  else
    bad "$ENV_FILE has only $keys keys — expected 30+, some are missing"
  fi
  # The rebuild procedure ends this file at 0600 (runbook step six). A live
  # host that drifted looser than the rebuild is the asymmetry worth catching:
  # every provider credential sits in here, and /root being 0700 is then the
  # only thing between them and any non-root process.
  mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null)
  if [[ "$mode" == "600" ]]; then
    pass "$ENV_FILE mode $mode (owner-only, as the runbook sets it)"
  else
    bad "$ENV_FILE mode $mode — expected 600 (run: chmod 600 $ENV_FILE)"
  fi
else
  bad "$ENV_FILE missing or empty — nothing will start without it"
fi

section "2. containers up and healthy"
for svc in postgres redis api; do
  name="career_staging-${svc}-1"
  state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo absent)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$name" 2>/dev/null || echo none)
  if [[ "$state" == "running" && "$health" != "unhealthy" ]]; then
    pass "$name: $state/$health"
  else
    bad "$name: $state/$health"
  fi
done

section "3. migrations at head"
db_head=$(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$PG_DB" \
  -tAc 'SELECT version_num FROM alembic_version' 2>/dev/null | tr -d '[:space:]')
# The repo head is the one revision no other revision points back to.
repo_head=$(
  cd "$REPO_ROOT/migrations/versions" 2>/dev/null || exit 0
  for f in *.py; do
    rev=$(grep -m1 -oE "^revision(: str)? = ['\"][^'\"]+" "$f" | grep -oE "[^'\"]+$")
    [[ -n "$rev" ]] || continue
    if ! grep -qE "^down_revision(: [^=]*)? = ['\"]${rev}['\"]" ./*.py; then echo "$rev"; fi
  done
)
if [[ -z "$db_head" ]]; then
  bad "cannot read alembic_version from $PG_DB"
elif [[ "$(printf '%s\n' "$repo_head" | wc -l)" -ne 1 ]]; then
  bad "migrations/versions has no single head (found: $(echo "$repo_head" | tr '\n' ' '))"
elif [[ "$db_head" == "$repo_head" ]]; then
  pass "alembic at head ($db_head)"
else
  bad "alembic MISMATCH — db=$db_head repo head=$repo_head (run: alembic upgrade head)"
fi

section "4. schema and RLS restored"
tables=$(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null)
rls=$(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM pg_class WHERE relrowsecurity = true" 2>/dev/null)
if [[ "${tables:-0}" -ge "$MIN_TABLES" ]]; then
  pass "tables = $tables (>= $MIN_TABLES)"
else
  bad "tables = ${tables:-?} — expected >= $MIN_TABLES, the restore is incomplete"
fi
if [[ "${rls:-0}" -ge "$MIN_RLS" ]]; then
  pass "RLS-enabled tables = $rls (>= $MIN_RLS)"
else
  bad "RLS-enabled tables = ${rls:-?} — expected >= $MIN_RLS, TENANT ISOLATION IS OFF"
fi
# FORCE matters as much as ENABLE: without it the table owner bypasses every
# policy, and the owner role is exactly what the migrations run as. `tenants`
# is the one documented exemption (migration 0001): it is ENABLE-only on
# purpose so the owner/provisioning role can seed the registry.
noforce=$(docker exec "$PG_CONTAINER" psql -U "$PG_OWNER" -d "$PG_DB" -tAc \
  "SELECT string_agg(relname, ' ') FROM pg_class
   WHERE relrowsecurity AND NOT relforcerowsecurity AND relname <> 'tenants'" 2>/dev/null |
  tr -d '[:space:]')
if [[ -z "$noforce" ]]; then
  pass "every RLS table has FORCE (except the documented 'tenants' registry)"
else
  bad "RLS without FORCE on: $noforce — owner queries bypass tenant policies"
fi

section "5. tenant storage present"
if [[ -d "$STORAGE_ROOT" ]]; then
  files=$(find "$STORAGE_ROOT" -type f 2>/dev/null | wc -l)
  tenants=$(find "$STORAGE_ROOT/tenants" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
  if [[ "$files" -gt 0 ]]; then
    pass "$STORAGE_ROOT: $files file(s) across $tenants tenant dir(s)"
  else
    bad "$STORAGE_ROOT exists but is EMPTY — tenant CVs were not restored"
  fi
else
  bad "$STORAGE_ROOT missing — tenant storage was not restored"
fi

section "6. units installed, enabled and alive"
for unit in career-worker.service career-admin-bot.service; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || echo missing)
  active=$(systemctl is-active "$unit" 2>/dev/null || echo inactive)
  if [[ "$enabled" == "enabled" && "$active" == "active" ]]; then
    pass "$unit: enabled/active"
  else
    bad "$unit: $enabled/$active — expected enabled/active"
  fi
done
for unit in career-backup.timer career-engine-nightly.timer \
            career-restore-test.timer career-verify-restore.timer; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || echo missing)
  active=$(systemctl is-active "$unit" 2>/dev/null || echo inactive)
  next=$(systemctl show "$unit" -p NextElapseUSecRealtime --value 2>/dev/null)
  if [[ "$enabled" == "enabled" && "$active" == "active" && -n "$next" && "$next" != "0" ]]; then
    pass "$unit: enabled/active, next run scheduled"
  else
    bad "$unit: $enabled/$active next='${next:-none}' — it will never fire"
  fi
done
# A timer that fires a unit which has been failing since the last rebuild is
# the quietest failure of all; check the actual results too.
for unit in career-backup.service career-restore-test.service career-engine-nightly.service; do
  result=$(systemctl show "$unit" -p Result --value 2>/dev/null)
  if [[ "$result" == "success" || -z "$result" ]]; then
    pass "$unit: last result ${result:-never-run}"
  else
    bad "$unit: last result '$result' — read: journalctl -u $unit"
  fi
done
if [[ -f /etc/systemd/system/career-alert@.service ]]; then
  pass "career-alert@.service installed (OnFailure alerting armed)"
else
  bad "career-alert@.service missing — failures will be silent"
fi

# Installed is not the same as REACHABLE, and for eleven months it was not.
# A unit with Restart=always only ever enters `failed` — the one state that
# triggers OnFailure — by exhausting its start rate limit. systemd's defaults
# (StartLimitIntervalSec=10s, StartLimitBurst=5) against RestartSec=5 allow
# two starts per window, so the limit could not be hit and the two always-on
# services restarted forever in silence however hopeless the cause. The unit
# files now widen the window; this asserts the arithmetic still works, because
# the failure mode of the fix is someone restoring an old unit file and every
# other check on this page still passing.
to_usec() {
  # systemd prints durations as "10s", "1min 30s", "500ms", "0", "infinity".
  local spec="$1" total=0 num unit tok
  [[ "$spec" == "infinity" ]] && { printf '%s' "-1"; return; }
  for tok in $spec; do
    num="${tok%%[a-zμ]*}"
    unit="${tok#"$num"}"
    [[ -n "$num" ]] || continue
    case "$unit" in
      us | μs) total=$((total + num)) ;;
      ms) total=$((total + num * 1000)) ;;
      s | "") total=$((total + num * 1000000)) ;;
      min | m) total=$((total + num * 60000000)) ;;
      h) total=$((total + num * 3600000000)) ;;
      d) total=$((total + num * 86400000000)) ;;
      *) ;;
    esac
  done
  printf '%s' "$total"
}
for unit in career-worker.service career-admin-bot.service; do
  burst=$(systemctl show "$unit" -p StartLimitBurst --value 2>/dev/null)
  window=$(to_usec "$(systemctl show "$unit" -p StartLimitIntervalUSec --value 2>/dev/null)")
  gap=$(to_usec "$(systemctl show "$unit" -p RestartUSec --value 2>/dev/null)")
  if [[ -z "$burst" || "$burst" -le 1 || "$window" -le 0 ]]; then
    bad "$unit: cannot read the start limit — OnFailure reachability unknown"
  elif [[ $((gap * (burst - 1))) -lt "$window" ]]; then
    pass "$unit: start limit reachable ($burst starts / $((window / 1000000))s at ${gap}us apart) — OnFailure can fire"
  else
    bad "$unit: OnFailure is UNREACHABLE — $burst restarts ${gap}us apart cannot exhaust a $((window / 1000000))s window, so this service can die forever in silence"
  fi
done

# A unit file in the repository is not a unit file on the machine. On
# 2026-08-06 the audit above was written, committed and reviewed while
# /etc/systemd/system still held the version from 2026-07-31 — the fix existed
# everywhere except where it runs. Every check on this page reads the LIVE
# unit, so drift is exactly what it cannot see by itself.
for unit_file in "$REPO_ROOT"/ops/systemd/*.service "$REPO_ROOT"/ops/systemd/*.timer; do
  name=$(basename "$unit_file")
  installed="/etc/systemd/system/$name"
  if [[ ! -e "$installed" ]]; then
    bad "$name: in the repository but NOT installed — run: cp $unit_file $installed && systemctl daemon-reload"
  elif cmp -s "$unit_file" "$installed"; then
    pass "$name: installed copy matches the repository"
  else
    bad "$name: the installed copy DIFFERS from the repository — the fix is not running (diff $installed $unit_file)"
  fi
done

# The wedge cover itself, asserted on the LIVE units: both loops send
# WATCHDOG=1 only after a cycle completes, so a deadline of zero means a
# process that spins forever without processing anything still reports
# `active` — the 2026-08-04 and 2026-08-06 shape. See career-worker.service.
for unit in career-worker.service career-admin-bot.service; do
  svc_type=$(systemctl show "$unit" -p Type --value 2>/dev/null)
  watchdog=$(to_usec "$(systemctl show "$unit" -p WatchdogUSec --value 2>/dev/null)")
  if [[ "$svc_type" != "notify" ]]; then
    bad "$unit: Type=$svc_type — systemd cannot hear this loop, so a wedged cycle is invisible"
  elif [[ "$watchdog" -le 0 ]]; then
    bad "$unit: Type=notify but NO watchdog deadline — nothing enforces the heartbeat"
  else
    pass "$unit: watchdog armed at $((watchdog / 1000000))s"
  fi
done

# A matching FILE is not a matching MACHINE, and `cmp` above cannot tell the
# difference. Two ways for every check on this page to pass while systemd runs
# something else entirely:
#
#   the daemon-reload that never came — the deploy copied the file and stopped.
#     systemd keeps serving the old unit out of memory, `cmp` is green because
#     it reads the disk, and the machine is on the previous release. This is
#     the 2026-08-06 shape with the copy step done.
#   a drop-in — /etc/systemd/system/<unit>.d/*.conf overrides any directive in
#     the unit without changing one byte of it, so `WatchdogSec=0` can be added
#     from outside the repository and every file comparison stays green.
#
# Both are visible only in what systemd has LOADED, which is what this asks.
for unit in career-worker.service career-admin-bot.service; do
  stale=$(systemctl show "$unit" -p NeedDaemonReload --value 2>/dev/null)
  dropins=$(systemctl show "$unit" -p DropInPaths --value 2>/dev/null)
  if [[ "$stale" == "yes" ]]; then
    bad "$unit: systemd is NOT running the file on disk — finish the deploy: systemctl daemon-reload && systemctl restart $unit"
  else
    pass "$unit: systemd is running the file on disk"
  fi
  if [[ -n "$dropins" ]]; then
    bad "$unit: overridden by drop-in(s), so the unit file in git is not the last word — $dropins"
  else
    pass "$unit: no drop-in overrides"
  fi
done

# The wedge alert hook, asserted on the LIVE unit rather than on the file.
# Without it a wedged worker is killed and restarted every WatchdogSec+RestartSec
# forever and never reaches `failed`, so OnFailure= never fires and nobody is
# told — a watchdog that heals the symptom and hides the illness.
for unit in career-worker.service career-admin-bot.service; do
  hook=$(systemctl show "$unit" -p ExecStopPost --value 2>/dev/null)
  case "$hook" in
    *alert_unit_failure.sh*ignore_exit_status=yes*)
      pass "$unit: watchdog alert hook armed and non-fatal" ;;
    *alert_unit_failure.sh*)
      # The leading '-' in the unit file. Without it a Telegram outage turns
      # the alerting into a failure of the service it was reporting on.
      bad "$unit: alert hook is loaded WITHOUT the leading '-' — a failed alert would fail the service itself" ;;
    *)
      bad "$unit: NO ExecStopPost alert hook loaded — a watchdog kill restarts it silently forever, since WatchdogSec+RestartSec can never exhaust the start limit" ;;
  esac
done

section "7. gateway and health"
caddy_state=$(systemctl is-active caddy 2>/dev/null || echo inactive)
if [[ "$caddy_state" == "active" ]]; then
  pass "caddy: active"
else
  bad "caddy: $caddy_state — no TLS, so no Salla/Meta webhooks"
fi
if [[ -s /etc/caddy/Caddyfile ]]; then
  pass "/etc/caddy/Caddyfile present"
else
  bad "/etc/caddy/Caddyfile missing"
fi
local_health=$(curl -fsS -m 10 "$HEALTH_URL" 2>/dev/null)
if [[ "$local_health" == *'"status":"ok"'* ]]; then
  pass "local health: $local_health"
else
  bad "local health FAILED at $HEALTH_URL"
fi
public_health=$(curl -fsS -m 15 "$PUBLIC_HEALTH_URL" 2>/dev/null)
if [[ "$public_health" == *'"status":"ok"'* ]]; then
  pass "public health (TLS + DNS): $public_health"
else
  bad "public health FAILED at $PUBLIC_HEALTH_URL — check the DNS A record and the cert"
fi

section "8. backup chain is alive again"
BACKUP_ENV="${CAREER_BACKUP_ENV:-/root/.config/career-backup/backup.env}"
if [[ -s "$BACKUP_ENV" ]]; then
  pass "$BACKUP_ENV present"
  # Freshness, not existence: a repo that stopped receiving snapshots looks
  # identical to a healthy one until the day it is needed.
  latest=$(
    set -a
    # shellcheck source=/dev/null
    source "$BACKUP_ENV"
    set +a
    export HOME="${HOME:-/root}"
    restic snapshots --tag db --latest 1 --json 2>/dev/null |
      grep -oE '"time":"[^"]+' | tail -1 | cut -d'"' -f4
  )
  if [[ -n "$latest" ]]; then
    age_days=$(((  $(date +%s) - $(date -d "$latest" +%s 2>/dev/null || echo 0) ) / 86400))
    if [[ "$age_days" -le 2 ]]; then
      pass "latest DB snapshot $latest (${age_days}d old)"
    else
      bad "latest DB snapshot is ${age_days}d old ($latest) — backups have stopped"
    fi
  else
    bad "cannot list snapshots — check RESTIC_REPOSITORY / B2 credentials"
  fi
else
  bad "$BACKUP_ENV missing — the new server is running with NO backups"
fi

section "9. document fonts installed on the host"
# The delivery worker and the funnel both render on the HOST venv, not inside
# the api container, so the host font set decides what a customer receives.
# fontconfig never fails — it substitutes silently — so the only honest test
# is whether it hands back the family we asked for. Without this a server
# rebuilt from the runbook draws every CV and every Arabic report in a
# fallback face, and every other check on this page still passes.
if command -v fc-match >/dev/null 2>&1; then
  for family in "Liberation Sans" "Noto Naskh Arabic" "DejaVu Sans"; do
    got=$(fc-match -f '%{family}' "$family" 2>/dev/null)
    if [[ ",${got}," == *",${family},"* || "$got" == "$family" ]]; then
      pass "font '$family' resolves to itself"
    else
      bad "font '$family' MISSING — fontconfig substitutes '$got'; documents render in the wrong face (apt-get install -y fonts-liberation fonts-dejavu-core fonts-noto-core)"
    fi
  done
else
  bad "fc-match absent — fontconfig is not installed, so no document can be rendered"
fi

if [[ $fail -eq 0 ]]; then
  printf '\n✅ verify_restore: every check passed — the service is back.\n'
else
  printf '\n❌ verify_restore: FAILURES above — do not call the recovery done.\n'
fi
exit $fail
