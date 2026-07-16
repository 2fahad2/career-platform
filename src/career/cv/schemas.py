"""Render data model — LEGACY §1.1 verbatim (pydantic).

The strict layer between our multi-tenant sources (confirmed achievement bank
+ customer profile, D6) and the template: every value the template touches is
a plain str/list by the time it gets here — the normalization layer (§1.9)
guarantees that, and this schema enforces it (the "v2" raw-JSON-in-the-PDF
bug is structurally impossible past this point).
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class ContactInfo(BaseModel):
    name: str
    email: EmailStr
    phone: str
    location: str
    linkedin: str | None = None
    github: str | None = None


class Experience(BaseModel):
    title: str
    company: str
    location: str
    start_date: str
    end_date: str | None = None
    current: bool = False
    achievements: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Education(BaseModel):
    degree: str
    field_of_study: str | None = None
    institution: str
    location: str
    graduation_date: str
    gpa: str | None = None
    honors: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    name: str
    issuer: str
    date: str
    credential_id: str | None = None


class Project(BaseModel):
    name: str
    description: str
    technologies: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    url: str | None = None


class MasterCV(BaseModel):
    contact: ContactInfo
    headline: str | None = None
    summary: str
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class TailoredCV(BaseModel):
    master_cv: MasterCV
    job_title: str
    company: str
    tailored_summary: str
    selected_experience: list[Experience] = Field(default_factory=list)
    selected_skills: list[str] = Field(default_factory=list)
    selected_projects: list[Project] = Field(default_factory=list)
    modifications: str
