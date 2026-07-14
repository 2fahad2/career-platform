"""Admin-channel message builders. Every string here is PII-free by
construction: TEN-#### codes, counts, and opaque reasons only — never a name,
phone, salary, or CV text (§15.13)."""

from __future__ import annotations


def activation_failed(ten_code: str, reason: str) -> str:
    return f"⚠️ {ten_code} · activation failed · reason: {reason}"


def support_request(ten_code: str) -> str:
    return f"🆘 {ten_code} · customer requested support"


def welcome_failed(ten_code: str, reason: str) -> str:
    return f"⚠️ {ten_code} · welcome/link failed · reason: {reason}"


def daily_run_summary(
    ten_code: str, *, jobs: int, cvs: int, delivered: int, est_cost_halalas: int
) -> str:
    return (
        f"{ten_code} · run complete · {jobs} jobs · {cvs} CVs · "
        f"{delivered} delivered · est. {est_cost_halalas} halalas"
    )
