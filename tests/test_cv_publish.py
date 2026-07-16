"""Binding + atomic publishing acceptance tests (LEGACY §7) — before code.

validate_cv_binding is the SOLE acceptance authority: it recomputes the
URL-v1 identity itself (a caller-attested identity can never authorize
READY), verifies sha256 against the completed bytes, guards locations and
scratch names, and NEVER raises. Publishing is the atomic pair discipline
over the StorageAdapter with the residual window made safe by sha binding.
The resolver's eight honest statuses gate on-demand generation fail-closed —
a placeholder identity never publishes; hints never accept (§7.6); the
quarantine preserves evidence, never deletes it (§7.7).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from career.cv import generate, publish
from career.cv.schemas import (
    ContactInfo,
    Experience,
    MasterCV,
    TailoredCV,
)
from career.storage import FilesystemStorageAdapter
from career_core.identity import derive_canonical_job_identity

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=UTC)
TENANT = str(uuid.uuid4())
JOB_URL = "https://careers.acme.com/jobs/8841?utm_source=x"

CONTACT = ContactInfo(
    name="Fahad Almulhim", email="fahad@example.com",
    phone="+966500000000", location="Riyadh, Saudi Arabia",
)


def _cv(contact: ContactInfo = CONTACT) -> TailoredCV:
    master = MasterCV(
        contact=contact,
        headline="Senior Business Analyst",
        summary="Base.",
        skills=["SQL", "Power BI", "BPMN", "UAT", "Jira"],
    )
    return TailoredCV(
        master_cv=master, job_title="Business Analyst", company="Acme",
        tailored_summary=(
            "Senior business analyst delivering regulated programs with "
            "measurable outcomes. Known for disciplined stakeholder work."
        ),
        selected_experience=[Experience(
            title="Senior BA", company="Acme", location="Riyadh",
            start_date="2021-03", achievements=["Did X.", "Did Y."],
        )],
        selected_skills=["SQL", "Power BI", "BPMN", "UAT", "Jira"],
        modifications="test",
    )


def _publish(storage: FilesystemStorageAdapter, **overrides: Any) -> dict[str, str]:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT, "cv": _cv(), "job_url": JOB_URL,
        "location": "Riyadh", "portal": "employer",
        "job_analysis": {"role_family": "Business Analysis"},
        "now": NOW,
    }
    kwargs.update(overrides)
    return publish.publish_cv_pair(storage, **kwargs)


# ── publish → validate roundtrip ─────────────────────────────────────────────


def test_publish_then_validate_is_ready(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    identity = derive_canonical_job_identity(JOB_URL)
    assert identity is not None
    hexpart = identity.split(":")[-1]
    assert keys["cv_key"].endswith(f"tailored_cvs/joburl-{hexpart}.pdf")
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=JOB_URL,
    )
    assert verdict["state"] == publish.STATE_READY
    assert verdict["blockers"] == []


def test_sidecar_carries_the_documented_fields(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    sidecar = json.loads(storage.get(keys["metadata_key"]))
    for field in ("job_title", "company", "job_url", "portal", "location",
                  "generated_at", "tailoring_summary", "job_analysis",
                  "sha256", "size_bytes", "tailored_cv_path",
                  "policy_version", "identity_version",
                  "canonical_job_identity"):
        assert field in sidecar, field
    assert sidecar["identity_version"] == "joburl-v1"
    assert sidecar["sha256"] == hashlib.sha256(
        storage.get(keys["cv_key"])
    ).hexdigest()


def test_publish_refuses_placeholder_identity(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    placeholder = ContactInfo(
        name=generate.PLACEHOLDER_CONTACT["name"],
        email="candidate@example.com", phone="+0", location="Riyadh",
    )
    try:
        _publish(storage, cv=_cv(placeholder))
        raise AssertionError("expected PublishRejected")
    except publish.PublishRejected as exc:
        assert "placeholder" in str(exc)


def test_publish_refuses_underivable_url(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    try:
        _publish(storage, job_url="not a url")
        raise AssertionError("expected PublishRejected")
    except publish.PublishRejected as exc:
        assert "identity" in str(exc)


# ── the validator: every blocker, and it never raises ────────────────────────


def test_validator_blocks_missing_paths_and_files(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    verdict = publish.validate_cv_binding(
        storage, cv_key="", metadata_key="", expected_job_url=JOB_URL
    )
    assert verdict["state"] == publish.STATE_BLOCKED
    assert "no_tailored_cv_path" in verdict["blockers"]

    verdict = publish.validate_cv_binding(
        storage,
        cv_key=f"tenants/{TENANT}/tailored_cvs/joburl-{'a' * 64}.pdf",
        metadata_key=f"tenants/{TENANT}/tailored_cvs/joburl-{'a' * 64}.metadata.json",
        expected_job_url=JOB_URL,
    )
    assert any(b.startswith("cv_file_missing:") for b in verdict["blockers"])


def test_validator_blocks_wrong_location_and_scratch_names(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    pdf = storage.get(keys["cv_key"])
    meta = storage.get(keys["metadata_key"])

    outside = f"tenants/{TENANT}/cv_assets/source.pdf"
    storage.put(outside, pdf)
    storage.put(outside + ".metadata.json", meta)
    verdict = publish.validate_cv_binding(
        storage, cv_key=outside, metadata_key=outside + ".metadata.json",
        expected_job_url=JOB_URL,
    )
    assert "cv_not_in_tailored_dir" in verdict["blockers"]
    assert "cv_is_source_asset_pdf" in verdict["blockers"]

    for scratch in ("thing_v2.pdf", "cv_test.pdf", "x_draft.pdf", "y_tmp.pdf"):
        key = f"tenants/{TENANT}/tailored_cvs/{scratch}"
        storage.put(key, pdf)
        storage.put(key + ".metadata.json", meta)
        verdict = publish.validate_cv_binding(
            storage, cv_key=key, metadata_key=key + ".metadata.json",
            expected_job_url=JOB_URL,
        )
        assert any(
            b.startswith("cv_filename_is_scratch:") for b in verdict["blockers"]
        ), scratch


def test_validator_recomputes_identity_never_trusts_metadata(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    # attacker rewrites the sidecar to claim a different job — sha still valid
    sidecar = json.loads(storage.get(keys["metadata_key"]))
    other_identity = derive_canonical_job_identity("https://evil.example/j/1")
    sidecar["canonical_job_identity"] = other_identity
    storage.put(keys["metadata_key"], json.dumps(sidecar).encode())
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=JOB_URL,
    )
    assert "canonical_identity_mismatch" in verdict["blockers"]

    # and validating against a DIFFERENT expected URL blocks on stem too
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url="https://careers.acme.com/jobs/9999",
    )
    assert verdict["state"] == publish.STATE_BLOCKED


def test_validator_blocks_sha_mismatch(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    storage.put(keys["cv_key"], b"%PDF-1.4 tampered")     # bytes swapped
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=JOB_URL,
    )
    assert "sha256_mismatch" in verdict["blockers"]


def test_validator_blocks_unparseable_metadata_and_never_raises(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    storage.put(keys["metadata_key"], b"not json at all")
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=JOB_URL,
    )
    assert "metadata_unparseable" in verdict["blockers"]

    storage.put(keys["metadata_key"], json.dumps(["a", "list"]).encode())
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=JOB_URL,
    )
    assert "metadata_not_a_dict" in verdict["blockers"]


def test_residual_window_is_safe_not_hidden(tmp_path: Any) -> None:
    """§7.2: PDF replaced but sidecar still old → sha mismatch → blocked,
    never a false ready."""
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    old_meta = storage.get(keys["metadata_key"])
    # re-publish with different content; then simulate the window by
    # restoring the OLD sidecar next to the NEW pdf
    cv2 = _cv().model_copy(update={"tailored_summary": (
        "A different truthful summary for the same posting rendering "
        "different bytes for the window simulation."
    )})
    _publish(storage, cv=cv2)
    storage.put(keys["metadata_key"], old_meta)
    verdict = publish.validate_cv_binding(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        expected_job_url=JOB_URL,
    )
    assert "sha256_mismatch" in verdict["blockers"]


# ── the resolver: eight honest statuses, fail-closed (§7.5) ──────────────────


def _resolver(storage: FilesystemStorageAdapter, **overrides: Any) -> publish.ResolveResult:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "job_url": JOB_URL,
        "generation_enabled": True,
        "dry_run": False,
        "contact": CONTACT,
        "generator": lambda: _publish(storage),
    }
    kwargs.update(overrides)
    return publish.resolve_tailored_cv(storage, **kwargs)


def test_resolver_reuses_an_existing_valid_cv(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    _publish(storage)
    calls: list[int] = []
    result = _resolver(storage, generator=lambda: calls.append(1))
    assert result.status == "existing_valid_cv"
    assert result.cv_key and calls == []                  # no generation


def test_resolver_honest_gates(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    assert _resolver(storage, generation_enabled=False).status == "generation_disabled"
    assert _resolver(storage, dry_run=True).status == "dry_run_generation_skipped"
    assert _resolver(storage, job_url="garbage").status == "missing_job_identity"
    placeholder = ContactInfo(
        name=generate.PLACEHOLDER_CONTACT["name"], email="c@example.com",
        phone="+0", location="Riyadh",
    )
    assert _resolver(storage, contact=placeholder).status == "validation_blocked"


def test_resolver_generation_paths(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    generated = _resolver(storage)
    assert generated.status == "generated_valid_cv"
    assert generated.cv_key

    def boom() -> dict[str, str]:
        raise RuntimeError("render exploded")

    storage2 = FilesystemStorageAdapter(tmp_path / "b")
    assert _resolver(storage2, generator=boom).status == "generation_failed"

    def bad_pair() -> dict[str, str]:
        keys = _publish(storage2)
        storage2.put(keys["cv_key"], b"tampered")          # fails binding
        return keys

    assert _resolver(storage2, generator=bad_pair).status == "validation_blocked"


# ── the budget loop (§7.5 digest side) ───────────────────────────────────────


def test_generation_budget_loop_semantics() -> None:
    calls: list[str] = []

    def fake_resolve(job: dict[str, Any]) -> publish.ResolveResult:
        calls.append(job["url"])
        return publish.ResolveResult("generated_valid_cv", cv_key=f"k:{job['url']}")

    jobs = [
        {"url": "u1", "cv_attachment_status": "no_tailored_cv"},
        {"url": "u2", "cv_attachment_status": "bound"},      # holder: free
        {"url": "u3", "cv_attachment_status": "no_tailored_cv"},
        {"url": "u4", "cv_attachment_status": "no_tailored_cv"},
        {"url": "u5", "cv_attachment_status": "no_tailored_cv"},
    ]
    out = publish.generate_missing_cvs(jobs, resolve=fake_resolve, budget=2)
    assert calls == ["u1", "u3"]                            # holders skipped
    assert out[0]["cv_generation_status"] == "generated_valid_cv"
    assert out[1].get("cv_generation_status") is None       # untouched holder
    assert out[3]["cv_generation_status"] == "generation_budget_exhausted"
    assert out[4]["cv_generation_status"] == "generation_budget_exhausted"
    assert jobs[3].get("cv_generation_status") is None      # inputs not mutated


# ── two-stage selection: hints never accept (§7.6) ───────────────────────────


def test_selection_matches_by_url_and_validator_decides(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    found = publish.select_tailored_cv(
        storage, tenant_id=TENANT,
        job={"apply_link": JOB_URL, "title": "Business Analyst",
             "company": "Acme"},
    )
    assert found == keys["cv_key"]

    # hint matches but the pair is tampered → the validator refuses → None
    storage.put(keys["cv_key"], b"tampered")
    assert publish.select_tailored_cv(
        storage, tenant_id=TENANT,
        job={"apply_link": JOB_URL, "title": "Business Analyst",
             "company": "Acme"},
    ) is None


def test_selection_returns_none_for_unknown_jobs(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    _publish(storage)
    assert publish.select_tailored_cv(
        storage, tenant_id=TENANT,
        job={"apply_link": "https://other.example/j/1", "title": "X",
             "company": "Y"},
    ) is None


# ── quarantine: evidence preserved, never deleted (§7.7) ─────────────────────


def test_quarantine_moves_the_pair_byte_identical(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    keys = _publish(storage)
    pdf, meta = storage.get(keys["cv_key"]), storage.get(keys["metadata_key"])
    moved = publish.quarantine_pair(
        storage, cv_key=keys["cv_key"], metadata_key=keys["metadata_key"],
        now=NOW,
    )
    assert not storage.exists(keys["cv_key"])              # gone from live
    assert storage.get(moved["cv_key"]) == pdf             # byte-identical
    assert storage.get(moved["metadata_key"]) == meta
    assert "tailored_cvs_quarantine/" in moved["cv_key"]
