"""Binding + atomic pair publishing — LEGACY §7 on the StorageAdapter.

- :func:`validate_cv_binding` is the SOLE acceptance authority (§7.1): it
  recomputes the URL-v1 identity itself — a caller-attested identity can
  never authorize READY — verifies sha256 against the completed bytes,
  guards locations and scratch names, and NEVER raises (an internal error
  becomes ``validator_error:internal``, a rejection, never a silent pass).
- Publishing (§7.2) renders in a private temp dir, sha-binds the sidecar to
  the completed bytes, then puts PDF first and sidecar second — each put is
  atomic (the adapter's temp→fsync→replace); the residual window between
  the two is made SAFE, not hidden: a stale sidecar can never match the new
  bytes, so the validator blocks rather than false-readying.
- The on-demand resolver (§7.5) gates generation fail-closed through eight
  honest statuses; a placeholder identity never publishes (§15.8's last
  line of defense); the budget loop annotates, never silently drops.
- Selection (§7.6) is two-stage: hints (URL, ids, company+title) only
  DISCOVER candidates in a stable order — the validator alone accepts.
- Quarantine (§7.7) preserves evidence byte-identical; it never deletes.

Deviation note: the LEGACY dual identity regime is NOT ported — this system
has no legacy artifacts by construction, so any sidecar that is not
``joburl-v1`` fails closed with ``unknown_identity_version``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from career.cv import generate
from career.cv.render import render_cv_pdf
from career.cv.schemas import ContactInfo, TailoredCV
from career.storage import StorageAdapter, tenant_key
from career_core.identity import (
    derive_canonical_job_identity,
    parse_canonical_job_identity,
)
from career_core.urltools import normalize_job_url_v1

logger = logging.getLogger("career.cv")

STATE_READY = "ready_for_apply_upload"
STATE_BLOCKED = "blocked"

TAILORED_PREFIX = "tailored_cvs"
QUARANTINE_PREFIX = "tailored_cvs_quarantine"
POLICY_VERSION = "1.0"

MAX_GENERATIONS_PER_RUN = 3

_SCRATCH_MARKERS = ("_test", "_smoke", "_smoketest", "_draft", "_temp",
                    "_tmp", "_fulltext", "_maxfit", "_stress")
_VERSION_SUFFIX_RE = re.compile(r"_v(?:[2-9]|\d{2,})(?:$|[._])")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class PublishRejected(Exception):
    """The pair may not be published — raised BEFORE anything is written."""


# ── publication (§7.2/§7.3/§7.4) ─────────────────────────────────────────────


def _is_placeholder_contact(contact: ContactInfo) -> bool:
    return (
        contact.name.strip() == generate.PLACEHOLDER_CONTACT["name"]
        or contact.phone.strip() == generate.PLACEHOLDER_CONTACT["phone"]
    )


def publish_cv_pair(
    storage: StorageAdapter,
    *,
    tenant_id: str,
    cv: TailoredCV,
    job_url: str,
    location: str | None,
    portal: str | None,
    job_analysis: dict[str, Any],
    now: datetime,
    renderer: Callable[[TailoredCV, Path], Path] = render_cv_pdf,
) -> dict[str, str]:
    """Render + sha-bind + publish the pair. Returns the two storage keys."""
    if _is_placeholder_contact(cv.master_cv.contact):
        # §7.5: a placeholder identity is never exposed on a published CV.
        raise PublishRejected("placeholder_contact_never_published")

    identity = derive_canonical_job_identity(job_url)
    if identity is None:
        raise PublishRejected("url_identity_underivable")
    hexpart = identity.split(":")[-1]

    pdf_key = tenant_key(tenant_id, TAILORED_PREFIX, f"joburl-{hexpart}.pdf")
    meta_key = tenant_key(
        tenant_id, TAILORED_PREFIX, f"joburl-{hexpart}.metadata.json"
    )

    with tempfile.TemporaryDirectory(prefix=".pub-") as tmp:
        rendered = renderer(cv, Path(tmp) / f"joburl-{hexpart}.pdf")
        pdf_bytes = Path(rendered).read_bytes()

    sidecar: dict[str, Any] = {
        "job_title": cv.job_title,
        "company": cv.company,
        "job_id": None,                       # provenance only, never identity
        "anchor_id": None,
        "job_url": job_url,
        "portal": portal,
        "location": location,
        "generated_at": now.isoformat(),
        "source_master_cv_path": "achievement_bank",   # D6: the bank IS the source
        "source_master_cv_sha256": None,
        "source_pdf_references": [],
        "tailoring_summary": {
            "summary_chars": len(cv.tailored_summary),
            "experience_entries": len(cv.selected_experience),
            "skills": len(cv.selected_skills),
            "modifications": cv.modifications,
        },
        "job_analysis": job_analysis,
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "size_bytes": len(pdf_bytes),
        "tailored_cv_path": pdf_key,
        "policy_version": POLICY_VERSION,
        "identity_version": "joburl-v1",
        "canonical_job_identity": identity,
    }

    # PDF first, sidecar second (§7.2 order) — each put is atomic; the window
    # between them is safe because the sidecar sha binds to these exact bytes.
    storage.put(pdf_key, pdf_bytes, content_type="application/pdf")
    storage.put(
        meta_key,
        json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json",
    )
    return {"cv_key": pdf_key, "metadata_key": meta_key}


# ── the sole acceptance authority (§7.1) ─────────────────────────────────────


def validate_cv_binding(
    storage: StorageAdapter,
    *,
    cv_key: str,
    metadata_key: str,
    expected_job_url: str,
) -> dict[str, Any]:
    """Is this CV demonstrably the one generated for this exact job?
    Never raises; READY only with zero blockers."""
    blockers: list[str] = []
    details: dict[str, Any] = {}
    try:
        # 1) required paths
        if not cv_key:
            blockers.append("no_tailored_cv_path")
        if not metadata_key:
            blockers.append("no_tailored_cv_metadata_path")
        if blockers:
            return {"state": STATE_BLOCKED, "blockers": blockers,
                    "details": details}

        # 2) file presence
        if not storage.exists(cv_key):
            blockers.append(f"cv_file_missing:{cv_key}")
        if not storage.exists(metadata_key):
            blockers.append(f"metadata_file_missing:{metadata_key}")
        if blockers:
            return {"state": STATE_BLOCKED, "blockers": blockers,
                    "details": details}

        # 3) location guards
        if f"/{TAILORED_PREFIX}/" not in f"/{cv_key}":
            blockers.append("cv_not_in_tailored_dir")
        if "/cv_assets/" in f"/{cv_key}":
            blockers.append("cv_is_source_asset_pdf")

        # 4) scratch filenames
        stem = Path(cv_key).stem
        if _VERSION_SUFFIX_RE.search(stem) or any(
            marker in stem for marker in _SCRATCH_MARKERS
        ):
            blockers.append(f"cv_filename_is_scratch:{Path(cv_key).name}")

        # 5) metadata parse
        metadata: dict[str, Any] | None = None
        try:
            parsed = json.loads(storage.get(metadata_key))
        except (ValueError, KeyError):
            blockers.append("metadata_unparseable")
        else:
            if isinstance(parsed, dict):
                metadata = parsed
            else:
                blockers.append("metadata_not_a_dict")

        # 6) identity equality — recomputed HERE; attestation never accepted
        if metadata is not None:
            if metadata.get("identity_version") != "joburl-v1":
                blockers.append("unknown_identity_version")
            else:
                derived = derive_canonical_job_identity(expected_job_url)
                if derived is None:
                    blockers.append("url_identity_underivable")
                else:
                    claimed = metadata.get("canonical_job_identity")
                    if parse_canonical_job_identity(claimed) is None:
                        blockers.append("metadata_canonical_identity_invalid")
                    elif claimed != derived:
                        blockers.append("canonical_identity_mismatch")
                    expected_stem = f"joburl-{derived.split(':')[-1]}"
                    if stem != expected_stem:
                        blockers.append("canonical_stem_mismatch")

        # 7) sha256 of the ACTUAL bytes
        if metadata is not None:
            actual = hashlib.sha256(storage.get(cv_key)).hexdigest()
            details["sha256"] = actual
            if actual != metadata.get("sha256"):
                blockers.append("sha256_mismatch")
    except Exception:  # noqa: BLE001 — the validator must never raise
        logger.warning("validator internal error", exc_info=True)
        blockers.append("validator_error:internal")

    state = STATE_READY if not blockers else STATE_BLOCKED
    return {"state": state, "blockers": blockers, "details": details}


# ── two-stage selection: hints never accept (§7.6) ───────────────────────────


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower() if value else ""


def select_tailored_cv(
    storage: StorageAdapter, *, tenant_id: str, job: dict[str, Any]
) -> str | None:
    """The safe PDF key of the CV made FOR this exact job, or None."""
    job_url = job.get("apply_link") or job.get("url") or ""
    job_url_key = normalize_job_url_v1(job_url)
    job_ids = {
        _norm(job.get(field))
        for field in ("source_job_id", "anchor_id", "posting_id")
        if job.get(field)
    }
    pair = (_norm(job.get("company")), _norm(job.get("title")))

    candidates: list[tuple[str, str]] = []       # (pdf_key, metadata_key)
    prefix = tenant_key(tenant_id, TAILORED_PREFIX)
    for key in storage.list_keys(prefix):        # stable, sorted (contract)
        if not key.endswith(".metadata.json"):
            continue
        try:
            metadata = json.loads(storage.get(key))
        except (ValueError, KeyError):
            continue
        if not isinstance(metadata, dict):
            continue
        matched = False
        if job_url_key and normalize_job_url_v1(
            metadata.get("job_url")
        ) == job_url_key:
            matched = True
        elif job_ids and (
            _norm(metadata.get("job_id")) in job_ids
            or _norm(metadata.get("anchor_id")) in job_ids
        ):
            matched = True
        elif pair[0] and pair == (
            _norm(metadata.get("company")), _norm(metadata.get("job_title"))
        ):
            matched = True
        if matched:
            pdf_key = key[: -len(".metadata.json")] + ".pdf"
            if (pdf_key, key) not in candidates:
                candidates.append((pdf_key, key))

    for pdf_key, meta_key in candidates:         # the validator alone accepts
        try:
            verdict = validate_cv_binding(
                storage, cv_key=pdf_key, metadata_key=meta_key,
                expected_job_url=str(job_url),
            )
        except Exception:  # noqa: BLE001, S112 — fail closed, try the others
            logger.warning("candidate validation crashed", exc_info=True)
            continue
        if verdict.get("state") == STATE_READY and storage.exists(pdf_key):
            return pdf_key
    return None


# ── the on-demand resolver (§7.5 — eight honest statuses) ────────────────────


@dataclass(frozen=True)
class ResolveResult:
    status: str
    cv_key: str | None = None
    metadata_key: str | None = None


def resolve_tailored_cv(
    storage: StorageAdapter,
    *,
    tenant_id: str,
    job_url: str,
    generation_enabled: bool,
    dry_run: bool,
    contact: ContactInfo,
    generator: Callable[[], dict[str, str] | None],
) -> ResolveResult:
    """Decides whether a job with no validated CV may get one generated.
    Never delivers, never writes the ledger, never enables generation."""
    existing = select_tailored_cv(
        storage, tenant_id=tenant_id, job={"apply_link": job_url}
    )
    if existing:
        return ResolveResult("existing_valid_cv", cv_key=existing)

    if not generation_enabled:
        return ResolveResult("generation_disabled")
    if dry_run:
        return ResolveResult("dry_run_generation_skipped")
    if derive_canonical_job_identity(job_url) is None:
        return ResolveResult("missing_job_identity")
    if _is_placeholder_contact(contact):
        # a placeholder profile must never produce an exposed CV (§7.5)
        return ResolveResult("validation_blocked")

    try:
        keys = generator()
    except Exception:  # noqa: BLE001 — honest failure, never a crash
        logger.warning("cv generation failed", exc_info=True)
        return ResolveResult("generation_failed")
    if not keys:
        return ResolveResult("generation_failed")

    verdict = validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=job_url,
    )
    if verdict["state"] == STATE_READY:
        return ResolveResult(
            "generated_valid_cv",
            cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        )
    return ResolveResult("validation_blocked")


# ── the digest-side budget loop (§7.5) ───────────────────────────────────────


def generate_missing_cvs(
    jobs: list[dict[str, Any]],
    *,
    resolve: Callable[[dict[str, Any]], ResolveResult],
    budget: int = MAX_GENERATIONS_PER_RUN,
) -> list[dict[str, Any]]:
    """Holders pass through free; once the budget is spent the rest are
    annotated ``generation_budget_exhausted``. Returns a NEW list."""
    out: list[dict[str, Any]] = []
    spent = 0
    for job in jobs:
        job = dict(job)                          # never mutate inputs
        if job.get("cv_attachment_status") != "no_tailored_cv":
            out.append(job)
            continue
        if spent >= budget:
            job["cv_generation_status"] = "generation_budget_exhausted"
            out.append(job)
            continue
        result = resolve(job)
        # LEGACY §7.5: only actual GENERATION work consumes the budget —
        # a reuse resolved for free (audit fix C).
        if result.status != "existing_valid_cv":
            spent += 1
        job["cv_generation_status"] = result.status
        if result.status in ("generated_valid_cv", "existing_valid_cv"):
            job["cv_key"] = result.cv_key
        out.append(job)
    return out


# ── quarantine (§7.7): all-or-safe move, evidence preserved ──────────────────


def quarantine_pair(
    storage: StorageAdapter,
    *,
    cv_key: str,
    metadata_key: str,
    now: datetime,
) -> dict[str, str]:
    pdf_bytes = storage.get(cv_key)
    meta_bytes = storage.get(metadata_key)
    shortsha = hashlib.sha256(pdf_bytes).hexdigest()[:8]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    subdir = f"{stamp}-{shortsha}"

    dest_pdf = cv_key.replace(
        f"/{TAILORED_PREFIX}/", f"/{QUARANTINE_PREFIX}/{subdir}/", 1
    )
    dest_meta = metadata_key.replace(
        f"/{TAILORED_PREFIX}/", f"/{QUARANTINE_PREFIX}/{subdir}/", 1
    )
    # copy BOTH first (all-or-safe), delete originals only after both landed
    storage.put(dest_pdf, pdf_bytes, content_type="application/pdf")
    storage.put(dest_meta, meta_bytes, content_type="application/json")
    storage.delete(cv_key)
    storage.delete(metadata_key)
    return {"cv_key": dest_pdf, "metadata_key": dest_meta}
