"""Telegram admin channel — the operator's out-of-band channel (whitepaper §08,
§10). It NEVER renders PII: only TEN-#### codes, counts, and opaque status
(§15.13). Messages are also passed through secret redaction as defense in depth.

Injectable so tests assert what would be sent without a live bot; the HTTP
implementation talks to the Bot API with an injectable transport, so every
payload shape is unit-asserted with zero network.
"""

from __future__ import annotations

from typing import Any, Protocol

from career.logging_filters import sanitize_secret_text


class TelegramAdminClient(Protocol):
    def send_admin(self, text: str) -> str: ...


class FakeTelegramAdminClient:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self._n = 0

    def send_admin(self, text: str) -> str:
        self._n += 1
        self.messages.append(sanitize_secret_text(text))
        return f"tg.fake.{self._n}"


class TelegramSendError(RuntimeError):
    """Non-200 from the Bot API — carries the HTTP status only, never the
    response body (it echoes the message text back)."""


class _RequestsTransport:  # pragma: no cover — exercised live
    def post(self, url: str, json: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import requests

        resp = requests.post(url, json=json, timeout=30)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return resp.status_code, payload


class HttpTelegramAdminClient:
    """Real Bot API sender. Every outbound text passes secret redaction —
    the admin channel is PII-free by contract, this guards secrets too."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        base_url: str = "https://api.telegram.org",
        transport: Any = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._base_url = base_url.rstrip("/")
        self._transport = transport or _RequestsTransport()

    def send_admin(self, text: str) -> str:
        status, body = self._transport.post(
            f"{self._base_url}/bot{self._bot_token}/sendMessage",
            json={
                "chat_id": self._chat_id,
                "text": sanitize_secret_text(text),
                "disable_web_page_preview": True,
            },
        )
        if status != 200 or not body.get("ok"):
            raise TelegramSendError(f"bot API HTTP {status}")
        return str(body["result"]["message_id"])
