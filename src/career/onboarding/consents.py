"""Purpose-separated consents (whitepaper §05/§12).

Four purposes, each its own decision: basic_processing (جمع البيانات وتحليل
السيرة), external_providers (مزودو تقنية خارجيون مع تجريد الهوية — §15.8),
daily_messages (الرسائل اليومية), and the optional, independent
anonymous_stats. History lives in the append-only ``consent_events`` table —
withdrawal is a NEW event, never an edit — and the current state is derived
from the latest event per purpose.

Processing gates fail closed: :func:`require_required_consents` raises before
any CV handling if a required consent is absent or withdrawn.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from career.db.models import ConsentEvent

_ACTIONS = frozenset({"granted", "withdrawn"})


class UnknownPurpose(Exception):
    """A purpose key that is not one of the four documented purposes."""


class ConsentMissing(Exception):
    """A required consent is absent or withdrawn — processing must not start."""


@dataclass(frozen=True)
class ConsentPurpose:
    key: str
    required: bool
    title_ar: str
    description_ar: str


#: The four documented purposes, in presentation order (whitepaper §05).
PURPOSES: tuple[ConsentPurpose, ...] = (
    ConsentPurpose(
        key="basic_processing",
        required=True,
        title_ar="التشغيل الأساسي",
        description_ar="جمع بياناتك الأساسية وتحليل سيرتك الذاتية لتقديم الخدمة.",
    ),
    ConsentPurpose(
        key="external_providers",
        required=True,
        title_ar="مزودو تقنية خارجيون",
        description_ar=(
            "استخدام مزودي تقنية خارجيين لمعالجة النصوص مع تجريد هويتك — "
            "لا يصل اسمك ولا رقمك لأي مزود."
        ),
    ),
    ConsentPurpose(
        key="daily_messages",
        required=True,
        title_ar="الرسائل اليومية",
        description_ar="استقبال فرص العمل والتحديثات اليومية على واتساب.",
    ),
    ConsentPurpose(
        key="anonymous_stats",
        required=False,
        title_ar="إحصاءات مجهولة الهوية (اختياري)",
        description_ar=(
            "المساهمة بإحصاءات مجهولة الهوية فعليًا لتحسين الخدمة — "
            "مستقلة تمامًا ولا تؤثر على الخدمة."
        ),
    ),
)

_BY_KEY: dict[str, ConsentPurpose] = {p.key: p for p in PURPOSES}
REQUIRED_KEYS: tuple[str, ...] = tuple(p.key for p in PURPOSES if p.required)

#: Displayed with the consent prompts (whitepaper §05: تُعرض حقوق السحب
#: والحذف والاحتفاظ ورابط السياسة).
RIGHTS_TEXT_AR = (
    "حقوقك: يمكنك سحب أي موافقة، وتصدير بياناتك، وطلب حذفها في أي وقت "
    "بأوامر مباشرة في المحادثة. تُحفظ بياناتك أثناء الاشتراك و90 يومًا بعده "
    "ثم تُحذف. التفاصيل في سياسة الخصوصية: {policy_url}"
)


def purpose(key: str) -> ConsentPurpose:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise UnknownPurpose(f"unknown consent purpose: {key!r}") from None


@dataclass(frozen=True)
class ConsentRecord:
    """A minimal, storage-agnostic view of one consent event. ``sequence``
    orders events (any monotonic value — timestamps, ids, row order)."""

    purpose: str
    action: str
    sequence: int


def derive_state(events: Iterable[ConsentRecord]) -> dict[str, bool]:
    """Current consent per purpose: the latest event wins; untouched purposes
    are off. Unknown purposes in history are an error, not a silent skip."""
    latest: dict[str, ConsentRecord] = {}
    for ev in events:
        purpose(ev.purpose)  # validates
        current = latest.get(ev.purpose)
        if current is None or ev.sequence > current.sequence:
            latest[ev.purpose] = ev
    return {
        p.key: (latest[p.key].action == "granted" if p.key in latest else False)
        for p in PURPOSES
    }


def missing_required(events: Iterable[ConsentRecord]) -> list[str]:
    state = derive_state(events)
    return [key for key in REQUIRED_KEYS if not state[key]]


# ── DB-backed operations (run inside a tenant-bound session) ─────────────────


def record_consent(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    purpose: str,
    action: str,
    policy_version: str | None = None,
    source_inbound_message_id: uuid.UUID | None = None,
) -> ConsentEvent:
    """Append one consent event. Validation happens before any DB write."""
    p = _BY_KEY.get(purpose)
    if p is None:
        raise UnknownPurpose(f"unknown consent purpose: {purpose!r}")
    if action not in _ACTIONS:
        raise ValueError(f"action must be granted|withdrawn, got {action!r}")
    event = ConsentEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        purpose=p.key,
        action=action,
        policy_version=policy_version,
        source_inbound_message_id=source_inbound_message_id,
    )
    session.add(event)
    session.flush()
    return event


def _load_records(session: Session, tenant_id: uuid.UUID) -> Sequence[ConsentRecord]:
    rows = session.execute(
        select(ConsentEvent)
        .where(ConsentEvent.tenant_id == tenant_id)
        .order_by(ConsentEvent.seq)  # total insertion order — see migration 0007
    ).scalars().all()
    return [
        ConsentRecord(purpose=r.purpose, action=r.action, sequence=r.seq)
        for r in rows
    ]


def consent_state(session: Session, *, tenant_id: uuid.UUID) -> dict[str, bool]:
    return derive_state(_load_records(session, tenant_id))


def require_required_consents(session: Session, *, tenant_id: uuid.UUID) -> None:
    """The processing gate: raises :class:`ConsentMissing` naming every absent
    or withdrawn required purpose. Call before any CV upload/extraction work."""
    gaps = missing_required(_load_records(session, tenant_id))
    if gaps:
        raise ConsentMissing(
            "required consents missing or withdrawn: " + ", ".join(gaps)
        )
