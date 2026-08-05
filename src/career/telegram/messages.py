"""Admin-channel message builders. Every string here is PII-free by
construction: TEN-#### codes, counts, and opaque reasons only — never a name,
phone, salary, or CV text (§15.13)."""

from __future__ import annotations


def activation_failed(ten_code: str, reason: str) -> str:
    return f"⚠️ {ten_code} · activation failed · reason: {reason}"


#: Plans whose holder was sold a faster human reply. Kept in sync with
#: views._PRIORITY_PLANS — the star on the card and the star on the alert are
#: the same promise, and لمّاح+ sells it as «أعرف مين أنت قبل ما أرد». The card
#: alone could not deliver that: the operator reads THIS line first and only
#: opens a card afterwards, so a tier visible only on the card is a tier
#: learned too late.
_PRIORITY_PLANS: frozenset[str] = frozenset({"executive"})


def support_request(ten_code: str, plan_code: str | None = None) -> str:
    star = "⭐ " if str(plan_code) in _PRIORITY_PLANS else ""
    return f"🆘 {star}{ten_code} · customer requested support"


def welcome_failed(ten_code: str, reason: str) -> str:
    return f"⚠️ {ten_code} · welcome/link failed · reason: {reason}"


def daily_run_summary(
    ten_code: str, *, jobs: int, cvs: int, delivered: int, est_cost_halalas: int
) -> str:
    return (
        f"{ten_code} · run complete · {jobs} jobs · {cvs} CVs · "
        f"{delivered} delivered · est. {est_cost_halalas} halalas"
    )
