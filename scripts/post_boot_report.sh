#!/usr/bin/env bash
# After a reboot, prove the machine came back — and say so where the operator
# will actually see it.
#
# A reboot is the least-tested path this server has: it had been up 16 days
# before the first planned one, so nothing had ever verified that the units,
# the containers, the gateway and the firewall all return by themselves. This
# runs once at boot, waits for the stack to settle, and reports to Telegram.
#
# TWO THINGS WERE WRONG WITH IT, and both are the same mistake in different
# clothes — a check that cannot fail is not a check:
#
#  1. It had never executed. `journalctl -u career-post-boot` was empty for the
#     entire life of the journal, because the unit was installed AFTER the last
#     boot. A report that has never run is not a report; it is an intention.
#     Proving it therefore cannot mean rebooting a host that is serving a
#     customer, so the settle time, the health URL and the Telegram endpoint
#     are overridable and the proof lives in tests/test_ops_watchdogs.py — run
#     on every commit, against a local sink, without a reboot and without ever
#     reading the live token.
#
#  2. It swallowed its own delivery: `curl ... >/dev/null 2>&1` with no check,
#     under `set -uo pipefail` without `-e`. A Telegram that refused the
#     message — a rotated token, a network still coming up at boot — exited 0
#     and looked exactly like a delivered report, which also meant the unit's
#     OnFailure= could never fire for the one thing it exists to do.
#
# `-e` is still deliberately absent, and must stay absent: nearly every line
# below is a command whose FAILURE is the news (`systemctl is-active` returns
# non-zero for a dead unit, `ufw status` for a firewall that is off). With
# `set -e` the first red check would exit the script and the operator would get
# nothing at all — the report exists precisely to describe red checks. What was
# missing was not `-e`; it was a decisive exit on the one step that must not
# fail quietly.
set -uo pipefail

ENV_FILE="${CAREER_ENV_FILE:-/root/career/.env.staging}"
TG_API="${CAREER_TELEGRAM_API:-https://api.telegram.org}"
HEALTH_URL="${CAREER_POST_BOOT_HEALTH_URL:-https://api.career-platform.net/health}"
# Docker, the containers' healthchecks and caddy need time after a boot. Zero
# is for the test that proves this script without one.
SETTLE_SECONDS="${CAREER_POST_BOOT_SETTLE:-90}"

read_key() {
    local key="$1" line
    line=$(grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null || true)
    printf '%s' "${line#*=}"
}
TOKEN="$(read_key TELEGRAM_ADMIN_BOT_TOKEN)"
CHAT="$(read_key TELEGRAM_ADMIN_CHAT_ID)"
if [[ -z "$TOKEN" || -z "$CHAT" ]]; then
    echo "telegram admin credentials absent at $ENV_FILE — the post-boot report cannot be delivered" >&2
    exit 1
fi

if [[ "$SETTLE_SECONDS" -gt 0 ]]; then
    sleep "$SETTLE_SECONDS"
fi

lines=""
add() { lines+="$1"$'\n'; }

ok_or_bad() { [[ "$1" == "$2" ]] && printf '✅' || printf '🔴'; }

# Every line is direction-pure: Arabic alone, or Latin/digits alone. His client
# scrambles a line that mixes the two, and a boot report is read once, quickly,
# on a phone.
add "🔄 السيرفر رجع بعد إعادة التشغيل"
add ""
for u in career-worker career-admin-bot caddy fail2ban; do
    add "$(ok_or_bad "$(systemctl is-active "$u" 2>/dev/null)" active) $u"
done
for t in career-engine-nightly.timer career-backup.timer career-restore-test.timer; do
    add "$(ok_or_bad "$(systemctl is-active "$t" 2>/dev/null)" active) $t"
done

running=$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)
add "$([[ "$running" -eq 3 ]] && printf '✅' || printf '🔴') حاويات تعمل"
add "${running}/3"

health=$(curl -fsS -m 20 "$HEALTH_URL" 2>/dev/null || echo "")
add "$([[ "$health" == *'"status":"ok"'* ]] && printf '✅' || printf '🔴') الصحة عبر النطاق العام"

ssh_up=$(systemctl is-active ssh.socket 2>/dev/null)
add "$(ok_or_bad "$ssh_up" active) استقبال الدخول الآمن"

fw=$(ufw status 2>/dev/null | head -1)
add "$([[ "$fw" == *active* ]] && printf '✅' || printf '🔴') الجدار الناري"

next=$(systemctl show career-engine-nightly.timer -p NextElapseUSecRealtime --value 2>/dev/null)
add ""
add "موعد التسليم القادم:"
add "${next:-غير معروف}"

# Send one message to the operator's Telegram with the bot token OUT of argv.
#
# /proc/<pid>/cmdline is world-readable and `ps` prints it: this script used to
# pass https://api.telegram.org/bot<TOKEN>/sendMessage as a curl ARGUMENT, so
# every alert it ever sent published the bot token to anyone with a shell on
# the host for as long as the request lasted. curl reads its whole request from
# stdin instead (-K -), where nothing is an argument and nothing is in /proc.
# Values in a curl config file are one per line, so the message's real newlines
# have to arrive as the \n escape curl understands.
#
# Kept BYTE-IDENTICAL with scripts/alert_unit_failure.sh — asserted by
# tests/test_ops_watchdogs.py, because two copies of a subtle escaping routine
# is one copy that rots.
send_telegram() {
    local text="$1" escaped
    escaped=${text//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    escaped=${escaped//$'\n'/\\n}
    printf 'url = "%s"\ndata-urlencode = "chat_id=%s"\ndata-urlencode = "text=%s"\n' \
        "${TG_API}/bot${TOKEN}/sendMessage" "${CHAT}" "${escaped}" |
        curl -fsS -m 20 -K - >/dev/null
}

# The report goes to the journal either way — a delivery failure must not also
# cost the record of what the machine looked like when it came back.
printf '%s' "$lines"

if ! send_telegram "$lines"; then
    echo "post-boot report composed but telegram REFUSED it — the operator was not told" >&2
    exit 1
fi
