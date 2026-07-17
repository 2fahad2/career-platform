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

#: Inline keyboard shape: rows of (label, callback_data).
Keyboard = list[list[tuple[str, str]]]


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
    """Keep-alive session; per-call timeout so a 50s long-poll is never
    killed by a 30s transport default (that mismatch stalled every button)."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def post(
        self, url: str, json: dict[str, Any], timeout: float = 30.0
    ) -> tuple[int, dict[str, Any]]:
        resp = self._session.post(url, json=json, timeout=timeout)
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

    def _call(
        self, method: str, payload: dict[str, Any], timeout: float = 30.0
    ) -> Any:
        status, body = self._transport.post(
            f"{self._base_url}/bot{self._bot_token}/{method}", json=payload,
            timeout=timeout,
        )
        if status != 200 or not body.get("ok"):
            raise TelegramSendError(f"bot API HTTP {status}")
        return body.get("result")

    @staticmethod
    def _markup(keyboard: Keyboard | None) -> dict[str, Any] | None:
        if not keyboard:
            return None
        return {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in keyboard
        ]}

    def send_admin(self, text: str) -> str:
        result = self._call("sendMessage", {
            "chat_id": self._chat_id,
            "text": sanitize_secret_text(text),
            "disable_web_page_preview": True,
        })
        return str(result["message_id"])

    # ── the watchtower console surface (design doc §2) ───────────────────

    def get_updates(self, offset: int, timeout: int = 50) -> list[dict[str, Any]]:
        result = self._call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=timeout + 10)   # transport must outlive the long poll
        return list(result or [])

    def send_screen(self, text: str, keyboard: Keyboard | None = None) -> str:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": sanitize_secret_text(text),
            "disable_web_page_preview": True,
        }
        markup = self._markup(keyboard)
        if markup:
            payload["reply_markup"] = markup
        return str(self._call("sendMessage", payload)["message_id"])

    def edit_screen(
        self, message_id: int, text: str, keyboard: Keyboard | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id, "message_id": message_id,
            "text": sanitize_secret_text(text),
            "disable_web_page_preview": True,
        }
        markup = self._markup(keyboard)
        if markup:
            payload["reply_markup"] = markup
        self._call("editMessageText", payload)

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": sanitize_secret_text(text) if text else "",
        })
