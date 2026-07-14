"""Secret redaction for logs — provider-agnostic port of LEGACY §9.2 (MR-3).

Ports the Telegram secret-redaction filter and generalizes it to every channel
(Telegram admin bot, WhatsApp Cloud API, Salla) — whitepaper §15.13: no secret
in any log, and the filter riding on handlers so records propagating up from
httpx/httpcore are scrubbed too.

Deterministic, stdlib-only, side-effect free. It cannot remediate *historical*
logs — a leaked token must be rotated. Register known secret literals via
``register_secret()`` so accidental logging of the exact value is also scrubbed.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

REDACTED = "[REDACTED]"
ID_REDACTED = "[ID_REDACTED]"

# Token-bearing Telegram Bot API URL segment (/bot<TOKEN>/method). Keep the
# endpoint, drop the token.
_BOT_URL_TOKEN_RE = re.compile(r"(?i)(/bot)[A-Za-z0-9_:%\-]+(?=/|\b)")

# access_token / token / sig=… inside a URL query string.
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|token|api_key|apikey|sig|signature|"
    r"auth|secret)=)[^&\s#]+"
)

# key=value / key: value secret pairs in free text. The value class excludes
# '&' and '#' so a URL query param does not swallow the following params; an
# optional Bearer/Basic scheme word is preserved while its token is redacted.
_KV_SECRET_RE = re.compile(
    r"(?i)\b(telegram_bot_token|bot_token|access_token|refresh_token|token|"
    r"authorization|api_key|apikey|secret|password|webhook_secret|"
    r"x-salla-signature|signature|client_secret)"
    r"(\s*[=:]\s*)(bearer\s+|basic\s+)?([^\s,'\"&#]+)"
)

# dict keys whose VALUES are replaced wholesale when redacting structures.
_SECRET_KEY_SUBSTRINGS = (
    "token", "authorization", "api_key", "apikey", "secret", "password",
    "cookie", "signature", "credential",
)

_MAX_DEPTH = 6

# Exact secret literals to scrub verbatim wherever they appear (populated at
# runtime from settings as real keys are loaded).
_KNOWN_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret string to be scrubbed verbatim from all logs."""
    if value and isinstance(value, str) and len(value) >= 6:
        _KNOWN_SECRETS.add(value)


def sanitize_secret_text(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    text = _BOT_URL_TOKEN_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _URL_QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _KV_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{REDACTED}", text
    )
    for secret in _KNOWN_SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def sanitize_secret_value(value: Any, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """Recurse dict/list/tuple/set preserving shape; depth- and cycle-guarded.
    Dict keys matching a secret substring have their value replaced wholesale."""
    if _seen is None:
        _seen = set()
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return sanitize_secret_text(value)
    obj_id = id(value)
    if obj_id in _seen:
        return REDACTED  # cycle
    if isinstance(value, dict):
        _seen.add(obj_id)
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key_l = str(k).lower()
            if any(sub in key_l for sub in _SECRET_KEY_SUBSTRINGS):
                out[k] = REDACTED
            else:
                out[k] = sanitize_secret_value(v, _depth + 1, _seen)
        return out
    if isinstance(value, (list, tuple, set)):
        _seen.add(obj_id)
        items = [sanitize_secret_value(v, _depth + 1, _seen) for v in value]
        if isinstance(value, tuple):
            return tuple(items)
        if isinstance(value, set):
            return set(items)
        return items
    return sanitize_secret_text(value)


def safe_exception_summary(exc: BaseException) -> str:
    """A scrubbed one-line summary — str(exc) may embed a token-bearing URL."""
    return sanitize_secret_text(f"{type(exc).__name__}: {exc}")


def mask_identifier(_value: Any) -> str:
    """Chat/phone identifiers are never rendered (§15.13)."""
    return ID_REDACTED


class SecretRedactionFilter(logging.Filter):
    """Attach to HANDLERS (not just loggers) so records propagating up from
    httpx/httpcore are scrubbed. A filter must never drop or raise on a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize_secret_text(record.msg)
            if record.args:
                record.args = sanitize_secret_value(record.args)  # httpx passes url as %s arg
            if record.exc_info:
                formatted = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = sanitize_secret_text(formatted)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = sanitize_secret_text(record.exc_text)
            if getattr(record, "stack_info", None):
                record.stack_info = sanitize_secret_text(record.stack_info)
        except Exception:  # noqa: BLE001,S110 — a filter must never break logging
            pass
        return True


def install_secret_redaction(*, quiet_loggers: tuple[str, ...] = ("httpx", "httpcore")) -> None:
    """Attach the redaction filter to every root handler and pin chatty HTTP
    loggers to WARNING so their DEBUG/INFO payloads never emit."""
    root = logging.getLogger()
    flt = SecretRedactionFilter()
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(flt)
    for name in quiet_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
