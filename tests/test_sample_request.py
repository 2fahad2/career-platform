"""The «عينة» promise: a prospect gets real artefacts before paying."""

from __future__ import annotations

from pathlib import Path

from career.whatsapp import samples
from career.whatsapp.client import FakeWhatsAppClient


def test_triggers_recognised() -> None:
    for word in ("عينة", "عينه", "ابي عينة", "sample", " عينة "):
        assert samples.is_sample_request(word), word


def test_non_triggers_ignored() -> None:
    for word in ("مرحبا", "تفعيل ABC123", "", None, "عينة من فضلك"):
        assert not samples.is_sample_request(word)


def test_send_samples_delivers_intro_documents_and_outro(tmp_path: Path) -> None:
    root = tmp_path
    (root / "data" / "samples").mkdir(parents=True)
    (root / samples.SAMPLE_CV).write_bytes(b"%PDF-1.4 cv")
    (root / samples.SAMPLE_REPORT).write_bytes(b"%PDF-1.4 report")
    wa = FakeWhatsAppClient()

    assert samples.send_samples(wa, "+966500000000", repo_root=root)

    kinds = [m.kind for m in wa.sent]
    assert kinds.count("document") == 2
    assert kinds[0] == "text" and kinds[-1] == "text"
    assert "عينتان" in (wa.sent[0].body or "")


def test_missing_artefacts_degrade_gracefully(tmp_path: Path) -> None:
    """No files on disk → the intro still goes out, no crash, no documents."""
    wa = FakeWhatsAppClient()
    assert samples.send_samples(wa, "+966500000000", repo_root=tmp_path)
    assert all(m.kind != "document" for m in wa.sent)


def test_real_artefacts_exist_in_repo() -> None:
    """The shipped samples must actually be present — the copy promises them."""
    root = Path(__file__).resolve().parents[1]
    assert (root / samples.SAMPLE_CV).exists()
    assert (root / samples.SAMPLE_REPORT).exists()
