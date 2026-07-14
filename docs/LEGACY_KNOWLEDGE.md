# LEGACY_KNOWLEDGE.md

> **Purpose.** This is a portable, from-scratch rebuild reference distilled from the
> `career_agent` project. It captures the *proven logic and code* — CV rendering,
> salary parsing, job identity, the discovery gate, enrichment/SSRF hardening,
> CV↔job binding, the atomic publish/quarantine flow, the ledger, Telegram
> secret-redaction, and the exact LLM prompts — so a new commercial project can be
> rebuilt with the same behavior.
>
> **Redaction contract (enforced).** This document contains **no secrets, tokens,
> API keys, credentials, personal data, real CV filenames, or ledger contents.**
> Everything below is logic and code only. Where the source referenced a real
> person's name, a real tailored-CV filename, or a real salary figure, it has been
> replaced with a neutral placeholder (`<CANDIDATE>`, `data/tailored_cvs/<role>_<id>.pdf`,
> `<AMOUNT>`). Configuration field *names* (e.g. `TELEGRAM_BOT_TOKEN`) are shown
> because they are variable names, never values.
>
> **Design ethos to carry over.** Every risky step is *fail-closed*: ambiguity,
> missing evidence, or an unexpected state produces a safe "blocked/skipped"
> outcome, never a false success. Truthfulness (never invent CV facts) and
> per-job binding (a CV is only valid for the exact job it was generated for) are
> the two invariants that everything else protects.

---

