#!/usr/bin/env bash
# TWO gates, because there are two questions and only one of them is about the
# code you are about to push.
#
#   scripts/gate.sh          «is this code safe to push»   — the SHIP gate.
#                            Repository truth decides the exit code.
#   scripts/gate.sh --host   «is THIS MACHINE running it»  — the HOST gate.
#                            The live machine decides the exit code, and a skip
#                            is a failure. scripts/deploy_preflight.sh requires
#                            it; that is the deploy path.
#
# Why this exists at all: for ten days CI was red at «Shellcheck ops scripts»
# and nobody noticed, because shellcheck was not installed locally — so pytest,
# the alembic drift check and the RLS attack tests never ran on any push while
# every local run looked green. A gate that only lives in the cloud is a gate
# you find out about late. Run this before pushing.
#
# Why it SPLIT on 2026-08-06: the live-host check found real drift — the units
# in /etc/systemd/system were the 31-July copies, no Type=notify, no
# WatchdogSec, no ExecStopPost, and career-verify-restore was never installed
# at all. True, serious, and not a fact about the code in the working tree. A
# commit gate that stays red until somebody deploys blocks everyone, and the
# reliable end of that story is the honest test being weakened or deleted by
# whoever needs to ship at 02:00. That is the exact failure mode this
# repository spent the day fixing, so it must not be recreated by the fix.
#
# What the split must NOT become: the ship gate quietly dropping the host
# tests. A green gate that says nothing about the machine is precisely what
# certified a nine-hour unalerted wedge. So the ship gate still RUNS the host
# checks and prints their verdict in a section of its own, next to the final
# result — it just does not let that verdict decide push-safety. Selection is
# by the `livehost` marker, which is derived from the source and asserted by
# TestTheShipAndHostGatesCannotDrift, not by a list of test names living here.
#
# The step ORDER below still matches .github/workflows/ci.yml. The one
# deliberate difference is that pytest runs twice, once per gate; CI runs the
# suite in a single pass, where the host tests skip honestly (a GitHub runner
# has no career unit installed, so it is not a deploy host).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PYTEST_LOCK=/tmp/career_test.lock

usage() {
  cat <<'EOF'
usage: scripts/gate.sh [--host]

  (no flag)  ship gate — ruff, mypy, shellcheck, lockfile, alembic drift and
             the repository test suite decide the exit code. The live-host
             checks are RUN and REPORTED, never silently dropped, but they do
             not block a push.
  --host     host gate — only the live-host checks, and their verdict is the
             exit code. A skip counts as a failure. Used by
             scripts/deploy_preflight.sh.
EOF
}

MODE=ship
case "${1:-}" in
  "") ;;
  --host) MODE=host ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    printf 'gate.sh: unknown argument %q\n\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac

fail=0
step() {
  printf '\n▶ %s\n' "$1"
  shift
  if "$@"; then printf '  ✓ pass\n'; else printf '  ✗ FAIL\n'; fail=1; fi
}

# ── the live-host checks ─────────────────────────────────────────────────────
#
# Run once, classified into one of four answers. «cannot run» is an answer of
# its own on purpose: on a host without systemd these checks legitimately have
# nothing to read, and printing nothing there is indistinguishable from
# printing a pass. It is said out loud instead, and in --host mode it is a
# failure, because the machines that deploy are never allowed to answer it.
HOST_VERDICT=unknown
HOST_OUTPUT=""

