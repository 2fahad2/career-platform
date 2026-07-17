"""HttpWhatsAppClient request shapes (Graph Cloud API) — before code.

The transport is injectable, so every payload the live client will send is
asserted here with zero network: text, interactive reply-buttons (ids are
the labels, hard-capped at WhatsApp's 20 chars), template with body
variables, the two-step document flow (storage ref → media upload → message
by media id), the two-step media download, and body-free errors.
"""

from __future__ import annotations

from typing import Any

from career.storage import FilesystemStorageAdapter
from career.whatsapp.client import HttpWhatsAppClient, WhatsAppSendError


class FakeTransport:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.post_replies: list[tuple[int, dict[str, Any]]] = []
        self.get_replies: list[tuple[int, Any]] = []

    def post(self, url: str, *, headers: dict[str, str],
             json: dict[str, Any] | None = None,
             data: dict[str, Any] | None = None,
             files: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        self.posts.append({"url": url, "headers": headers, "json": json,
                           "data": data, "files": files})
        return self.post_replies.pop(0)

    def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, Any]:
        self.gets.append(url)
        return self.get_replies.pop(0)


def _client(transport: FakeTransport, storage: Any = None) -> HttpWhatsAppClient:
    return HttpWhatsAppClient(
        "tok-123", "555000111", storage=storage, transport=transport
    )


def test_send_text_payload() -> None:
    t = FakeTransport()
    t.post_replies = [(200, {"messages": [{"id": "wamid.X1"}]})]
    mid = _client(t).send_text("+966500000001", "مرحبا")
    assert mid == "wamid.X1"
    call = t.posts[0]
    assert call["url"].endswith("/555000111/messages")
    assert call["headers"]["Authorization"] == "Bearer tok-123"
    assert call["json"] == {
        "messaging_product": "whatsapp", "to": "+966500000001",
        "type": "text", "text": {"body": "مرحبا"},
    }


def test_send_interactive_buttons_ids_capped_at_20() -> None:
    t = FakeTransport()
    t.post_replies = [(200, {"messages": [{"id": "wamid.X2"}]})]
    long_label = "زر طويل جدا يتجاوز حد واتساب للعناوين"
    _client(t).send_interactive("+9665", "اختر:", ("نعم", long_label))
    payload = t.posts[0]["json"]
    assert payload["type"] == "interactive"
    buttons = payload["interactive"]["action"]["buttons"]
    assert buttons[0]["reply"] == {"id": "نعم", "title": "نعم"}
    assert buttons[1]["reply"]["title"] == long_label[:20]
    assert buttons[1]["reply"]["id"] == long_label[:20]


def test_send_template_with_variables() -> None:
    t = FakeTransport()
    t.post_replies = [(200, {"messages": [{"id": "wamid.X3"}]})]
    _client(t).send_template(
        "+9665", "daily_opportunities_utility", "ar",
        variables={"1": "فرصتان"},
    )
    payload = t.posts[0]["json"]
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "daily_opportunities_utility"
    assert payload["template"]["language"] == {"code": "ar"}
    assert payload["template"]["components"] == [{
        "type": "body",
        "parameters": [{"type": "text", "text": "فرصتان"}],
    }]


def test_send_document_uploads_storage_ref_then_sends_media_id(tmp_path: Any) -> None:
    storage = FilesystemStorageAdapter(tmp_path)
    storage.put("tenants/t/tailored_cvs/x.pdf", b"%PDF-1.4 bytes")
    t = FakeTransport()
    t.post_replies = [
        (200, {"id": "MEDIA-9"}),                        # upload
        (200, {"messages": [{"id": "wamid.X4"}]}),       # message
    ]
    mid = _client(t, storage).send_document(
        "+9665", "tenants/t/tailored_cvs/x.pdf",
        filename="Fahad - BA.pdf", caption="CV",
    )
    assert mid == "wamid.X4"
    upload = t.posts[0]
    assert upload["url"].endswith("/555000111/media")
    assert upload["data"] == {"messaging_product": "whatsapp"}
    name, payload, mime = upload["files"]["file"]
    assert name == "Fahad - BA.pdf" and payload == b"%PDF-1.4 bytes"
    assert mime == "application/pdf"
    message = t.posts[1]["json"]
    assert message["document"] == {
        "id": "MEDIA-9", "filename": "Fahad - BA.pdf", "caption": "CV",
    }


def test_download_media_two_step() -> None:
    t = FakeTransport()
    t.get_replies = [
        (200, {"url": "https://lookaside.example/blob", "id": "M1"}),
        (200, b"raw-bytes"),
    ]
    result = _client(t).download_media("M1")
    assert result == (b"raw-bytes", None)
    assert t.gets[0].endswith("/M1")
    assert t.gets[1] == "https://lookaside.example/blob"


def test_download_media_failure_returns_none() -> None:
    t = FakeTransport()
    t.get_replies = [(404, {"error": {"code": 100}})]
    assert _client(t).download_media("gone") is None


def test_non_200_raises_body_free() -> None:
    t = FakeTransport()
    t.post_replies = [(401, {"error": {"message": "secret-ish details",
                                       "code": 190}})]
    try:
        _client(t).send_text("+9665", "hi")
        raise AssertionError("expected WhatsAppSendError")
    except WhatsAppSendError as exc:
        assert "401" in str(exc) and "190" in str(exc)
        assert "secret" not in str(exc)                  # body never quoted
