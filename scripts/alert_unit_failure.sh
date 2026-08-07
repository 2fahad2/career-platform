#!/usr/bin/env bash
# Tell the operator when a unit has stopped doing its job — and say WHICH way.
#
# Every career unit sets Restart=always, so a transient crash heals itself and
# nobody needs to know. What nobody was told is the case that matters: a unit
# that gave up and STAYED down — the worker could be dead for a day and the
# only symptom would be silence on WhatsApp.
#
# Two callers, deliberately:
#
#   OnFailure=career-alert@%n.service   — the unit entered `failed`. Reached by
#     the oneshots (a nightly run that exited non-zero, a weekly probe that came
#     back red) and by an always-on unit that exhausted its start rate limit.
#
#   ExecStopPost=-<this script> %n      — the unit STOPPED, for any reason, and
#     systemd hands us $SERVICE_RESULT. This is the only path that reliably
#     catches a WATCHDOG kill: a wedged worker restarts every WatchdogSec+
#     RestartSec (305s), and twenty of those do not fit in the 600s start-limit
#     window, so the wedge would otherwise restart forever without the unit
#     ever entering `failed`. The `-` prefix in the unit file means a failure
#     here never becomes a failure of the service itself.
#
# One message, three shapes. «🔴 خدمة توقفت ولم تعد تشتغل» went out on
# 2026-08-06 10:02 for a nightly delivery run that failed one step — the unit
# had not stopped, nothing needed restarting, and the operator was told the
# wrong thing about his own system at five past ten. The headline is now
# chosen from what systemd actually reports.
#
# Deliberately says nothing about WHY: journal lines can carry payloads, and
# §15.13 keeps the admin channel PII-free. The operator gets the unit name, the
# shape of the failure, and the command that shows him the rest.
set -euo pipefail

UNIT="${1:-unknown}"
# Overridable ONLY so the alert path can be proven end to end without reading
# the live secret file (tests/test_ops_watchdogs.py). The default is the file
# systemd actually runs with.
ENV_FILE="${CAREER_ENV_FILE:-/root/career/.env.staging}"
TG_API="${CAREER_TELEGRAM_API:-https://api.telegram.org}"
# Same reason: the de-duplication state and the clock are overridable so the
# suppression can be driven through real invocations in a test instead of being
# described in a comment. Neither has any effect on the deployed defaults.
STATE_DIR="${CAREER_ALERT_STATE_DIR:-/run/career-alerts}"
NOW="${CAREER_ALERT_NOW:-$(date +%s)}"

