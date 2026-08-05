"""CV upload security pipeline (whitepaper §05 + §11).

Order of controls, each fail-closed:
1. size limit → 2. magic-byte MIME sniff (the extension is never consulted) →
3. injectable malware scanner → 4. structural inspection (PDF: encryption,
JavaScript, embedded files, open-actions, page limit · DOCX: macros, OLE
embeddings, remote templates, zip anomalies) → 5. metadata-stripping
sanitization → 6. sandboxed text extraction (subprocess with rlimits +
wall-clock timeout + output cap).

Step 3 was, for the whole of the live period, an interface with nothing behind
it: the runner injected a scanner whose ``scan`` returned None unconditionally,
so every file passed, and nothing anywhere said so — no boot warning, no health
light, and a ``scan_status`` column that read ``clean`` on a file no engine had
ever looked at. The honest docstring on that stand-in was the only disclosure,
and docstrings are not an operator surface. This module now carries a REAL
engine client (:class:`ClamdScanner`, clamd's INSTREAM over its unix socket), a
three-way readiness state the watchtower can render
(:func:`scanner_health`), and a DECLARED policy for what an unscannable upload
gets (:class:`ScanPolicy`) — the one option removed is the old one: passing
silently while presenting as scanned.

Bytes live in object storage via StorageAdapter — never in the DB (§10). The
service records an honest verdict row either way (§15.12): ``processed`` with
a stored sanitized document + extracted text, or ``rejected`` with findings
and nothing stored — and ``processed`` now distinguishes ``clean`` (an engine
said so) from ``unscanned`` (nobody did, and we say it out loud).
"""

from __future__ import annotations

import hashlib
import io
import os
import socket
import struct
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any, Protocol

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
    #: PII-free slug naming WHY no engine looked at these bytes, or None when
    #: one did. A report that is ``ok`` and carries a reason means the file
    #: was accepted under the declared policy — never that it was scanned.
    unscanned_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def scanned(self) -> bool:
        return self.unscanned_reason is None


class MalwareScanner(Protocol):
    """Injectable scanner boundary: returns a finding label or None.

    Raising :class:`ScannerUnavailable` (or its ``ScannerAbsent`` subclass) is
    part of the contract, not an error path bolted on: an engine that cannot
    answer must SAY so, because the alternative shape — returning None, which
    means «clean» — is precisely the silent pass this module exists to end.
    """

    def scan(self, data: bytes) -> str | None: ...


class ExtractionFailed(Exception):
    """Sandboxed extraction failed (timeout, crash, or output overflow)."""


# ── the malware engine: client, readiness, and a declared failure policy ─────


#: The three answers the operator needs, and there are only three. «Configured
#: but unreachable» is NOT a flavour of «absent»: absent is a posture someone
#: chose and can see, unreachable is a promise that stopped being kept — and
#: the two get different treatment below for exactly that reason.
SCANNER_READY = "ready"
SCANNER_UNREACHABLE = "unreachable"
SCANNER_ABSENT = "absent"

#: The key :func:`scanner_health` results are published under on the watchtower
#: health dict, so the probe and the renderer cannot drift apart on a string.
HEALTH_KEY = "virus_scanner"


@dataclass(frozen=True)
class ScannerHealth:
    """A health signal, not a log line — see the module docstring for why the
    difference mattered here. ``detail`` is always a PII-free slug: a file
    name, a customer, or a raw daemon reply must never reach the operator's
    Telegram screen (§15.13)."""

    state: str
    engine: str
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == SCANNER_READY


class ScannerUnavailable(Exception):
    """The engine is configured but produced no verdict (down, hung, over its
    stream limit, or answering something we do not understand)."""


class ScannerAbsent(ScannerUnavailable):
    """No engine is configured at all. Separate from the above because the
    policy answer is deliberately different, not because the message is."""


#: What an upload nobody could scan is worth. Two decisions, stated rather
#: than implied, and both overridable per environment because the right answer
#: during an incident is not the right answer on a normal Tuesday.
REJECT = "reject"
ACCEPT_UNSCANNED = "accept_unscanned"


@dataclass(frozen=True)
class ScanPolicy:
    """The declared answer to «we could not scan this file».

    ``on_unavailable`` defaults to REJECT. An operator who configured an engine
    declared the scan a requirement, and a daemon that stops answering is the
    failure an attacker can *cause* — fill the socket, hang the stream, and
    every payload after it walks through. An honest rejection the customer can
    retry beats a pass we would have to lie about.

    ``on_absent`` defaults to ACCEPT_UNSCANNED, and this is the deliberate
    asymmetry. There is no engine on this host yet (ClamAV cannot be installed
    in this pass), and refusing every upload would take the product down for
    people who have already paid — for a control that was never actually
    running. So the file goes through the rest of §11 at full strength (magic
    sniff, structural inspection, sanitization, sandboxed extraction — which is
    what actually defends the parser and the operator's viewer), the row is
    stamped ``unscanned`` instead of ``clean``, and the health screen shows the
    missing engine permanently. Nothing is hidden; the posture is simply
    visible instead of pretended.

    Flip ``on_absent`` to REJECT the moment an engine is expected everywhere,
    and ``on_unavailable`` to ACCEPT_UNSCANNED only to deliberately degrade
    during a daemon outage — a choice a human makes and the health screen
    keeps showing.
    """

    on_unavailable: str = REJECT
    on_absent: str = ACCEPT_UNSCANNED