run_host_checks() {
  local require="$1" out rc summary
  out=$(CAREER_REQUIRE_LIVE_UNITS="$require" \
    flock "$PYTEST_LOCK" .venv/bin/python -m pytest tests/ \
    -m "livehost" -q -rs --tb=short -p no:cacheprovider 2>&1)
  rc=$?
  HOST_OUTPUT="$out"
  summary=$(printf '%s\n' "$out" | grep -vE '^\s*$' | tail -n 1)
  if [[ $rc -eq 5 ]]; then
    # No test carried the marker. Either they were deleted or the marker was
    # renamed; either way the alarm is gone and «0 checks, 0 failures» must
    # never read as green.
    HOST_VERDICT=absent
  elif [[ "$out" == *"CAREER_REQUIRE_LIVE_UNITS is set but"* ]]; then
    # Required and unaskable. Still a failure — see below — but «this host has
    # drifted» and «nothing about any host was verified» are different facts
    # and deserve different sentences from whoever is reading.
    HOST_VERDICT=cannot-run
  elif [[ $rc -ne 0 ]]; then
    HOST_VERDICT=red
  elif [[ "$summary" == *skipped* ]]; then
    HOST_VERDICT=cannot-run
  elif [[ "$summary" == *passed* ]]; then
    HOST_VERDICT=green
  else
    # Exit 0 with a summary naming neither. Unrecognised is not a pass.
    HOST_VERDICT=unclear
  fi
}

report_host_checks() {
  printf '\n'
  printf '═══════════════════════════════════════════════════════════════════════\n'
  case "$HOST_VERDICT" in
    green)
      printf '  LIVE HOST — ✅ this machine runs the units in this repository\n' ;;
    red)
      printf '  LIVE HOST — ❌ DRIFT: this machine is NOT running what the repo says\n' ;;
    cannot-run)
      printf '  LIVE HOST — ⚠️  COULD NOT RUN. This is NOT a pass.\n' ;;
    absent)
      printf '  LIVE HOST — ❌ NO HOST CHECK RAN (nothing carries @pytest.mark.livehost)\n' ;;
    *)
      printf '  LIVE HOST — ❌ UNCLEAR result. Treat as red until a human reads it.\n' ;;
  esac
  printf '═══════════════════════════════════════════════════════════════════════\n'
  printf '%s\n' "$HOST_OUTPUT" | sed 's/^/  │ /'
  case "$HOST_VERDICT" in
    green) ;;
    cannot-run)
      printf '\n  Nothing about any machine was verified — the checks could not read a\n'
      printf '  host here. That is NOT the same as «this host is fine».\n'
      if [[ "$MODE" == host ]]; then
        printf '  On a machine that deploys, «could not run» is a refusal: the deploy\n'
        printf '  gate has no evidence, so it does not certify.\n'
      else
        printf '  Harmless in CI or on a laptop. On the SERVER, run the deploy gate —\n'
        printf '  scripts/deploy_preflight.sh — where this answer is a failure.\n'
      fi
      ;;
    *)
      printf '\n  What to do — on the SERVER, not in CI:\n'
      printf '    sudo cp ops/systemd/*.service ops/systemd/*.timer /etc/systemd/system/\n'
      printf '    sudo systemctl daemon-reload\n'
      printf '    sudo systemctl restart career-worker.service career-admin-bot.service\n'
      printf '    sudo systemctl enable --now career-verify-restore.timer\n'
      printf '    scripts/deploy_preflight.sh      # must go green before you call it deployed\n'
      ;;
  esac
}

# ── host gate ────────────────────────────────────────────────────────────────
if [[ "$MODE" == host ]]; then
  # CAREER_REQUIRE_LIVE_UNITS=1 — on a machine that deploys, «skipped» is not
  # an available answer.
  run_host_checks 1
  report_host_checks
  if [[ "$HOST_VERDICT" == green ]]; then
    printf '\n✅ host gate green — this machine runs the repository'"'"'s units\n'
    exit 0
  fi
  printf '\n❌ host gate RED — this machine is not deployed\n'
  exit 1
fi

