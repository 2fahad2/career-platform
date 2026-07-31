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

# Credentials embedded in a DSN/URL userinfo — postgresql+psycopg://user:PW@host,
# redis://:PW@host, amqp://u:PW@h. SQLAlchemy/psycopg errors echo the whole DSN.
_URL_CREDENTIALS_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]{0,64}:)[^\s/@]{1,256}(?=@)"
)

# access_token / token / sig=… inside a URL query string. ``hub.verify_token``
# is the Meta webhook verification param — uvicorn's access log prints the full
# request line, query string included.
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:hub\.verify_token|verify_token|access_token|refresh_token|"
    r"token|api_key|apikey|key|sig|signature|auth|secret|client_secret|"
    r"password|passwd|pwd)=)[^&\s#]+"
)

# key=value / key: value secret pairs in free text. The name may carry any
# prefix (DB_PASSWORD, ANTHROPIC_API_KEY, x-salla-signature) — a bare \b would
# refuse to match "password" inside "db_password". The value may be quoted
# (JSON/dict dumps); the opening quote and an optional Bearer/Basic scheme word
# are preserved while the token itself is redacted. The value class excludes
# '&' and '#' so a URL query param does not swallow the following params.
_KV_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"([A-Za-z0-9_.\-]*(?:token|password|passwd|pwd|secret|api_?key|"
    r"authorization|signature|credential|auth))"
    r"(['\"]?\s*[=:]\s*)(['\"]?)(bearer\s+|basic\s+)?([^\s,'\"&#]+)"
)

# Provider token SHAPES we actually hold. These fire even when the value is
# logged bare, with no key next to it (a repr, an error body, a URL path):
#   sk-ant-…            Anthropic API key (the only LLM provider — locked)
#   EAA…                Meta / WhatsApp Cloud API access token
#   <digits>:AA…        Telegram bot token
#   ory_at_/ory_rt_…    Salla merchant access/refresh tokens (Ory-issued)
#   eyJ….….…            any JWT (Salla also hands out JWT-shaped tokens)
# SearchAPI.io keys are opaque alphanumerics with no distinguishing shape —
# they are covered by the key=value rule and by register_secret() (config.py
# registers every settings secret), NOT by a shape rule: a pattern loose
# enough to catch them would redact hashes, UUIDs and job ids too.
_PROVIDER_TOKEN_RE = re.compile(
    r"(?i)(?:"
    r"sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|EAA[A-Za-z0-9]{20,}"
    r"|(?<![\d.])\d{6,12}:AA[A-Za-z0-9_\-]{20,}"
    r"|ory_(?:at|rt|pat|st)_[A-Za-z0-9_.\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r")"
)

# "Authorization: Bearer <token>" survives the key=value rule only when the
# header name is present; httpx/graph reprs often show the scheme alone.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9\-._~+/=]{8,})")

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
    text = _URL_CREDENTIALS_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _URL_QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _KV_SECRET_RE.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4) or ''}{REDACTED}"
        ),
        text,
    )
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _PROVIDER_TOKEN_RE.sub(REDACTED, text)
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
    # Unknown object: scrub its rendering, but hand the ORIGINAL back when
    # nothing was redacted — a %d arg must not silently become a str.
    rendered = str(value)
    scrubbed = sanitize_secret_text(rendered)
    return value if scrubbed == rendered else scrubbed


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


# One shared filter instance: identity makes "already armed?" a cheap check
# and keeps a single object attached across every handler in the process.
_FILTER = SecretRedactionFilter()
_MARK = "_career_secret_redaction"


def _arm_handler(handler: logging.Handler) -> None:
    try:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(_FILTER)
    except Exception:  # noqa: BLE001,S110 — arming must never break startup
        pass


def _arm_existing_handlers() -> None:
    """Every handler reachable right now: root, every named logger, and the
    last-resort handler stdlib uses when nothing is configured at all."""
    seen: set[int] = set()
    loggers: list[logging.Logger] = [logging.getLogger()]
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):  # skip PlaceHolder
            loggers.append(obj)
    for logger_ in loggers:
        for handler in list(logger_.handlers):
            if id(handler) not in seen:
                seen.add(id(handler))
                _arm_handler(handler)
    if logging.lastResort is not None:
        _arm_handler(logging.lastResort)


