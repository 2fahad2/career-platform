"""WhatsApp Cloud API client — the injectable send boundary.

The delivery/worker code depends only on the ``WhatsAppClient`` protocol, so it
is fully testable now with ``FakeWhatsAppClient`` (no network). The HTTP
implementation is a thin skeleton wired and integration-tested once the Meta
access token + phone number id arrive (a Fahad step) — no speculative,
untested payload code is shipped before it can be verified live.
"""

from __future__ import annotations

import logging
import math
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger("career.whatsapp.client")

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


#: The Graph version every call in this module speaks. One constant rather
#: than four literals: a token inspected against a different version than the
#: one that sends is an inspection of something else.
GRAPH_BASE_URL = "https://graph.facebook.com/v21.0"


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
        base_url: str = GRAPH_BASE_URL,
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
        # audit minor: every upload was pinned application/pdf — the privacy
        # export (بياناتي.json) arrived mislabeled and often unopenable.
        mime = {
            ".json": "application/json",
            ".pdf": "application/pdf",
            ".docx": ("application/vnd.openxmlformats-officedocument"
                      ".wordprocessingml.document"),
        }.get("." + filename.rsplit(".", 1)[-1].lower(), "application/pdf")
        status, body = self._transport.post(
            f"{self._base_url}/{self._phone_number_id}/media",
            headers=self._headers(),
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, data, mime)},
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


# ── is the credential we speak through still a credential? ──────────────────
#
# WHAT META ACTUALLY ISSUED US, measured on 2026-08-08 with a read-only
# `GET /debug_token` against the live staging value (no token, no id and no
# phone number is reproduced here or anywhere this code can print):
#
#     type                     SYSTEM_USER
#     expires_at               0        ← never
#     data_access_expires_at   0        ← never
#     is_valid                 true
#     scopes                   whatsapp_business_management,
#                              whatsapp_business_messaging,
#                              manage_app_solution,
#                              whatsapp_business_manage_events, public_profile
#
# SO THERE IS NOTHING TO RENEW, and this module deliberately does not renew
# anything. `expires_at: 0` is Meta's own spelling of «no expiry»: a System
# User token is not the ~1h short-lived user token, not the ~60d long-lived
# user token, and it has no `fb_exchange_token` refresh path — the exchange
# endpoint answers such a request with an error, so a refresher written for it
# would be a scheduled job that can only ever fail. `data_access_expires_at: 0`
# says the same about the OTHER clock Meta runs (Platform Terms data access,
# the 90-day one that applies to user tokens): it does not apply to us either.
# The «roughly two weeks» in the project notes was TRUE of the temporary token
# Fahad pasted in on 17 July and is no longer true of what is configured.
#
# WHAT CAN STILL TAKE IT AWAY, all of it instantaneous and none of it
# announced. This is the whole reason the check exists:
#
#   * the token is revoked — a human clicks it away in Business Settings, the
#     System User is deleted or demoted, or the app's secret is reset;
#   * the app is disabled, restricted or its WhatsApp product is removed;
#   * a SCOPE is withdrawn. The token stays `is_valid: true` and every send
#     starts coming back 4xx/#200 «permission denied». `is_valid` alone cannot
#     see this, which is why the scopes are asserted and not merely logged;
#   * the WABA or the phone number is UNASSIGNED from the System User. The
#     token is perfectly valid and perfectly useless: it can no longer address
#     the number it sends from. `debug_token` cannot see this at all, which is
#     why there is a second, equally read-only GET.
#
# Everything below is one GET, one optional GET, and arithmetic. Nothing here
# POSTs, nothing sends a message, and the transport is injectable so every
# branch is proved with zero network.


class TokenVerdict(StrEnum):
    """The headline. Details ride on :class:`TokenHealth`'s other fields."""

    #: Valid, never expires, every required scope present, the number we send
    #: from is addressable. The only state that says nothing to anyone.
    PERMANENT = "permanent"
    #: Valid and every scope present, but Meta gave it an expiry. THE
    #: REGRESSION GUARD: it means the permanent System User token was replaced
    #: by a temporary one — the exact shape of credential that died on 29 July
    #: and of the ~2-week token this product ran on through July. Nothing in
    #: the environment file would say so; `expires_at` does.
    DATED = "dated"
    #: Meta still honours the token, and it cannot do our job: a scope was
    #: withdrawn, or the phone number is no longer assigned to it.
    DEGRADED = "degraded"
    #: Meta refuses it. Every message to every customer fails from this
    #: instant, and no server-side action recovers it.
    INVALID = "invalid"
    #: Nothing configured to ask about.
    UNCONFIGURED = "unconfigured"
    #: Meta could not be asked. NOT a verdict about the token — see
    #: :func:`inspect_token` on why this never escalates into an alarm.
    UNREADABLE = "unreadable"


