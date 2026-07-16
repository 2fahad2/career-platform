"""One-page enforcement — LEGACY §1.5 caps + §1.6 summary authority.

Caps applied BEFORE render (never trusting the renderer to complain):
summary ≤400 chars via complete-sentence retention, ≤4 roles, designer
bullet pacing [4,4,3,2], ≤2 projects, ≤14 skills. Bullets: achievements
verbatim FIRST, remaining slots filled with JD-keyword-ranked
responsibilities — every bullet originates from the bank, nothing is ever
invented or padded.
"""

from __future__ import annotations

import re

from career.cv.schemas import Experience, TailoredCV
from career_core.sentences import enforce_complete_summary

MAX_SUMMARY_CHARS = 400
MAX_EXPERIENCE_ENTRIES = 4
MAX_ACHIEVEMENTS_PER_JOB = 4
MAX_PROJECTS = 2
MAX_SKILLS = 14

#: Designer pacing (§1.5): rank 1 & 2 → 4 bullets, rank 3 → 3, rank 4+ → 2.
BULLET_PACING = (4, 4, 3, 2)

_WORD_RE = re.compile(r"[a-z0-9+#]+")


def _keyword_score(text: str, jd_keywords: tuple[str, ...]) -> int:
    tokens = set(_WORD_RE.findall(text.lower()))
    return sum(1 for kw in jd_keywords if kw.lower() in tokens)


def _paced_bullets(
    exp: Experience, target: int, jd_keywords: tuple[str, ...]
) -> list[str]:
    bullets = list(exp.achievements[:MAX_ACHIEVEMENTS_PER_JOB])[:target]
    if len(bullets) < target and exp.responsibilities:
        ranked = sorted(
            (r for r in exp.responsibilities if r not in bullets),
            key=lambda r: _keyword_score(r, jd_keywords),
            reverse=True,
        )
        bullets.extend(ranked[: target - len(bullets)])
    return bullets                       # fewer than target is honest — no padding


def enforce_one_page(cv: TailoredCV, *, jd_keywords: tuple[str, ...]) -> TailoredCV:
    """Return a new TailoredCV guaranteed to fit the v5 single page."""
    roles: list[Experience] = []
    for rank, exp in enumerate(cv.selected_experience[:MAX_EXPERIENCE_ENTRIES]):
        target = BULLET_PACING[min(rank, len(BULLET_PACING) - 1)]
        roles.append(
            exp.model_copy(
                update={"achievements": _paced_bullets(exp, target, jd_keywords)}
            )
        )
    return cv.model_copy(
        update={
            "tailored_summary": enforce_complete_summary(
                cv.tailored_summary, MAX_SUMMARY_CHARS
            ),
            "selected_experience": roles,
            "selected_skills": list(cv.selected_skills[:MAX_SKILLS]),
            "selected_projects": list(cv.selected_projects[:MAX_PROJECTS]),
        }
    )
