"""Job-card delivery building blocks (whitepaper §08/§09, D2/D8).

Pure formatters + honest accounting: the Arabic card, the D8 displayed
filename, and the grouped daily bundle where each card is bound to ITS
document. A job whose required CV is unresolved never enters the bundle —
it is counted as FAILED («CV مطلوب ولم يُحل = فشل لا تسليم»). Outcome
buttons land in the append-only outcome_events table (§14 measurement).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from career.db.models import OutcomeEvent
from career_core.urltools import strip_tracking_params

#: The standing safety statement (§9.1) — every daily header carries it.
SAFETY_LINE_AR = "⚠️ التقديم يدوي من طرفك — لا تقديم تلقائي أبدًا"

#: Outcome buttons (§14): titles ≤ 20 chars (WhatsApp cap); the machine id
#: carries the job url so a tap lands in outcome_events without lookup.
OUTCOME_PROMPT_AR = "بعد ما تطلع على الفرصة والـCV — علّمني وش قررت 👇"
OUTCOME_APPLIED_AR = "قدمت ✅"
OUTCOME_IGNORED_AR = "ما ناسبتني"

_SALARY_LABELS_AR = {
    "EXPLICIT_CONFIRMED": "✅ الراتب: معلن ومناسب",
    "INFERRED_HIGH": "✅ الراتب: مرجّح مرتفع",
    "INFERRED_MEDIUM": "🟡 الراتب: محتمل مناسب",
    "INFERRED_LOW": "🔴 الراتب: يبدو أقل من هدفك",
    "UNKNOWN": "❓ الراتب: غير معلن",
}
_TIER_LABELS_AR = {
    1: "🏛 شركة من الطراز الأول",
    2: "🏢 شركة قوية",
    3: "🏗 شركة مقبولة",
    4: "⚠️ شركة غير معروفة",
}
_DEFAULT_LOCATION_AR = "الرياض، السعودية"


def _score_emoji(score: int) -> str:
    if score >= 75:
        return "🟢"
    if score >= 60:
        return "🟡"
    return "🔴"


def build_job_card(job: dict[str, Any], *, rank: int) -> str:
    """The Arabic job card — a pure, side-effect-free formatter (§9.1)."""
    reasons = job.get("reasons") or {}
    score = int(reasons.get("role_score") or 0)
    salary = _SALARY_LABELS_AR.get(
        str(reasons.get("salary_status")), _SALARY_LABELS_AR["UNKNOWN"]
    )
    tier = _TIER_LABELS_AR.get(int(reasons.get("company_tier") or 4))
    location = str(job.get("location") or "").strip() or _DEFAULT_LOCATION_AR
    link = strip_tracking_params(str(job.get("url") or ""))
    lines = [
        f"#{rank} {job.get('title', '')}",
        f"🏢 {job.get('company', '')}",
        f"📍 {location}",
        f"🎯 المطابقة: {score}/100 {_score_emoji(score)}",
        salary,
        str(tier),
        f"🔗 {link}",
    ]
    return "\n".join(lines)


# ── D8: the displayed filename ───────────────────────────────────────────────

_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|]+')


def _clean(part: str) -> str:
    return re.sub(r"\s+", " ", _ILLEGAL_FILENAME_RE.sub(" ", part)).strip()


def display_filename(
    customer_name: str, job_title: str, company: str, *, used: set[str]
) -> str:
    """`FirstName LastName - Job Title.pdf`; the company is appended only on
    a same-day collision (D8). Storage/binding names are untouched — this is
    display-time only."""
    base = f"{_clean(customer_name)} - {_clean(job_title)}.pdf"
    if base not in used:
        return base
    return f"{_clean(customer_name)} - {_clean(job_title)} - {_clean(company)}.pdf"


# ── the grouped daily bundle ─────────────────────────────────────────────────


def build_daily_bundle(
    jobs: list[dict[str, Any]], *, customer_name: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """(bundle, failures). Each entry binds a card to ITS document; a job
    with no resolved CV is excluded and reported — never silently dropped,
    never delivered card-only (§08: a missing CV is never success)."""
    failures: list[dict[str, str]] = []
    entries: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for index, job in enumerate(jobs, start=1):
        group = str(job.get("url") or "")
        cv_key = job.get("cv_key")
        if not cv_key:
            failures.append({"group": group, "reason": "cv_required_unresolved"})
            continue
        filename = display_filename(
            customer_name, str(job.get("title", "")), str(job.get("company", "")),
            used=used_names,
        )
        used_names.add(filename)
        entries.append({
            "group": group,
            "card": {"kind": "text", "body": build_job_card(job, rank=index)},
            "document": {
                "kind": "document", "ref": cv_key, "filename": filename,
                "caption": f"#{index} CV — {str(job.get('title', ''))[:80]}",
            },
            "outcome": {
                "kind": "buttons",
                "body": f"#{index} — {OUTCOME_PROMPT_AR}",
                "buttons": [
                    [f"applied:{group}", OUTCOME_APPLIED_AR],
                    [f"ignored:{group}", OUTCOME_IGNORED_AR],
                ],
            },
        })

    header = (
        f"🎯 وظائف اليوم — {len(entries)} فرصة مختارة لك\n{SAFETY_LINE_AR}"
    )
    return {"grouped": True, "header": header, "jobs": entries}, failures


# ── outcome events (§14 measurement fuel) ────────────────────────────────────

_OUTCOME_RE = re.compile(r"^(applied|ignored):(.+)$", re.DOTALL)


def parse_outcome_button(button_id: str | None) -> tuple[str, str] | None:
    if not button_id:
        return None
    match = _OUTCOME_RE.match(button_id.strip())
    if not match or not match.group(2).strip():
        return None
    return match.group(1), match.group(2).strip()


def record_outcome(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    job_ref: str,
    outcome: str,
    reason: str | None,
    now: datetime,
    delivery_id: uuid.UUID | None = None,
) -> OutcomeEvent:
    row = OutcomeEvent(
        id=uuid.uuid4(), tenant_id=tenant_id, delivery_id=delivery_id,
        job_ref=job_ref, outcome=outcome, reason=reason, occurred_at=now,
    )
    session.add(row)
    session.flush()
    return row