_DEFAULT_POLICY = ScanPolicy()

#: clamd refuses a stream longer than StreamMaxLength (25 MB by default) by
#: closing it mid-send, which reads back as a truncated reply rather than a
#: verdict. We check the bound ourselves so an oversized file is a stated
#: «could not scan», never a mystery.
_CLAMD_STREAM_MAX = 25 * 1024 * 1024
_CLAMD_CHUNK = 64 * 1024
_CLAMD_REPLY_MAX = 4096


def _slug(text: str, limit: int = 64) -> str:
    """Signature names and daemon words reduced to a safe label. The finding
    is written to a customer's row and read on the admin channel, so nothing
    but ASCII word characters survives."""
    kept = [c if (c.isalnum() or c in "._-") else "_" for c in text.strip()]
    return "".join(kept)[:limit] or "unknown"


class ClamdScanner:
    """clamd over its local unix socket, INSTREAM — the file never touches the
    daemon's filesystem and we never shell out.

    Written against clamd's protocol rather than a library on purpose: adding a
    dependency was not available in this pass, and the wire format (``zPING`` →
    ``PONG``, ``zINSTREAM`` → length-prefixed chunks → a terminating zero
    length → ``stream: OK`` / ``stream: <sig> FOUND``) is small enough that a
    client is less risk than an unpinned package. Every failure mode collapses
    to :class:`ScannerUnavailable` with a slug, because the caller's only
    question is «did an engine give a verdict».
    """

    def __init__(
        self,
        socket_path: str,
        *,
        timeout_s: float = 8.0,
        max_bytes: int = _CLAMD_STREAM_MAX,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        try:
            sock.connect(self.socket_path)
        except TimeoutError as exc:
            sock.close()
            raise ScannerUnavailable("engine_timeout") from exc
        except OSError as exc:
            sock.close()
            raise ScannerUnavailable("engine_unreachable") from exc
        return sock

    def _read_reply(self, sock: socket.socket) -> str:
        """Read to the NUL terminator. The cap is not politeness: a wedged or
        wrong daemon on that path must not be able to stream at us forever
        while the customer waits."""
        buf = bytearray()
        try:
            while b"\x00" not in buf and len(buf) < _CLAMD_REPLY_MAX:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                buf.extend(chunk)
        except TimeoutError as exc:
            raise ScannerUnavailable("engine_timeout") from exc
        except OSError as exc:
            raise ScannerUnavailable("engine_unreachable") from exc
        return buf.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()

    def health(self) -> ScannerHealth:
        """A PING, not a scan: the health screen must be cheap enough to redraw
        on every tap and must never send a customer's bytes anywhere."""
        try:
            with self._connect() as sock:
                sock.sendall(b"zPING\x00")
                reply = self._read_reply(sock)
        except ScannerUnavailable as exc:
            return ScannerHealth(SCANNER_UNREACHABLE, "clamd", str(exc))
        if reply.upper() != "PONG":
            return ScannerHealth(
                SCANNER_UNREACHABLE, "clamd", "engine_reply_unrecognized"
            )
        return ScannerHealth(SCANNER_READY, "clamd")

    def scan(self, data: bytes) -> str | None:
        if len(data) > self.max_bytes:
            # Checked before connecting: an over-limit stream is aborted by the
            # daemon halfway through, which looks like a network fault and is
            # not one. The pipeline's own size limit runs first and is lower,
            # so reaching here means the two bounds were configured apart.
            raise ScannerUnavailable("file_exceeds_scanner_limit")
        try:
            with self._connect() as sock:
                sock.sendall(b"zINSTREAM\x00")
                for start in range(0, len(data), _CLAMD_CHUNK):
                    chunk = data[start:start + _CLAMD_CHUNK]
                    sock.sendall(struct.pack("!I", len(chunk)) + chunk)
                sock.sendall(struct.pack("!I", 0))
                reply = self._read_reply(sock)
        except TimeoutError as exc:
            raise ScannerUnavailable("engine_timeout") from exc
        except OSError as exc:
            raise ScannerUnavailable("engine_unreachable") from exc
        return _parse_clamd_verdict(reply)


def _parse_clamd_verdict(reply: str) -> str | None:
    """``stream: OK`` → clean · ``stream: X FOUND`` → the signature name ·
    anything else (including ``... ERROR``) → no verdict at all. An empty
    reply is the shape a killed daemon leaves behind, and it must not read as
    clean."""
    if not reply:
        raise ScannerUnavailable("engine_no_reply")
    body = reply.split(":", 1)[1].strip() if ":" in reply else reply
    if body.endswith(" FOUND"):
        return _slug(body[: -len(" FOUND")])
    if body == "OK":
        return None
    if body.endswith("ERROR"):
        raise ScannerUnavailable("engine_error")
    raise ScannerUnavailable("engine_reply_unrecognized")


class UnconfiguredScanner:
    """The honest stand-in for «no engine on this host».

    It does not return None — that would mean «clean», which is the lie this
    replaces. It raises, and :func:`validate_upload` applies ``on_absent``:
    the upload is accepted and stamped ``unscanned``, and the health screen
    says so for as long as it stays true.
    """

    def scan(self, data: bytes) -> str | None:
        raise ScannerAbsent("no_engine_configured")

    def health(self) -> ScannerHealth:
        return ScannerHealth(SCANNER_ABSENT, "none", "no_engine_configured")


def build_scanner(
    socket_path: str | None = None, *, timeout_s: float | None = None
) -> MalwareScanner:
    """The scanner this host actually has, engine or not.

    The socket path is read from ``CV_SCAN_CLAMD_SOCKET`` when not passed.
    Reading the environment here rather than from ``career.config.Settings`` is
    a temporary shape — the architect note in the deliverable asks for the two
    fields to move there, so that a typo in the path is caught at boot with the
    rest of the environment instead of at the first customer upload.
    """
    path = socket_path if socket_path is not None else os.environ.get(
        "CV_SCAN_CLAMD_SOCKET", ""
    ).strip()
    if not path:
        return UnconfiguredScanner()
    timeout = timeout_s if timeout_s is not None else float(
        os.environ.get("CV_SCAN_TIMEOUT_S", "8.0")
    )
    return ClamdScanner(path, timeout_s=timeout)


def scanner_health(scanner: object) -> ScannerHealth:
    """The three-way readiness state of whatever scanner was injected.

    Total by design: a scanner that predates this contract and exposes no
    ``health`` cannot be reported as ready, so it reads as unreachable with a
    slug that names the real problem. The watchtower probe calls this and
    publishes it under :data:`HEALTH_KEY`.
    """
    probe = getattr(scanner, "health", None)
    if probe is None:
        return ScannerHealth(
            SCANNER_UNREACHABLE, "unknown", "scanner_reports_no_health"
        )
    try:
        return probe()  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001 — a probe never breaks the health screen
        return ScannerHealth(SCANNER_UNREACHABLE, "unknown", "health_probe_failed")


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


def validate_upload(
    data: bytes,
    *,
    scanner: MalwareScanner,
    limits: Limits,
    policy: ScanPolicy = _DEFAULT_POLICY,
) -> ScanReport:
    size = len(data)
    if size > limits.max_bytes:
        return ScanReport(None, size, None, ("size_exceeds_limit",))

    mime = sniff_mime(data)
    if mime is None:
        return ScanReport(None, size, None, ("unrecognized_file_type",))

    findings: list[str] = []
    unscanned: str | None = None
    try:
        verdict = scanner.scan(data)
    except ScannerUnavailable as exc:
        # The two cases are told apart HERE and nowhere else, so the reason
        # slug the row carries always matches the branch that was taken.
        reason = _slug(str(exc))
        action = (
            policy.on_absent if isinstance(exc, ScannerAbsent)
            else policy.on_unavailable
        )
        if action == REJECT:
            findings.append(f"scanner_unavailable:{reason}")
        else:
            unscanned = reason
    else:
        if verdict:
            findings.append(f"malware_detected:{verdict}")

    page_count: int | None = None
    if mime == _PDF_MIME:
        page_count, pdf_findings = _inspect_pdf(data, limits)
        findings.extend(pdf_findings)
    else:
        findings.extend(_inspect_docx(data))

    return ScanReport(mime, size, page_count, tuple(findings), unscanned)


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
    policy: ScanPolicy | None = None,
) -> CvUpload:
    """Validate → sanitize → store → extract, recording an honest verdict row.

    The consent gate runs FIRST (§12): no byte is inspected before the
    required consents exist. On rejection nothing is stored and the findings
    are recorded (PII-free slugs). On success the SANITIZED bytes are stored
    (never the original), plus the extracted text for the C5.6 extractor.

    A processed row lands on ``clean`` only when an engine actually said so.
    When the declared policy accepted a file no engine could look at, the row
    says ``unscanned`` and names why — the column used to read ``clean`` for
    every upload the product has ever taken, which is the claim this exists to
    stop making.
    """
    require_required_consents(session, tenant_id=tenant_id)
    limits = limits or Limits()
    policy = policy or _DEFAULT_POLICY

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

    report = validate_upload(
        data, scanner=scanner, limits=limits, policy=policy
    )
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
    row.scan_status = "clean" if report.scanned else "unscanned"
    row.status = "processed"
    findings_row: dict[str, Any] = {"findings": []}
    if report.unscanned_reason is not None:
        findings_row["unscanned"] = report.unscanned_reason
    row.scan_findings = findings_row
    row.processed_at = func.now()
    session.flush()
    return row
