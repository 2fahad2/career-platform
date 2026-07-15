"""WhatsApp Cloud API client — the injectable send boundary.

The delivery/worker code depends only on the ``WhatsAppClient`` protocol, so it
is fully testable now with ``FakeWhatsAppClient`` (no network). The HTTP
implementation is a thin skeleton wired and integration-tested once the Meta
access token + phone number id arrive (a Fahad step) — no speculative,
untested payload code is shipped before it can be verified live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SentMessage:
    to_phone: str
    kind: str  # text | document | template | interactive
    message_id: str
    body: str | None = None
    template_name: str | None = None
    document_ref: str | None = None
    buttons: tuple[str, ...] = ()
    variables: dict[str, str] = field(default_factory=dict)


class WhatsAppClient(Protocol):
    def send_text(self, to_phone: str, body: str) -> str: ...
    def send_document(
        self, to_phone: str, document_ref: str, *, filename: str, caption: str = ""
    ) -> str: ...
    def send_template(
        self, to_phone: str, template_name: str, language: str,
        variables: dict[str, str] | None = None, buttons: tuple[str, ...] = (),
    ) -> str: ...
    def send_interactive(
        self, to_phone: str, body: str, buttons: tuple[str, ...]
    ) -> str: ...
    def download_media(self, media_id: str) -> tuple[bytes, str | None] | None:
        """Fetch inbound media bytes (+ filename if known); None if unavailable."""
        ...


class FakeWhatsAppClient:
    """Records everything sent; returns deterministic message ids for tests."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        #: media_id -> (bytes, filename) — seed in tests for inbound documents.
        self.media: dict[str, tuple[bytes, str | None]] = {}
        self._n = 0

    def _next_id(self) -> str:
        self._n += 1
        return f"wamid.fake.{self._n}"

    def send_text(self, to_phone: str, body: str) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage(to_phone, "text", mid, body=body))
        return mid

    def send_document(
        self, to_phone: str, document_ref: str, *, filename: str, caption: str = ""
    ) -> str:
        mid = self._next_id()
        self.sent.append(
            SentMessage(to_phone, "document", mid, body=caption, document_ref=document_ref)
        )
        return mid

    def send_template(
        self, to_phone: str, template_name: str, language: str,
        variables: dict[str, str] | None = None, buttons: tuple[str, ...] = (),
    ) -> str:
        mid = self._next_id()
        self.sent.append(
            SentMessage(to_phone, "template", mid, template_name=template_name,
                        buttons=buttons, variables=variables or {})
        )
        return mid

    def send_interactive(
        self, to_phone: str, body: str, buttons: tuple[str, ...]
    ) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage(to_phone, "interactive", mid, body=body, buttons=buttons))
        return mid

    def download_media(self, media_id: str) -> tuple[bytes, str | None] | None:
        return self.media.get(media_id)


class HttpWhatsAppClient:  # pragma: no cover — wired when Meta creds arrive
    """Skeleton. Fill the payloads and POST to the Graph API once the access
    token + phone number id exist and can be tested against a live WABA."""

    def __init__(self, access_token: str, phone_number_id: str,
                 base_url: str = "https://graph.facebook.com/v20.0") -> None:
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._base_url = base_url.rstrip("/")

    def _unimplemented(self) -> str:
        raise NotImplementedError(
            "HttpWhatsAppClient is wired in C4 once the Meta access token exists"
        )

    def send_text(self, to_phone: str, body: str) -> str:
        return self._unimplemented()

    def send_document(
        self, to_phone: str, document_ref: str, *, filename: str, caption: str = ""
    ) -> str:
        return self._unimplemented()

    def send_template(
        self, to_phone: str, template_name: str, language: str,
        variables: dict[str, str] | None = None, buttons: tuple[str, ...] = (),
    ) -> str:
        return self._unimplemented()

    def send_interactive(
        self, to_phone: str, body: str, buttons: tuple[str, ...]
    ) -> str:
        return self._unimplemented()

    def download_media(self, media_id: str) -> tuple[bytes, str | None] | None:
        self._unimplemented()
        return None