# ── ship gate ────────────────────────────────────────────────────────────────
step "ruff" .venv/bin/ruff check src tests scripts
step "mypy src" .venv/bin/python -m mypy src/
step "mypy scripts" .venv/bin/python -m mypy scripts/ --explicit-package-bases
if command -v shellcheck >/dev/null 2>&1; then
  # scripts/*.sh too: the recovery helpers are exactly the code that must not
  # rot, because nobody runs them until the night everything is already wrong.
  step "shellcheck" shellcheck ops/backup/*.sh scripts/*.sh
else
  printf '\n▶ shellcheck\n  ! not installed — CI WILL still run it (apt-get install shellcheck)\n'
  fail=1
fi
step "lockfile" test -s requirements.lock

DB_OWNER_PASSWORD=$(grep '^DB_OWNER_PASSWORD=' .env.staging | cut -d= -f2)
DB_PASSWORD=$(grep '^DB_PASSWORD=' .env.staging | cut -d= -f2)
REDIS_PASSWORD=$(grep '^REDIS_PASSWORD=' .env.staging | cut -d= -f2)
export DB_OWNER_PASSWORD DB_PASSWORD REDIS_PASSWORD
export DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=career_test \
  REDIS_HOST=127.0.0.1 REDIS_PORT=6380 \
  SALLA_WEBHOOK_SECRET=test_secret_123 WHATSAPP_APP_SECRET=wa_app_secret_123 \
  WHATSAPP_VERIFY_TOKEN=wa_verify_123 CI_REQUIRE_DB=1

step "alembic drift" .venv/bin/alembic check

# Every collected test lands in exactly one of the two gates. Asserted by
# arithmetic rather than assumed, because the way a check disappears is not by
# failing — it is by being in neither selection and therefore never run, which
# is what «deselect the noisy test» does and what nobody sees afterwards.
collected() {
  local out
  out=$(flock "$PYTEST_LOCK" .venv/bin/python -m pytest tests/ --collect-only -q \
    -p no:cacheprovider "$@" 2>/dev/null)
  printf '%s\n' "$out" | grep -oE '[0-9]+(/[0-9]+)? tests? collected' |
    grep -oE '^[0-9]+' | tail -n 1
}
selection_is_closed_world() {
  local total ship host
  total=$(collected)
  ship=$(collected -m "not livehost")
  host=$(collected -m "livehost")
  printf '  total=%s ship=%s host=%s\n' "${total:-?}" "${ship:-?}" "${host:-?}"
  [[ -n "$total" && -n "$ship" && -n "$host" ]] || return 1
  [[ "$host" -ge 1 ]] || {
    printf '  the host gate would certify an EMPTY selection\n'
    return 1
  }
  [[ $((ship + host)) -eq "$total" ]]
}
# Called directly rather than through step(): a function passed by name is
# invisible to the linter's reachability analysis (SC2317), and this file has
# to stay clean without a suppression comment standing in for a reason.
printf '\n▶ gate selection is closed-world\n'
if selection_is_closed_world; then printf '  ✓ pass\n'; else printf '  ✗ FAIL\n'; fail=1; fi

# The push verdict. Repository truth only — see the header for why the host
# tests are not in here and where they are instead.
step "pytest (repository truth)" \
  flock "$PYTEST_LOCK" .venv/bin/python -m pytest tests/ -q -m "not livehost"

# Run the machine's checks anyway, and print them where they cannot be missed:
# last, right above the verdict. Advisory here, blocking in --host.
run_host_checks 0
report_host_checks

printf '\n───────────────────────────────────────────────────────────────────────\n'
if [[ $fail -eq 0 ]]; then
  printf '✅ SHIP gate green — safe to push\n'
else
  printf '❌ SHIP gate RED — do not push\n'
fi
case "$HOST_VERDICT" in
  green) printf '✅ HOST gate green — this machine is deployed\n' ;;
  cannot-run) printf '⚠️  HOST gate COULD NOT RUN here — unverified, not passed (see above)\n' ;;
  *) printf '❌ HOST gate RED — this machine needs a deploy (see above). Not a push blocker.\n' ;;
esac
printf '   host gate on its own: scripts/gate.sh --host · deploy: scripts/deploy_preflight.sh\n'
exit $fail