#: The two permissions a Cloud API send actually needs. `manage_app_solution`,
#: `whatsapp_business_manage_events` and `public_profile` are also present on
#: the live token and are deliberately NOT required: a check that fails when a
#: permission we do not use is tidied away is a check that cries wolf.
REQUIRED_SCOPES: tuple[str, ...] = (
    "whatsapp_business_messaging",
    "whatsapp_business_management",
)

#: Meta's OAuthException code for «this access token is not usable» — expired,
#: revoked, invalidated by a password or permission change. When the token
#: cannot even inspect ITSELF, this is the code that comes back, and it is a
#: VERDICT (the token is dead) rather than a failure to reach Meta.
GRAPH_INVALID_TOKEN_CODE = 190
#: The same statement one layer down: the token is fine, the permission is not.
GRAPH_PERMISSION_CODE = 200


@dataclass(frozen=True)
class TokenHealth:
    """What Meta says about the credential RIGHT NOW.

    Live state, never a configured constant — that distinction is the whole
    lesson of 2026-07-29, where `SALLA_TOKEN_EXPIRES_AT` was a date a human
    typed once and the credential it described had been dead for nine days.
    Every field here is read back from `/debug_token` on the run that uses it.

    It carries NO token, no phone number and no id, so the generated repr is
    safe in a traceback, an assertion diff and the journal (§15.13).
    """

    verdict: TokenVerdict
    #: Meta's own word: `SYSTEM_USER`, `USER`, `PAGE`, `APP`. Empty when the
    #: token could not be read.
    token_type: str = ""
    #: True when `expires_at` is 0 — Meta's spelling of «never».
    never_expires: bool = False
    #: Whole days from now until `expires_at`; negative when already past.
    #: None when it never expires, or when nothing could be measured.
    days_left: int | None = None
    #: True when `data_access_expires_at` is 0. For a System User token this
    #: is always the case; for a USER token it is the second clock, and it can
    #: run out while the token itself is still valid.
    data_access_never_expires: bool = True
    #: Required scopes Meta did NOT list back.
    missing_scopes: tuple[str, ...] = ()
    #: Could this token still address the phone number id we send from?
    #: None when no id was configured to try, or the try was inconclusive.
    number_addressable: bool | None = None
    #: Meta's numeric error code when the inspection itself was refused.
    error_code: int | None = None

    @property
    def readable(self) -> bool:
        """Was Meta answered at all? Callers degrade on False and never alarm."""
        return self.verdict is not TokenVerdict.UNREADABLE

    @property
    def healthy(self) -> bool:
        return self.verdict is TokenVerdict.PERMANENT


def _debug_token_url(base_url: str, token: str) -> str:
    # `input_token` HAS to travel in the query string — Graph takes the token
    # under inspection nowhere else. The token doing the inspecting travels in
    # the Authorization header instead of the documented `access_token=`
    # parameter, so only one copy is ever in a URL; `get_settings()` registers
    # WHATSAPP_ACCESS_TOKEN with the log scrubber, which redacts the other.
    #
    # Self-inspection (a token debugging itself) rather than the app token
    # `APP_ID|APP_SECRET`: both were tried live and answer identically, and
    # the app-token form would need META_APP_ID and WHATSAPP_APP_SECRET in
    # `Settings` for no extra fact. It also has a property the app token does
    # not — a dead token cannot inspect itself, so the refusal IS the answer.
    query = urllib.parse.urlencode({"input_token": token})
    return f"{base_url.rstrip('/')}/debug_token?{query}"


def _graph_error(payload: Any) -> tuple[int | None, str]:
    if not isinstance(payload, dict):
        return None, ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, ""
    code = error.get("code")
    return (int(code) if isinstance(code, int) else None), str(error.get("type", ""))


