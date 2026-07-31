"""Secret redaction must survive the WIRING, not just exist as a class (§15.13).

The audit found ``install_secret_redaction()`` armed nothing in three of four
entry points: they called it BEFORE ``logging.basicConfig()``, which then
installed a fresh unfiltered handler. A unit test of the filter class passed
happily while every secret rode into the journal in clear.

So these tests emit real-SHAPED (never real-valued) secrets through each entry
point's ACTUAL logging setup — the real module, the real statements, in a real
subprocess — and assert the bytes that reach stdout/stderr are redacted. Each
entry point's own setup path is exercised:

  * scripts/run_worker_loop.py   — systemd career-worker.service
  * scripts/run_admin_bot.py     — systemd career-admin-bot.service
  * src/career/engine/cli.py     — systemd career-engine-nightly.service
                                   (scripts/run_nightly.py is a shim over it)
  * src/career/main.py under uvicorn's own dictConfig — the API container

Subprocesses, not in-process fixtures: logging state is process-global, and an
in-process test would pollute (or be polluted by) the rest of the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from career.logging_filters import REDACTED, sanitize_secret_text

REPO = Path(__file__).resolve().parents[1]

# Fake values with the real SHAPE of every credential we hold. None of these is
# a live secret and none is read from .env — the shape is the whole point.
ANTHROPIC = "sk-ant-api03-FAKEfakeFAKE0000000000000000AA"
META = "EAAFAKEfake0000000000000000000000abcdef"
TELEGRAM = "123456789:AAFfakeFAKE00000000000000000000000000"
SALLA_ORY = "ory_at_FAKEfake0000000000000000000000"
SALLA_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlLTAwMCJ9.SIGfakeSIG0000"
SEARCHAPI = "FAKEsearchapikey00000000000000000000abcd"
DB_PASSWORD = "FAKEdbpassword123"
DSN = f"postgresql+psycopg://career_app:{DB_PASSWORD}@127.0.0.1:5433/career"
WA_VERIFY = "FAKEverifytoken12345"

ALL_SECRETS = (
    ANTHROPIC, META, TELEGRAM, SALLA_ORY, SALLA_JWT, DB_PASSWORD, WA_VERIFY,
)

# One log line carrying every credential family, in the shapes they really
# appear in: bare token, key=value, DSN, URL query, Bearer header.
LEAKY_LINE = (
    f"boot: anthropic={ANTHROPIC} meta_token={META} telegram {TELEGRAM} "
    f"salla {SALLA_ORY} jwt {SALLA_JWT} SEARCHAPI_API_KEY={SEARCHAPI} "
    f"DB_PASSWORD={DB_PASSWORD} dsn={DSN} "
    f"authorization: Bearer {SALLA_JWT}"
)
LEAKY_URL = (
    "https://graph.facebook.com/v20.0/1/messages"
    f"?access_token={META}&hub.verify_token={WA_VERIFY}"
)

_PROBE_TAIL = f"""
import logging
log = logging.getLogger("career.probe.wiring")
log.error({LEAKY_LINE!r})
log.error("upstream call %s", {LEAKY_URL!r})
try:
    raise RuntimeError("failed with token=" + {ANTHROPIC!r})
except RuntimeError:
    log.exception("handler blew up")
logging.shutdown()
"""


def _run(code: str) -> str:
    """Run a probe in a clean interpreter; return stdout+stderr together."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    proc = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [sys.executable, "-c", code],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180,
    )
    return proc.stdout + proc.stderr


def _assert_clean(output: str, *, secrets: tuple[str, ...] = ALL_SECRETS) -> None:
    assert output.strip(), "probe produced no output at all — it never logged"
    for secret in secrets:
        assert secret not in output, f"LEAKED {secret[:12]}… in:\n{output}"
    assert REDACTED in output, f"nothing was redacted at all:\n{output}"


