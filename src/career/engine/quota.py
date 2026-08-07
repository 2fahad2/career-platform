"""SearchAPI credit watch (Fahad's call: alert BEFORE the quota dies).

Pure decision over the provider's ``/api/v1/me`` account fields. On the trial
plan every counter reads 0 (allowance untracked) — no alert fires; a dead
trial shows up as discovery failures, which already alert the admin channel.
Once a paid plan exists, ``monthly_allowance`` goes positive and this becomes
the early-warning authority.
"""

from __future__ import annotations

from typing import Any

#: Alert floor — roughly a few nightly runs' worth of queries, so the warning
#: lands days before the engine starts failing, not hours.
MIN_REMAINING_FLOOR = 100

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _ar_num(value: int) -> str:
    """Arabic-Indic digits, so a count can live INSIDE an Arabic sentence.

    Fahad's client reverses any line mixing Arabic with Latin digits, and this
    is the alert he must be able to act on: it is the only warning that the
    search credit the whole nightly engine runs on is about to die. Same
    helper as `telegram/console._ar_digits` and `refresh_salla_token.ar_num`
    — local by design, because each module states its own numerals.
    """
    return str(value).translate(_ARABIC_DIGITS)


def quota_alert(account: dict[str, Any]) -> str | None:
    """Arabic admin alert when paid credits run low; None = nothing to say."""
    try:
        allowance = int(account.get("monthly_allowance") or 0)
        remaining = int(account.get("remaining_credits") or 0)
        used = int(account.get("current_month_usage") or 0)
    except (TypeError, ValueError):
        return None
    if allowance <= 0:
        return None
    # live-probed provider quirk: on the paid plan remaining_credits reads 0
    # (it tracks one-time credits, not the monthly allowance) — derive the
    # real headroom from the allowance minus this month's usage instead.
    remaining = max(remaining, allowance - used)
    threshold = max(MIN_REMAINING_FLOOR, allowance // 10)
    # The top-up URL gets a LINE OF ITS OWN in both branches: inside the
    # Arabic sentence it arrived reversed — an unreadable link on the one
    # alert that exists to be acted on — and alone it is also tappable.
    if remaining <= 0:
        return (
            "🔴 رصيد محرك البحث انتهى — التشغيلة الليلية القادمة بتفشل.\n"
            "جدد الاشتراك الآن:\n"
            "searchapi.io/pricing"
        )
    if remaining <= threshold:
        return (
            f"🟠 رصيد محرك البحث قرب يخلص: باقي {_ar_num(remaining)} "
            f"من {_ar_num(allowance)}.\n"
            "جدد قبل ما تنقطع التشغيلات:\n"
            "searchapi.io/pricing"
        )
    return None