def inspect_token(
    access_token: str,
    phone_number_id: str = "",
    *,
    now: datetime | None = None,
    base_url: str = GRAPH_BASE_URL,
    transport: Any = None,
    attempts: int = 3,
) -> TokenHealth:
    """Ask Meta what our credential is, and never assume.

    WHY IT RETRIES, WHERE `scripts/refresh_salla_token.py` REFUSES TO. That
    script must not repeat its call because Salla's refresh tokens are
    single-use and a second exchange revokes the installation. This is a
    read-only GET: repeating it changes nothing, costs nothing and cannot
    rotate anything. So a blip on Meta's side or on ours is retried in-process
    rather than turned into a page, which is the difference between a check the
    operator trusts and one he mutes.

    WHY AN UNREADABLE ANSWER IS NEVER AN ALARM. «We could not reach Meta» is a
    statement about the network, and the credential is overwhelmingly likely to
    be exactly as valid as it was a minute ago. Alarming on it would put
    Meta's uptime on Fahad's phone in the middle of the night with no action
    attached, and the state that DOES need him — a revoked token — is
    indistinguishable to a human reading a noisy channel. The caller reports
    the transient fact through its exit code (systemd's `OnFailure=`,
    deduplicated) and says nothing on Telegram.
    """
    now = now or datetime.now(UTC)
    token = (access_token or "").strip()
    if not token:
        return TokenHealth(TokenVerdict.UNCONFIGURED)

    transport = transport or _RequestsTransport()
    headers = {"Authorization": f"Bearer {token}"}
    url = _debug_token_url(base_url, token)

    status: int | None = None
    payload: Any = None
    for attempt in range(max(1, attempts)):
        try:
            status, payload = transport.get(url, headers=headers)
        except Exception:  # noqa: BLE001 — the network is not a verdict
            logger.info("debug_token attempt %d could not reach Meta",
                        attempt + 1, exc_info=True)
            status, payload = None, None
            continue
        if status == 200:
            break
        code, _kind = _graph_error(payload)
        if code in (GRAPH_INVALID_TOKEN_CODE, GRAPH_PERMISSION_CODE):
            # Meta answered, and the answer is «no». Retrying a verdict is how
            # a check spends three timeouts to learn what it already knew.
            return TokenHealth(TokenVerdict.INVALID, error_code=code)
        if status is not None and 400 <= status < 500:
            # A 4xx we do not have a name for is still Meta refusing to tell
            # us anything about this token, and «we cannot inspect the
            # credential» is not a state to sit on quietly.
            return TokenHealth(TokenVerdict.INVALID, error_code=code)

    if status != 200:
        return TokenHealth(TokenVerdict.UNREADABLE)

    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return TokenHealth(TokenVerdict.UNREADABLE)

    if not data.get("is_valid", False):
        code, _kind = _graph_error(data)
        return TokenHealth(
            TokenVerdict.INVALID,
            token_type=str(data.get("type", "")),
            error_code=code,
        )

    expires_at = data.get("expires_at")
    expires_at = int(expires_at) if isinstance(expires_at, int) else 0
    data_access = data.get("data_access_expires_at")
    data_access = int(data_access) if isinstance(data_access, int) else 0
    never = expires_at == 0
    days_left = None if never else math.floor(
        (expires_at - now.timestamp()) / 86400.0
    )

    scopes = data.get("scopes")
    listed = {str(s) for s in scopes} if isinstance(scopes, list) else set()
    missing = tuple(s for s in REQUIRED_SCOPES if s not in listed)

    addressable = _number_addressable(
        phone_number_id, base_url=base_url, headers=headers, transport=transport
    )

    if missing or addressable is False:
        verdict = TokenVerdict.DEGRADED
    elif not never:
        verdict = TokenVerdict.DATED
    else:
        verdict = TokenVerdict.PERMANENT
    return TokenHealth(
        verdict,
        token_type=str(data.get("type", "")),
        never_expires=never,
        days_left=days_left,
        data_access_never_expires=data_access == 0,
        missing_scopes=missing,
        number_addressable=addressable,
    )