def _patch_add_handler() -> None:
    """Arm every handler attached from now on, whoever attaches it.

    ``logging.basicConfig``, ``logging.config.dictConfig`` (uvicorn's own
    config) and hand-rolled setups all end up in ``Logger.addHandler``, so one
    hook here covers configuration that happens AFTER install — the exact
    failure this replaces, where basicConfig replaced the handler we filtered.
    """
    original = logging.Logger.addHandler
    if getattr(original, _MARK, False):
        return

    def add_handler(self: logging.Logger, hdlr: logging.Handler) -> None:
        original(self, hdlr)
        _arm_handler(hdlr)

    setattr(add_handler, _MARK, True)
    add_handler.__doc__ = original.__doc__
    logging.Logger.addHandler = add_handler  # type: ignore[method-assign]


def _patch_handler_handle() -> None:
    """Scrub inside ``Handler.handle`` — the invariant that cannot be lost.

    Filters live on handler objects, so any handler the process never sees at
    install time (created later, attached by ``logger.handlers.append``, owned
    by a library) escapes them. ``Handler.handle`` is the one funnel every
    emission crosses, whatever the configuration order, so redaction is hooked
    there once and holds for handlers that do not exist yet. This is also the
    only layer that reaches ``exc_info``: a traceback is scrubbed the moment
    something is about to write it, and not a moment earlier.
    """
    original = logging.Handler.handle
    if getattr(original, _MARK, False):
        return

    def handle(self: logging.Handler, record: logging.LogRecord) -> Any:
        _FILTER.filter(record)  # idempotent: re-scrubbing redacted text is a no-op
        return original(self, record)

    setattr(handle, _MARK, True)
    handle.__doc__ = original.__doc__
    logging.Handler.handle = handle  # type: ignore[method-assign]


def _patch_record_factory() -> None:
    """Scrub msg/args at record CREATION — the layer no handler can lose.

    Covers the one hole the handler layer cannot: a handler that never goes
    through ``addHandler`` (``logger.handlers.append``, a handler used
    directly, a third-party logging bridge). Traceback/stack scrubbing stays
    in the handler filter: formatting an exception here would cost work on
    records nobody emits and would strip ``exc_info`` from consumers that
    legitimately inspect it.
    """
    factory = logging.getLogRecordFactory()
    if getattr(factory, _MARK, False):
        return

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize_secret_text(record.msg)
            if record.args:
                record.args = sanitize_secret_value(record.args)
        except Exception:  # noqa: BLE001,S110 — never break logging
            pass
        return record

    setattr(record_factory, _MARK, True)
    logging.setLogRecordFactory(record_factory)


def install_secret_redaction(
    *, quiet_loggers: tuple[str, ...] = ("httpx", "httpcore")
) -> None:
    """Arm secret redaction process-wide — idempotent and ORDER-PROOF.

    Design (audit fix): the previous version only walked ``root.handlers`` at
    call time, so every entry point that called it BEFORE
    ``logging.basicConfig()`` filtered nothing — basicConfig then installed a
    fresh, unfiltered handler and every secret rode straight into the journal.
    Ordering is not something four entry points plus uvicorn can be trusted to
    keep, so redaction no longer depends on it. Four layers, weakest last:

    1. ``Handler.handle`` is hooked once: every record crossing any handler —
       including handlers that do not exist yet, and ``uvicorn.access`` with
       its ``propagate=False`` own handler — is scrubbed, exc_info included.
       This is what makes call order irrelevant.
    2. ``Logger.addHandler`` is hooked, so handlers attached later (basicConfig,
       ``dictConfig``) carry the filter object as well.
    3. Handlers that already exist are armed immediately, plus
       ``logging.lastResort``. 2 and 3 keep the conventional, auditable
       "filter rides on the handler" property visible in ``handler.filters``.
    4. The log-record factory scrubs msg/args at creation, covering a handler
       whose ``handle`` is overridden or whose ``emit`` is called directly.

    Called twice, it changes nothing (single shared filter, marked hooks), so
    entry points may call it early, late, or both. Chatty HTTP loggers are
    pinned to WARNING so their DEBUG/INFO payloads never emit; re-call after a
    reconfiguration that resets levels.
    """
    _patch_handler_handle()
    _patch_add_handler()
    _arm_existing_handlers()
    _patch_record_factory()
    for name in quiet_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
