"""Consent logic acceptance tests (whitepaper §05/§12) — written before the code.

Four purposes, separate by design: basic_processing, external_providers and
daily_messages are required to run the service; anonymous_stats is optional
and independent. Consent state is derived from the append-only event history
(latest event per purpose wins). Processing gates fail closed: no CV handling
before the required consents exist.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from career.db.session import tenant_session
from career.onboarding import consents

# ── purposes are data (whitepaper §05: منفصلة بالغرض) ────────────────────────


def test_the_four_documented_purposes_in_order() -> None:
    keys = [p.key for p in consents.PURPOSES]
    assert keys == [
        "basic_processing",
        "external_providers",
        "daily_messages",
        "anonymous_stats",
    ]


def test_only_anonymous_stats_is_optional() -> None:
    required = {p.key for p in consents.PURPOSES if p.required}
    assert required == {"basic_processing", "external_providers", "daily_messages"}
    assert not consents.purpose("anonymous_stats").required


def test_every_purpose_has_arabic_copy_and_rights_are_displayed() -> None:
    for p in consents.PURPOSES:
        assert p.title_ar.strip()
        assert p.description_ar.strip()
    # حقوق السحب والحذف والاحتفاظ ورابط السياسة تُعرض نصًا
    assert "سحب" in consents.RIGHTS_TEXT_AR
    assert "حذف" in consents.RIGHTS_TEXT_AR


# ── pure state derivation: latest event per purpose wins ─────────────────────


def _ev(purpose: str, action: str, seq: int) -> consents.ConsentRecord:
    return consents.ConsentRecord(purpose=purpose, action=action, sequence=seq)


def test_granted_then_withdrawn_is_off() -> None:
    state = consents.derive_state(
        [_ev("daily_messages", "granted", 1), _ev("daily_messages", "withdrawn", 2)]
    )
    assert state["daily_messages"] is False


def test_withdrawn_then_regranted_is_on() -> None:
    state = consents.derive_state(
        [
            _ev("daily_messages", "granted", 1),
            _ev("daily_messages", "withdrawn", 2),
            _ev("daily_messages", "granted", 3),
        ]
    )
    assert state["daily_messages"] is True


def test_untouched_purpose_is_off_by_default() -> None:
    state = consents.derive_state([_ev("basic_processing", "granted", 1)])
    assert state["basic_processing"] is True
    assert state["external_providers"] is False
    assert state["anonymous_stats"] is False


def test_missing_required_lists_exactly_the_gaps() -> None:
    events = [_ev("basic_processing", "granted", 1)]
    assert consents.missing_required(events) == ["external_providers", "daily_messages"]
    all_granted = [
        _ev("basic_processing", "granted", 1),
        _ev("external_providers", "granted", 2),
        _ev("daily_messages", "granted", 3),
    ]
    assert consents.missing_required(all_granted) == []


def test_unknown_purpose_is_rejected() -> None:
    with pytest.raises(consents.UnknownPurpose):
        consents.purpose("tracking_pixels")
    with pytest.raises(consents.UnknownPurpose):
        consents.derive_state([_ev("tracking_pixels", "granted", 1)])


# ── DB-backed gate: fail closed before consent (§15-adjacent, tested hard) ───


def _record(session, tenant_id: str, purpose: str, action: str = "granted") -> None:
    consents.record_consent(
        session, tenant_id=uuid.UUID(tenant_id), purpose=purpose, action=action
    )


def test_gate_refuses_processing_before_required_consents(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        with pytest.raises(consents.ConsentMissing) as err:
            consents.require_required_consents(s, tenant_id=uuid.UUID(a))
    assert "external_providers" in str(err.value)


def test_gate_passes_once_all_required_are_granted(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        for p in ("basic_processing", "external_providers", "daily_messages"):
            _record(s, a, p)
    with tenant_session(a) as s:
        consents.require_required_consents(s, tenant_id=uuid.UUID(a))  # no raise


def test_withdrawal_closes_the_gate_again(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        for p in ("basic_processing", "external_providers", "daily_messages"):
            _record(s, a, p)
        _record(s, a, "external_providers", action="withdrawn")
    with tenant_session(a) as s:
        with pytest.raises(consents.ConsentMissing):
            consents.require_required_consents(s, tenant_id=uuid.UUID(a))


def test_optional_stats_never_blocks_the_gate(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        for p in ("basic_processing", "external_providers", "daily_messages"):
            _record(s, a, p)
        # anonymous_stats deliberately never granted.
    with tenant_session(a) as s:
        consents.require_required_consents(s, tenant_id=uuid.UUID(a))  # no raise


def test_record_consent_writes_the_append_only_event(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        _record(s, a, "basic_processing")
    with tenant_session(a) as s:
        rows = s.execute(
            text("SELECT purpose, action FROM consent_events ORDER BY occurred_at")
        ).all()
    assert ("basic_processing", "granted") in [tuple(r) for r in rows]


def test_invalid_action_is_rejected_before_touching_the_db(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    with tenant_session(a) as s:
        with pytest.raises(ValueError, match="action"):
            consents.record_consent(
                s, tenant_id=uuid.UUID(a), purpose="daily_messages", action="maybe"
            )
