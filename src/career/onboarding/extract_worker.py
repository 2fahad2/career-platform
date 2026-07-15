"""Sandboxed text-extraction worker (whitepaper §11) — runs as a subprocess.

Reads the file bytes from stdin, writes extracted UTF-8 text to stdout.
Resource limits are applied INSIDE the child before parsing starts, so a
malicious file that explodes the parser hits the rlimits, not the service:

    python -m career.onboarding.extract_worker <pdf|docx> [mem_mb] [cpu_s]

Exit codes: 0 ok · 2 bad usage · 3 parse failure. The parent additionally
enforces a wall-clock timeout and an output-size cap.
"""

from __future__ import annotations

import io
import resource
import sys
import zipfile

from defusedxml import ElementTree  # entity-expansion-safe for untrusted XML

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _apply_rlimits(mem_mb: int, cpu_s: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # imported after rlimits are in place

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    parts: list[str] = []
    for para in root.iter(f"{_WORD_NS}p"):
        runs = [node.text or "" for node in para.iter(f"{_WORD_NS}t")]
        if runs:
            parts.append("".join(runs))
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] not in ("pdf", "docx"):
        return 2
    mem_mb = int(argv[1]) if len(argv) > 1 else 512
    cpu_s = int(argv[2]) if len(argv) > 2 else 30
    _apply_rlimits(mem_mb, cpu_s)
    data = sys.stdin.buffer.read()
    try:
        text = _pdf_text(data) if argv[0] == "pdf" else _docx_text(data)
    except Exception:  # noqa: BLE001 — any parser explosion is a plain failure
        return 3
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    raise SystemExit(main(sys.argv[1:]))
