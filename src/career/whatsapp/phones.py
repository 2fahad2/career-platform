"""Phone-shape tolerance for channel lookups.

LIVE BUG (26 July): Meta delivers ``from`` WITHOUT the leading ``+``, so
``customer_channels.phone_e164`` holds ``9665…`` — while operator settings
(``CANARY_TEST_PHONE``) are written the human way, ``+9665…``. Every lookup
that compared the two literally never matched, which silently disabled:

* the evening window-nudge (the operator was never reminded to keep the 24h
  window open) — three delivery days expired unopened 21–23 July;
* the §14 canary-first ordering in the nightly run.

Lookups now compare against every plausible spelling of the same number.
"""

from __future__ import annotations


def phone_variants(raw: str | None) -> list[str]:
    """Every spelling of one number that may sit in the DB: as given, without
    the ``+``, and with it. Empty list for empty input (never matches)."""
    if not raw:
        return []
    stripped = str(raw).strip()
    digits = stripped.lstrip("+")
    if not digits:
        return []
    return list(dict.fromkeys([stripped, digits, f"+{digits}"]))


def same_phone(a: str | None, b: str | None) -> bool:
    """True when both strings denote the same number regardless of ``+``."""
    if not a or not b:
        return False
    return str(a).strip().lstrip("+") == str(b).strip().lstrip("+")
