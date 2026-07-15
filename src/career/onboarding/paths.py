"""Three-layer career-path assessment (whitepaper §05).

Requested (what the customer wants) → suggested (what the system sees: a
numeric fit score, concrete strengths, concrete gaps, and the closest three
paths) → approved (Primary/Secondary/Stretch with the customer's consent).

Scoring is deterministic and reads ONLY the confirm-gated achievement bank
(§15.5) — never raw extractions. Path families are DATA and injectable
(D5/D10 spirit: nothing specialization-specific is hardwired into logic);
the defaults cover the launch niche and its neighbors. Insisting on a weak
path (below :data:`WEAK_FIT_THRESHOLD`) requires an explicit override, which
is recorded with an acknowledgment timestamp — الإبلاغ الصريح — and an
optional ~80/20 realistic/stretch split.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career.db.models import CareerPathAssessment, ProfileFact
from career.onboarding.confirmation import achievement_bank

#: Below this fit score a path counts as weak: approving it as Primary needs
#: an explicit, acknowledged customer override (§05).
WEAK_FIT_THRESHOLD = 40


class UnknownFamily(Exception):
    """A path key that is not in the family registry."""


class AssessmentNotFound(Exception):
    """The assessment id does not exist for this tenant."""


class WeakPathRequiresOverride(Exception):
    """A weak path was approved as Primary without the explicit override."""


@dataclass(frozen=True)
class PathFamily:
    key: str
    label_ar: str
    #: lowercase tokens matched against experience titles (Arabic + English).
    title_tokens: tuple[str, ...]
    #: lowercase tokens matched against skill names.
    skill_tokens: tuple[str, ...]
    #: lowercase tokens matched against certification names.
    cert_tokens: tuple[str, ...] = ()


DEFAULT_FAMILIES: tuple[PathFamily, ...] = (
    PathFamily(
        key="business_analyst",
        label_ar="محلل أعمال",
        title_tokens=("business analyst", "business analysis", "محلل أعمال", "محلل الأعمال"),
        skill_tokens=("sql", "power bi", "requirements", "stakeholder", "bpmn", "تحليل"),
        cert_tokens=("cbap", "ccba", "pmi-pba"),
    ),
    PathFamily(
        key="project_manager",
        label_ar="مدير مشاريع",
        title_tokens=("project manager", "program manager", "pmo", "مدير مشاريع", "مدير مشروع"),
        skill_tokens=("project management", "agile", "scrum", "planning", "إدارة مشاريع"),
        cert_tokens=("pmp", "prince2", "capm", "psm"),
    ),
    PathFamily(
        key="it_operations",
        label_ar="عمليات تقنية المعلومات",
        title_tokens=("it operations", "it manager", "service delivery", "it governance",
                      "عمليات تقنية", "مدير تقنية"),
        skill_tokens=("itil", "itsm", "sla", "incident", "governance", "servicenow"),
        cert_tokens=("itil", "cobit"),
    ),
    PathFamily(
        key="data_analyst",
        label_ar="محلل بيانات",
        title_tokens=("data analyst", "bi analyst", "محلل بيانات"),
        skill_tokens=("sql", "python", "tableau", "power bi", "excel", "statistics"),
        cert_tokens=("dasca", "microsoft certified: data analyst"),
    ),
    PathFamily(
        key="service_delivery",
        label_ar="إدارة تقديم الخدمات",
        title_tokens=("service delivery", "delivery manager", "تقديم الخدمات"),
        skill_tokens=("sla", "itil", "vendor", "kpi", "operations"),
        cert_tokens=("itil",),
    ),
    PathFamily(
        key="software_engineering",
        label_ar="هندسة برمجيات",
        title_tokens=("software engineer", "developer", "programmer", "مهندس برمجيات", "مطور"),
        skill_tokens=("java", "python", "javascript", "react", "docker", "git"),
        cert_tokens=("oca", "ocp", "aws certified developer"),
    ),
)

_BY_KEY = {f.key: f for f in DEFAULT_FAMILIES}


def family(key: str, families: tuple[PathFamily, ...] = DEFAULT_FAMILIES) -> PathFamily:
    registry = _BY_KEY if families is DEFAULT_FAMILIES else {f.key: f for f in families}
    try:
        return registry[key]
    except KeyError:
        raise UnknownFamily(key) from None


def resolve_requested(
    requested_path: str, families: tuple[PathFamily, ...] = DEFAULT_FAMILIES
) -> PathFamily | None:
    """Match the customer's free-text path to a known family — Arabic or
    English. None means an unknown path: assessed honestly, never guessed."""
    needle = requested_path.strip().lower()
    if not needle:
        return None
    for fam in families:
        if needle == fam.label_ar or any(
            tok in needle or needle in tok for tok in fam.title_tokens
        ):
            return fam
    return None


# ── deterministic scoring against the bank ───────────────────────────────────


@dataclass(frozen=True)
class PathScore:
    path: str
    score: int
    strengths: tuple[str, ...] = field(default_factory=tuple)
    gaps: tuple[str, ...] = field(default_factory=tuple)


def _bank_texts(bank: list[ProfileFact]) -> tuple[list[str], list[str], list[str]]:
    titles, skills, certs = [], [], []
    for fact in bank:
        p = fact.payload or {}
        if fact.category == "experience" and p.get("title"):
            titles.append(str(p["title"]))
        elif fact.category == "skill" and p.get("name"):
            skills.append(str(p["name"]))
        elif fact.category == "certification" and p.get("name"):
            certs.append(str(p["name"]))
    return titles, skills, certs


def score_path(fam: PathFamily, bank: list[ProfileFact]) -> PathScore:
    """Additive, capped, order-independent — same evidence, same score."""
    titles, skills, certs = _bank_texts(bank)
    strengths: list[str] = []
    gaps: list[str] = []

    matched_titles = sorted(
        t for t in titles if any(tok in t.lower() for tok in fam.title_tokens)
    )
    title_pts = 0
    if matched_titles:
        title_pts = 40 + (10 if len(matched_titles) >= 2 else 0)
        strengths.append("مسميات وظيفية مطابقة للمسار: " + "، ".join(matched_titles))
    else:
        gaps.append(f"لا توجد خبرة بمسمى قريب من مسار «{fam.label_ar}»")

    matched_skills = sorted(
        s for s in skills if any(tok in s.lower() for tok in fam.skill_tokens)
    )
    skill_pts = min(30, 10 * len(matched_skills))
    if matched_skills:
        strengths.append("مهارات مطابقة: " + "، ".join(matched_skills))
    else:
        gaps.append(f"مهارات مسار «{fam.label_ar}» غير ظاهرة في ملفك")

    matched_certs = sorted(
        c for c in certs if any(tok in c.lower() for tok in fam.cert_tokens)
    )
    cert_pts = min(10, 10 * len(matched_certs))
    if matched_certs:
        strengths.append("شهادات داعمة: " + "، ".join(matched_certs))

    return PathScore(
        path=fam.key,
        score=min(100, title_pts + skill_pts + cert_pts),
        strengths=tuple(strengths),
        gaps=tuple(gaps),
    )


def _score_custom(requested_path: str, bank: list[ProfileFact]) -> PathScore:
    """An unknown path scores only on literal evidence for it — no invention."""
    ad_hoc = PathFamily(
        key="custom",
        label_ar=requested_path.strip(),
        title_tokens=(requested_path.strip().lower(),),
        skill_tokens=(),
    )
    return score_path(ad_hoc, bank)


# ── the persisted three layers ───────────────────────────────────────────────


def assess(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    requested_path: str,
    families: tuple[PathFamily, ...] = DEFAULT_FAMILIES,
) -> CareerPathAssessment:
    """Layer 1+2: evaluate the requested path against the bank and persist a
    draft with the numeric fit, strengths, gaps, and the closest 3 paths."""
    bank = achievement_bank(session, tenant_id=tenant_id)
    scored = sorted(
        (score_path(f, bank) for f in families),
        key=lambda s: (-s.score, s.path),
    )
    requested_family = resolve_requested(requested_path, families)
    requested_score = (
        next(s for s in scored if s.path == requested_family.key)
        if requested_family is not None
        else _score_custom(requested_path, bank)
    )

    assessment = CareerPathAssessment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        requested_path=requested_path.strip()[:64],
        suggested={
            "requested": {
                "path": requested_score.path,
                "score": requested_score.score,
                "strengths": list(requested_score.strengths),
                "gaps": list(requested_score.gaps),
            },
            "closest": [
                {
                    "path": s.path,
                    "score": s.score,
                    "strengths": list(s.strengths),
                    "gaps": list(s.gaps),
                }
                for s in scored[:3]
            ],
        },
        fit_score=requested_score.score,
        status="draft",
    )
    session.add(assessment)
    session.flush()
    return assessment


def approve(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    assessment_id: uuid.UUID,
    primary: str,
    secondary: str | None = None,
    stretch: str | None = None,
    customer_override: bool = False,
    stretch_ratio_percent: int | None = None,
    families: tuple[PathFamily, ...] = DEFAULT_FAMILIES,
) -> CareerPathAssessment:
    """Layer 3: record Primary/Secondary/Stretch with the customer's consent.

    A weak Primary (fit below :data:`WEAK_FIT_THRESHOLD`) without an explicit
    override raises — the flow must inform the customer that opportunities
    will shrink and no experience will be invented, then pass override=True
    (§05). Approving supersedes any previously approved assessment."""
    assessment = session.execute(
        select(CareerPathAssessment).where(
            CareerPathAssessment.tenant_id == tenant_id,
            CareerPathAssessment.id == assessment_id,
        )
    ).scalar_one_or_none()
    if assessment is None:
        raise AssessmentNotFound(str(assessment_id))

    for key in filter(None, (primary, secondary, stretch)):
        family(key, families)  # validates — unknown slots are loud

    bank = achievement_bank(session, tenant_id=tenant_id)
    primary_score = score_path(family(primary, families), bank).score
    if primary_score < WEAK_FIT_THRESHOLD and not customer_override:
        raise WeakPathRequiresOverride(
            f"primary {primary!r} scores {primary_score} < {WEAK_FIT_THRESHOLD}"
        )

    # A single active approval per tenant: supersede earlier ones.
    for earlier in session.execute(
        select(CareerPathAssessment).where(
            CareerPathAssessment.tenant_id == tenant_id,
            CareerPathAssessment.status == "approved",
        )
    ).scalars():
        earlier.status = "superseded"

    assessment.approved = {"primary": primary, "secondary": secondary, "stretch": stretch}
    assessment.customer_override = customer_override
    if customer_override:
        assessment.override_acknowledged_at = func.now()  # الإبلاغ الصريح موثق
    assessment.stretch_ratio_percent = stretch_ratio_percent
    assessment.status = "approved"
    assessment.approved_at = func.now()
    session.flush()
    return assessment


def active_assessment(
    session: Session, *, tenant_id: uuid.UUID
) -> CareerPathAssessment | None:
    """The single approved assessment downstream consumers read (C5.9/C6)."""
    return session.execute(
        select(CareerPathAssessment).where(
            CareerPathAssessment.tenant_id == tenant_id,
            CareerPathAssessment.status == "approved",
        )
    ).scalar_one_or_none()
