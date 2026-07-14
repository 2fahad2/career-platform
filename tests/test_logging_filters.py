"""Tests for secret redaction in logs (LEGACY §9.2, whitepaper §15.13).

No PII/secret may survive into a log record — message, args, or traceback.
"""

from __future__ import annotations

import logging

from career.logging_filters import (
    REDACTED,
    SecretRedactionFilter,
    mask_identifier,
    register_secret,
    safe_exception_summary,
    sanitize_secret_text,
    sanitize_secret_value,
)


class TestSanitizeText:
    def test_telegram_bot_url_keeps_endpoint(self) -> None:
        out = sanitize_secret_text("POST https://api.telegram.org/bot123456:AAH-secretTOKEN/sendMessage")
        assert "AAH-secretTOKEN" not in out
        assert "/sendMessage" in out
        assert f"/bot{REDACTED}" in out

    def test_url_query_token_redacted(self) -> None:
        out = sanitize_secret_text("GET https://graph.facebook.com/v20.0/x?access_token=EAABsecret&id=5")
        assert "EAABsecret" not in out
        assert "id=5" in out

    def test_kv_pairs_redacted(self) -> None:
        for raw in ("api_key=sk-abc123", "authorization: Bearer xyz789",
                    "client_secret = shh-9", "X-Salla-Signature: deadbeef"):
            out = sanitize_secret_text(raw)
            assert REDACTED in out
            for leak in ("sk-abc123", "xyz789", "shh-9", "deadbeef"):
                assert leak not in out

    def test_registered_secret_scrubbed_verbatim(self) -> None:
        register_secret("super-secret-value-1234")
        out = sanitize_secret_text("connecting with super-secret-value-1234 now")
        assert "super-secret-value-1234" not in out
        assert REDACTED in out

    def test_short_registered_secret_ignored(self) -> None:
        register_secret("abc")  # < 6 chars, not registered
        assert "abc" in sanitize_secret_text("value abc here")

    def test_plain_text_untouched(self) -> None:
        assert sanitize_secret_text("run completed for TEN-0007: 2 jobs") == (
            "run completed for TEN-0007: 2 jobs"
        )


class TestSanitizeValue:
    def test_dict_secret_keys_replaced(self) -> None:
        out = sanitize_secret_value({"api_key": "leak", "count": 3, "token": "t"})
        assert out == {"api_key": REDACTED, "count": 3, "token": REDACTED}

    def test_nested_structures(self) -> None:
        out = sanitize_secret_value({"outer": {"password": "p", "ok": [1, 2]}})
        assert out == {"outer": {"password": REDACTED, "ok": [1, 2]}}

    def test_numbers_bools_none_unchanged(self) -> None:
        assert sanitize_secret_value([1, True, None, 2.5]) == [1, True, None, 2.5]

    def test_tuple_shape_preserved(self) -> None:
        out = sanitize_secret_value(("api_key=leak", 7))
        assert isinstance(out, tuple)
        assert "leak" not in out[0] and out[1] == 7

    def test_cycle_guard(self) -> None:
        d: dict[str, object] = {}
        d["self"] = d
        out = sanitize_secret_value(d)
        assert out["self"] == REDACTED

    def test_depth_guard(self) -> None:
        deep: dict[str, object] = {}
        cur = deep
        for _ in range(10):
            nxt: dict[str, object] = {}
            cur["next"] = nxt
            cur = nxt
        # Should not raise; deep levels collapse to REDACTED.
        assert sanitize_secret_value(deep) is not None


class TestExceptionAndIdentifiers:
    def test_safe_exception_summary_scrubs(self) -> None:
        exc = ValueError("failed calling /bot999:SEKRET/send")
        out = safe_exception_summary(exc)
        assert "SEKRET" not in out
        assert out.startswith("ValueError:")

    def test_mask_identifier_never_renders(self) -> None:
        assert mask_identifier("+966500000000") == "[ID_REDACTED]"
        assert mask_identifier(123456789) == "[ID_REDACTED]"


class TestLoggingFilter:
    def test_filter_scrubs_message(self, caplog) -> None:
        logger = logging.getLogger("career.test.redact.msg")
        logger.addFilter(SecretRedactionFilter())
        with caplog.at_level(logging.INFO, logger="career.test.redact.msg"):
            logger.info("calling api_key=sk-leaky-42")
        assert "sk-leaky-42" not in caplog.text
        assert REDACTED in caplog.text

    def test_filter_scrubs_args(self, caplog) -> None:
        logger = logging.getLogger("career.test.redact.args")
        logger.addFilter(SecretRedactionFilter())
        with caplog.at_level(logging.INFO, logger="career.test.redact.args"):
            logger.info("request to %s", "https://api.telegram.org/bot7:TOKENX/m")
        assert "TOKENX" not in caplog.text

    def test_filter_scrubs_traceback(self, caplog) -> None:
        logger = logging.getLogger("career.test.redact.exc")
        logger.addFilter(SecretRedactionFilter())
        with caplog.at_level(logging.ERROR, logger="career.test.redact.exc"):
            try:
                raise RuntimeError("boom with token=verysecret99")
            except RuntimeError:
                logger.exception("handler failed")
        assert "verysecret99" not in caplog.text

    def test_filter_never_raises_on_bad_record(self) -> None:
        flt = SecretRedactionFilter()
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, object(), None, None)
        assert flt.filter(rec) is True