def _number_addressable(
    phone_number_id: str, *, base_url: str, headers: dict[str, str],
    transport: Any,
) -> bool | None:
    """Can this token still READ the number it sends from?

    The one failure `/debug_token` is blind to. Unassign the phone number (or
    the whole WABA) from the System User and the token stays valid, keeps its
    scopes, and every single send comes back refused — a total outage that an
    `is_valid` check would certify as healthy.

    `?fields=id` on purpose: the cheapest possible proof of access, and it
    brings back nothing that must not be logged. A non-2xx is «no»; a network
    failure is None, because an unreachable Meta already had its say above.
    """
    ident = (phone_number_id or "").strip()
    if not ident:
        return None
    try:
        status, _body = transport.get(
            f"{base_url.rstrip('/')}/{ident}?fields=id", headers=headers
        )
    except Exception:  # noqa: BLE001 — inconclusive, never a verdict
        logger.info("could not read the sending number", exc_info=True)
        return None
    if status == 200:
        return True
    if status is not None and 400 <= status < 500:
        return False
    return None


# ── the ladder: what a human is supposed to DO about it ─────────────────────
#
# The wording lives HERE, at the Graph boundary, rather than in
# `career.engine.cli` where the boot check consumes it, and that is a
# structural choice: `scripts/check_whatsapp_token.py` logs the same English
# sentences, and importing `engine.cli` to reach them would put a watchdog
# that touches no database on the wrong side of the D20 owner-role ratchet
# (tests/test_rls_runtime_role.py closes over import edges). One ladder, two
# consumers, neither of them inheriting superuser reach for four strings.


@dataclass(frozen=True)
class TokenProblem:
    """One provable statement about the live credential.

    Deliberately shaped like `cli.EnvProblem` (`key` names the variable a human
    edits, `english` goes to the journal, `arabic` to the admin channel) so the
    boot check is a one-line adapter — and deliberately NOT that class, so this
    module keeps its independence from the nightly CLI.

    THE ARABIC HALF CARRIES NO LATIN AND NO DIGITS. Fahad's client reverses any
    line that mixes scripts, and Latin digits count as Latin
    (tests/test_alert_direction_purity.py). Numbers live in the English half;
    `format_env_alert` lists the variable names separately.
    """

    key: str
    english: str
    arabic: str


#: The only recovery that exists, and it is entirely manual: Business Settings
#: → System users → Generate new token → the app, both WhatsApp permissions,
#: expiry «Never» → paste into the environment file → restart. A System User
#: token has no `fb_exchange_token` path, so no server-side step can be offered
#: here the way one could be for a 60-day user token.
_FRESH_CREDENTIAL_AR = (
    "الحل: أنشئ رمزًا جديدًا لمستخدم النظام من إعدادات أعمال ميتا بصلاحية "
    "دائمة، ثم ضعه في ملف الإعدادات وأعد تشغيل الخدمة"
)
_FRESH_CREDENTIAL_HINT = (
    "generate a fresh System User token in Meta Business Settings with both "
    "WhatsApp permissions and expiry «Never», put it in the environment file "
    "and restart — a System User token has no refresh endpoint, so there is "
    "nothing a server can do"
)

#: Days of runway at or below which a DATED token is urgent. See
#: `scripts/check_whatsapp_token.HANDOVER_DAYS` for why this is a HUMAN's
#: response time and not the Salla refresher's retry budget.
DEFAULT_HANDOVER_DAYS = 7