# ── how often the same, still-unfixed failure is allowed to speak ───────────
#
# THE STORM THIS EXISTS TO STOP, in the arithmetic of the units themselves.
#
# `career-worker.service` is deliberately built so a wedge can never exhaust
# the start rate limit: WatchdogSec 300 + RestartSec 5 = 305s per kill-and-
# restart, and (StartLimitBurst 20 − 1) × 305 = 5795s does not fit in
# StartLimitIntervalSec 600. So systemd restarts a permanently wedged worker
# forever — 86400 / 305 ≈ 283 kills a day — and before this block each one
# reached ExecStopPost= and sent a Telegram. 283 IDENTICAL messages a day.
# This script's own header says an alert that fires on every blip gets muted by
# week two; by that standard 283 a day is worse than the wedge, because a muted
# channel silences the backup failure and the start-limit-hit too.
#
# THE ALTERNATIVE THAT WAS REJECTED, and why.
#
# The other fix is to retune the unit so the wedge DOES exhaust the start limit
# — e.g. StartLimitIntervalSec=3600 with StartLimitBurst=12, where 11 × 305s =
# 3355s < 3600s — and systemd then gives up, the unit enters `failed`,
# OnFailure= sends «استنفدت محاولات التشغيل» once, and it STAYS DOWN. One
# message instead of 283. It was rejected, twice over:
#
#   1. It converts self-healing incidents into outages. Restart=always is the
#      only unattended recovery this host has. The 2026-08-04 incident was a
#      Postgres restarted underneath the worker; it healed by itself in 100s
#      with nobody awake. Wedges from an expired-then-refreshed token, one
#      poisoned message, a container bounce all heal on restart. Under the
#      retune, a DB outage longer than the window leaves the worker
#      permanently dead AFTER Postgres comes back, and WhatsApp stays unserved
#      until a human types `systemctl reset-failed`. For a one-operator
#      business in another timezone that is a strictly worse Friday night.
#   2. The same knobs govern the timeout verdict. Making the limit reachable
#      for a 305s wedge also makes it reachable for a 185s hung start, so a
#      Postgres that is merely slow to come back at boot would permanently
#      disable the worker — the failure mode the retune is supposed to prevent,
#      caused by the retune.
#
# The storm is a property of the NOTIFICATION, so it is fixed in the
# notification. systemd keeps trying; the operator gets told once, then
# reminded on a human schedule.
#
#: Seconds between reminders for one ongoing failure. 3600 caps a permanent
#: wedge at 24 messages a day against 283 — and, decisively, each one carries a
#: number that has changed (how long, how many kills), so they are reminders
#: and not repetitions. Muting is what happens to identical messages. It is
#: also ≥ 10 kill-cycles for both loop units (10 × 305s = 3050s < 3600s), which
#: is what makes the suppression worth having at all; asserted in
#: tests/test_ops_watchdogs.py so this paragraph cannot drift into a lie.
REPEAT_S=3600
#: Seconds of quiet after which the NEXT failure is a new incident rather than
#: a continuation. Derived from the units, not chosen: a wedged worker produces
#: a kill every WatchdogSec+RestartSec (305s worker, 245s bot), so silence for
#: more than twice the longest of those means the failure stopped happening and
#: something else started it again. Without this an outage that healed at 11:00
#: and returned at 14:00 would be reported at 14:00 as «still broken for three
#: hours», which sends the operator to the wrong three hours of journal.
INCIDENT_GAP_S=900

# systemd sets SERVICE_RESULT for ExecStopPost= and leaves it unset for
# OnFailure=. An empty value therefore means «asked by OnFailure», not
# «stopped cleanly», and the two are read differently below.
RESULT="${SERVICE_RESULT-}"
FROM_STOP_POST=0
if [[ -n "${SERVICE_RESULT+set}" ]]; then
    FROM_STOP_POST=1
fi

if [[ "$FROM_STOP_POST" == "1" ]]; then
    # This runs on EVERY stop, including the `systemctl restart` of a deploy
    # and a clean shutdown. Only the verdicts that CANNOT escalate on their own
    # are worth a phone from here; the rest is noise that would teach him to
    # mute the channel by week two.
    #
    # The test for «worth a phone» is the start-limit arithmetic, not the
    # severity of the word. `exit-code` is deliberately silent: a crash
    # restarts every RestartSec=5, trips 20-starts-in-600s in about a hundred
    # seconds, and OnFailure= then sends «استنفدت محاولات التشغيل» with the
    # recovery commands. The two verdicts below cannot reach that limit and so
    # are never announced by anything else:
    #
    #   watchdog  WatchdogSec 300 + RestartSec 5 = 305s per restart; 20 of
    #             those need 6100s and the window is 600s.
    #   timeout   TimeoutStartSec 180 + RestartSec 5 = 185s per restart; three
    #             fit in the window, never twenty. This one arrived WITH
    #             Type=notify: under Type=simple a hung start-up is not a
    #             failure at all, so making systemd wait for READY=1 created a
    #             new way to restart forever in silence — a boot check blocked
    #             on a Postgres that accepts the connection and never answers
    #             (no connect_timeout on the session it opens) is killed at
    #             180s, every time, with nobody told.
    case "$RESULT" in
        watchdog|timeout) ;;
        *) exit 0 ;;
    esac
