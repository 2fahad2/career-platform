#!/usr/bin/env bash
# The deploy gate: may this machine be called deployed?
#
#   scripts/deploy_preflight.sh
#
# Exit 0 = /etc/systemd/system holds this repository's units AND systemd has
# actually loaded them. Exit 1 = it does not, and the deploy is not finished
# however many files were copied.
#
# This is the OTHER half of the 2026-08-06 split. scripts/gate.sh answers «is
# this code safe to push» and deliberately does not fail on host drift, because
# a commit gate that stays red until somebody deploys blocks everyone and gets
# the honest test deleted. That leniency is only defensible if some gate is
# strict, and this is it: nothing here is advisory, and unlike gate.sh a SKIP
# is a failure (CAREER_REQUIRE_LIVE_UNITS=1 — see gate.sh --host), because a
# machine that cannot answer «am I running the right units» has not proved it
# is running them.
#
# WHY A SEPARATE SCRIPT AND NOT A DUPLICATE CHECK. It owns no checking logic at
# all — one line of it delegates to `gate.sh --host`, which runs the same
# pytest selection the commit gate reports. Two implementations of «is this
# host deployed» would be two implementations to keep in step, and
# scripts/verify_restore.sh §6 already reads the live units for a different
# question («is the service back after a restore / is it still healthy this
# Sunday»). This one is for the ten minutes around a deploy, and it is the name
# the runbook can hand to whoever is doing it.
#
# Run it BEFORE the deploy to see exactly what has drifted, and AFTER the
# daemon-reload to certify. It never starts, stops, copies or reloads anything
# — it only reads.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

printf '════════════════════════════════════════════════════════════════════\n'
printf '  DEPLOY PREFLIGHT — does this machine run what the repository says?\n'
printf '  (read-only; the SHIP gate is a separate question: scripts/gate.sh)\n'
printf '════════════════════════════════════════════════════════════════════\n'

scripts/gate.sh --host
host_rc=$?

if [[ $host_rc -eq 0 ]]; then
  printf '\n✅ DEPLOY PREFLIGHT PASSED — this machine is deployed.\n'
  exit 0
fi

cat <<'EOF'

❌ DEPLOY PREFLIGHT REFUSES.

This host is not running the units in this repository, so it must not be
called deployed. The named properties above are the whole diagnosis — each
line carries the property, the repository's value and the machine's value.

  1. cp the units:      sudo cp ops/systemd/*.service ops/systemd/*.timer \
                             /etc/systemd/system/
  2. tell systemd:      sudo systemctl daemon-reload
  3. restart the loops: sudo systemctl restart career-worker.service \
                             career-admin-bot.service
  4. arm what is new:   sudo systemctl enable --now career-verify-restore.timer
  5. run this again.    It reads the LOADED properties, not the files, so it
                        stays red until step 2 and step 3 have really happened.

EOF
exit 1