def _script_probe(path: str, *, call: str = "mod.main()") -> str:
    """Load a real entry-point module, stop it right after its logging setup
    (its first ``get_settings()`` call), then try to leak."""
    return f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("entry_under_test", {path!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["entry_under_test"] = mod
spec.loader.exec_module(mod)

class _StopAfterLoggingSetup(Exception):
    pass

def _boom(*a, **k):
    raise _StopAfterLoggingSetup

# Stop at the first line after logging setup; never touch DB/Redis/network.
mod.get_settings = _boom
# The single-instance flock is unrelated to logging and may be held by the
# live systemd unit on this host.
if hasattr(mod, "_acquire_single_instance_lock"):
    mod._acquire_single_instance_lock = lambda name: None
try:
    {call}
except _StopAfterLoggingSetup:
    pass
""" + _PROBE_TAIL


class TestEntryPointWiring:
    """Each of these FAILED before the fix (install ran before basicConfig)."""

    def test_worker_loop_entry_point_redacts(self) -> None:
        out = _run(_script_probe(str(REPO / "scripts" / "run_worker_loop.py")))
        _assert_clean(out)

    def test_admin_bot_entry_point_redacts(self) -> None:
        out = _run(_script_probe(str(REPO / "scripts" / "run_admin_bot.py")))
        _assert_clean(out)

    def test_engine_cli_entry_point_redacts(self) -> None:
        out = _run(_script_probe(
            str(REPO / "src" / "career" / "engine" / "cli.py"),
            call="mod.main([])",
        ))
        _assert_clean(out)

    def test_nightly_shim_uses_the_cli_setup(self) -> None:
        """scripts/run_nightly.py is a shim — prove it inherits the CLI path."""
        src = (REPO / "scripts" / "run_nightly.py").read_text()
        assert "from career.engine.cli import main" in src

    def test_api_under_uvicorn_logging_config_redacts(self) -> None:
        """uvicorn applies its dictConfig BEFORE importing the app, and gives
        ``uvicorn.access`` its own handler with propagate=False — a root-only
        filter never sees the request line (query string included)."""
        out = _run(f"""
import logging, logging.config
import uvicorn.config
logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
import career.main  # noqa: F401 — module-level basicConfig + install
logging.getLogger("uvicorn.access").info(
    '%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET",
    "/webhooks/whatsapp?hub.verify_token={WA_VERIFY}&hub.challenge=1", "1.1", 200,
)
logging.getLogger("uvicorn.error").error("upstream %s", {LEAKY_URL!r})
""" + _PROBE_TAIL)
        _assert_clean(out)
        assert "hub.challenge=1" in out, "redaction ate the non-secret params"


class TestOrderIndependence:
    """The regression itself: configuration AFTER install must stay redacted."""

    def test_basic_config_after_install_is_still_redacted(self) -> None:
        out = _run("""
import logging
from career.logging_filters import install_secret_redaction
install_secret_redaction()          # the old, broken order
logging.basicConfig(level=logging.INFO)
""" + _PROBE_TAIL)
        _assert_clean(out)

    def test_basic_config_force_replacing_handlers_is_still_redacted(self) -> None:
        out = _run("""
import logging
from career.logging_filters import install_secret_redaction
logging.basicConfig(level=logging.INFO)
install_secret_redaction()
logging.basicConfig(level=logging.INFO, force=True)   # blows the handler away
""" + _PROBE_TAIL)
        _assert_clean(out)

    def test_handler_added_by_hand_after_install_is_redacted(self) -> None:
        out = _run("""
import logging, sys
from career.logging_filters import install_secret_redaction
install_secret_redaction()
h = logging.StreamHandler(sys.stderr)
logging.getLogger("career.probe.wiring").addHandler(h)
logging.getLogger("career.probe.wiring").setLevel(logging.INFO)
""" + _PROBE_TAIL)
        _assert_clean(out)

    def test_handler_bypassing_add_handler_is_still_redacted(self) -> None:
        """``logger.handlers.append`` skips the addHandler hook entirely — the
        ``Handler.handle`` and record-factory layers keep this one clean."""
        out = _run("""
import logging, sys
from career.logging_filters import install_secret_redaction
install_secret_redaction()
lg = logging.getLogger("career.probe.wiring")
lg.handlers.append(logging.StreamHandler(sys.stderr))  # no addHandler call
lg.setLevel(logging.INFO)
""" + _PROBE_TAIL)
        _assert_clean(out)

    def test_install_is_idempotent(self) -> None:
        out = _run("""
import logging
from career.logging_filters import (
    SecretRedactionFilter, install_secret_redaction,
)
logging.basicConfig(level=logging.INFO)
for _ in range(3):
    install_secret_redaction()
h = logging.getLogger().handlers[0]
n = sum(isinstance(f, SecretRedactionFilter) for f in h.filters)
factory = logging.getLogRecordFactory()
print("filters=%d" % n)
print("factory_wrapped_once=%s" % (factory.__name__ == "record_factory",))
""")
        assert "filters=1" in out
        assert "factory_wrapped_once=True" in out

    def test_no_handlers_at_all_last_resort_is_redacted(self) -> None:
        """Nothing configured: stdlib emits via ``logging.lastResort``."""
        out = _run("""
from career.logging_filters import install_secret_redaction
install_secret_redaction()
""" + _PROBE_TAIL)
        _assert_clean(out)


class TestCredentialShapeCoverage:
    """Every credential family we hold, logged bare (no key beside it)."""

    @pytest.mark.parametrize(
        ("name", "raw"),
        [
            ("anthropic", f"claude call failed for {ANTHROPIC}"),
            ("meta_whatsapp", f"graph rejected {META}"),
            ("telegram", f"bot auth {TELEGRAM} refused"),
            ("salla_ory", f"salla token {SALLA_ORY} expired"),
            ("salla_jwt", f"salla token {SALLA_JWT} expired"),
            ("db_dsn_password", f"could not connect: {DSN}"),
            ("bearer_header", f"headers: 'authorization': 'Bearer {SALLA_JWT}'"),
            ("meta_url_query", LEAKY_URL),
        ],
    )
    def test_bare_credential_shape_is_redacted(self, name: str, raw: str) -> None:
        out = sanitize_secret_text(raw)
        for secret in (ANTHROPIC, META, TELEGRAM, SALLA_ORY, SALLA_JWT,
                       DB_PASSWORD, WA_VERIFY):
            assert secret not in out, f"{name} leaked {secret[:10]}…"
        assert REDACTED in out

    @pytest.mark.parametrize(
        "raw",
        [
            f"DB_PASSWORD={DB_PASSWORD}",
            f"DB_OWNER_PASSWORD={DB_PASSWORD}",
            f"ANTHROPIC_API_KEY={SEARCHAPI}",
            f"SEARCHAPI_API_KEY={SEARCHAPI}",
            f"SALLA_WEBHOOK_SECRET={SEARCHAPI}",
            f"TELEGRAM_ADMIN_BOT_TOKEN={TELEGRAM}",
            f'{{"searchapi_api_key": "{SEARCHAPI}"}}',
            f"redis://:{DB_PASSWORD}@127.0.0.1:6380/0",
        ],
    )
    def test_prefixed_env_style_names_are_redacted(self, raw: str) -> None:
        """A bare ``\\b`` before "password" never matched "DB_PASSWORD"."""
        out = sanitize_secret_text(raw)
        assert DB_PASSWORD not in out
        assert SEARCHAPI not in out
        assert TELEGRAM not in out
        assert REDACTED in out

    def test_operational_text_survives(self) -> None:
        """Redaction must not eat the journal we actually read."""
        for benign in (
            "run completed for TEN-0007: 2 jobs delivered",
            "GET /health 200 in 12ms",
            "discovery_failed: every source down (3 attempts)",
            "https://example.com/careers/job/12345",
        ):
            assert sanitize_secret_text(benign) == benign