def token_problems(
    health: TokenHealth | None, warn_days: int = DEFAULT_HANDOVER_DAYS,
) -> list[TokenProblem]:
    """Everything wrong with the live credential, worst first.

    WHERE IT REUSES `cli._stored_token_problems` AND WHERE IT DIVERGES. Same
    shape, on purpose: measure what REMAINS rather than what was configured,
    escalate as the margin shrinks instead of repeating, name what to DO, and
    never be fatal — a boot check that can refuse a boot turns «sales are
    stopped» into «the product is down for the people who already paid».

    It diverges on the SILENT rung. Salla says nothing above its five-day
    margin because a refresher is actively working inside it and four more
    attempts are still coming; here nothing is working on our behalf at all,
    so a dated token is reported at ANY runway. On this credential a countdown
    is not a schedule — it is a regression, and the day it appears is the day
    to hear about it.
    """
    if health is None or not health.readable:
        return []
    problems: list[TokenProblem] = []

    if health.verdict is TokenVerdict.INVALID:
        code = f" (Graph error {health.error_code})" if health.error_code else ""
        return [TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            f"META REFUSES IT{code} — it has been revoked or invalidated, so "
            "every message to every customer fails right now: no daily CV, no "
            f"activation welcome, no renewal notice. {_FRESH_CREDENTIAL_HINT}",
            f"ميتا ترفض رمز واتساب — لا شيء يصل أي عميل الآن. {_FRESH_CREDENTIAL_AR}",
        )]

    if health.missing_scopes:
        problems.append(TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            "Meta still honours the token but it no longer carries "
            f"{', '.join(health.missing_scopes)} — sends are refused with a "
            f"permission error, not a token error. {_FRESH_CREDENTIAL_HINT}",
            f"رمز واتساب فقد صلاحية الإرسال — كل رسالة سترفضها ميتا. "
            f"{_FRESH_CREDENTIAL_AR}",
        ))

    if health.number_addressable is False:
        problems.append(TokenProblem(
            "WHATSAPP_PHONE_NUMBER_ID",
            "the token is valid but can no longer read the number it sends "
            "from — the number or its WABA was unassigned from the System "
            "User. Re-assign it under the business's WhatsApp accounts; "
            "nothing about the token itself is wrong",
            "الرمز سليم لكنه لم يعد يملك رقم الإرسال — أعد ربط الرقم بحساب "
            "واتساب للأعمال في إعدادات أعمال ميتا",
        ))

    if not health.never_expires:
        problems.append(_dated_token_problem(health, warn_days))
    elif not health.data_access_never_expires:
        # `expires_at: 0` WITH a data-access clock set is the signature of a
        # long-lived USER token rather than a System User one: it authenticates
        # long after it may still read what it was granted, and the failure
        # lands months later as refusals nobody connects to a credential.
        problems.append(TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            f"reports type {health.token_type or 'UNKNOWN'} with no expiry but "
            "a data-access clock that DOES run out — this is not the permanent "
            f"System User token this product is configured around. "
            f"{_FRESH_CREDENTIAL_HINT}",
            f"رمز واتساب الحالي ليس الرمز الدائم المطلوب. {_FRESH_CREDENTIAL_AR}",
        ))
    return problems


def _dated_token_problem(health: TokenHealth, warn_days: int) -> TokenProblem:
    """The regression rung: a credential that counts down again.

    This is the one that matters most in practice. The permanent token cannot
    expire, so the realistic way this product goes dark on a clock is that
    somebody pastes a 60-day user token over it — which is exactly what was
    configured through July. Nothing in the environment file would say so.
    ``expires_at`` says so, on every run, for free.
    """
    days_left = health.days_left
    kind = health.token_type or "UNKNOWN"
    if days_left is None:
        return TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            f"has an expiry Meta would not quantify (type {kind}) — the "
            "permanent System User token this product runs on reports "
            f"expires_at 0. {_FRESH_CREDENTIAL_HINT}",
            f"رمز واتساب الحالي له تاريخ انتهاء ولا نعرف كم بقي منه. "
            f"{_FRESH_CREDENTIAL_AR}",
        )
    if days_left < 0:
        return TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            f"EXPIRED {-days_left} days ago (type {kind}) — every customer is "
            f"receiving nothing and has been for {-days_left} days. "
            f"{_FRESH_CREDENTIAL_HINT}",
            f"رمز واتساب منتهي ولا يصل العملاء شيء. {_FRESH_CREDENTIAL_AR}",
        )
    if days_left <= 1:
        return TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            f"expires in {days_left} days (type {kind}) — delivery stops within "
            f"the day and nothing renews it automatically. {_FRESH_CREDENTIAL_HINT}",
            f"رمز واتساب يوشك أن ينتهي والتسليم سيتوقف. {_FRESH_CREDENTIAL_AR}",
        )
    if days_left <= warn_days:
        return TokenProblem(
            "WHATSAPP_ACCESS_TOKEN",
            f"expires in {days_left} days (type {kind}) and no refresh path "
            f"exists for it. {_FRESH_CREDENTIAL_HINT}",
            f"رمز واتساب قارب على الانتهاء ولا يوجد تجديد تلقائي له. "
            f"{_FRESH_CREDENTIAL_AR}",
        )
    return TokenProblem(
        "WHATSAPP_ACCESS_TOKEN",
        f"is a TEMPORARY credential: type {kind}, {days_left} days left. The "
        "permanent System User token it replaced reported expires_at 0 and "
        "could not run out; this one puts the whole product back on a clock "
        f"with nothing scheduled to wind it. {_FRESH_CREDENTIAL_HINT}",
        f"رمز واتساب الحالي مؤقت وليس دائمًا، وسينتهي ويوقف التسليم. "
        f"{_FRESH_CREDENTIAL_AR}",
    )