else
    # OnFailure: ask systemd what it recorded. `Result` survives into the
    # failed state, so this is a fact and not a guess.
    RESULT="$(systemctl show "$UNIT" -p Result --value 2>/dev/null || true)"
    UNIT_TYPE="$(systemctl show "$UNIT" -p Type --value 2>/dev/null || true)"
fi

RECOVERY=""
case "$RESULT" in
    watchdog)
        # The heartbeat stopped while the process stayed alive: a cycle stopped
        # completing. systemd already killed and restarted it.
        HEADLINE='🔴 خدمة تعلّقت ولم تعد ترد على النبض — أُعيد تشغيلها'
        ;;
    timeout)
        # Distinct from the wedge on purpose: a wedge means the loop ran and
        # stopped completing cycles, so the evidence is in the cycle errors; a
        # start timeout means the process never got past its boot checks, so
        # the evidence is the first seconds of the journal and nothing after.
        # One sentence for each, or he reads the wrong half at 03:00.
        HEADLINE='🔴 خدمة لم تكمل الإقلاع ضمن المهلة — أُعيد تشغيلها'
        ;;
    start-limit-hit)
        # systemd has STOPPED restarting it. Recovery is manual, by design —
        # see the start-limit note in career-worker.service.
        HEADLINE='🔴 خدمة استنفدت محاولات التشغيل وتوقفت نهائيًا'
        RECOVERY="systemctl reset-failed ${UNIT} && systemctl start ${UNIT}"
        ;;
    *)
        case "${UNIT_TYPE:-}" in
            oneshot)
                # A scheduled job or a probe: the nightly delivery run, the
                # backup, the weekly restore/health verification. Nothing is
                # «down» and nothing needs restarting — a run failed.
                HEADLINE='🔴 مهمة مجدولة فشلت ولم تكتمل'
                ;;
            "")
                # systemd could not tell us what this unit is (not installed,
                # or no systemctl here). Saying «stopped» would be a guess,
                # and a wrong sentence about his own system costs more than a
                # vague one.
                HEADLINE='🔴 وحدة نظام أبلغت عن فشل'
                ;;
            *)
                HEADLINE='🔴 خدمة توقفت ولم تعد تشتغل'
                ;;
        esac
        ;;
esac

# ── the de-duplication decision ─────────────────────────────────────────────
#
# Keyed on unit AND verdict, so a wedge and a hung start on the same unit are
# two incidents that each get their own first message: they send the operator
# to different halves of the journal, and suppressing one because the other
# just spoke would hide a change in the failure.
#
# The stamp lives in /run — tmpfs, so a reboot clears it. That is the intended
# reading: a reboot is an intervention, and the first failure after one is news.
#
# Everything here FAILS OPEN. If the state directory cannot be created, if the
# stamp is unreadable, if it holds garbage — the alert is sent. The cost of
# getting suppression wrong in that direction is a noisy hour; the cost in the
# other direction is nine silent ones, which is the incident this whole file
# exists because of.
STAMP_KEY=$(printf '%s.%s' "$UNIT" "${RESULT:-unknown}" | tr -c 'A-Za-z0-9._@-' '_')
STAMP="${STATE_DIR}/${STAMP_KEY}"
FIRST_TS="$NOW"
LAST_SENT="$NOW"
KILLS=1
SEND=1
REPEAT_LINES=""

# Arabic-Indic digits keep a line that carries a number direction-pure: an
# Arabic sentence with Latin digits in it arrives scrambled in his client, and
# these lines exist to be read at 03:00 (see the bidi note near the message).
ar_num() { printf '%s' "$1" | sed 'y/0123456789/٠١٢٣٤٥٦٧٨٩/'; }

