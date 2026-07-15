"""CV upload security pipeline (whitepaper §05 + §11).

Order of controls, each fail-closed:
1. size limit → 2. magic-byte MIME sniff (the extension is never consulted) →
3. injectable malware scanner → 4. structural inspection (PDF: encryption,
JavaScript, embedded files, open-actions, page limit · DOCX: macros, OLE
embeddings, remote templates, zip anomalies) → 5. metadata-stripping
sanitization → 6. sandboxed text extraction (subprocess with rlimits +
wall-clock timeout + output cap).

Bytes live in object storage via StorageAdapter — never in the DB (§10). The
service records an honest verdict row either way (§15.12): ``processed`` with
a stored sanitized document + extracted text, or ``rejected`` with findings
and nothing stored.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from typing import Protocol

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from sqlalchemy import func
from sqlalchemy.orm import Session

from career.db.models import CvUpload, Document
from career.onboarding.consents import require_required_consents
from career.storage import StorageAdapter
from career.storage.adapter import tenant_key

_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_ZIP_MAX_ENTRIES = 2048
_ZIP_MAX_UNCOMPRESSED = 100 * 1024 * 1024

#: Catalog/name-tree keys that mean active or hidden content in a PDF.
_PDF_DANGEROUS_ROOT = ("/OpenAction", "/AA")
_PDF_DANGEROUS_NAMES = ("/JavaScript", "/EmbeddedFiles")


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 10 * 1024 * 1024
    max_pages: int = 15
    timeout_s: float = 30.0
    mem_mb: int = 512
    max_text_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class ScanReport:
    mime: str | None
    size_bytes: int
    page_count: int | None
    findings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


class MalwareScanner(Protocol):
    """Injectable scanner boundary: returns a finding label or None."""

    def scan(self, data: bytes) -> str | None: ...


class ExtractionFailed(Exception):
    """Sandboxed extraction failed (timeout, crash, or output overflow)."""


def sniff_mime(data: bytes) -> str | None:
    """Magic bytes only — a DOCX must be a zip that actually contains
    word/document.xml, not merely something ending in .docx."""
    if data.startswith(b"%PDF-"):
        return _PDF_MIME
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return _DOCX_MIME
        except zipfile.BadZipFile:
            return None
    return None


# ── structural inspection ────────────────────────────────────────────────────


def _inspect_pdf(data: bytes, limits: Limits) -> tuple[int | None, list[str]]:
    findings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — unparseable = rejected, not crashed
        return None, ["pdf_unparseable"]
    if reader.is_encrypted:
        return None, ["pdf_encrypted"]
    page_count = len(reader.pages)
    if page_count > limits.max_pages:
        findings.append("pdf_page_limit_exceeded")
    root = reader.trailer.get("/Root", {})
    for key in _PDF_DANGEROUS_ROOT:
        if key in root:
            findings.append("pdf_open_action")
            break
    names = root.get("/Names")
    if names is not None:
        if "/JavaScript" in names:
            findings.append("pdf_javascript")
        if "/EmbeddedFiles" in names:
            findings.append("pdf_embedded_files")
    return page_count, findings


def _inspect_docx(data: bytes) -> list[str]:
    findings: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > _ZIP_MAX_ENTRIES:
                findings.append("zip_anomaly:entry_count")
            if sum(i.file_size for i in infos) > _ZIP_MAX_UNCOMPRESSED:
                findings.append("zip_anomaly:uncompressed_size")
            names = z.namelist()
            for name in names:
                if name.startswith(("/", "\\")) or ".." in name:
                    findings.append("zip_anomaly:path_traversal")
                    break
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                findings.append("docx_macros")
            if any(n.startswith("word/embeddings/") for n in names):
                findings.append("docx_ole_embeddings")
            for name in names:
                if name.endswith(".rels"):
                    rels = z.read(name).decode("utf-8", errors="replace")
                    if 'TargetMode="External"' in rels and "attachedTemplate" in rels:
                        findings.append("docx_remote_template")
                        break
    except zipfile.BadZipFile:
        findings.append("zip_anomaly:corrupt")
    return findings


def validate_upload(data: bytes, *, scanner: MalwareScanner, limits: Limits) -> ScanReport:
    size = len(data)
    if size > limits.max_bytes:
        return ScanReport(None, size, None, ("size_exceeds_limit",))

    mime = sniff_mime(data)
    if mime is None:
        return ScanReport(None, size, None, ("unrecognized_file_type",))

    findings: list[str] = []
    verdict = scanner.scan(data)
    if verdict:
        findings.append(f"malware_detected:{verdict}")

    page_count: int | None = None
    if mime == _PDF_MIME:
        page_count, pdf_findings = _inspect_pdf(data, limits)
        findings.extend(pdf_findings)
    else:
        findings.extend(_inspect_docx(data))

    return ScanReport(mime, size, page_count, tuple(findings))


# ── sanitization (metadata + any residual active content) ────────────────────


def sanitize_pdf(data: bytes) -> bytes:
    """Rebuild the PDF from its pages only: document-level JavaScript, embedded
    files, open-actions and metadata do not survive; page-level annotations and
    actions are stripped."""
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for page in reader.pages:
        for key in ("/Annots", "/AA"):
            if key in page:
                del page[NameObject(key)]
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def sanitize_docx(data: bytes) -> bytes:
    """Rewrite the archive without docProps/* (author/company/revision
    metadata). Active content is grounds for rejection upstream, not repair."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename.startswith("docProps/"):
                continue
            dst.writestr(info.filename, src.read(info.filename))
    return out.getvalue()


# ── sandboxed extraction ─────────────────────────────────────────────────────


def _run_sandboxed(argv: list[str], input_bytes: bytes, *, timeout_s: float) -> bytes:
    try:
        # argv is built entirely from constants + validated ints — no customer
        # input ever reaches the command line (the file bytes go via stdin).
        proc = subprocess.run(  # noqa: S603
            argv, input=input_bytes, capture_output=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionFailed("extraction_timeout") from exc
    if proc.returncode != 0:
        raise ExtractionFailed(f"extraction_failed:exit_{proc.returncode}")
    return proc.stdout


def extract_text_sandboxed(data: bytes, kind: str, limits: Limits) -> str:
    """Extract text in a subprocess with rlimits set inside the child (see
    extract_worker). Output larger than the cap is an error, not a truncation."""
    out = _run_sandboxed(
        [
            sys.executable, "-m", "career.onboarding.extract_worker",
            kind, str(limits.mem_mb), str(int(limits.timeout_s)),
        ],
        data,
        timeout_s=limits.timeout_s,
    )
    if len(out) > limits.max_text_bytes:
        raise ExtractionFailed("extraction_failed:output_too_large")
    return out.decode("utf-8", errors="replace")


# ── the end-to-end service ───────────────────────────────────────────────────


def process_cv_upload(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    data: bytes,
    original_filename: str | None,
    scanner: MalwareScanner,
    storage: StorageAdapter,
    limits: Limits | None = None,
) -> CvUpload:
    """Validate → sanitize → store → extract, recording an honest verdict row.

    The consent gate runs FIRST (§12): no byte is inspected before the
    required consents exist. On rejection nothing is stored and the findings
    are recorded (PII-free slugs). On success the SANITIZED bytes are stored
    (never the original), plus the extracted text for the C5.6 extractor.
    """
    require_required_consents(session, tenant_id=tenant_id)
    limits = limits or Limits()

    row = CvUpload(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        original_filename=original_filename,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        scan_status="scanning",
        status="received",
    )
    session.add(row)
    session.flush()

    report = validate_upload(data, scanner=scanner, limits=limits)
    row.mime_detected = report.mime
    row.page_count = report.page_count

    if not report.ok:
        row.scan_status = "rejected"
        row.status = "rejected"
        row.scan_findings = {"findings": list(report.findings)}
        row.processed_at = func.now()
        session.flush()
        return row

    kind = "pdf" if report.mime == _PDF_MIME else "docx"
    sanitized = sanitize_pdf(data) if kind == "pdf" else sanitize_docx(data)
    try:
        text = extract_text_sandboxed(sanitized, kind, limits)
    except ExtractionFailed as exc:
        row.scan_status = "rejected"
        row.status = "rejected"
        row.scan_findings = {"findings": [str(exc)]}
        row.processed_at = func.now()
        session.flush()
        return row

    suffix = "pdf" if kind == "pdf" else "docx"
    doc_id = uuid.uuid4()
    doc_key = tenant_key(str(tenant_id), "uploads", f"{doc_id}.{suffix}")
    storage.put(doc_key, sanitized, content_type=report.mime or "application/octet-stream")
    text_key = tenant_key(str(tenant_id), "uploads", f"{doc_id}.txt")
    storage.put(text_key, text.encode("utf-8"), content_type="text/plain")

    document = Document(
        id=doc_id,
        tenant_id=tenant_id,
        storage_key=doc_key,
        content_sha256=hashlib.sha256(sanitized).hexdigest(),
        content_type=report.mime or "application/octet-stream",
        size_bytes=len(sanitized),
        status="active",
    )
    session.add(document)
    session.flush()  # the FK target must exist before cv_uploads points at it

    row.document_id = doc_id
    row.extracted_text_storage_key = text_key
    row.scan_status = "clean"
    row.status = "processed"
    row.scan_findings = {"findings": []}
    row.processed_at = func.now()
    session.flush()
    return row
