#!/usr/bin/env bash
# After a reboot, prove the machine came back — and say so where the operator
# will actually see it.
#
# A reboot is the least-tested path this server has: it had been up 16 days
# before the first planned one, so nothing had ever verified that the units,
# the containers, the gateway and the firewall all return by themselves. This
# runs once at boot, waits for the stack to settle, and reports to Telegram.
set -uo pipefail

ENV_FILE="/root/career/.env.staging"
read_key() {
    local key="$1" line
    line=$(grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null || true)
    printf '%s' "${line#*=}"
}
TOKEN="$(read_key TELEGRAM_ADMIN_BOT_TOKEN)"
CHAT="$(read_key TELEGRAM_ADMIN_CHAT_ID)"

# give docker, the containers' healthchecks and caddy time to settle
sleep 90

lines=""
add() { lines+="$1"$'\n'; }

ok_or_bad() { [[ "$1" == "$2" ]] && printf '✅' || printf '🔴'; }

add "🔄 السيرفر رجع بعد إعادة التشغيل"
add ""
for u in career-worker career-admin-bot caddy fail2ban; do
    add "$(ok_or_bad "$(systemctl is-active "$u" 2>/dev/null)" active) $u"
done
for t in career-engine-nightly.timer career-backup.timer career-restore-test.timer; do
    add "$(ok_or_bad "$(systemctl is-active "$t" 2>/dev/null)" active) $t"
done

running=$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)
add "$([[ "$running" -eq 3 ]] && printf '✅' || printf '🔴') حاويات تعمل: $running من ٣"

health=$(curl -fsS -m 20 https://api.career-platform.net/health 2>/dev/null || echo "")
add "$([[ "$health" == *'"status":"ok"'* ]] && printf '✅' || printf '🔴') الصحة عبر النطاق العام"

ssh_up=$(systemctl is-active ssh.socket 2>/dev/null)
add "$(ok_or_bad "$ssh_up" active) استقبال الدخول الآمن"

fw=$(ufw status 2>/dev/null | head -1)
add "$([[ "$fw" == *active* ]] && printf '✅' || printf '🔴') الجدار الناري"

next=$(systemctl show career-engine-nightly.timer -p NextElapseUSecRealtime --value 2>/dev/null)
add ""
add "موعد التسليم القادم:"
add "${next:-غير معروف}"

if [[ -n "$TOKEN" && -n "$CHAT" ]]; then
    curl -fsS -m 20 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT}" \
        --data-urlencode "text=${lines}" >/dev/null 2>&1
fi
printf '%s' "$lines"
