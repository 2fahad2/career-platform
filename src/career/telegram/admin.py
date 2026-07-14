"""Telegram admin channel — the operator's out-of-band channel (whitepaper §08,
§10). It NEVER renders PII: only TEN-#### codes, counts, and opaque status
(§15.13). Messages are also passed through secret redaction as defense in depth.

Injectable so tests assert what would be sent without a live bot; the HTTP
implementation is wired once the admin bot token exists (and is rotated —
LEGACY §9.2 rotation gate).
"""

from __future__ import annotations

from typing import Protocol

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


class HttpTelegramAdminClient:  # pragma: no cover — wired when bot token exists
    def __init__(self, bot_token: str, chat_id: str,
                 base_url: str = "https://api.telegram.org") -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._base_url = base_url.rstrip("/")

    def send_admin(self, text: str) -> str:
        raise NotImplementedError(
            "HttpTelegramAdminClient is wired once the admin bot token is set + rotated"
        )
