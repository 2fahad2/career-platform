"""HttpTelegramAdminClient — Bot API payload shapes, secret redaction, and
body-free errors, all with an injectable transport (zero network)."""

from __future__ import annotations

from typing import Any

import pytest

from career.telegram.admin import HttpTelegramAdminClient, TelegramSendError


class FakeTransport:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.post_replies: list[tuple[int, dict[str, Any]]] = []

    def post(
        self, url: str, json: dict[str, Any], timeout: float = 30.0
    ) -> tuple[int, dict[str, Any]]:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        return self.post_replies.pop(0)


def _client(t: FakeTransport) -> HttpTelegramAdminClient:
    return HttpTelegramAdminClient("123:abc", "42", transport=t)


def test_send_admin_payload_and_message_id() -> None:
    t = FakeTransport()
    t.post_replies = [(200, {"ok": True, "result": {"message_id": 77}})]
    mid = _client(t).send_admin("📊 ملخص — TEN-0002 · DELIVERED")
    assert mid == "77"
    call = t.posts[0]
    assert call["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert call["json"]["chat_id"] == "42"
    assert "TEN-0002" in call["json"]["text"]


def test_send_admin_redacts_secrets() -> None:
    t = FakeTransport()
    t.post_replies = [(200, {"ok": True, "result": {"message_id": 1}})]
    _client(t).send_admin("crash log: access_token=sk-ant-api03-abcdef123456")
    assert "sk-ant" not in t.posts[0]["json"]["text"]


def test_send_admin_error_is_body_free() -> None:
    t = FakeTransport()
    t.post_replies = [(403, {"ok": False, "description": "chat text echo"})]
    with pytest.raises(TelegramSendError) as err:
        _client(t).send_admin("hi")
    assert "chat text echo" not in str(err.value)
    assert "403" in str(err.value)
