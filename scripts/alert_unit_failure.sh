#!/usr/bin/env bash
# Tell the operator when a unit has actually given up.
#
# Every career unit sets Restart=always, so a transient crash heals itself and
# nobody needs to know. What nobody was told is the case that matters: a unit
# that exhausted its restarts and STAYED down — the worker could be dead for a
# day and the only symptom would be silence on WhatsApp.
#
# Invoked by systemd as career-alert@<unit>.service (OnFailure=), so the failing
# unit's name arrives as $1. Deliberately says nothing about WHY: journal lines
# can carry payloads, and §15.13 keeps the admin channel PII-free. The operator
# gets the unit name and reads the journal themselves.
set -euo pipefail

UNIT="${1:-unknown}"
ENV_FILE="/root/career/.env.staging"

if [[ ! -r "$ENV_FILE" ]]; then
    echo "no env file at $ENV_FILE — cannot alert" >&2
    exit 1
fi

# Read the two keys instead of sourcing the file. The env file holds JSON
# values (the Salla catalog and pricing maps), and `source` hands those braces
# to bash as commands — the first self-test died with «basic}: command not
# found» before it ever reached curl. An alerting path must not depend on
# every other line in the file being shell-safe.
read_key() {
    local key="$1" line
    line=$(grep -m1 "^${key}=" "$ENV_FILE" || true)
    printf '%s' "${line#*=}"
}

TOKEN="$(read_key TELEGRAM_ADMIN_BOT_TOKEN)"
CHAT="$(read_key TELEGRAM_ADMIN_CHAT_ID)"
if [[ -z "$TOKEN" || -z "$CHAT" ]]; then
    echo "telegram admin credentials absent — cannot alert" >&2
    exit 1
fi

TEXT=$(printf '🔴 خدمة توقفت ولم تعد تشتغل\n%s\nراجع السجل على السيرفر' "$UNIT")

curl -fsS -m 15 -X POST \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=${TEXT}" \
    >/dev/null
