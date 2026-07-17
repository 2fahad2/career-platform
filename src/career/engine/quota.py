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


def quota_alert(account: dict[str, Any]) -> str | None:
    """Arabic admin alert when paid credits run low; None = nothing to say."""
    try:
        allowance = int(account.get("monthly_allowance") or 0)
        remaining = int(account.get("remaining_credits") or 0)
    except (TypeError, ValueError):
        return None
    if allowance <= 0:
        return None
    threshold = max(MIN_REMAINING_FLOOR, allowance // 10)
    if remaining <= 0:
        return (
            "🔴 رصيد محرك البحث انتهى — التشغيلة الليلية القادمة بتفشل.\n"
            "جدد الاشتراك الآن: searchapi.io/pricing"
        )
    if remaining <= threshold:
        return (
            f"🟠 رصيد محرك البحث قرب يخلص: باقي {remaining} من {allowance}.\n"
            "جدد قبل ما تنقطع التشغيلات: searchapi.io/pricing"
        )
    return None
