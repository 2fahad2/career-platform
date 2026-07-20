"""WhatsApp Cloud API client — the injectable send boundary.

The delivery/worker code depends only on the ``WhatsAppClient`` protocol, so it
is fully testable now with ``FakeWhatsAppClient`` (no network). The HTTP
implementation is a thin skeleton wired and integration-tested once the Meta
access token + phone number id arrive (a Fahad step) — no speculative,
untested payload code is shipped before it can be verified live.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: A reply button: either a bare label (id == title, both capped at 20 —
#: onboarding answers resolve by label) or an ``(id, title)`` pair where the
#: id is a machine token (Graph caps ids at 256, titles at 20).
ButtonSpec = str | Sequence[str]


def button_pair(spec: ButtonSpec) -> tuple[str, str]:
    if isinstance(spec, str):
        return spec[:20], spec[:20]
    ident, title = spec
    return str(ident)[:256], str(title)[:20]


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
        self, to_phone: str, body: str, buttons: Sequence[ButtonSpec]
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
        self, to_phone: str, body: str, buttons: Sequence[ButtonSpec]
    ) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage(
            to_phone, "interactive", mid, body=body,
            buttons=tuple(button_pair(b)[0] for b in buttons),
        ))
        return mid

    def download_media(self, media_id: str) -> tuple[bytes, str | None] | None:
        return self.media.get(media_id)


class WhatsAppSendError(RuntimeError):
    """Non-200 from the Graph API — message carries the HTTP status and the
    Graph error CODE only, never the body (it can quote message content)."""


class _RequestsTransport:  # pragma: no cover — exercised live in C4's gate
    def post(
        self, url: str, *, headers: dict[str, str],
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        import requests

        resp = requests.post(url, headers=headers, json=json, data=data,
                             files=files, timeout=60)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return resp.status_code, payload

    def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, Any]:
        import requests

        resp = requests.get(url, headers=headers, timeout=60)
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return resp.status_code, resp.json()
        return resp.status_code, resp.content


class HttpWhatsAppClient:
    """The real Graph Cloud API client. ``document_ref`` values are STORAGE
    KEYS (the C7 bundles carry them) — sending uploads the bytes to the
    /media endpoint first, then messages by media id. The transport is
    injectable so every payload shape is unit-asserted with zero network."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        base_url: str = "https://graph.facebook.com/v21.0",
        storage: Any = None,
        transport: Any = None,
    ) -> None:
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._base_url = base_url.rstrip("/")
        self._storage = storage
        self._transport = transport or _RequestsTransport()

    # ── plumbing ─────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _post_message(self, payload: dict[str, Any]) -> str:
        status, body = self._transport.post(
            f"{self._base_url}/{self._phone_number_id}/messages",
            headers=self._headers(),
            json={"messaging_product": "whatsapp", **payload},
        )
        if status != 200:
            code = (body.get("error") or {}).get("code", "?")
            raise WhatsAppSendError(f"graph HTTP {status} (code {code})")
        return str(body["messages"][0]["id"])

    # ── the protocol ─────────────────────────────────────────────────────

    def send_text(self, to_phone: str, body: str) -> str:
        return self._post_message(
            {"to": to_phone, "type": "text", "text": {"body": body}}
        )

    def send_interactive(
        self, to_phone: str, body: str, buttons: Sequence[ButtonSpec]
    ) -> str:
        pairs = [button_pair(spec) for spec in buttons[:3]]
        return self._post_message({
            "to": to_phone, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": ident, "title": title}}
                    for ident, title in pairs
                ]},
            },
        })

    def send_template(
        self, to_phone: str, template_name: str, language: str,
        variables: dict[str, str] | None = None, buttons: tuple[str, ...] = (),
    ) -> str:
        template: dict[str, Any] = {
            "name": template_name, "language": {"code": language},
        }
        if variables:
            template["components"] = [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": variables[key]}
                    for key in sorted(variables)
                ],
            }]
        return self._post_message(
            {"to": to_phone, "type": "template", "template": template}
        )

    def send_document(
        self, to_phone: str, document_ref: str, *, filename: str, caption: str = ""
    ) -> str:
        if self._storage is None:
            raise WhatsAppSendError("no storage wired for document refs")
        data = self._storage.get(document_ref)
        status, body = self._transport.post(
            f"{self._base_url}/{self._phone_number_id}/media",
            headers=self._headers(),
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, data, "application/pdf")},
        )
        if status != 200:
            code = (body.get("error") or {}).get("code", "?")
            raise WhatsAppSendError(f"graph media HTTP {status} (code {code})")
        media_id = str(body["id"])
        return self._post_message({
            "to": to_phone, "type": "document",
            "document": {"id": media_id, "filename": filename,
                         "caption": caption},
        })

    def download_media(self, media_id: str) -> tuple[bytes, str | None] | None:
        status, meta = self._transport.get(
            f"{self._base_url}/{media_id}", headers=self._headers()
        )
        if status != 200 or not isinstance(meta, dict) or "url" not in meta:
            return None
        # audit fix: the Bearer token rides this request — pin the scheme and
        # host to Meta's CDN so a poisoned url can never exfiltrate it.
        from urllib.parse import urlparse

        parsed = urlparse(str(meta["url"]))
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host.endswith(".fbcdn.net") or host.endswith(".facebook.com")
            or host.endswith(".whatsapp.net")
        ):
            return None
        status, blob = self._transport.get(
            str(meta["url"]), headers=self._headers()
        )
        if status != 200 or not isinstance(blob, bytes):
            return None
        return blob, None