if mkdir -p "$STATE_DIR" 2>/dev/null; then
    # ── one reader-modifier-writer at a time ────────────────────────────────
    #
    # Everything from here to the `mv` is read-decide-write on one stamp, and
    # without a lock two copies read the same «no stamp yet» and both send —
    # and, worse, the second `mv` throws away the first one's increment, so
    # KILLS undercounts. Eight simultaneous first kills of one unit produced
    # three messages and a stamp claiming two.
    #
    # HOW REACHABLE IS THIS, honestly: not very. systemd serialises a single
    # unit's ExecStopPost= hooks, so the wedged worker cannot race itself. What
    # gets here is a human running the script by hand while a unit flaps, or a
    # future caller that fans out. The send direction fails open (extra
    # messages, never silence), so the race was never going to hide an
    # incident — but KILLS is the number the reminder quotes to the operator as
    # «مرات التعطل منذ أول إنذار», and a lost update makes it under-report, in
    # the reassuring direction, in the one line that tells him this is getting
    # worse. A lock costs one uncontended syscall and removes both.
    #
    # The lock is a SEPARATE file, never renamed: `mv -f` replaces the stamp's
    # inode, so a lock held on the stamp itself would be a lock on a file that
    # no longer has that name. Failing to take it is not fatal — an unlockable
    # state directory falls back to exactly the previous behaviour rather than
    # to silence, and the -w deadline keeps a stale holder from stalling a
    # unit's stop past its TimeoutStopSec.
    lock_fd=""
    if { exec {lock_fd}>"${STAMP}.lock"; } 2>/dev/null \
       && flock -w 5 "$lock_fd" 2>/dev/null; then
        :
    else
        lock_fd=""
    fi
    if [[ -r "$STAMP" ]]; then
        p_first=""; p_kill=""; p_sent=""; p_kills=""
        read -r p_first p_kill p_sent p_kills < "$STAMP" || true
        # A stamp that is not four integers is a stamp we do not trust, and an
        # untrusted stamp must not be allowed to silence anything.
        if [[ "$p_first" =~ ^[0-9]+$ && "$p_kill" =~ ^[0-9]+$ \
              && "$p_sent" =~ ^[0-9]+$ && "$p_kills" =~ ^[0-9]+$ ]]; then
            # Digits are not yet a NUMBER. Bash reads a leading zero as octal,
            # so a stamp of `008` passes the regex above and then makes every
            # `$(( ))` below a fatal expansion error — which unwinds the rest of
            # this block, INCLUDING the write. The alert still goes out, so the
            # direction is open; but the stamp is never replaced, so it is the
            # one bad-stamp shape that does not self-heal. That key's storm cap
            # stays off until the next reboot: 283 messages a day, which is the
            # exact outcome this whole section exists to prevent. Forcing base
            # ten costs four characters and makes the stamp survivable.
            p_first=$(( 10#$p_first )); p_kill=$(( 10#$p_kill ))
            p_sent=$(( 10#$p_sent )); p_kills=$(( 10#$p_kills ))
            quiet=$(( NOW - p_kill ))
            since_sent=$(( NOW - p_sent ))
            since_first=$(( NOW - p_first ))
            # All three, not just the one that decides suppression: every
            # interval this block can print or compare has to be a real
            # duration, or a stamp from the future puts a negative number into
            # an Arabic sentence — where the minus sign alone is enough to
            # scramble the reading order in his client.
            if (( quiet < 0 || since_sent < 0 || since_first < 0 )); then
                # THE CLOCK MOVED BACKWARDS. A stamp in the future is not a
                # duration, it is evidence that the wall clock stepped — NTP
                # correcting a drifted host, a hypervisor restoring a snapshot,
                # a manual `date -s`. Doubt about the clock is doubt, and doubt
                # sends.
                #
                # This branch is not a nicety. Without it a backward step of Δ
                # bought SILENCE for Δ + REPEAT_S of real time: `quiet` went
                # negative, a negative number is not greater than
                # INCIDENT_GAP_S, so the «still broken» branch ran, and
                # `NOW - p_sent` was negative there too — so every one of a
                # wedged unit's 305s kills was suppressed until the clock had
                # caught up AND run another hour. A file whose header promises
                # to fail open cannot have a path where a clock step produces
                # nine quiet hours.
                #
                # A MONOTONIC SOURCE WAS CONSIDERED AND REJECTED. /proc/uptime
                # is immune to the step by construction, and /run being tmpfs
                # means a stamp can never outlive the boot its uptime is
                # measured against — so it would work. But the two numbers in
                # the message («مستمر منذ دقائق», «مدة السلامة») are sentences
                # the operator reads against his own journal, which is stamped
                # in wall time; and a second clock would need its own injection
                # point beside CAREER_ALERT_NOW; and it answers only half the
                # question, because a FORWARD step still has to be handled and
                # already is (a large jump reads as quiet and correctly says
                # «broke again»). Sanity-checking the interval covers both
                # directions in the arithmetic that is already here.
                #
                # The incident is re-based rather than continued: FIRST_TS,
                # KILLS and LAST_SENT keep their defaults, so the stamp written
                # below is self-consistent under the NEW clock and ordinary
                # suppression resumes on the next kill. Carrying the old
                # timestamps forward would leave `p_first` in the future and
                # print a negative age in the next reminder.
                REPEAT_LINES=$'\n'"⚠️ ساعة الخادم رجعت للخلف — أُعيد بدء احتساب هذا العطل"
            elif (( quiet > INCIDENT_GAP_S )); then
                # BROKE AGAIN. It stopped failing, then started again. He needs
                # to know that the clock restarted, or he reads the wrong hours.
                REPEAT_LINES=$'\n'"⚠️ تعطلت من جديد بعد فترة سلامة"
                REPEAT_LINES+=$'\n'"مدة السلامة قبل هذا العطل بالدقائق: $(ar_num $(( quiet / 60 )))"
            else
                # STILL BROKEN — same failure, still running.
                FIRST_TS="$p_first"
                KILLS=$(( p_kills + 1 ))
                if (( since_sent >= REPEAT_S )); then
                    REPEAT_LINES=$'\n'"🔁 ما زال العطل مستمرًا — هذا تذكير وليس عطلًا جديدًا"
                    REPEAT_LINES+=$'\n'"مستمر منذ دقائق عددها: $(ar_num $(( since_first / 60 )))"
                    REPEAT_LINES+=$'\n'"مرات التعطل منذ أول إنذار: $(ar_num "$KILLS")"
                else
                    SEND=0
                    LAST_SENT="$p_sent"
                fi
            fi
        fi
    fi
    # Recorded on EVERY invocation, sent or not: the suppressed kills are what
    # the reminder counts, and the kill time is what decides whether the next
    # failure is «still» or «again».
    tmp="${STAMP}.$$"
    if printf '%s %s %s %s\n' "$FIRST_TS" "$NOW" "$LAST_SENT" "$KILLS" > "$tmp" 2>/dev/null; then
        mv -f "$tmp" "$STAMP" 2>/dev/null || rm -f "$tmp"
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
    # Released BEFORE the message goes out: the critical section is the
    # decision and the stamp, not the twenty-second curl. Holding it across the
    # send would serialise unrelated alerts behind a slow Telegram.
    if [[ -n "$lock_fd" ]]; then
        exec {lock_fd}>&-
    fi
else
    echo "cannot use $STATE_DIR — alerting without de-duplication" >&2
fi

if [[ "$SEND" != "1" ]]; then
    exit 0
fi

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
# Kept BYTE-IDENTICAL with scripts/post_boot_report.sh — asserted by
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

# Every line is direction-pure: Arabic alone, or the unit name and the command
# alone. Mixed lines arrive scrambled in his client (bidi), and an alert he has
# to decipher at 03:00 is an alert that costs him time twice.
TEXT="${HEADLINE}${REPEAT_LINES}"$'\n'"${UNIT}"$'\n'"راجع السجل على السيرفر"$'\n'"journalctl -u ${UNIT} -n 50 --no-pager"
if [[ -n "$RECOVERY" ]]; then
    TEXT="${TEXT}"$'\n'"الاستعادة يدوية:"$'\n'"${RECOVERY}"
fi

send_telegram "$TEXT"