## Table of Contents
1. [CV Template & Design (PDF generation)](#1-cv-template--design-pdf-generation)
2. [Salary Logic](#2-salary-logic)
3. [Job Identity (URL-v1)](#3-job-identity-url-v1)
4. [Gate & Ranking](#4-gate--ranking)
5. [Search Queries](#5-search-queries)
6. [Enrichment & SSRF](#6-enrichment--ssrf)
7. [Binding & Publishing](#7-binding--publishing)
8. [The Ledger](#8-the-ledger)
9. [Telegram Integration](#9-telegram-integration)
10. [Model Calls (Prompts)](#10-model-calls-prompts)
11. [Known Bugs — "Do Not Repeat"](#11-known-bugs--do-not-repeat)
12. [Calibrated Settings](#12-calibrated-settings)
13. [Additional Notes](#13-additional-notes)

---

## 1. CV Template & Design (PDF generation)

**Stack:** `WeasyPrint` (HTML/CSS → PDF) + `Jinja2` templating. The template is
force-rewritten to disk on every service init so the on-disk file can never drift
from the in-code source of truth. Data model is a `pydantic` `TailoredCV`.

### 1.1 Data model (schemas)

```python
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional

class ContactInfo(BaseModel):
    name: str
    email: EmailStr
    phone: str
    location: str
    linkedin: Optional[str] = None
    github: Optional[str] = None

class Experience(BaseModel):
    title: str
    company: str
    location: str
    start_date: str
    end_date: Optional[str] = None
    current: bool = False
    achievements: List[str] = Field(default_factory=list)
    responsibilities: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)

class Education(BaseModel):
    degree: str
    field_of_study: Optional[str] = None
    institution: str
    location: str
    graduation_date: str
    gpa: Optional[str] = None
    honors: List[str] = Field(default_factory=list)

class Certification(BaseModel):
    name: str
    issuer: str
    date: str
    credential_id: Optional[str] = None

class Project(BaseModel):
    name: str
    description: str
    technologies: List[str] = Field(default_factory=list)
    achievements: List[str] = Field(default_factory=list)
    url: Optional[str] = None

class MasterCV(BaseModel):
    contact: ContactInfo
    headline: Optional[str] = None
    summary: str
    experience: List[Experience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    certifications: List[Certification] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)

class TailoredCV(BaseModel):
    master_cv: MasterCV
    job_title: str
    company: str
    tailored_summary: str
    selected_experience: List[Experience] = Field(default_factory=list)
    selected_skills: List[str] = Field(default_factory=list)
    selected_projects: List[Project] = Field(default_factory=list)
    modifications: str
```

### 1.2 The CV template — **verbatim** (the "v5" design)

This is the most important artifact to reproduce exactly. Design language:
blue accent (`#2c3e50` / `#34495e`), thin section rules, pill-badge skills, flex
role/edu rows, single-page A4.

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{{ cv.master_cv.contact.name }} - CV</title>
    <style>
        @page { size: A4; margin: 16mm 16mm 14mm 16mm; }
        * { box-sizing: border-box; }
        html, body {
            font-family: "Liberation Sans","DejaVu Sans",Arial,sans-serif;
            color: #111; font-size: 10pt; line-height: 1.32; margin: 0;
        }
        .name      { font-size: 27pt; font-weight: 700; color: #2c3e50; letter-spacing: .2px; }
        .headline  { font-size: 12pt; font-weight: 700; color: #34495e; margin-top: 1mm; }
        .contact   { font-size: 9.5pt; color: #7f8c8d; margin-top: 1.5mm; word-break: break-all; }
        .summary   { font-size: 10pt; margin-top: 3mm; text-align: justify; }
        .section   {
            font-size: 14pt; font-weight: 700; color: #34495e;
            border-bottom: 0.5pt solid #bdc3c7; padding-bottom: 0.8mm;
            margin: 4mm 0 2mm 0;
        }
        .role-head {
            display: flex; justify-content: space-between; gap: 6mm;
            align-items: baseline; margin-top: 2mm;
        }
        .role-left    { font-size: 11pt; }
        .role-title   { font-weight: 700; color: #111; }
        .role-company { font-style: italic; color: #7f8c8d; font-size: 10.5pt; }
        .role-dates   { font-size: 9.5pt; color: #95a5a6; white-space: nowrap; }
        ul.bullets    { margin: 1mm 0 0 5mm; padding: 0; list-style: none; }
        ul.bullets li { font-size: 10pt; margin: 0.6mm 0; text-indent: -3mm; padding-left: 3mm; }
        ul.bullets li::before { content: "\2022\00a0\00a0"; }
        .edu-head     {
            display: flex; justify-content: space-between; align-items: baseline; margin-top: 1mm;
        }
        .edu-degree   { font-weight: 700; font-size: 11pt; }
        .edu-inst     { font-style: italic; color: #7f8c8d; font-size: 10.5pt; }
        .edu-year     { font-size: 9.5pt; color: #95a5a6; }
        .skills-wrap  { display: flex; flex-wrap: wrap; gap: 1.2mm 1.5mm; margin-top: 0.8mm; }
        .pill         {
            background: #ecf0f1; color: #2c3e50; font-size: 9pt; line-height: 1.15;
            padding: 0.5mm 2.6mm; border-radius: 2pt; display: inline-block;
            border: 0.3pt solid #dde2e5;
        }
        .tagline       { font-size: 9.5pt; margin-top: 2mm; color: #111; }
        .tagline-label { font-weight: 700; color: #34495e; }
    </style>
</head>
<body>
    <div class="name">{{ cv.master_cv.contact.name }}</div>
    {% if cv.master_cv.headline %}
    <div class="headline">{{ cv.master_cv.headline }}</div>
    {% endif %}
    <div class="contact">{{ cv.master_cv.contact.email }} | {{ cv.master_cv.contact.phone }} | {{ cv.master_cv.contact.location }}{% if cv.master_cv.contact.linkedin %} | {{ cv.master_cv.contact.linkedin }}{% endif %}{% if cv.master_cv.contact.github %} | {{ cv.master_cv.contact.github }}{% endif %}</div>

    <div class="summary">{{ cv.tailored_summary }}</div>

    <div class="section">Professional Experience</div>
    {% for exp in cv.selected_experience %}
      <div class="role-head">
        <div class="role-left"><span class="role-title">{{ exp.title }}</span><span class="role-company"> - {{ exp.company }}</span></div>
        <div class="role-dates">{{ exp.start_date | fmt_month }} - {{ exp.end_date | fmt_month }}</div>
      </div>
      <ul class="bullets">
        {% for achievement in exp.achievements %}<li>{{ achievement }}</li>{% endfor %}
      </ul>
    {% endfor %}

    <div class="section">Education</div>
    {% for edu in cv.master_cv.education %}
      <div class="edu-head">
        <div><span class="edu-degree">{{ edu.degree }}{% if edu.field_of_study %} in {{ edu.field_of_study }}{% endif %}</span><span class="edu-inst"> - {{ edu.institution }}</span></div>
        <div class="edu-year">{{ edu.graduation_date | fmt_month }}</div>
      </div>
    {% endfor %}

    <div class="section">Skills</div>
    <div class="skills-wrap">
        {% for skill in cv.selected_skills %}<span class="pill">{{ skill }}</span>{% endfor %}
    </div>
    {% if cv.master_cv.certifications %}
    <div class="tagline"><span class="tagline-label">Certifications:</span> {{ cv.master_cv.certifications | map(attribute='name') | join(' | ') }}</div>
    {% endif %}
    {% if cv.master_cv.languages %}
    <div class="tagline"><span class="tagline-label">Languages:</span> {{ cv.master_cv.languages | join(' | ') }}</div>
    {% endif %}

    {% if cv.selected_projects %}
    <div class="section">Projects</div>
    {% for project in cv.selected_projects %}
      <div class="role-head"><div class="role-left"><span class="role-title">{{ project.name }}</span></div></div>
      {% if project.description %}<div style="font-size: 10pt; margin-top: 0.5mm;">{{ project.description }}</div>{% endif %}
    {% endfor %}
    {% endif %}
</body>
</html>
```

**Section order (top → bottom):** Name → Headline → Contact line → Summary →
Professional Experience → Education → Skills (pills) → Certifications (inline) →
Languages (inline) → Projects (optional).

### 1.3 Jinja date filter (`fmt_month`)

`'YYYY-MM' → 'MMM YYYY'`; `None`/`Present`/`Current`/`Now` → `Present`;
anything unparseable passes through untouched.

```python
@staticmethod
def _fmt_month_filter(yyyy_mm):
    if not yyyy_mm:
        return "Present"
    s = str(yyyy_mm).strip()
    if s.lower() in {"present", "current", "now"}:
        return "Present"
    try:
        parts = s.split("-")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            m = int(parts[1])
            if 1 <= m <= 12:
                return f"{months[m]} {parts[0]}"
    except Exception:
        pass
    return s
```

### 1.4 Render entry point

```python
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

# jinja_env.filters["fmt_month"] = self._fmt_month_filter
template = self.jinja_env.get_template("cv_template.html")
html_content = template.render(cv=cv)
HTML(string=html_content).write_pdf(output_path)
```

Filename sanitizer for scratch outputs (canonical per-job names come from the
pipeline — see §7):

```python
@staticmethod
def _safe_name(value: str) -> str:
    import re
    name = re.sub(r"[\s/\\]+", "_", value)
    name = re.sub(r"[^\w\-]", "", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_") or "untitled"
```

### 1.5 One-page enforcement (content budget)

Caps applied *before* render. These numbers are calibrated to fit the v5 design
on a single A4 page:

```python
self.max_summary_chars       = 400
self.max_experience_entries  = 4     # SAMA + 2 mid + earliest role
self.max_achievements_per_job = 4    # per-role ceiling
self.max_projects            = 2
self.max_skills              = 14
```

**Per-role bullet pacing (designer pacing — top roles get visual weight):**
`targets = [4, 4, 3, 2]` (rank 1 & 2 → 4 bullets, rank 3 → 3, rank 4+ → 2).
Achievements are kept verbatim and prioritized; remaining slots filled with
JD-keyword-ranked responsibilities. **No invention** — every bullet originates
from the master CV.

### 1.6 Complete-sentence summary enforcement (critical, DD-CV-06)

**Never cut a summary mid-sentence or append `"..."`.** The old code sliced at
`max_chars`, dropped the partial word, and appended `"..."`, producing summaries
ending like `"…SAR <AMOUNT> and…"`. Replaced with deterministic complete-sentence
retention + a pre-publication quality predicate. No LLM, no network, no invented
text — retained text is always a truthful verbatim subset.

```python
_TERMINATORS = ".?!"
_ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "u.s.", "u.k.", "u.a.e.",
    "mr.", "mrs.", "ms.", "dr.", "prof.", "ph.d.", "no.",
    "inc.", "ltd.", "co.", "jr.", "sr.", "st.",
}
_DANGLING_CONNECTORS = {
    "and", "or", "including", "with", "for", "to", "of", "in", "on", "by",
    "through", "while", "which", "that", "as", "such", "the", "a", "an",
}

def _split_complete_sentences(text: str) -> list[str]:
    """Split into COMPLETE sentences. A boundary is '. ? !' followed by
    whitespace/EOT, excluding decimals (3.5) and known abbreviations. Any
    trailing fragment without a terminator is dropped."""
    sentences, start, i, n = [], 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _TERMINATORS and (i + 1 >= n or text[i + 1].isspace()):
            is_decimal = (ch == "." and i > 0 and text[i-1].isdigit()
                          and i+1 < n and text[i+1].isdigit())
            seg = text[start:i+1]; toks = seg.split()
            last_tok = toks[-1].lower() if toks else ""
            is_abbrev = last_tok in _ABBREVIATIONS
            if not is_decimal and not is_abbrev:
                sentence = text[start:i+1].strip()
                if sentence:
                    sentences.append(sentence)
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                start = j; i = j; continue
        i += 1
    return sentences

def enforce_complete_summary(summary, max_chars: int = 400) -> str:
    if not isinstance(summary, str):
        return ""
    s = summary.strip()
    if len(s) <= max_chars:
        return s
    sentences = _split_complete_sentences(s)
    prefix = []
    for sent in sentences:
        candidate = " ".join(prefix + [sent]).strip()
        if len(candidate) <= max_chars:
            prefix.append(sent)
        else:
            break
    if prefix:
        return " ".join(prefix).strip()
    fitting = [x for x in sentences if len(x) <= max_chars]
    if fitting:
        return min(fitting, key=len)
    return s  # over budget → quality gate blocks it

def validate_summary_quality(summary, *, max_chars: int = 400):
    """Returns (True, None) or (False, reason_code). Never exposes summary text."""
    if summary is None:            return (False, "summary_missing")
    if not isinstance(summary, str): return (False, "summary_not_string")
    s = summary.strip()
    if not s:                      return (False, "summary_missing")
    if len(s) > max_chars:         return (False, "summary_too_long")
    if s.endswith("…") or s.endswith("..."):
        return (False, "summary_trailing_ellipsis")
    core = s.rstrip("\"'”’)]").rstrip()
    if not core:                   return (False, "summary_incomplete_terminal")
    if core[-1] not in _TERMINATORS: return (False, "summary_incomplete_terminal")
    body = core.rstrip(_TERMINATORS).rstrip("\"'”’)]").rstrip()
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", body)
    if words and words[-1].lower() in _DANGLING_CONNECTORS:
        return (False, "summary_dangling_connector")
    return (True, None)
```

`validate_summary_quality` is called as a **pre-publication gate** before any
PDF/render/publish (see §7). A failing summary blocks the whole CV *before* any
file is written.

### 1.7 Cover letter template — verbatim

Rendered with the same WeasyPrint+Jinja stack. `letter_text` is split into
paragraphs on `\n\n`; `current_date` is `datetime.now().strftime("%d %B %Y")`.

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Cover Letter - {{ cv.job_title }} - {{ cv.master_cv.contact.name }}</title>
    <style>
        @page {
            size: A4;
            margin: 2cm;
        }
        body {
            font-family: Arial, sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #222;
        }
        h1 {
            font-size: 18pt;
            margin: 0 0 5px 0;
            color: #2c3e50;
        }
        .headline {
            font-size: 10pt;
            margin-bottom: 8px;
            color: #34495e;
            font-weight: bold;
        }
        .contact {
            font-size: 9pt;
            margin-bottom: 20px;
            color: #666;
        }
        .meta {
            margin-bottom: 20px;
        }
        .meta div {
            margin-bottom: 4px;
        }
        p {
            margin: 0 0 14px 0;
            text-align: justify;
        }
    </style>
</head>
<body>
    <h1>{{ cv.master_cv.contact.name }}</h1>
    {% if cv.master_cv.headline %}
    <div class="headline">{{ cv.master_cv.headline }}</div>
    {% endif %}
    <div class="contact">
        {{ cv.master_cv.contact.email }} | {{ cv.master_cv.contact.phone }} | {{ cv.master_cv.contact.location }}
        {% if cv.master_cv.contact.linkedin %} | {{ cv.master_cv.contact.linkedin }}{% endif %}
        {% if cv.master_cv.contact.github %} | {{ cv.master_cv.contact.github }}{% endif %}
    </div>

    <div class="meta">
        <div>{{ current_date }}</div>
        <div>{{ cv.company }}</div>
        <div>{{ cv.job_title }}</div>
    </div>

    {% for paragraph in letter_paragraphs %}
    <p>{{ paragraph }}</p>
    {% endfor %}
</body>
</html>
```

Note: the CV template is force-rewritten on every startup; the cover letter
template is only written if absent (`if not cover_template_file.exists()`).

### 1.8 End-to-end generation flow (exactly how a production CV is made)

The precise call chain that produces every real tailored CV:

```
job (title, company, jd_text, url/id)
  │
  ▼ 1. JobAnalyzer.analyze(jd, title, company)          → role_family/seniority/
  │      LLM prompt §10.1, stub fallback                   match_score/key_requirements
  ▼ 2. CVTailor.tailor(jd, analysis, title, company)
  │    a. load master CV (loader merges private overlay — §13 PII split)
  │    b. _rank_experience         → LLM prompt §10.3, rule-based fallback
  │    c. _enrich_experience_bullets → pacing [4,4,3,2], JD-keyword ranked,
  │                                    achievements verbatim first, NO invention
  │    d. _rank_skills             → JD relevance scoring, cap 12 (visual fit)
  │    e. _rank_projects
  │    f. _select_summary_variant  → §10.5 (deterministic, pre-authored variants)
  │    g. HumanizationService.humanize_summary → prompt §10.2 + invented-content
  │                                    guard; <150 chars → fall back to variant
  │    h. PageEnforcementService.enforce_one_page → §1.5 caps + §1.6 complete-
  │                                    sentence summary enforcement
  ▼ 3. ValidationService.validate  (warnings only, non-blocking)
  ▼ 4. validate_summary_quality    → §1.6 gate; failure blocks BEFORE any file
  ▼ 5. _publish_tailored_cv_pair   → §7.2 atomic render+sidecar publish
  │      filename: joburl-<64hex>.pdf (URL-v1) or {company}_{role}_{id}.pdf
  ▼ 6. validate_cv_binding         → §7.1 sole acceptance authority;
         only ready_for_apply_upload exposes the path
```

Every step degrades safely: LLM unavailable → rule-based ranking + variant
summary; the *look* of the PDF is identical either way because the template and
page-enforcement caps are deterministic.

**Where identity and CV-binding enter the flow (verified in `build_digest`):**
each discovered candidate gets, at digest-build time — BEFORE any gate/review —
(a) its **URL-v1 canonical identity attached** (`identity_version` +
`canonical_job_identity`) whenever a valid http(s) URL derives one (blank/
malformed URL → fields absent → downstream fails closed); native
`posting_id`/`source_job_id` are retained as **provenance only**, never the
identity authority; and (b) its **per-job tailored CV bound** via
`_enrich_job_cv_fields` → `cv_attachment_path`/`cv_attachment_status`. Then
score → sort by fit desc → `jobs = top[:TOP_N]` +
`review_candidates = scored[:30]` from the SAME pass (enabling review never
triggers a second discovery).

### 1.9 Master CV knowledge base — `master_cv.json` shape (keys only)

The single knowledge base driving CV generation, cover letters, AND apply-form
answers. Structure below is the complete key map (values redacted — the real
file is PII-split, see §13). Rebuild this shape as-is; every consumer reads it
through a loader that merges the private overlay.

```jsonc
{
  "_meta": { "schema_version", "last_updated", "owner", "purpose",
             "consumed_by": [], "todo_fields": [], "resolved_fields_log": [] },

  "personal": {
    "first_name", "last_name", "full_name", "preferred_name", "pronouns",
    "email", "phone",
    "location": { "city", "region", "country", "country_code", "timezone",
                  "willing_to_relocate", "remote_preference",
                  "street_address", "district", "postal_code",
                  "address_line_1", "address_line_2", "address_full",
                  "address_line_1_arabic", "city_arabic", "address_full_arabic" },
    "date_of_birth", "nationality", "national_id", "passport_number",
    "gender", "marital_status", "number_of_dependents",
    "arabic_full_name", "arabic_given_name", "arabic_family_name", "gosi_number"
  },

  "professional_headline": {
    "primary",
    "variants": { "business_analyst", "consultant", "solution_consultant",
                  "implementation_consultant", "digital_transformation" }
  },

  "summary": {                       // ← feeds §10.5 variant selection
    "primary", "short", "one_liner",
    "variants": { "functional_consultant", "solution_consultant",
                  "implementation_consultant", "technical_ba_lead",
                  "digital_transformation" }
  },

  "experience": [{                   // ← feeds ranking + bullet enrichment
    "id", "title", "employer", "client", "display_company",
    "location": { "city", "country" },
    "start_date", "end_date", "is_current", "employment_type", "industry",
    "description", "responsibilities": [], "achievements": [], "skills_used": []
  }],

  "education": [{ "id", "degree", "field_of_study", "institution",
    "institution_short", "location": {...}, "start_date", "end_date",
    "graduation_year", "gpa", "honors", "graduation_date", "degree_english" }],

  "certifications": [{ "id", "name", "short_name", "full_name", "issuer",
    "issue_date", "expiry_date", "credential_id", "credential_url", "is_active" }],

  "skills": {                        // nested categories, flattened by consumers
    "strategic_business_analysis": [], "digital_transformation": [],
    "solution_design": [], "operational_excellence": [],
    "stakeholder_management": [], "governance_compliance": [],
    "methodologies": [],
    "technical": { "itsm": [], "endpoint_device_management": [],
                   "infrastructure_security": [], "reporting": [],
                   "collaboration_documentation": [] },
    "soft_skills": []
  },

  "languages": [{ "language", "language_native_script", "proficiency",
    "proficiency_cefr", "read", "write", "speak", "is_primary" }],

  "years_of_experience",

  "application_fields": {           // ← apply-form answer bank (portal questions)
    "work_authorization", "salary_expectations", "notice_period_days",
    "availability_to_start", "willing_to_travel(_percentage)",
    "willing_to_work_remotely/hybrid/onsite", "preferred_work_arrangement",
    "desired_employment_types": [], "has_drivers_license", "has_own_transport",
    "criminal_record(_response)", "referral_source(_alternatives)",
    "previously_worked_at": { "<company>": "..." },
    "work_authorized_saudi_arabia", "requires_visa_sponsorship",
    "current/expected_salary_sar_monthly + currency + period",
    "salary_disclosure_policy", "highest_education_degree/field/graduation_year",
    "cover_letter_policy", "additional_documents_policy",
    "arabic_address_policy", "references_policy",
    "max_attempts_per_apply", "telegram_notification_policy"
  },

  "target_roles": { "primary": [], "secondary": [], "industries_preferred": [],
    "industries_avoid": [], "company_size_preferred": [],
    "exclude_companies": [], "exclude_keywords_in_role": [] },

  "voluntary_disclosure": { "gender", "ethnicity", "race", "veteran_status",
    "disability_status", "hispanic_or_latino", ... },   // EEO-form defaults

  "references": { "available_on_request", "default_response",
    "default_response_arabic", "minimum_required_typical", "policy" },

  "qa_bank": {                      // ← canned interview/portal answers
    "why_this_role": { "template", "placeholders": [] },
    "why_this_company": { "template", "placeholders": [] },
    "greatest_strength", "greatest_weakness", "five_year_plan",
    "biggest_achievement", "challenging_project", "leadership_example",
    "conflict_resolution", "salary_expectations", "notice_period",
    "why_leaving_current_role", "tell_me_about_yourself",
    "are_you_authorized_to_work_in_country": { "saudi_arabia", "default_other_countries" },
    "do_you_require_sponsorship": { ... },
    "highest_education", "current_employer", "current_job_title",
    "willing_to_relocate", "preferred_start_date"
  },

  "cover_letter": { "default_greeting", "default_closing",
    "templates": { "functional_consultant": {"subject","body"},
                   "solution_consultant": {...}, "generic": {...} } },

  "documents": { "default_cv_pdf", "default_cv_docx",
    "tailored_cvs": { "<variant>": "path|null" },
    "cover_letter_pdf", "certifications_pdfs": { "<cert>": "path|null" } },

  "_privacy": { "pii_split", "private_overlay", "redacted_fields": [], "note" }
}
```

Key insights a rebuild must keep:
- **One knowledge base, three consumers**: CV generation (§1.8), cover letters
  (§10.7 payload), and apply-form filling (`application_fields` + `qa_bank`).
- **Variants over generation**: headline/summary/cover-letter variants are
  pre-authored per role family and *selected*, not generated — safer and free.
- **`_privacy` block** documents the PII split (§13) inside the file itself.
- The digest's role→CV map (§13) and the summary-variant selector (§10.5) both
  key off the same variant names — keep them consistent.

**Normalization rules — raw JSON → render schema (`_normalize_nested_format`).**
These decide *what literally appears on the PDF*; get them wrong and the CV
shows the wrong employer name or raw JSON:

- **Company shown on the CV:** `display_company or employer or company` — in
  that order. `display_company` exists precisely so consulting placements can
  show the client-facing brand.
- **Phone:** dict → `international_formatted or international`; else `str()`.
- **Location:** dict → `"City, Country"` (both stripped); experience/education
  default `"Riyadh, Saudi Arabia"` when absent.
- **Name:** `full_name` or `"first_name last_name"`.
- **Headline:** dict → `.primary`; else legacy `headline`/`title`.
- **Summary:** dict → `primary or short or one_liner or ""`.
- **`current` detection:** `is_current`/`current` flag, else
  `end_date in {"present","current","now"}` (case-insensitive);
  `current=True` forces `end_date=None` (template then prints "Present").
- **Technologies:** `skills_used or technologies`.
- **Certification display rule:** prefer `short_name`, EXCEPT
  `Security+` → use `full_name` ("CompTIA Security+") because the bare short
  name reads wrong without the issuer. Date: `issue_date or date or "N/A"`.
- **Skills:** nested category dict (incl. `technical.{sub}` lists) flattened in
  **insertion order**, deduplicated first-wins — category order in the JSON is
  therefore the skill display priority.
- **Languages:** `[{language, proficiency}]` → `"English (Fluent)"` strings.
- **Two source formats supported:** nested (detected by `personal` or
  `professional_headline` key) and legacy flat; both normalize to the same
  strict schema. **NEVER pass a dict where the schema expects str — that exact
  bug made v2 render raw JSON inside the PDF.**
- Missing `master_cv.json` → `FileNotFoundError` with an explicit "will not
  fabricate placeholder CV data" message — the service refuses to invent a CV.

**Pre-render validation (`ValidationService.validate` — warnings never block,
issues mark invalid):**

```python
min_summary_length = 150         # issue
min_experience_entries = 1       # issue
min_achievements_per_job = 2     # warning per role
min_skills = 5                   # issue
max_estimated_pages = 1.15       # warning (chars/3000 heuristic)
weak_phrases = ["experienced professional","diverse background",
    "proven track record","results-driven","team player","hard worker",
    "go-getter","self-starter","dynamic individual"]   # >=2 → warning
# issues: missing/invalid email (regex), missing phone, "lorem ipsum" in
#   summary, summary < 10 words, ARABIC characters detected anywhere in
#   summary/experience/achievements/skills (CV must be English-only —
#   Unicode blocks U+0600–06FF, U+0750–077F, U+FB50–FDFF, U+FE70–FEFF)
# warning: linkedin URL not matching linkedin.com/in/…
```

The Arabic-leak guard matters: the knowledge base legitimately contains Arabic
fields (names, addresses) for apply forms — this gate stops them bleeding into
the English CV.

### 1.10 Verifying the look — demo render

To confirm a rebuilt template is pixel-faithful, render placeholder data through
the real service and compare against the reference output (a demo PDF generated
this way — 1 page A4, name 27pt blue, pill skills, `Mar 2021 - Present` dates):

```python
from app.services.pdf_service import PDFService
from app.schemas.cv import MasterCV, TailoredCV, ContactInfo, Experience, Education, Certification

master = MasterCV(
    contact=ContactInfo(name="John Placeholder", email="john.placeholder@example.com",
                        phone="+000 00 000 0000", location="Riyadh, Saudi Arabia",
                        linkedin="linkedin.com/in/placeholder"),
    headline="IT Operations & Business Analysis Leader | PMP | PMI-PBA",
    summary="Placeholder summary.",
    education=[Education(degree="Bachelor of Science", field_of_study="Information Systems",
                         institution="Example University", location="Riyadh",
                         graduation_date="2013-06")],
    certifications=[Certification(name="PMP", issuer="PMI", date="2020-01")],
    languages=["Arabic (Native)", "English (Fluent)"],
)
cv = TailoredCV(master_cv=master, job_title="IT Operations Manager", company="Demo Employer",
    tailored_summary="...(complete sentences, <=400 chars)...",
    selected_experience=[Experience(title="Senior IT Operations Manager",
        company="Example Financial Group", location="Riyadh",
        start_date="2021-03", end_date=None, current=True,
        achievements=["...", "...", "...", "..."])],   # 4/4/3/2 pacing
    selected_skills=["IT Service Management", "ITIL v4", "..."],  # <=12
    modifications="demo render — placeholder data only")
PDFService().render_to_pdf(cv, output_filename="demo_template_check.pdf")
# Verify: exactly 1 page; sections in §1.2 order; dates via fmt_month.
```

---

## 2. Salary Logic

There are **two** salary layers. The digest gate (§2.1) is the real production
path used to accept/reject jobs; the estimation service (§2.2) is the older
Phase-4 heuristic. Both enforce the **hard minimum of 24,000 SAR/month**.

### 2.1 Digest salary gate (production)

Gate outcomes:

```python
SALARY_GATE_PASS_CONFIRMED       = "PASS_CONFIRMED"
SALARY_GATE_PASS_LIKELY_HIGH     = "PASS_LIKELY_HIGH"
SALARY_GATE_PASS_POSSIBLE_HIGH   = "PASS_POSSIBLE_HIGH"
SALARY_GATE_FAIL_LIKELY_LOW      = "FAIL_LIKELY_LOW"
SALARY_GATE_FAIL_JUNIOR_OR_SUPPORT = "FAIL_JUNIOR_OR_SUPPORT"
SALARY_GATE_UNKNOWN              = "UNKNOWN_INSUFFICIENT_EVIDENCE"

SALARY_INFERENCE_DISCLAIMER = (
    "Salary is inferred unless explicitly stated; inferred salary is not guaranteed."
)
```

**Junior/support disqualifier tokens** (title contains any → immediate fail):

```python
_JUNIOR_SUPPORT_TITLE_TOKENS = (
    "helpdesk", "help desk", "desktop support", "l1 support", "l2 support",
    "level 1 support", "level 2 support", "service desk agent", "junior", "intern",
    "trainee", "cabling", "field technician", "hardware technician",
)
```

**Explicit-amount parsing (regex + period normalization).** Range patterns are
tried before single patterns; each numeric group has an optional `k` group so
k-notation is detected per-amount:

```python
_SALARY_RANGE_PATTERNS = [
    r'(?:sar|sr)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:–|-|—|to)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?',
    r'([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:–|-|—|to)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:sar|sr)\b',
]
_SALARY_SINGLE_PATTERNS = [
    r'(?:sar|sr)\s*([\d,]+(?:\.\d+)?)\s*(k\b)?',
    r'([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:sar|sr)\b',
]

# Period markers are PHRASES only — never the bare word "year" (so "5+ years
# experience" is not mistaken for an annual salary).
_ANNUAL_MARKER_RX = re.compile(
    r'per\s*annum|\bannum\b|\bannual(?:ly)?\b|\byearly\b|per\s*year'
    r'|/\s*year|/\s*yr\b|\ba\s*year\b|\bp\.?\s*a\.?\b')
_MONTHLY_MARKER_RX = re.compile(
    r'per\s*month|/\s*month|/\s*mo\b|\bmonthly\b|\ba\s*month\b|\bpcm\b|\bp\.?\s*m\.?\b')
```

Period detection & normalization (annual → monthly is `/12` **exactly once**,
using `Decimal` + `ROUND_HALF_UP` to avoid float drift; monthly wins ties;
`None` = ambiguous → **fail closed**):

```python
def _detect_salary_period(context):
    if _MONTHLY_MARKER_RX.search(context): return "monthly"
    if _ANNUAL_MARKER_RX.search(context):  return "annual"
    return None  # ambiguous → caller fails closed

def _to_monthly_sar(value, period):
    if period == "annual":
        return float((Decimal(str(value)) / Decimal(12)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))
    return float(value)

def _amount_with_k(num, k_group):
    val = float(num.replace(",", ""))
    if k_group:
        val *= 1000.0
    return val
```

Local period detection prefers the **trailing** marker (`"SAR 30,000 per month"`)
in a tight 15-char forward window, falling back to a 20-char backward lookback
(`"Monthly salary: 15,000 SAR"`):

```python
def _period_for_match(text_l, m):
    forward = _detect_salary_period(text_l[m.end(): m.end() + 15])
    if forward is not None:
        return forward
    return _detect_salary_period(text_l[max(0, m.start() - 20): m.start()])
```

**Multi-representation consistency (fail-closed on contradiction).** Every
explicit amount is enumerated and normalized with its own local period. If two
trusted representations materially disagree (tolerance `1.0` SAR — only absorbs
cents from non-12-divisible annual figures), the whole parse returns `None`:

```python
_SALARY_CONSISTENCY_TOL_SAR = 1.0
# ...single value → (v, v); range → (lo, hi) with lo/hi swapped if inverted...
base_lo, base_hi = trusted[0]
for lo_m, hi_m in trusted[1:]:
    if (abs(lo_m - base_lo) > _SALARY_CONSISTENCY_TOL_SAR
            or abs(hi_m - base_hi) > _SALARY_CONSISTENCY_TOL_SAR):
        return None
return (base_lo, base_hi)
```

**Gate scoring (`_score_salary_gate`).** Order of decisions:

1. Junior/support token in title → `FAIL_JUNIOR_OR_SUPPORT` (confidence 95).
2. Explicit SAR amount parsed from JD → `PASS_CONFIRMED` if `lo >= 24_000`, else
   `FAIL_LIKELY_LOW` (confidence 95).
3. Otherwise an **evidence score** is accumulated:

```python
tier_pts = {1: 30, 2: 20, 3: 10, 4: 0}          # company tier
# seniority (title):
#   exec (head of / director / vp / chief) +20
#   manager / lead / principal / head        +15
#   senior / consultant / specialist / partner +10
#   analyst / advisor                         +5
# regulated employer signal (+10): sama, central bank, neom, aramco, sabic, pif,
#   bank, financial, regulatory, compliance, vision 2030
# JD-derived (only when jd_text present):
#   "team of"/"team size"/"budget"/"board"/"executive"   +8
#   years >= 7 → +10   ;  years >= 5 → +5
# aggregator source (source_quality_score <= 25)          -5

# Buckets:
#   evidence >= 35 → PASS_LIKELY_HIGH   (est 24,000–35,000; conf min(85, ev))
#   evidence >= 20 → PASS_POSSIBLE_HIGH (est 18,000–28,000; conf min(60, ev))
#   evidence >= 10 → FAIL_LIKELY_LOW    (est 10,000–20,000; conf min(60, ev))
#   else           → UNKNOWN            (est None; conf 0)
```

### 2.2 Phase-4 estimation service (older heuristic)

`HARD_MIN_MONTHLY_SAR = 24_000`. Parsed text → `estimated_min = parsed*0.9`,
`estimated_max = parsed*1.1`, confidence `0.85`. Heuristic (no text): base `26,000`
adjusted by role family, seniority, and company quality:

```python
base = 26_000
# role family: BA+2k(→28k) ; IT ops/service =26k ; project/pmo +3k(→29k)
#              security/governance +4k(→30k)
# seniority: senior +3k ; manager/lead +6k ; entry/junior -6k
# company: bank/sama/aramco/neom/government +4k ; consulting +3k
base = max(18_000, base)
estimated_min, estimated_max = base, int(base * 1.25)   # 25% uncertainty band
# text parse: annual→/12 ; weekly→*4.33 ; daily→*22
```

Threshold decision: `min >= 24k` → PASS; range spans threshold → UNCERTAIN;
else FAIL.

### 2.3 Known salary edge cases that were fixed (do not regress)

- **Annual mistaken for monthly** — a large raw number is *never* assumed monthly
  by magnitude; without a reliable period marker the parse fails closed
  (commit `fba3a16` "normalize annual salary evidence to monthly").
- **"5+ years" ≠ annual salary** — period markers are phrases, never the bare
  word "year".
- **Range endpoints double-counted** — single-value scan skips spans already
  captured by a range match.
- **Contradictory monthly vs annual** — `"30,000 per month"` vs `"150,000 per
  year"` → fail closed, no silent winner.
- **Reason precision lost** — display uses `_fmt_sar` (whole numbers clean,
  fractional shows 2 dp so near-threshold values aren't obscured) (commit
  `83d8441`).

---

## 3. Job Identity (URL-v1)

Single authority for deriving a deterministic internal identity for jobs that
carry a URL but no native id. **The identity is a pure function of the normalized
URL** and is the ONE definition reused by same-run dedupe and cross-day
suppression, so those can never diverge.

```
identity = joburl:v1:<64-lowercase-hex sha256(normalized_url)>
```

**Tracking params stripped (single definition):**

```python
_UTM_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "google_jobs_apply", "ref", "fbclid", "gclid",
})
```

**Normalization steps, in order:**
1. `str(raw or "").strip()` — trim surrounding whitespace.
2. Strip tracking/UTM params (parse query, drop keys in `_UTM_PARAMS`
   case-insensitively, re-encode).
3. `.rstrip("/")` — drop a single trailing slash.
4. `.lower()` — lowercase the whole URL.
5. Empty result → `""` (no usable URL).

```python
def strip_tracking_params(url: str) -> str:
    if not url: return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in qs.items() if k.lower() not in _UTM_PARAMS}
        return urlunparse(parsed._replace(query=urlencode(filtered, doseq=True)))
    except Exception:
        return url

def normalize_job_url_v1(raw_url) -> str:
    u = str(raw_url or "").strip()
    if not u: return ""
    return strip_tracking_params(u).rstrip("/").lower()

def derive_canonical_job_identity(raw_url):
    normalized = normalize_job_url_v1(raw_url)
    if not normalized: return None
    try:
        parsed = urlparse(normalized)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"joburl:v1:{digest}"
```

**Hash input** = the fully-normalized URL string (steps 1–4). SHA-256, lowercase
hex, 64 chars. No random/uuid/timestamp/`hash()`; no title/company/id fallback;
no network. Constants: `IDENTITY_VERSION = "joburl-v1"`,
`CANONICAL_IDENTITY_PREFIX = "joburl:v1:"`.

> **Beware the second, older normalizer.** The DB search pipeline has its own
> `normalize_url` (in `jobs/normalization.py`) with a *different* tracking-key
> set (`utm_* , gclid, fbclid, mc_cid, mc_eid, ref, source`) that also drops the
> URL fragment but does NOT lowercase or strip a trailing slash. It exists only
> for DB dedupe. **The URL-v1 list above is the single identity authority** —
> never mix the two (that divergence is exactly why the delivered-ledger key
> deliberately reuses the digest expression, §8.1).

**Parsing / filename stem** (path-safe, hex-only → cannot contain `/`, `\`, `..`,
query, fragment):

```python
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

def parse_canonical_job_identity(identity):
    if not isinstance(identity, str): return None
    if not identity.startswith("joburl:v1:"): return None
    hexpart = identity[len("joburl:v1:"):]
    return hexpart if _HEX64_RE.match(hexpart) else None

def canonical_identity_safe_stem(identity):
    hexpart = parse_canonical_job_identity(identity)
    return f"joburl-{hexpart}" if hexpart else None
```

**Why this exists:** aggregator jobs (jobilize, etc.) carry only a `url` — no
`job_id`/`anchor`/`posting_id`. Without URL-v1 the resolver returned
`missing_job_identity` and no CV could ever be generated. The validator
**independently recomputes** the identity from the current URL (a caller-attested
value can never authorize a match).

---

## 4. Gate & Ranking

The intelligent gate scores every discovered job through salary + company tier +
source quality + role match, then decides SEND/BLOCK. Nothing is filtered until
the caller picks `final_send_decision == "SEND"`.

### 4.1 Company TIER classification

```python
_TIER1_KEYWORDS = frozenset({
    "sama", "saudi central bank", "central bank of saudi", "neom", "saudi aramco",
    "aramco", "sabic", "qiddiya", "gosi", "zatca", "zakat", "public investment fund",
    "pif", "stc", "saudi telecom", "riyad bank", "national commercial bank", "ncb", "snb",
    "alrajhi", "al rajhi", "sabb", "ministry", "وزارة", "هيئة", "مؤسسة النقد",
    "maaden", "saudi electricity", "hrdf", "diriyah", "roshn", "amaala", "taqnia",
    "elm", "stc pay", "national center",
})
_TIER2_KEYWORDS = frozenset({
    "delivery hero", "hungerstation", "motorola solutions", "baker hughes",
    "accenture", "deloitte", "kpmg", "pwc", "ernst", "mckinsey", "bcg",
    "bain", "ibm", "oracle", "sap ", "microsoft", "google", "cisco", "dxc",
    "cognizant", "tcs", "infosys", "wipro", "capgemini", "ericsson", "huawei",
    "nokia", "aws", "amazon", "schlumberger", "halliburton", "siemens",
    "booz allen", "mobily", "zain", "etihad etisalat", "alfanar",
    "advanced electronics", "penta consulting", "jasara",
})
_TIER4_SIGNALS = frozenset({   # low-quality aggregators
    "jooble", "bebee", "jobleads", "theirstack", "jobstack", "talentify",
    "ziprecruiter", "careerjet", "neuvoo", "adzuna", "simplyhired",
})

def _classify_company_tier(company, url=""):
    company_l = company.lower()
    for kw in _TIER1_KEYWORDS:
        if kw in company_l: return 1, f"tier1:{kw}"
    for kw in _TIER2_KEYWORDS:
        if kw in company_l: return 2, f"tier2:{kw}"
    for sig in ("solutions llc", "staffing", "recruitment", "talent", "manpower",
                "outsourc", " msp", "consult"):
        if sig in company_l: return 3, f"tier3_recruiter:{sig}"
    if len(company.strip()) < 4: return 4, "tier4_name_too_short"
    return 3, "tier3_default"

# tier → company_quality_score:  {1: 95, 2: 75, 3: 55, 4: 25}
```

### 4.2 Source-quality scoring (by URL domain)

```python
_HIGH_SOURCE_DOMAINS = frozenset({          # direct ATS → 90
    "greenhouse.io", "lever.co", "myworkdayjobs.com",
    "successfactors", "taleo.net", "smartrecruiters.com",
    "bamboohr.com", "workable.com", "recruitee.com", "icims.com",
})
_GOOD_SOURCE_DOMAINS = frozenset({          # known board → 65
    "linkedin.com", "bayt.com", "gulftalent.com", "naukrigulf.com", "indeed.com",
})
_LOW_SOURCE_DOMAINS = frozenset({           # aggregator → 20
    "jooble.org", "bebee.com", "jobleads.com", "theirstack.com",
    "careerjet", "neuvoo", "adzuna", "ziprecruiter", "simplyhired", "talentify",
    "founditgulf.com", "nationalpostdoc.org",
})
# no url → 0 ; unknown source → 50
```

### 4.3 Role match score (0–100, `_compute_role_match_score`)

**Hard rejects → return `0`** if the *title* contains any dev token or support
token:

```python
dev_tokens = ("software engineer","software developer","full stack",
    "frontend developer","backend developer","mobile developer","react developer",
    "devops engineer","java developer","python developer","ios developer",
    "android developer")
support_tokens = ("helpdesk","help desk","desktop support","l1 support",
    "l2 support","service desk agent","field technician","cabling technician")
```

Six additive dimensions (capped at 100):

```python
# 1. Role alignment (0–30) — max points of any matching title phrase:
role_map = {
    "it manager":30, "it operations manager":30, "technology manager":28,
    "it service delivery":30, "service delivery manager":28,
    "business analyst":28, "technical business analyst":30,
    "business technology":28, "it governance":28,
    "digital transformation":25, "technology operations":25,
    "it operations":25, "it lead":20, "operations manager":18,
    "information technology manager":28, "it director":25,
    "technology partner":25, "business process analyst":22,
}
# 2. Seniority (0–20):
#    head of/director/vp/chief → 18
#    senior/sr./lead/manager/principal/head → 20
#    consultant/specialist/partner/advisor → 15
#    analyst/coordinator → 10 ; else 5
# 3. Domain fit (0–15): it ops/service delivery/sla/itil/servicenow →15 ;
#    business analyst/requirements/user stories/uat →14 ;
#    governance/compliance/sama/regulatory/audit →13 ;
#    digital transformation/change management →12  (max of matches)
# 4. Skills match (0–15, sum capped):
skill_hits = {"itil":3,"pmp":3,"pmi":2,"prince2":2,"servicenow":3,"stakeholder":2,
    "requirements":2,"agile":2,"jira":1,"governance":2,"sla":2,"incident":1,
    "change management":2,"business process":2}
# 5. Business/IT bridge (0–10): +5 if any(business/stakeholder/requirements/strategy)
#    +5 if any(technology/it/digital/systems/service delivery/operations/infrastructure)
# 6. Governance/delivery (0–10): +5 if any(governance/sla/compliance/regulatory/sama)
#    +5 if any(delivery/incident/problem/change/reporting)
total = min(100, role_score + seniority + domain + skills + min(10,bridge) + min(10,gov))
```

### 4.4 SEND / BLOCK decision (`_apply_intelligent_gate`)

```
if salary_gate == FAIL_JUNIOR_OR_SUPPORT        → BLOCK "junior_or_support_role"
elif role_match_score < 70                       → BLOCK "role_match_too_low:<n>"
elif salary_gate == FAIL_LIKELY_LOW              → BLOCK "salary_likely_below_24k"
elif salary_gate in (PASS_CONFIRMED, PASS_LIKELY_HIGH):
        if tier == 4:
            SEND only if PASS_CONFIRMED and role_match_score >= 90
            else BLOCK "tier4_needs_confirmed_salary_and_match>=90"
        else: SEND
elif salary_gate == PASS_POSSIBLE_HIGH:
        SEND if role_match_score >= 90 and company_quality_score >= 85
        else BLOCK
elif salary_gate == UNKNOWN:
        SEND if role_match_score >= 90 and company_quality_score >= 85 and sq_score >= 70
        else BLOCK
```

**Ranking / cap (`_select_gate_passed_jobs`, cap `MAX_GATE_SEND = 8`):** keep only
`SEND`, then sort by `(salary_gate_order, -company_quality_score, -role_match_score)`:

```python
gate_order = {PASS_CONFIRMED:0, PASS_LIKELY_HIGH:1, PASS_POSSIBLE_HIGH:2, UNKNOWN:3}
return passed[:MAX_GATE_SEND]
```

### 4.5 Discovery-time fit score (digest, pre-gate)

Runs at discovery when only title/company/location exist (no JD yet). Its
`fit_score`/`fit_label` feed the Claude-review input (§10.6) and the plain
digest summary. Hard reject (score 0) if the title contains any of:
`software engineer, software developer, full stack, frontend developer,
backend developer, mobile developer, react developer, node.js developer,
java developer, python developer, ios developer, android developer,
devops engineer`.

```python
# Title family bonus: +20 for the FIRST matching token (one bonus max):
_STRONG_TITLE_TOKENS = ("it manager","it operations","service delivery",
    "business analyst","business technology","digital transformation",
    "technology operations","it governance","it service",
    "technical business analyst","technology manager","it lead",
    "operations manager")

# Content signals summed over title+company+location blob (capped 100):
_SCORE_SIGNALS = {
    # IT ops & service mgmt: itil 8, it operations 10, service delivery 8,
    #   incident management 7, change management 6, problem management 6,
    #   sla 6, servicenow 6, monitoring 4, noc 4,
    # BA: business analyst 9, requirements 6, user stories 6, stakeholder 6,
    #   business process 5, gap analysis 5, agile 4, scrum 3, jira 4, uat 4,
    # Governance/regulated: governance 7, compliance 6, risk 5, audit 4,
    #   regulatory 6, sama 9, central bank 7,
    # Digital: digital transformation 8, technology partner 6,
    #   business technology 7, pmp 5, pmi 4, prince2 4,
    # Management: it manager 10, technology manager 8, operations manager 6,
    #   team lead 4, head of 5,
    # Saudi context: saudi 3, ksa 3, riyadh 3, vision 2030 5, neom 3,
    #   sabic 3, aramco 3,
}

def _fit_label(score):    # STRONG >=70 · GOOD >=50 · FAIR >=30 · else LOW
```

Reason string = first 5 matched signals joined. Distinct from the full
role-match score (§4.3), which runs later with JD text at the gate.

### 4.6 Deterministic filter service (older, still valid)

Hard filters: allowed title tokens `("technical business analyst","it operations")`;
title containing `"lead"` → reject; `>= 3` dev tokens in body → reject; stated
salary `< 24,000` → reject. Then a weighted keyword `SCORE_SIGNALS` map produces a
0–100 score; `< 55` still matches but flags weak alignment. **Note:** `"sql"` is
deliberately NOT a dev-reject token (BAs need it).

---

## 5. Search Queries

### 5.1 Digest target queries — the 12 (production)

```python
TARGET_QUERIES = (
    "IT Manager",
    "IT Operations Manager",
    "IT Service Delivery Manager",
    "Business Analyst",
    "Business Technology Partner",
    "Business Technology Consultant",
    "IT Governance Manager",
    "Technical Business Analyst",
    "Digital Transformation Manager",
    "Technology Operations Manager",
    "Service Delivery Manager",
    "IT Operations Lead",
)
TARGET_LOCATIONS = ("Riyadh, Saudi Arabia", "Saudi Arabia")
TOP_N = 20
MAX_PER_QUERY = 5
```

### 5.2 SerpAPI — TWO providers; know which is production

**Production (digest path): the `google_jobs` engine adapter.** The digest's
`_run_discovery` calls the fetchers **directly** (verified against source —
see the orchestration note below for the two distinct paths):

```python
params = {
    "engine": "google_jobs",
    "q": query,               # keywords + family query_suffix expansion
    "location": location,     # per TARGET_LOCATIONS
    "hl": "en",
    "api_key": settings.SERPAPI_API_KEY,
}
# GET https://serpapi.com/search.json → data["jobs_results"]
# stdlib urllib, Accept: application/json, timeout _REQUEST_TIMEOUT
```

Row acceptance: `title` AND `company_name` required; URL preference
**`apply_link` > `share_link` > `related_link`** (canonical employer/ATS link,
never a Google page); description assembled from `job_highlights` when the main
description is empty; skip reasons counted, never silent.

**Family tuning** (per family: `query_suffix` appended to the query +
title-relevance filter, *lenient*): any `positive_signal` in title → accept;
else any `negative_pattern` regex → reject; **neither → accept**. Family
detection order (most specific first): `technical business analyst →
business analyst → project manager → it manager`. The negative patterns are
anchored noise-title rejects (`^sales manager, ^store manager, ^restaurant
manager, ^area/regional manager, ^procurement manager$, ^warehouse/kitchen/
outlet/branch manager, ^account manager$, ^general manager$`, + for IT:
`^marketing/hr/finance manager$`).

**Older path (Phase-4 search service): plain `google` engine** — organic web
results, kept for reference:

```python
params = {"engine": "google", "q": q, "num": min(10, max_results), "api_key": key}
# pull item["link"] from data["organic_results"][:max_results]
```

**Discovery orchestration — TWO DISTINCT PATHS (source-verified; do not merge
them in a rebuild):**

1. **Digest path (`_run_discovery` — what the daily digest actually runs):**
   calls exactly TWO fetchers directly — `_fetch_serpapi_google_jobs(queries,
   locations, MAX_PER_QUERY=5)` then `_fetch_jobspy(queries, MAX_PER_QUERY=5)`
   — each in its own try/except (`{"status":"error","reason":<ExcClass>}`),
   then `dedupe_discovered_candidates` and done. **No lever, no local pools,
   no `max_candidates` cap, and NO LinkedIn URL filter at this layer.**
   LinkedIn exclusion in the digest path comes from three other layers:
   JobSpy's site exclusion, google_jobs domain classification (linkedin.com is
   an aggregator, never preferred), and the JD-unsafe/enrichment blocklist.
   google_jobs fan-out: 12 queries × 2 locations × ≤5; jobspy: ONE call with
   all 12 keywords OR-joined, location "Saudi Arabia", ≤5.
2. **Autopilot path (`discover_from_all_configured_sources` — the separate
   autonomous-discovery service):** sources in order **google_jobs → lever →
   jobspy → local pools**, each isolated with sanitized statuses
   (`live_fetch_ok / safe_empty / not_configured / error_sanitized /
   skipped_policy` — exception class name only, never the message); LinkedIn
   URL-filtered out of candidates (counted); then global dedupe + cap; default
   limits `max_per_query=6`, `max_candidates=80`. Greenhouse/Workday boards
   report `not_configured` until their env lists are set; `ashby` has no
   adapter.

**Lever adapter (honest status: safe-empty scaffold).** Correct code, but
`KNOWN_LEVER_COMPANIES` is empty (example slugs commented out, never verified)
→ returns `[]` by design. Mechanics if activated: GET
`https://api.lever.co/v0/postings/{slug}?mode=json` (httpx, 10s), location
substring filter, keyword match on title+team, job URL
`https://jobs.lever.co/{slug}/{id}`, `createdAt` is unix-ms.

Search-service query builder (ATS-targeted, 6 role terms × 6 ATS sites = 36
queries) — an alternate discovery strategy:

```python
role_terms = ['"IT Operations" Saudi Arabia', '"Service Delivery" Saudi Arabia',
    '"Technical Business Analyst" Saudi Arabia', '"Business Analyst" Saudi Arabia',
    '"PMO" Saudi Arabia', '"IT Governance" Saudi Arabia']
ats_sites = ["site:workdayjobs.com","site:myworkdayjobs.com","site:greenhouse.io",
    "site:lever.co","site:smartrecruiters.com","site:taleo.net"]
# queries = [f"{term} {site}" for term in role_terms for site in ats_sites]
```

### 5.3 JobSpy settings

```python
sites = ["indeed"]              # Glassdoor N/A for Saudi; ZipRecruiter 403 geoblocked
results_wanted = max_results
hours_old = 336                 # 14 days
country_indeed = "Saudi Arabia"
# LinkedIn is EXCLUDED per policy. Optional dependency: pip install python-jobspy;
# if missing, adapter returns [] without breaking the app.
```

**Query expansion (recall recovery)** — appended with `" OR "`:

```python
_QUERY_EXPANSION = {
    "project manager": ["Program Manager","PMO","Delivery Manager"],
    "it manager": ["Technology Manager","IT Director","Infrastructure Manager"],
    "business analyst": ["Business Analysis"],
    "technical business analyst": ["Systems Analyst","IT Business Analyst"],
}
```

**Post-filter role relevance** (`_ROLE_FILTERS`): each family has `require`
regexes (title must match one) and `reject` regexes. Two-tier: any require hit →
keep; any reject hit → skip; a rule existed but neither matched → skip
(conservative); no rule for any keyword → keep (permissive). Reject list for
analyst families notably excludes data/financial/credit/risk/fraud/hr/marketing/
supply-chain/investment/research/compensation/payroll analysts.

### 5.4 Google Jobs canonical apply routing (HR-3, `ae502e4`)

Google Jobs results often land on an aggregator page while `apply_options`
contains the direct employer/ATS link. The adapter picks the best destination:

```python
# Score: ats > employer > unknown > aggregator
_TYPE_PRIORITY = {"ats": 4, "employer": 3, "unknown": 1, "aggregator": 0}

# Employer career-page domain signals (prefix match on domain):
_EMPLOYER_DOMAIN_SIGNALS = ("careers.", "career.", "jobs.", "recruiting.",
                            "hire.", "apply.", "talent.", "joinus.")

# Aggregator domain blocklist (route_type = aggregator) includes:
# glassdoor, linkedin, bayt, naukrigulf, gulftalent, monster, ziprecruiter,
# careerbuilder, simplyhired, reed.co.uk, totaljobs, expertini, talent.com,
# laimoon, drjobpro, wuzzuf, tanqeeb, akhtaboot, rozee.pk, jobrapido, neuvoo,
# jora, adzuna, whatjobs, snagajob (each with its www. variant).
```

**`_route_apply_url(item, selected_url)`** — never returns None, never blocks a
row:
1. Classify the already-selected URL (`route_type` + confidence `high` only for
   ats/employer).
2. Scan every entry in `apply_options`/`apply_links` (keys `link`, `apply_link`,
   `url`; must start with `http`) and keep the highest-priority link.
3. Result embedded as `raw_metadata["apply_routing"]`:
   `{original_apply_url, canonical_apply_url, route_type, route_confidence,
   canonical_domain, phase}`.

**`_promote_canonical_apply_url(current_url, canonical)`** — *structural*
promotion of the canonical URL to the job's effective top-level URL. Fails
closed (keeps `current_url`) for any non-str, empty, malformed,
non-http(s)-scheme, or hostless value, and never raises. A usable current URL is
never replaced by an empty value. No blocked-domain list here — the routing
layer already guarantees the canonical is an equal-or-better route.

**Why this matters:** without promotion, the digest's identity/dedupe/CV-binding
all key on the aggregator URL instead of the real apply URL — a job's identity
then changes when the aggregator page rotates.

---

## 6. Enrichment & SSRF

JD enrichment is **HTTP GET only** — no browser, no JS, no forms, no credentials,
no email auth. `timeout = 10s`. Max body `_JD_MAX_BYTES = 200_000` (a
`200_000+1` read detects oversize). Snippet returned is `text[:10_000]`.

Status vocabulary:

```python
JD_ENRICH_NOT_REQUESTED="NOT_REQUESTED"; JD_ENRICH_SUCCESS="SUCCESS"
JD_ENRICH_FAILED_TIMEOUT="FAILED_TIMEOUT"; JD_ENRICH_FAILED_HTTP="FAILED_HTTP"
JD_ENRICH_FAILED_UNSAFE="FAILED_UNSAFE_SOURCE"; JD_ENRICH_FAILED_TOO_LARGE="FAILED_TOO_LARGE"
JD_ENRICH_FAILED_UNKNOWN="FAILED_UNKNOWN"
```

**Blocked JD domains** (checked on BOTH the initial URL and the final redirect
target — one policy governs the whole fetch):

```python
_JD_UNSAFE_DOMAINS = frozenset({
    "jooble.org","bebee.com","jobleads.com","theirstack.com",
    "linkedin.com","nationalpostdoc.org","founditgulf.com",
})
```

**SSRF guard (MR-2) — fail-closed.** Rejects internal/private/loopback/link-local/
metadata destinations *before* the request and again on the redirect target:

```python
def _ip_is_internal(ip):
    mapped = getattr(ip, "ipv4_mapped", None)   # unwrap ::ffff:127.0.0.1
    if mapped is not None: ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)

def _host_resolves_public_only(hostname):
    if not hostname: return False
    try:                                        # direct IP literal
        return not _ip_is_internal(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    try:                                        # named host → resolve ALL addrs
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return False                            # resolution failure → fail closed
    if not infos: return False
    for info in infos:
        addr = info[4][0] if info[4] else ""
        try:
            if _ip_is_internal(ipaddress.ip_address(addr)): return False
        except ValueError:
            return False                        # unparseable → fail closed
    return True                                 # only if EVERY addr is public
```

`169.254.0.0/16` (link-local, includes the cloud metadata `169.254.169.254`) is
covered by `is_link_local`. IPv4-mapped IPv6 is unwrapped and re-checked so
`::ffff:127.0.0.1` cannot slip through. Integer/octal/hex host tricks are caught
because `getaddrinfo` resolves them and every resolved address is validated.

**Fetch flow (`_safe_fetch_jd`):**
1. Empty URL → `FAILED_UNKNOWN`.
2. `_is_unsafe_jd_domain(url)` → `FAILED_UNSAFE`.
3. `_is_public_jd_url(url)` false → `FAILED_UNSAFE` (SSRF pre-check).
4. GET with browser-like headers, `timeout=10`. `urlopen` auto-follows 30x.
5. On the **final** URL (`resp.geturl()`): must be valid http(s) with hostname,
   not an unsafe domain, and pass the SSRF check again → else `FAILED_UNSAFE`.
6. Status not in `(200, 203)` → `FAILED_HTTP`.
7. `Content-Type` must contain `text` or `json` → else `FAILED_UNSAFE`.
8. Read `_JD_MAX_BYTES + 1`; if oversize → `FAILED_TOO_LARGE`.
9. Strip tags (`<[^>]{0,300}>` → space), collapse whitespace, return
   `text[:10_000]`, `SUCCESS`.
10. `TimeoutError` → `FAILED_TIMEOUT`; any other exception → `FAILED_UNKNOWN`.

Request headers used:

```python
{"User-Agent": "Mozilla/5.0 (compatible; CareerDigest/1.0)",
 "Accept": "text/html,text/plain;q=0.9,*/*;q=0.8",
 "Accept-Language": "en-US,en;q=0.9"}
```

---

## 7. Binding & Publishing

### 7.1 `validate_cv_binding` — the single enforcement point

Answers exactly one question: *is this CV demonstrably the one generated for this
exact job?* Returns
`{"state": "ready_for_apply_upload" | "blocked", "blockers": [...], "details": {...}}`.
**Never raises.** States: `STATE_READY = "ready_for_apply_upload"`,
`STATE_BLOCKED = "blocked"`.

**Checks, in order (any failure appends a blocker):**

1. **Required paths** — `cv_path` and `cv_metadata_path` must be present
   (`no_tailored_cv_path` / `no_tailored_cv_metadata_path`); if absent, return
   immediately.
2. **File presence** — `cv_file_missing:<path>` / `metadata_file_missing:<path>`.
3. **Location guards** — CV must be under `data/tailored_cvs/`
   (`cv_not_in_tailored_dir`) and must NOT be under `data/cv_assets/`
   (`cv_is_source_asset_pdf`). Both dirs are `.resolve()`d and checked via
   `relative_to`.
4. **Scratch filename** — reject version suffixes (`_v2.._v9`, `_v10+`) or scratch
   markers `(_test,_smoke,_smoketest,_draft,_temp,_tmp,_fulltext,_maxfit,_stress)`
   → `cv_filename_is_scratch:<name>`.
5. **Metadata parse** — must be JSON dict (`metadata_unparseable` /
   `metadata_not_a_dict`).
6. **Identity equality — two regimes selected by `identity_version`:**
   - **URL-v1 (`joburl-v1`)**: independently recompute
     `derive_canonical_job_identity(expected_job_url)`; require it to equal the
     metadata's `canonical_job_identity` (`url_identity_underivable`,
     `metadata_canonical_identity_invalid`, `canonical_identity_mismatch`), and the
     PDF filename **stem** must equal `joburl-<64hex>` (`canonical_stem_mismatch`).
     Company/title are descriptive-only here.
   - **Legacy (`identity_version` is `None`)**: `job_id` (preferred) or
     `anchor_id` must match case-insensitively (`JR342343 == jr342343`), AND
     normalized company must match, AND normalized job title must match
     (`job_id_mismatch` / `company_mismatch` / `job_title_mismatch`).
   - Unknown version → `unknown_identity_version`.
7. **SHA-256 integrity** — the actual file hash must equal `metadata.sha256`
   (`sha256_mismatch`).

`state = STATE_READY` only if `blockers` is empty. A caller-attested identity can
never authorize READY in the URL-v1 regime — the validator recomputes it.

```python
def _norm_str(v):   # case-insensitive equality helper
    return re.sub(r"\s+", " ", str(v)).strip().lower() if v else ""
def _ids_match(a, b):
    return a is not None and b is not None and str(a).strip().lower() == str(b).strip().lower()
```

### 7.2 Atomic pair publication (DD-CV-03)

The PDF and its `.metadata.json` sidecar are built entirely inside a unique temp
subdir of the tailored-CV root (same filesystem), fsynced, sha-bound, then
published with `os.replace`. A previously validator-ready pair is **never touched**
until the new pair is fully built, so a failed regeneration cannot destroy it.

```python
TAILORED_CV_DIR = Path("data/tailored_cvs")

def _publish_tailored_cv_pair(...):
    pub_dir = Path(tempfile.mkdtemp(prefix=".pub-", dir=str(TAILORED_CV_DIR)))
    try:
        temp_pdf = Path(pdf_service.render_to_pdf(tailored_cv,
                        output_filename=filename, output_dir=pub_dir))
        _fsync_path(temp_pdf)               # durable before hashing
        os.chmod(temp_pdf, 0o600)           # private permissions
        final_pdf  = TAILORED_CV_DIR / temp_pdf.name
        final_meta = final_pdf.with_suffix(".metadata.json")
        _assert_safe_publish_target(final_pdf)   # inside root, not a symlink
        _assert_safe_publish_target(final_meta)
        temp_meta = pub_dir / final_meta.name
        _write_metadata_sidecar(out_path=temp_meta, pdf_for_hash=temp_pdf,
                                final_pdf_path=final_pdf, ...)  # sha bound to completed bytes
        os.chmod(temp_meta, 0o600)
        os.replace(str(temp_pdf), str(final_pdf))     # publish PDF
        os.replace(str(temp_meta), str(final_meta))   # then sidecar
        _fsync_dir(TAILORED_CV_DIR)
        return {"cv_pdf_path": str(final_pdf), "cv_metadata_path": str(final_meta)}
    finally:
        shutil.rmtree(pub_dir, ignore_errors=True)
```

**Two `os.replace` calls are not jointly atomic.** The residual window (PDF
published, metadata not yet) is made *safe* — not eliminated — by sha binding: a
stale/absent sidecar won't match the new PDF, so `validate_cv_binding` returns
`blocked` (never a false ready). Safety helpers:

```python
def _assert_safe_publish_target(final_path):
    root = TAILORED_CV_DIR.resolve(); parent = final_path.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError(...)               # refuse outside root
    if final_path.is_symlink():
        raise ValueError(...)               # refuse onto a symlink
```

### 7.3 Metadata sidecar schema (field by field)

```python
sidecar = {
    "job_title":               job_title,
    "company":                 company,
    "job_id":                  job_id,          # portal id (legacy/provenance)
    "anchor_id":               anchor_id,
    "job_url":                 job_url,
    "portal":                  portal,          # workday/greenhouse/lever/...
    "location":                location,
    "generated_at":            <UTC ISO-8601>,
    "source_master_cv_path":   "data/cv/master_cv.json",
    "source_master_cv_sha256": <sha256 or None>,
    "source_pdf_references":   [],              # app pipeline uses no visual PDFs
    "tailoring_summary":       {...},           # lengths/counts/modifications
    "job_analysis":            {...},           # role_family/seniority/score/etc
    "sha256":                  <sha256 of the COMPLETED pdf bytes>,
    "size_bytes":              <pdf size>,
    "tailored_cv_path":        <final canonical pdf path>,
    "policy_version":          "1.0",
    # URL-v1 only:
    "identity_version":        "joburl-v1",
    "canonical_job_identity":  "joburl:v1:<64hex>",
}
# Written UTF-8, indent=2, ensure_ascii=False, then fsynced.
```

### 7.4 Canonical filenames

- **URL-v1:** `joburl-<64hex>.pdf` (hex-only stem).
- **Legacy:** `{slug(company)}_{slug(job_title)}_{id}.pdf` — `id` = `job_id` or
  `anchor_id`, original case preserved (portal refs like `JR342343` stay
  searchable); slug = lowercase alnum joined by single underscores.

### 7.5 On-demand generation resolver (fail-closed gating)

`resolve_tailored_cv(...)` decides whether a job with no existing validated CV may
get one generated. Order:

1. Existing validator-approved CV (injected lookup) → reuse, no generation
   (`existing_valid_cv`).
2. `generation_enabled` False → `generation_disabled` (no generator call).
3. `dry_run` True → `dry_run_generation_skipped` (nothing written).
4. Identity required: a pre-attached canonical identity that disagrees with the
   recompute → `missing_job_identity`; no `job_id`/`anchor`/URL-v1 identity →
   `missing_job_identity`.
5. `require_private=True` profile load; a `PrivateProfileMissingError` →
   `generation_disabled`; any `[PRIVATE_PROFILE]`/`[REDACTED]` marker still in the
   profile → `validation_blocked` (**a placeholder CV is never exposed**).
6. Generate via a single injected boundary with `asyncio.wait_for(timeout=90s)`.
7. `validate_cv_binding` is the **sole** final acceptance authority — a path is
   exposed ONLY when it returns `ready_for_apply_upload` (`generated_valid_cv`);
   anything else → `validation_blocked`.

Statuses: `existing_valid_cv, no_tailored_cv, generation_disabled,
dry_run_generation_skipped, generated_valid_cv, generation_failed,
validation_blocked, missing_job_identity`. Default timeout `_DEFAULT_TIMEOUT_S = 90.0`.
The resolver never delivers to Telegram, never writes the ledger, never uploads/
submits, and never enables generation on its own.

**Digest-side budget loop (`_generate_missing_tailored_cvs`):** applies the
resolver only to jobs with `cv_attachment_status == "no_tailored_cv"` — jobs
already holding a bound CV pass through and **never consume budget**. Budget =
`MAX_GENERATIONS_PER_RUN` (3), or 1 in any pinned pilot mode. Once spent,
remaining eligible jobs are annotated `generation_budget_exhausted` (no path)
and processing continues. A generated path is bound ONLY when the resolver
returns `generated_valid_cv` AND the path still passes the safe attachment
resolver (defensive re-check — the file could vanish between publish and use;
the same re-check runs in `_enrich_job_cv_fields` at selection time). Per-job
`cv_generation_status` recorded; returns a new list, never mutates inputs.
Pilot selectors: `--generation-job-url` matches by **exact** normalized-URL
equality only (no substring/title fallback; 0 or multiple matches → exit 2);
`--generation-top-one` pre-sorts by `_norm_url_key` for deterministic ties,
then takes the first ranked job lacking a CV.

### 7.6 Existing-CV selection — two-stage, hints never accept

`_select_tailored_cv(job)` returns the safe PDF path of the CV made FOR this
exact job, or `None`. **Discovery hints NEVER accept a CV on their own** — the
validator is the single final authority:

1. **Candidate discovery (hints only), stable order:** index every
   `*.metadata.json` in the tailored dir (skipping any whose PDF fails the safe
   attachment resolver), then match candidates by:
   a) normalized job URL (`_norm_url_key(apply_link or url)`), then
   b) anchor/source id (`source_job_id or anchor_id or posting_id`, normalized),
   then c) normalized `company+title`. De-duplicated, order preserved.
2. **Final authority:** each candidate is submitted to `validate_cv_binding`;
   a CV is returned only when the validator explicitly reports
   `ready_for_apply_upload` AND the path still passes the safe attachment
   resolver. A blocked verdict, an exception, or a malformed return fails
   closed (no CV). Candidates are tried in the stable discovery order.

### 7.7 Quarantine (repairing a bad published pair)

To fix a bad live pair, **all-or-safe move** the old PDF+metadata into
`data/tailored_cvs_quarantine/<track>/<UTC-timestamp>-<shortsha>/` (byte-identical),
then re-render ONCE via a single-job selector against the exact URL. Never delete
evidence. The quarantine dir must be in `.gitignore` (it holds evidence, not code).

---

## 8. The Ledger

Two distinct ledgers exist; keep them separate in a rebuild.

### 8.1 Delivered-jobs suppression ledger (digest cross-day dedupe)

Prevents re-sending the same active posting every day. A delivered job is
suppressed for a bounded TTL, then may resurface.

```python
DELIVERED_LEDGER_TTL_DAYS = 30
_DELIVERED_LEDGER_VERSION = 1
# path anchored via __file__ parents[2] so it's identical under manual run or systemd:
#   data/daily_manual_apply_digest/delivered_ledger.json
```

**Same-run dedupe (`_dedupe_gate_jobs`)** — runs on gate-passed jobs before the
cap: first by normalized URL (`_strip_tracking_params(link).rstrip("/").lower()`,
`apply_link` then `url`), then by the `(company.lower().strip(),
title.lower().strip())` pair. Returns `(deduped, n_removed)`, order preserved.
`_norm_url_key` is a thin wrapper over the shared `normalize_job_url_v1` (§3) —
one identity everywhere.

**DB-pipeline dedupe (search service, `DedupeKey`)** — separate, coarser triple:
duplicate if `(normalized_company AND normalized_title)` match, OR
`normalized_url` matches, OR `text_fingerprint(company,title,url,description)`
matches.

**Suppression key = the SAME URL identity as same-run dedupe** (deliberate reuse
so they can never diverge): `apply_link` then `url`; tracking params stripped;
trailing slash + case normalized. `normalize_url`/`_hash_url` are intentionally
NOT reused (they diverge on case/trailing-slash and `_hash_url` drops the whole
query string, which false-merges distinct URLs).

```python
def _delivered_key(job):
    raw_link = job.get("apply_link") or job.get("url") or ""
    if not raw_link: return None
    norm_url = _strip_tracking_params(raw_link).rstrip("/").lower()
    return norm_url or None
```

Structure: `{"version":1, "ttl_days":30, "entries": {<key>: {"delivered_at":ISO,
"expires_at":ISO}}}`. Read is **fail-open** (missing/unreadable/malformed →
empty ledger; unknown jobs never treated as delivered). Filtering runs BEFORE the
send cap so a suppressed job never consumes a limited slot. Recording happens
ONLY after confirmed successful delivery; expired entries are pruned on write;
write is atomic (temp + fsync + `os.replace`, `sort_keys=True`). A write failure
returns `False` (logged, never raised into the send flow). Malformed/unparseable
timestamps are ignored (never suppress).

### 8.2 Submit-attempt ledger (append-only JSONL duplicate guard)

Single source of truth for "have we already attempted / maybe-submitted /
confirmed this job?" — across drivers and restarts. File:
`data/submit_attempts/ledger.jsonl`.

**Redaction allowlist (only these keys may land in a record):**

```python
_ALLOWED_RECORD_KEYS = (
    "record_id","event_type","created_at","application_id","job_id","posting_id",
    "canonical_job_key","company","title","portal","driver","state",
    "submit_click_count_delta","canonical_url_hash","evidence_path","source",
    "redacted_summary",
)
```

**Atomic append** — `_sanitize_record` strips everything not on the allowlist,
stamps `record_id`/`created_at`, then writes one whole line via
`os.open(..., O_WRONLY|O_CREAT|O_APPEND, 0o644)` so concurrent writers can't
interleave. Read path is corruption-tolerant (counts and skips bad lines, never
raises).

**Canonical job key** (stable 16-hex, identical across processes/restarts/drivers):

```python
def build_canonical_job_key(company, title, portal, *, posting_id=None, url=None):
    parts = [_slug(portal), _slug(company), _slug(title),
             _slug(posting_id) if posting_id else canonical_url_hash(url)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

def canonical_url_hash(url):   # strip query+fragment, lowercase host, then hash[:16]
    ...
```

**Event types → state ranking** (highest wins; CONFIRMED is irreversibly
strongest): `none(0) < discovered(1) < handoff(2) < failed(3) < in_flight(4) <
maybe(5) < confirmed(6)`.

**Guard verdicts (`guard_before_submit_click`):** LinkedIn → `block_linkedin`
unconditionally (defense in depth); missing company/title/portal →
`block_insufficient_context`; state CONFIRMED → `block_already_confirmed`; MAYBE
(no override) → `block_maybe_no_resubmit`; IN_FLIGHT (no override) →
`block_submit_in_flight`, **unless** the in-flight record is older than
`STALE_IN_FLIGHT_WINDOW_SECONDS = 3600` (1h) → falls through to allow. Every BLOCK
records a `submit_click_blocked_duplicate` event (except LinkedIn).

---

## 9. Telegram Integration

### 9.1 Message / card structure

Cards are **pure, side-effect-free** formatters returning a Markdown/HTML string
or a `telegram.InlineKeyboardMarkup`. Two paradigms coexist:

- **Cockpit cards** (`ui/cards.py`) use `parse_mode="Markdown"`. Because legacy
  Markdown has no escape sequence, all user-supplied strings are routed through
  `_md_neutralize`, which swaps the four reserved chars for visually-identical
  Unicode lookalikes:

```python
_MD_NEUTRALIZE = str.maketrans({
    "*": "∗",   # U+2217   _: "ˍ" U+02CD   `: "ʼ" U+02BC   [: "❲"   ]: "❳"
})
```

- **Digest cards** use `parse_mode="HTML"` with `_html_e = html.escape(..., quote=False)`
  and URL buttons; tracking params stripped before the button URL is built.

Score visuals: `>=75 🟢`, `>=60 🟡`, else `🔴`; `⚪` for None. Score accepts 0–1
or 0–100 (`s <= 1.0 → *100`). Section separator: `━`×26. Recommendation localizes
to Arabic: `APPLY→✅ يُنصح بالتقديم`, `MAYBE→🟡 قابل للنظر`, `SKIP→🔴 لا يُنصح`.

**Callback prefixes** (single source of truth): `apply / force / dismiss / view /
retry / newtry / applyagain / follow / cancelapp / details / showlog / submit`,
all `"<prefix>:<id>"`.

Gate/tier display strings: `TIER_1 🏛 Top-tier · TIER_2 🏢 Strong · TIER_3 🏗
Acceptable · TIER_4 ⚠ Weak/unknown`. Salary: `✅ confirmed/likely high · 🟡
possible high · ❓ unclear · 🔴 likely low / junior`.

**Standing safety statement shown in the digest** (verbatim):

> The system only recommends jobs and sends CVs to Telegram for manual review. It
> does not submit, fill, upload CVs to job portals, create accounts, solve CAPTCHA,
> use Gmail, or automate LinkedIn.

**Digest job card — exact structure (`_build_job_card_html`):**

```
<b>#{rank} {title}</b>
🏢 {company}
📍 {location}                      ← "Riyadh, Saudi Arabia" default when location
                                      is empty or already contains "riyadh"
🎯 Match: {role_match_score}/100
{salary label}                     ← ✅/🟡/❓/🔴 human salary-gate label
{tier label}                       ← 🏛/🏢/🏗/⚠ tier label
💬 {why}                           ← Claude review match_reason, shown ONLY if
                                      len > 20 and no comma in first 30 chars
                                      (suppresses raw keyword dumps)
⚠️ Risk: {concern_or_risk}         ← only if present
📄 CV: {pdf filename}              ← only when attach_cv and validated path;
                                      else "Not attached — awaiting CV QA"
🔗 Apply link unavailable          ← only when no safe URL for a button
```

Buttons (`_build_job_card_buttons`): one row of **URL-only** inline buttons —
`[Apply]` (tracking-stripped link) + `[Company]` (a company web-search URL);
no `callback_data`, plain labels (Telegram adds its own external-link arrow).

Send specifics (`_send_telegram_html_async`): checks the rotation gate first
(fail-closed), `parse_mode="HTML"`, `text[:4096]`, link previews disabled via
PTB 21 `LinkPreviewOptions(is_disabled=True)`; all failures logged through
`safe_exception_summary` (never raw `{exc}`). Digest footer line (verbatim):
`⚠️ Manual apply only — no auto-submit`; empty digest: `✅ No jobs met quality
threshold today.`

CV document send (`_send_telegram_document_async`): rotation gate first; ONE
file per call bound to a single job; `bot.send_document(chat_id, document=Path,
filename=basename, caption, read_timeout=60, write_timeout=60)`; returns bool,
never raises. The **caller** must pre-filter to a safe PDF/DOCX via the safe
attachment resolver (§13) — this function adds no extra guard by design (one
authority, not two).

Delivery flow (`_send_intelligent_gate_cards` — verified against source):
1. **Same-run dedupe happens INSIDE the send function** (`_dedupe_gate_jobs`),
   and the header shows `♻️ Duplicates removed: N`.
2. Header lines (verbatim shape): `🎯 <b>Daily Apply — Intelligent Gate</b>` /
   `📅 {Riyadh-local time, "%d %b %Y, %H:%M (Riyadh)"}` /
   `🔍 <b>N</b> jobs passed · M discovered` /
   `📊 Salary threshold: likely 24K+ SAR/month` / dupes line /
   `⚠️ Manual apply only — no auto-submit`.
3. Per job (rank order): **card (+URL buttons)** → its **CV document** with
   caption `#{rank} CV — {title[:80]} @ {company[:60]}`, immediately after its
   own card so the binding is visually unambiguous.
4. **"CV required" semantics under `--attach-cv`:** card sent but NO resolved
   CV → the job is INCOMPLETE — counted as failed, NOT delivered (a missing CV
   is never success). Card fails → CV never attempted. One job's failure never
   aborts the others.
5. Returns `(header_ok, delivered_jobs, n_failed)`; only `delivered_jobs` are
   recorded to the suppression ledger — a failed job may reappear next run
   while delivered siblings never repeat. Recording is NOT gated on
   `n_failed == 0`.

Non-gate text path labels: `deterministic` (plain digest summary) /
`claude-reviewed` (review summary) / `fallback+warning` (review requested but
failed — summary + disconnect warning appended).

### 9.2 Secret redaction (MR-3) — `TelegramSecretRedactionFilter`, full logic

**Why:** the Bot API embeds the token in the request URL (`/bot<TOKEN>/method`),
and httpx puts `request.url` into log records — so tokens leak into logs unless
scrubbed. This module is stdlib-only, deterministic, side-effect free. It cannot
remediate *historical* logs — the token must be **rotated**.

```python
REDACTED = "[REDACTED]"; CHAT_ID_REDACTED = "[CHAT_ID_REDACTED]"

# token-bearing Bot API URL segment; anchored to bot + following '/' or word-end
_BOT_URL_TOKEN_RE = re.compile(r"(?i)(/bot)[A-Za-z0-9_:%\-]+(?=/|\b)")
# key=value / key: value secret pairs
_KV_SECRET_RE = re.compile(
    r"(?i)\b(telegram_bot_token|bot_token|token|authorization|api_key|apikey|secret)"
    r"(\s*[=:]\s*)([^\s,'\"]+)")
_SECRET_KEY_SUBSTRINGS = ("token","authorization","api_key","apikey","secret",
                          "password","cookie")
_MAX_DEPTH = 6

def sanitize_secret_text(value):
    text = value if isinstance(value, str) else str(value)
    text = _BOT_URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)   # keep endpoint
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    tok = _configured_token()                       # exact bare token anywhere
    if tok and tok in text:
        text = text.replace(tok, REDACTED)
    return text

def sanitize_secret_value(value, _depth=0, _seen=None):
    # recurse dict/list/tuple/set preserving shape; depth+cycle guarded;
    # dict keys matching _SECRET_KEY_SUBSTRINGS → value replaced wholesale;
    # numbers/bools/None returned unchanged; other objects → sanitized str.
    ...

class TelegramSecretRedactionFilter(logging.Filter):
    def filter(self, record):
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize_secret_text(record.msg)
            if record.args:
                record.args = sanitize_secret_value(record.args)   # httpx passes url as %s arg
            if record.exc_info:                                     # pre-render + scrub traceback
                formatted = logging.Formatter().formatException(record.exc_info)
                record.exc_text = sanitize_secret_text(formatted)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = sanitize_secret_text(record.exc_text)
            if getattr(record, "stack_info", None):
                record.stack_info = sanitize_secret_text(record.stack_info)
        except Exception:
            pass          # a filter must NEVER drop/raise on a record
        return True
```

Attach the filter to **handlers** (not just loggers) so it also scrubs records
propagating up from httpx/httpcore. Also use `safe_exception_summary(exc)` instead
of logging `{exc}` directly (httpx exceptions embed the token-bearing URL in
`str(exc)`), and pin `telegram`/`httpx`/`httpcore` loggers to `WARNING` so PTB
DEBUG/INFO chat+message payloads never emit. `mask_chat_identifier` always returns
`[CHAT_ID_REDACTED]` — the chat id is never rendered.

**Rotation gate (fail-closed, single authority every network path consults):**

```python
TELEGRAM_ROTATION_REQUIRED_REASON = "telegram_token_rotation_required"
def telegram_rotation_gate_enabled(settings=None) -> bool:
    try:
        if settings is None:
            from app.core.config import settings as settings
        return bool(getattr(settings, "TELEGRAM_TOKEN_ROTATED_AFTER_MR3", False))
    except Exception:
        return False        # absent flag / error → False (closed)
```

Live Telegram (send/poll/commands) stays disabled until the operator rotates the
leaked token and sets `TELEGRAM_TOKEN_ROTATED_AFTER_MR3=true`. No secret material
is stored in code/config — only this boolean gate.

---

## 10. Model Calls (Prompts)

**LLM router.** Two task types; provider order OpenAI → Claude → rule-based
fallback. `ROUTINE` → `OPENAI_MODEL_ROUTINE` (`gpt-4o-mini`); `NUANCED` →
`OPENAI_MODEL_STRONG` (`gpt-4o`). Claude fallback models come from
`ANTHROPIC_MODEL_ROUTINE` / `ANTHROPIC_MODEL_STRONG`. `max_retries=3`.

**Client behavior:** single user-role message on both providers; exponential
backoff `2**attempt` → 1s, 2s, 4s; a **budget pre-flight guard** runs before
every call (blocks with an explicit reason at the spend cap; a guard *failure*
never blocks); token usage tracked per call — tracking failure never breaks
generation. Sampling settings differ by provider:

| | OpenAI (primary) | Claude (fallback) |
|---|---|---|
| temperature | **not set** (provider default) | **0.7** default |
| max_tokens | not set | **1024** default |
| transport | official SDK | stdlib `urllib`, 30s timeout |
| API | chat.completions | `/v1/messages`, `anthropic-version: 2023-06-01` |
| response parse | `choices[0].message.content` | first text block, else all text blocks joined |

Output determinism therefore comes from the validators (§10.2 guard, §10.7
grounding, §1.6 gate), never from sampling settings. **Caution for a rebuild:**
Claude's 1024 max_tokens ceiling can truncate long outputs the OpenAI path
would complete — raise it if the fallback path generates full letters/CVs.

> **Rebuild note:** when rebuilding on Anthropic, default to the latest capable
> models. Model IDs at time of writing: Opus `claude-opus-4-8`, Sonnet
> `claude-sonnet-4-6`, Haiku `claude-haiku-4-5-20251001`, Fable `claude-fable-5`.
> The config below pins older ids — treat them as placeholders to update.

All prompts demand strict JSON and parse defensively (direct `json.loads`, then
strip ``` fences, then regex-extract the first `{...}` object).

### 10.1 Job analysis prompt (verbatim)

```
You are a precise job analysis engine.

Analyze the following job opening and return ONLY valid JSON.
Do not add markdown fences, explanations, or extra text.

Job title: {job_title}
Company: {company}

Job description:
{job_description}

Return JSON with exactly these keys:
{
  "role_family": "string",
  "seniority": "string",
  "match_score": 0.0,
  "key_requirements": ["string"],
  "recommendation": "APPLY" or "MAYBE" or "SKIP"
}

Rules:
- match_score must be between 0 and 1
- key_requirements should be concise and important
- recommendation should reflect the overall fit implied by the role
- role_family should be something like Business Analysis, IT Operations, Project Management, Infrastructure, Security, Support, etc.
- seniority should be one of: Entry-level, Mid-level, Senior, Manager, Unknown
```

Output normalization: `match_score` clamped to `[0,1]`; `recommendation` forced
into `{APPLY,MAYBE,SKIP}` (default MAYBE); missing `role_family`/`seniority` →
raise (triggers rule-based stub).

### 10.2 Summary humanization prompt (verbatim — the most safety-critical)

```
You are rewriting a CV summary for a job application.

Return ONLY the rewritten summary text. No markdown. No bullet points.

Rules:
- Keep it truthful. Do NOT invent employers, years, tools, certifications, or achievements.
- Do NOT insert frameworks, methodologies, tools, or certifications from the job description unless they already appear verbatim in the original summary. For example, if the job mentions ITIL, Agile, PRINCE2, Six Sigma, or any other framework but the original summary does not contain it, you MUST NOT add it.
- Use ONLY information already present in the original summary.
- Make it sound natural, professional, and tailored to the role.
- Write in active voice with strong, clear language.
- Keep it concise: 2 to 4 sentences maximum.
- Maximum length: 420 characters.
- You may emphasize existing skills from the original summary that are relevant to the role, but never introduce new skills, tools, or frameworks.
- Avoid generic phrases like "results-driven", "team player", "go-getter".
- Focus on specific expertise and measurable impact already stated in the original summary.

Target job title: {job_title}
Target company: {company}
Key requirements: {key_requirements}

Job description:
{job_description}

Original CV summary:
{summary}
```

**Post-generation truthfulness guard (`_contains_invented_content`)** — the prompt
is *not* trusted alone. The rewrite is rejected (falls back to rule-based) if it:
- introduces new number+unit tokens (`\b\d+[\d,\.]*\s*(?:years?|%|\$|million|
  billion|k|projects?|teams?|clients?|users?)\b`) not in the original;
- matches invention patterns (`\b\d+\+?\s*years?\s+(?:of\s+)?experience\b`,
  `\bover\s+\d+\b`, `\bmore\s+than\s+\d+\b`, `\b\d+%\s+(?:increase|improvement|
  reduction|growth)\b`) absent from the original;
- introduces new certifications (`pmp|cissp|cisa|cism|aws|azure|gcp|ccna|ccnp|
  itil|prince2|scrum master|csm|psm`);
- introduces any framework/tool from `_KNOWN_FRAMEWORKS` not in the original
  (`itil, prince2, cobit, togaf, six sigma, lean, agile, scrum, kanban, safe,
  devops, devsecops, iso 27001, iso 9001, nist, sox, gdpr, hipaa, pmbok,
  waterfall, kaizen, bpmn, archimate, terraform, kubernetes, docker, ansible,
  jenkins, splunk, servicenow, jira, confluence, tableau, power bi, snowflake,
  databricks, airflow, sap, oracle erp, salesforce, dynamics 365`);
- introduces a **proper-noun-ish token** (`\b[A-Z][A-Za-z0-9+#.&/-]{2,}\b`) that
  appears neither in the original summary NOR in the master-CV vocabulary
  whitelist (built from every factual token in the master CV — employers, tools,
  certs, institutions, plus a neutral stop-word allowlist).

Additional validation: reject if `< 100` or `> 1000` chars; trim to 420; if
trimmed `< 100`, reject. Rule-based fallback also applies formal→casual word
swaps (`utilize→use, leverage→use, facilitate→enable, prior to→before, in order
to→to, responsible for→manage`, …).

### 10.3 Experience-ranking prompt (verbatim — ranking only, never rewriting)

```
You are ranking CV experience entries for a specific job.

Return ONLY valid JSON with this exact shape:
{
  "experience_order": [0, 1, 2]
}

Rules:
- Use ONLY the provided experience indexes.
- Include all indexes exactly once.
- Rank the most relevant experience first for the target role.
- Do not invent or rewrite anything.
- Focus on role fit, requirements match, and relevance.

Target job title: {job_title}
Target company: {company}
Key requirements: {key_requirements}

Job description:
{job_description}

Experience entries:
{json.dumps(payload)}
```

The returned order is validated and normalized: non-int indexes rejected; empty →
raise; any missing index is appended in original order (nothing is dropped).
Falls back to rule-based scoring on any failure.

### 10.4 Page-parse prompt (discovery → structured job)

```
You extract job postings into strict JSON.
Return ONLY valid JSON with exactly these keys:
{
  "title": "string",
  "company": "string",
  "location": "string",
  "description": "string",
  "salary_range": "string or null",
  "posted_date": "YYYY-MM-DD or null"
}

Rules:
- Use English.
- Keep description as the core responsibilities/requirements; remove navigation/legal boilerplate.
- If company is not explicitly present, infer carefully from page (otherwise use "Unknown Company").
- posted_date should be null if not confidently extracted.

URL: {url}

Page text:
{page_text[:12000]}
```

### 10.5 Summary-variant selection (no LLM — deterministic, safer)

Before any LLM call, the best pre-written summary variant is picked from
`master_cv.summary.variants` by keyword scoring (title +4, role_family +3, JD +1
per signal). This uses only facts the user pre-authored. Signals map:
`functional_consultant, solution_consultant, implementation_consultant,
technical_ba_lead, digital_transformation`. No variant applies → primary/fallback
summary.

### 10.6 Digest review prompt (`--claude-review`) — verbatim

> In the original source the candidate is referenced by first name; replaced here
> with `<CANDIDATE>` per the redaction contract. Everything else is verbatim.
> Constants: `CLAUDE_REVIEW_MAX_INPUT_JOBS = 30`,
> `CLAUDE_REVIEW_OUTPUT_TOP_N = 20`, `CLAUDE_REVIEW_TIMEOUT_SECONDS = 60`
> (hard wall-clock budget via `ThreadPoolExecutor` + `future.result(timeout)`,
> independent of the LLM client's own retry loop). Task type: `ROUTINE`,
> `max_retries=2`.

Input sanitization (`_sanitize_jobs_for_llm`) — only these fields ever reach the
LLM, with caps: `title[:200], company[:200], location[:200], source[:50], url,
fit_score, fit_label, recommended_cv` (an already-validated
existing-path-or-`"UNKNOWN"`). Never secrets, env, settings, or other paths.

```
You are a job-fit reviewer for <CANDIDATE>, a candidate targeting these role families in Saudi Arabia: IT Manager, IT Operations Manager, IT Service Delivery Manager, Business Analyst, Business Technology Partner, Business Technology Consultant, IT Governance, IT Operations Lead, Service Delivery Lead, Technical Business Analyst, Digital Transformation, Technology Operations.

Candidate positioning: IT Operations, Business Analysis, Service Delivery, Governance, SAMA / regulated environment, PMP, PMI-PBA. Do NOT position the candidate as a pure infrastructure engineer.

Prefer: Riyadh, Saudi Arabia; government / semi-government / banking / regulated / enterprise environments; lead, manager, senior specialist, consultant, partner roles; business + technology bridge roles.

Demote or reject: Helpdesk, Desktop support, L1 support, Junior/intern, Cabling/hardware technician, pure Network engineer, pure System administrator, pure Infrastructure engineer, roles clearly below current level, roles outside Saudi Arabia unless explicitly remote-Saudi.

Below is a JSON array of already-discovered job postings (title, company, location, source, url, a deterministic heuristic fit_score/fit_label, and a recommended_cv path). Review and rerank them for <CANDIDATE>'s fit.

Jobs:
{jobs_json}

Return ONLY a valid JSON array (no markdown fences, no extra text) of the best-fit jobs, ranked best-first, at most {CLAUDE_REVIEW_OUTPUT_TOP_N} items. Each item MUST have exactly these keys:
{
  "apply_link": "<copied exactly from the input url field of the job you mean>",
  "final_rank": <integer, 1 = best>,
  "fit_label": "STRONG" | "GOOD" | "FAIR" | "LOW" | "REJECT",
  "match_reason": "<short string, why this fits the candidate's targets>",
  "concern_or_risk": "<short string, or empty string if none>",
  "recommended_cv": "<copied exactly from that same job's input recommended_cv field>"
}

Rules:
- apply_link MUST be copied verbatim from one of the input "url" values. Never invent a new URL.
- recommended_cv MUST be copied verbatim from that same job's input "recommended_cv" value. Never invent a new path.
- Mark poor fits per the demote/reject list above as fit_label "REJECT".
- Do not output anything except the JSON array.
```

**No-fabrication response validation (`_parse_and_validate_claude_response`) —
the crucial part:** `apply_link` and `recommended_cv` are **NEVER trusted from
the LLM's text**. They are always re-derived from the original sanitized input
by an exact-URL match (`by_url = {j["url"]: j ...}`); any item whose
`apply_link` doesn't match a known input url is **dropped** (the LLM cannot
reference a job it wasn't given). `fit_label` outside the allowed set falls back
to the deterministic label; bad `final_rank` falls back to position;
`match_reason`/`concern_or_risk` capped at 400 chars; `REJECT` items filtered
out; sorted by `final_rank`; capped at `OUTPUT_TOP_N`. Zero valid items →
parse error → deterministic fallback (review layer degrades, digest still runs).
Failure taxonomy: `_LLMUnavailableError / _LLMTimeoutError / _LLMParseError` →
classified `claude_review_status` + sanitized `claude_review_fallback_reason`.

### 10.7 Cover letter prompt (HR-4 grounded) — verbatim

The three-block structure below is the distilled result of the fabrication
incidents — the separation of *candidate facts* from *employer requirements* is
the fix. `cv_payload` = name/headline/tailored summary/top-3 experiences
(title+company+achievements+technologies)/top-12 skills/certifications/education/
languages, JSON-serialized.

```
You are writing a job application cover letter for a candidate.

Return ONLY the cover letter text. No markdown. No bullet points.

You are given three clearly separated blocks:
 (1) VERIFIED CANDIDATE FACTS — the ONLY facts you may state about the candidate.
 (2) JOB CONTEXT — what the employer is asking for. These are the EMPLOYER'S
     requirements, NOT facts about the candidate.
 (3) TARGET — the role and company to address.

Strict grounding rules:
- State a candidate fact ONLY if it appears in VERIFIED CANDIDATE FACTS.
- NEVER infer, assume, or upgrade a candidate fact from JOB CONTEXT. If the job asks
  for a certification, degree, skill, tool, or a number of years the candidate does
  not have in VERIFIED CANDIDATE FACTS, do NOT claim the candidate has it.
- NEVER invent or estimate numbers, percentages, money amounts, team sizes, project
  counts, years of experience, employers, job titles, certifications, degrees,
  languages, or performance metrics.
- Omit any claim you cannot support directly from VERIFIED CANDIDATE FACTS.
- Do not exaggerate scope (e.g. "led", "owned", "managed a team of N", "delivered N
  projects") beyond what the facts state.
- You MAY describe the role's needs and express genuine, generic motivation
  (interest in the role, enthusiasm to contribute, willingness to discuss relevant
  experience), provided it does not assert unverified experience or qualifications.
- Tone: professional, warm, confident, human.
- Length: around 180 to 260 words. Structure: 4 short paragraphs maximum.
- Mention the target company and role naturally.

(1) VERIFIED CANDIDATE FACTS:
{json.dumps(cv_payload, ensure_ascii=False)}

(2) JOB CONTEXT (employer requirements — NOT candidate facts):
Job analysis: {json.dumps(job_analysis, ensure_ascii=False)}
Job description: {job_description}

(3) TARGET:
Role: {tailored_cv.job_title}
Company: {tailored_cv.company}
```

Validation: word count must be 150–350 (target 180–260). Task type `NUANCED`,
`max_retries=3`. Then the **deterministic post-generation grounding check** runs
before any successful return — an unsupported claim raises and the service falls
back to a deterministic template letter (4 fixed paragraphs built only from the
tailored CV).

**Grounding validator (`_find_unsupported_claims`) — 7 claim categories, each
checked ONLY when asserted as the candidate's own possession (describing the
role's requirements is allowed):**

1. **Certifications** (`_CERT_TOKENS`: pmp, capm, pmi-acp, prince2, cissp, cisa,
   cism, ceh, comptia, security+, network+, ccna, ccnp, ccie, itil, togaf, csm,
   psm, cfa, cpa, mcse, mcsa, rhce, cka, ckad, aws/azure/gcp certified, aws,
   azure, gcp) — first-person claim without the cert in truth → reject.
2. **Named tools/skills** (`_TECH_TOKENS`: kubernetes, terraform, ansible,
   docker, openshift, kafka, spark, hadoop, tensorflow, pytorch, splunk,
   servicenow, salesforce, tableau, power bi, sap, react, angular, django,
   aws, azure, gcp).
3. **Inflated titles** (`_TITLE_TOKENS`: cto/ceo/cio/cfo/coo (+spelled-out),
   vice president, managing director, it director, head of, director of) —
   with careful disambiguation: application framing ("applying for the CTO
   role") is allowed; employment framing ("as CTO", "held the position of") is
   a claim.
4. **Degrees** (`_DEGREE_TOKENS`: phd, mba, master's/of, bachelor's/of, bsc,
   msc, b.tech, m.tech, …) — requires matching education in truth.
5. **Employers** — 13+ first-person employment regex patterns
   ("worked at/for/with X", "my tenure at X", "I joined X", "At X, I led…"
   restricted to work verbs so "At <Target>, I am applying…" is NOT flagged).
   Any company not in candidate truth is rejected — **including the target
   company** (job context is never employment evidence).
6. **Category-bound numbers** (`_NUMERIC_CATEGORIES`): years / percent / money
   / team / projects / users / clients / systems / stakeholders / locations,
   each with its own regexes. A claimed number must be supported by candidate
   truth **in the same category** — "6 years" never authorizes "team of 6",
   "6 projects", "6%", or "SAR 6 million". English number-words are normalized
   to digits first (0–999 incl. hyphenated tens and "hundred" phrases;
   thousand/million/billion left as words for the money patterns). Vague
   magnitudes ("dozens of projects", "multi-million") → always unsupported.
   An unresolved magnitude word governing a count noun fails closed.
7. **Ownership-scope inflation** (`_OWNERSHIP_SCOPE_PATTERNS`): "I led/owned/
   ran/managed… enterprise/entire/company-wide/department/sole/record/
   end-to-end…", "I was solely responsible", "I achieved record…" → reject.
   Weaker truthful wording ("I contributed to…") is untouched.

Fail-safe: any validator-internal error → `["validator_error:internal"]`
(rejection, never a silent pass). Logging: claim **categories only** — never the
letter text, PII, or the fabricated values.

> Note: a second module, `cover_letter_generator.py`, is a superseded Phase-2
> static-template stub (hardcoded 4-line letter). Do **not** port it —
> `CoverLetterService` above is the sole authority.

---

## 11. Known Bugs — "Do Not Repeat"

Distilled from commits, code comments, and project history. Each line is a trap a
new build will fall into unless carried over.

**CV / rendering**
- **Dict rendered as raw JSON in the PDF (the "v2" bug).** Passing a dict where
  the render schema expects a string (nested phone/location/summary objects)
  printed raw JSON on the CV. Fix: the strict normalization layer (§1.9) that
  flattens every nested value before the pydantic schema — never feed the
  template unnormalized JSON.
- **Arabic characters bleeding into the English CV.** The knowledge base
  legitimately holds Arabic fields for apply forms; without the Unicode-block
  leak guard (§1.9 validation) they can end up in the rendered summary/skills.
- **Summary truncated mid-sentence.** Old code sliced at `max_chars` + `"..."` →
  `"…SAR <AMOUNT> and…"`. Fix: complete-sentence retention + pre-publication
  quality gate that blocks *before* any PDF is written (`ee0ce3f`).
- **Bad pair already on disk.** A render fix only prevents *future* bad
  publications — an already-published broken pair must be quarantined + re-rendered
  once, then manually verified. Preventive gates don't heal existing artifacts.
- **A display-only `"..."` preview** in an API path is not a publication bug, but
  don't let it leak into the published summary.
- **Punctuation-less summaries** correctly fail closed (`summary_incomplete_terminal`)
  — that's intended, not a regression.

**LLM truthfulness**
- LLMs invent frameworks/certs/metrics from the JD. The prompt alone is
  insufficient — keep the `_contains_invented_content` guard + master-CV
  vocabulary whitelist (`09c4df7` grounded cover letters in candidate truth).

**Salary**
- Annual figures silently treated as monthly (`fba3a16`). Never infer period from
  magnitude; require an explicit phrase marker; fail closed on ambiguity.
- "5+ years experience" parsed as annual salary — markers must be phrases, not the
  bare word "year".
- Range endpoints double-counted as lone values — skip spans already in a range.
- Contradictory monthly/annual evidence silently resolved — fail closed instead.
- Salary reason precision lost to integer formatting near the threshold (`83d8441`).

**Identity / binding**
- Aggregator jobs carry only a URL (no `job_id`/`anchor`/`posting_id`) → resolver
  returned `missing_job_identity`, so **no CV could ever be generated**. Fix:
  URL-v1 identity (`b01b7be`). Thread a stable identity from discovery onward.
- Caller-attested identity must never authorize a binding — recompute
  independently in the validator.
- A placeholder/redacted profile must never produce an exposed CV — guard for
  `[PRIVATE_PROFILE]`/`[REDACTED]` markers.

**Atomic writes**
- A failed regeneration destroyed the previous good pair. Fix: build on temps in a
  `.pub-*` subdir, sha-bind, then `os.replace`; never touch the prior pair until
  the new one is complete (`b408da1`).
- Two `os.replace` calls aren't jointly atomic — rely on sha binding to make the
  residual window *safe* (validator blocks on mismatch) rather than pretending it's
  eliminated.

**Enrichment / SSRF**
- A discovered URL (or a **redirect target**) can point at an internal IP
  (`169.254.169.254`, `127.0.0.1`, `10.x`) → SSRF. Re-apply the guard to the final
  post-redirect URL, not just the initial one (`dee5755`, `9f9dcb4`). Unwrap
  IPv4-mapped IPv6; resolve named hosts and reject if *any* address is internal;
  fail closed on resolution failure.

**Telegram / secrets**
- The bot token leaks into httpx/httpcore logs via the request URL. Attach the
  redaction filter to handlers (so propagated records are scrubbed), pin those
  loggers to WARNING, and never log `{exc}` raw (`b2e1444`, `99deb15`).
- Redaction cannot fix historical logs — **rotate** the token and gate all live
  paths behind a fail-closed boolean.
- Telegram authorization must fail closed (`12c1cfa`).

**Duplicate suppression**
- The digest re-sent the same active postings daily (P4-DUP-1). Fix: delivered
  ledger with a 30-day TTL (`0bdae0f`). The cross-day key MUST be byte-identical to
  the same-run dedupe key, or they diverge (P4-DUP-2). Do not reuse
  `normalize_url`/`_hash_url` here — they drop the query string / differ on case.

**CLI flag semantics — the `--dry-run` trap (cost: one reverted implementation)**
- The digest's `--dry-run` is `action="store_true"` with `default=True` and **no
  false form** — it can never be turned off from the CLI, and it gates
  **GENERATION only** (the Telegram send gate is a separate `--send-telegram`).
  A first implementation of the live-delivery pilot failed twice because of
  this: (a) it rejected on `args.dry_run` (always True → mode unusable), and
  (b) the delivery path never generated (`generation_dry_run` always True →
  generation silently skipped).
- The proven pattern: **never mutate `args.dry_run`** (discovery/build_digest
  semantics must stay unchanged). Instead derive:
  `live_generation_mode = generation_only or deliver_top_one`;
  `generation_dry_run = args.dry_run and not live_generation_mode` — the pilot
  overrides generation dry-run for its own run only.
- In a rebuild: give every boolean flag an explicit off form, and never overload
  one flag to gate two different side effects.

**Pilot exit semantics**
- A live pilot that returns exit 0 for every outcome (success and all failures) is
  machine-indistinguishable. Give bounded pilots distinct exit codes (`cef71c3`):
  `0` delivered+recorded, `3` no-eligible, `4` bundle-not-ready, `5`
  delivery-incomplete, `6` ledger-write-failed, `2` CLI validation.
- `--deliver-top-one` bounded pilot shape: cap the delivery path to ONE ranked,
  previously-undelivered job+CV bundle (1 card + 1 CV doc, ≤1 live generation)
  AFTER ranking + suppression, BEFORE generation; fail-closed bundle-ready gate
  (no fallback to the 2nd job); success derived from the REAL outcome (delivered
  bundle identity must equal the selected URL-v1 key), never inferred from
  header/text sends.

**Process**
- An enabled scheduler/timer runs the *working tree* — never leave uncommitted
  production changes across the daily run window.
- Test-order pollution: some async tests only pass in isolation. Don't import
  helpers from sibling `tests/test_*.py`; inline small builders and import only
  stable app services.

---

## 12. Calibrated Settings

Every tuned number, no secrets. Config field *names* only (values come from env).

**Salary**
| Constant | Value |
|---|---|
| Hard minimum | `24,000 SAR/month` |
| Consistency tolerance | `1.0 SAR` |
| Annual→monthly | `/12` once, `Decimal` + `ROUND_HALF_UP` |
| Weekly→monthly (legacy) | `×4.33` |
| Daily→monthly (legacy) | `×22` |
| Evidence buckets | `>=35 likely-high`, `>=20 possible`, `>=10 low`, else unknown |
| Estimation band | `min` … `min×1.25` |

**Gate / ranking**
| Constant | Value |
|---|---|
| `MAX_GATE_SEND` | `8` |
| `MAX_GENERATIONS_PER_RUN` | `3` |
| `MAX_CV_ATTACHMENTS` | `5` |
| role-match SEND floor | `70` |
| tier4 SEND | needs `PASS_CONFIRMED` and match `>= 90` |
| possible-high SEND | match `>= 90` and cq `>= 85` |
| unknown-salary SEND | match `>= 90`, cq `>= 85`, sq `>= 70` |
| tier→cq score | `{1:95, 2:75, 3:55, 4:25}` |
| source score | `high 90 / good 65 / low 20 / unknown 50 / none 0` |

**Discovery**
| Constant | Value |
|---|---|
| `TOP_N` | `20` |
| `MAX_PER_QUERY` | `5` |
| digest `TARGET_QUERIES` | 12 (see §5.1) |
| `TARGET_LOCATIONS` | Riyadh + Saudi Arabia |
| JobSpy `hours_old` | `336` (14 days) |
| JobSpy sites | `["indeed"]` |
| `DISCOVERY_MAX_RESULTS_PER_SOURCE` | `10` |
| `SCOUTING_INTERVAL_HOURS` | `6` |

**Enrichment / SSRF**
| Constant | Value |
|---|---|
| JD fetch timeout | `10 s` |
| `_JD_MAX_BYTES` | `200,000` |
| JD snippet cap | `10,000 chars` |
| accepted HTTP status | `200, 203` |
| tag-strip regex bound | `<[^>]{0,300}>` |

**CV layout** (see §1.5) — summary `400`, experience `4`, achievements/role `4`,
projects `2`, skills `14`; per-role pacing `[4,4,3,2]`; humanizer skill cap `12`;
summary humanize target `2–4 sentences / ≤420 chars`.

**Ledgers**
| Constant | Value |
|---|---|
| `DELIVERED_LEDGER_TTL_DAYS` | `30` |
| `_DELIVERED_LEDGER_VERSION` | `1` |
| `STALE_IN_FLIGHT_WINDOW_SECONDS` | `3600` (1 h) |
| ledger file perms | `0o644` (append), CV pair `0o600` |

**Generation / resolver**
| Constant | Value |
|---|---|
| `_DEFAULT_TIMEOUT_S` | `90.0 s` |
| `POLICY_VERSION` | `"1.0"` |
| retries (LLM) | `3` |

**Claude review layer**
| Constant | Value |
|---|---|
| `CLAUDE_REVIEW_MAX_INPUT_JOBS` | `30` |
| `CLAUDE_REVIEW_OUTPUT_TOP_N` | `20` |
| `CLAUDE_REVIEW_TIMEOUT_SECONDS` | `60` (hard wall-clock) |
| review `max_retries` | `2`, task type `ROUTINE` |
| `match_reason`/`concern_or_risk` cap | `400 chars` |

**Fit score (discovery-time, §4.5)**
| Constant | Value |
|---|---|
| title family bonus | `+20` (first match only) |
| fit labels | `STRONG >=70 · GOOD >=50 · FAIR >=30 · LOW` |
| CV doc send timeouts | `read=60s, write=60s` |

**Production scheduled run (the daily digest as actually deployed)**
| Item | Value |
|---|---|
| schedule | systemd timer, daily ~`09:00` (Asia/Riyadh) |
| ExecStart flags | `--dry-run --claude-review --send-telegram --intelligent-gate --enrich-jd` |
| meaning | text cards only — no CV attach/generation on the scheduled path; generation modes are manual-pilot flags |
| caution | an enabled timer runs the **working tree** — never leave uncommitted changes across the run window |

**Cover letter**
| Constant | Value |
|---|---|
| target length | `180–260 words`, ≤4 paragraphs |
| hard bounds | reject `< 150` or `> 350` words |
| experiences in payload | top `3` |
| skills in payload | top `12` |
| number-word normalization | `0–999` (+hundred phrases) |

**CAPTCHA / provider config field names** (values from env — never commit values):
`CAPTCHA_SOLVE_TIMEOUT_SECONDS=180`, `CAPTCHA_SOLVE_MAX_RETRIES=3`,
`CAPTCHA_SOLVE_MIN_BALANCE=0.50`, `CAPTCHA_SOLVE_SESSION_BUDGET_USD=0.50`,
`CAPTCHA_SOLVE_PER_APP_BUDGET_USD=0.05`,
`CAPTCHA_SOLVE_FAILURE_RATE_THRESHOLD=0.30`. Model names:
`OPENAI_MODEL_ROUTINE="gpt-4o-mini"`, `OPENAI_MODEL_STRONG="gpt-4o"` (update to
latest on rebuild). Safety gates default OFF: `ENABLE_AUTONOMOUS_DISCOVERY`,
`SCHEDULER_ENABLED`, `GMAIL_INTAKE_ENABLED`, `SEMANTIC_*`, and
`TELEGRAM_TOKEN_ROTATED_AFTER_MR3`.

---

## 13. Additional Notes

**Architecture invariants worth keeping.**
- **Thin service facade.** The application service is a coordinator that
  *delegates*; multi-field logic lives in a dedicated flow module; API routes
  delegate through the service boundary and never duplicate service logic.
- **Single-authority modules.** One module owns each cross-cutting truth:
  job identity, CV↔job binding, secret redaction, the tracking-param stripper.
  Every consumer imports/aliases that one implementation so semantics can't drift.
- **Injectable boundaries.** The resolver takes injectable generator/validator/
  profile-loader/existing-lookup so the whole pipeline is unit-testable with temp
  paths and mocks — no live LLM, network, private-profile read, or production write.

**Truthfulness pipeline (defense in depth, in order).**
1. Prefer pre-authored summary variants (no LLM).
2. Truthfulness-constrained prompt.
3. Post-generation invented-content guard + master-CV vocabulary whitelist.
4. Complete-sentence quality gate before publication.
5. SHA-bound per-job binding validation before the CV is ever exposed/attached.
Any layer can fail closed; a CV only ships when all pass.

**PII split pattern.** `master_cv.json` is committed **redacted** (placeholders);
real PII lives in a git-ignored `private_profile.json` merged at runtime by a
loader. Generation requires the real overlay (`require_private=True`) and blocks if
any `[PRIVATE_PROFILE]`/`[REDACTED]` marker survives. If you split PII this way,
also purge git history (the original history may still contain PII).

Loader mechanics (`master_cv_loader`): constants `PLACEHOLDER="[PRIVATE_PROFILE]"`,
`REDACTED="[REDACTED]"`; **recursive deep-merge** — private dict values merge into
matching public dicts, everything else replaces (deep-copied); an `_overlay_meta`
key in the overlay is skipped. Overlay absent + not required → public redacted
profile as-is (offline/CI-safe); required + absent/unreadable/non-dict →
`PrivateProfileMissingError` (fail loudly). `redact_master_cv_for_logging` masks
by **key-name pattern** (email/phone/address/national_id/passport/salary/names/
arabic_*/gender/marital/… ) AND by **value pattern** (email-shaped, 8+ digit
runs) — the only safe way to log a profile.

**Role → tailored-CV routing pattern.** A `_CV_ROLE_MAP` maps ordered tuples of
title tokens (most specific first) to a pre-built tailored-CV PDF path, with a
generic human-readable PDF fallback. The real filenames are intentionally NOT
reproduced here (they contain a real name). Shape only:

```python
_CV_ROLE_MAP = [
    (("it manager","it governance","operations manager","technology manager",
      "technology operations"), "data/tailored_cvs/<role_a>_<id>.pdf"),
    (("business analyst","technical business analyst","business technology"),
      "data/tailored_cvs/<role_b>_<id>.pdf"),
    (("service delivery","it service delivery","it operations","governance",
      "digital transformation"), "data/tailored_cvs/<role_c>_<id>.pdf"),
]
_FALLBACK_CV_PATH = "data/cv_assets/<candidate_resume>.pdf"   # human-usable PDF, NOT master_cv.json
```

**Safe attachment resolver.** Only `.pdf`/`.docx` that exist on disk and contain no
forbidden name tokens (`secret, token, credential, private_profile, master_cv`) may
be attached to Telegram; `.json`/`.env`/`.txt` are excluded by extension first.

**Hard automation boundaries baked into the digest** (carry these as first-class
policy): recommend + attach CV to Telegram for **manual** review only — no submit,
no form fill, no CV upload to portals, no account creation, no CAPTCHA solve, no
Gmail, no LinkedIn automation. LinkedIn is blocked at multiple independent layers
(ledger guard, JobSpy exclusion, unsafe-domain list). The digest CLI has no
browser/submit/apply imports and uses HTTP-GET discovery guarded by the SSRF
check — auto-apply is structurally unreachable from it.

**Provider graceful degradation.** Every external dependency degrades safely:
no LLM → rule-based analysis/tailoring; no SerpAPI → stub returns 0 links; JobSpy
not installed → empty results; unreadable ledger → treated empty (fail-open for
suppression, so nothing is wrongly hidden).

**Determinism for tests.** Same-run tie-breaks and ordering use the canonical
URL-v1 key so equal-ranked results are deterministic; salary rounding uses
`Decimal`; identity uses sha256 (never `hash()`), so results are reproducible
across processes and restarts.

---

## Appendix — Coverage Map (what is documented vs. deliberately excluded)

Closing statement of scope, so nothing is silently missing. Every
production-critical module of the digest+CV pipeline is documented above; every
other legacy subsystem is **explicitly listed here as out of scope** for the
digest/CV rebuild (portable later if the new product grows into auto-apply).

**Documented (module → section):**

| Module | Section |
|---|---|
| `pdf_service` (CV + cover templates, render) | §1.2–1.4, §1.7 |
| `page_enforcement_service` (caps + summary gate) | §1.5–1.6 |
| `cv_service` (normalization raw JSON → schema) | §1.9 |
| `master_cv_loader` (PII overlay merge) | §13 |
| `validation_service` (pre-render checks) | §1.9 |
| digest salary gate + parsers | §2.1 |
| `salary_estimation_service` (legacy) | §2.2 |
| `job_identity` (URL-v1) | §3 |
| digest tier/source/role gate + ranking | §4.1–4.4 |
| digest discovery fit score | §4.5 |
| `filter_service` (legacy) | §4.6 |
| queries, SerpAPI, JobSpy, Google Jobs routing | §5 |
| JD enrichment + SSRF guard | §6 |
| `tailored_cv_binding` / pipeline / resolver / selection / quarantine | §7 |
| delivered ledger + submit-attempt ledger + dedupe layers | §8 |
| Telegram cards, digest cards, doc send, secret redaction, harden_logging | §9 |
| all 6 production prompts + validators, LLM router/client behavior | §10 |
| bug history, calibrated constants, runtime config | §11–12 |
| architecture invariants, PII split, safety boundaries | §13 |

**Deliberately excluded (exists in legacy, out of scope for this rebuild):**

- **Portal apply automation** — Workday step executors/mappers (~40 modules),
  Greenhouse/SmartRecruiters/Lever live drivers, portal intelligence
  (detector/field extractor/action planner), browser runtime, submit services.
  Large subsystem; port only if the new product includes auto-apply. The
  submit-attempt ledger (§8.2) and its LinkedIn/duplicate guards ARE documented
  because any future apply layer must reuse them.
- **Account management** — signup detection/filling, credential vault (Fernet),
  password generator, login flows. Secrets-adjacent by nature.
- **Gmail integration** — OAuth intake, confirmation detection, verification
  codes, interview extraction. (Known playbook: Greenhouse 8-box email code
  verification via Gmail readonly — retained in operator memory, not here.)
- **CAPTCHA solving** (2Captcha) — config field names in §12; policy: never on
  LinkedIn, budget-capped.
- **Telegram cockpit/bridge** — interactive operator command handlers, approval
  flows, rate limiting, whitelists. The *presentation* layer (§9.1) is
  documented; the interactive wiring is legacy-specific.
- **DB layer & API routes** — SQLAlchemy models/repositories, FastAPI routes,
  dashboards, scheduler registry. Standard plumbing, no distilled knowledge.
- **Phase/closure snapshot services** (~50 `*_snapshot_service` modules) —
  project-governance scaffolding, not product logic.

Anything not in either list does not exist in the legacy codebase.

## Appendix — Verification Record

This report was not only extracted — it was **executed against**. Final
verification pass (2026-07-14, on the live legacy environment):

- **120 executable assertions, all passing**, ran the real production functions
  and asserted every documented number/behavior: URL-v1 normalization + hash +
  parser rejects; every salary-parse edge case (annual÷12 Decimal, k-notation,
  ranges, contradiction fail-closed, "5+ years" immunity, period markers);
  salary-gate buckets and confidences; tier classification; source-quality
  scores; role-match hard-rejects and an exact-total example (70 for
  IT Operations Manager @ tier-1); fit-label thresholds; SEND/BLOCK decisions
  for every gate branch; SSRF guard on metadata/loopback/RFC1918/IPv4-mapped-
  IPv6/hostless inputs; summary enforcement + every quality reason code;
  same-run dedupe (URL then company+title); delivered-key normalization;
  canonical job key stability; LinkedIn/insufficient-context guard verdicts;
  review-response no-fabrication boundary (fake URLs dropped, CV path
  re-derived); and all 15 grounding-validator claim classes (fabricated
  cert/employer/degree/tool, category-bound numbers, word-numbers, vague
  magnitudes, scope inflation, target-company-as-employer, fail-closed on
  non-string).
- **Byte-diff**: the CV template and cover-letter template in §1 are
  byte-identical to the production-written files on disk.
- **Set-diff**: TIER1/TIER2 keyword lists, all three source-domain lists,
  junior-support tokens, role_map weights, skill_hits weights, UTM params, and
  the JD-unsafe domain list in this report are exactly equal to the source
  frozensets/dicts.
- **Verbatim prompt lines** spot-checked with exact-string grep across source
  and report (humanization/analysis/cover/ranking/page-parse/review).
- One report error was found by this pass and fixed: the digest's discovery
  orchestration (two distinct paths, §5.2) — proof the method works.

---

*End of LEGACY_KNOWLEDGE.md — logic and code only; no secrets, tokens, credentials,
personal data, real CV filenames, or ledger contents included.*
