"""Standing privacy commands acceptance tests (whitepaper §12) — before code.

Rights are conversation commands executed through privacy_requests with a
DECLARED deadline. Export bundles the customer's own data. Deletion actually
deletes the personal tables — profile, facts, claims, assessments, policies,
uploads, channels, messages — while the regulatory exceptions survive:
financial records, consent history, audit events, the request trail, and the
PII-free tenant skeleton. Pause suspends without extending the period (§05:
وقف مؤقت لا يمدد).
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text

from career.db.session import tenant_session
from career.onboarding import collection, confirmation, consents, privacy
from career.storage import FilesystemStorageAdapter

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _seed_personal_data(session, tenant_id: uuid.UUID) -> None:
    for p in consents.REQUIRED_KEYS:
        consents.record_consent(session, tenant_id=tenant_id, purpose=p, action="granted")
    collection.apply_answer(session, tenant_id=tenant_id, key="cv_full_name", value="Fahad A")
    collection.apply_answer(
        session, tenant_id=tenant_id, key="expected_salary_sar", value=Decimal("12000")
    )
    confirmation.add_conversation_fact(
        session, tenant_id=tenant_id, category="skill", payload={"name": "SQL"}
    )
    session.execute(
        sql_text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_code, status, salla_order_id, amount_sar, currency, "
            " current_period_end) "
            "VALUES (:id, :tid, 'basic', 'ACTIVE', :oid, 149, 'SAR', :pend)"
        ),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "oid": f"O-{uuid.uuid4()}",
         "pend": NOW + timedelta(days=21)},
    )
    session.execute(
        sql_text(
            "INSERT INTO customer_channels (id, tenant_id, provider, phone_e164) "
            "VALUES (:id, :tid, 'whatsapp', :phone)"
        ),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "phone": "+966501234567"},
    )
    channel_id = session.execute(sql_text("SELECT id FROM customer_channels")).scalar_one()
    session.execute(
        sql_text(
            "INSERT INTO inbound_messages "
            "(id, tenant_id, channel_id, wa_message_id, message_type, classification, "
            " text_body) "
            "VALUES (:id, :tid, :cid, :wamid, 'text', 'other', 'نص شخصي من العميل')"
        ),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "cid": str(channel_id),
         "wamid": f"wamid-{uuid.uuid4()}"},
    )
    # migration-0009 contact fields + a funnel analysis with a stored report
    session.execute(
        sql_text("UPDATE customer_profiles SET email = :e, linkedin_url = :l,"
                 " region = :r WHERE tenant_id = :tid"),
        {"e": "fahad@example.com", "l": "linkedin.com/in/fahad",
         "r": "الرياض", "tid": str(tenant_id)},
    )
    sub_id = session.execute(sql_text(
        "SELECT id FROM subscriptions WHERE tenant_id = :tid"),
        {"tid": str(tenant_id)}).scalar_one()
    session.execute(
        sql_text("INSERT INTO funnel_sessions "
                 "(id, tenant_id, subscription_id, channel_id, state, context,"
                 " report) VALUES (:id, :tid, :sid, :cid, 'DONE', '{}'::jsonb,"
                 " '{\"scores\": {\"overall\": 72}}'::jsonb)"),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "sid": str(sub_id),
         "cid": str(channel_id)},
    )


# ── the request trail with declared deadlines ────────────────────────────────


def test_open_request_records_the_declared_deadline(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        req = privacy.open_request(s, tenant_id=tid, kind="export", now=NOW)
        assert req.status == "received"
        assert req.deadline_at == NOW + timedelta(
            days=privacy.DEADLINES_DAYS["export"]
        )
    with pytest.raises(privacy.UnknownRequestKind):
        with tenant_session(a) as s:
            privacy.open_request(s, tenant_id=tid, kind="sell_my_data", now=NOW)


# ── export: the customer's own data, fulfilled to storage ────────────────────


def test_export_bundle_covers_the_personal_data(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        req = privacy.open_request(s, tenant_id=tid, kind="export", now=NOW)
        key = privacy.fulfill_export(
            s, tenant_id=tid, request_id=req.id, storage=storage, now=NOW
        )
    bundle = json.loads(storage.get(key).decode("utf-8"))
    assert bundle["profile"]["cv_full_name"] == "Fahad A"
    assert bundle["facts"][0]["payload"] == {"name": "SQL"}
    assert {c["purpose"] for c in bundle["consents"]} >= set(consents.REQUIRED_KEYS)
    # audit fix: 0009 contact fields + funnel report must be in the export
    assert bundle["profile"]["email"] == "fahad@example.com"
    assert bundle["profile"]["region"] == "الرياض"
    assert bundle["funnel_analyses"][0]["report"]["scores"]["overall"] == 72
    with tenant_session(a) as s:
        row = s.execute(
            sql_text("SELECT status, fulfilled_at, details FROM privacy_requests")
        ).one()
    assert row.status == "fulfilled"
    assert row.fulfilled_at is not None
    assert row.details["storage_key"] == key


# ── deletion: personal data gone, regulatory records retained ────────────────


def test_deletion_deletes_personal_and_retains_regulatory(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        req = privacy.open_request(s, tenant_id=tid, kind="delete", now=NOW)
        report = privacy.execute_deletion(s, tenant_id=tid, request_id=req.id, now=NOW)

    assert report.deleted["profile_facts"] == 1
    assert report.deleted["customer_profiles"] == 1
    assert report.deleted["customer_channels"] == 1  # الرقم يُزال
    assert report.deleted["inbound_messages"] == 1   # سجلات المحادثة الشخصية

    with tenant_session(a) as s:
        def count(table: str) -> int:
            return s.execute(sql_text(f"SELECT count(*) FROM {table}")).scalar_one()

        # personal → gone
        for table in (
            "profile_facts", "customer_profiles", "forbidden_claims",
            "career_path_assessments", "search_policies", "cv_uploads",
            "customer_channels", "inbound_messages",
        ):
            assert count(table) == 0, table
        # regulatory → retained (§12)
        assert count("subscriptions") == 1
        assert count("consent_events") >= 3
        assert count("privacy_requests") == 1
        row = s.execute(sql_text("SELECT status FROM privacy_requests")).one()
        assert row.status == "fulfilled"


def test_deletion_requires_a_matching_open_request(two_tenants: tuple[str, str]) -> None:
    """Deleting a customer's data is not a casual call — it must reference an
    open request of kind delete (the WhatsApp flow confirms before opening)."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        with pytest.raises(privacy.RequestNotFound):
            privacy.execute_deletion(s, tenant_id=tid, request_id=uuid.uuid4(), now=NOW)
        export_req = privacy.open_request(s, tenant_id=tid, kind="export", now=NOW)
        with pytest.raises(privacy.RequestNotFound):
            privacy.execute_deletion(
                s, tenant_id=tid, request_id=export_req.id, now=NOW
            )


def test_deletion_reports_storage_keys_to_purge(
    two_tenants: tuple[str, str],
) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        doc_id = uuid.uuid4()
        s.execute(
            sql_text(
                "INSERT INTO documents (id, tenant_id, storage_key, content_sha256, "
                "content_type, size_bytes, status) VALUES "
                "(:id, :tid, :key, :sha, 'application/pdf', 10, 'active')"
            ),
            {"id": str(doc_id), "tid": str(tid),
             "key": f"tenants/{tid}/uploads/cv.pdf", "sha": "0" * 64},
        )
        s.execute(
            sql_text(
                "INSERT INTO cv_uploads (id, tenant_id, document_id, "
                "extracted_text_storage_key) VALUES (:id, :tid, :doc, :txt)"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "doc": str(doc_id),
             "txt": f"tenants/{tid}/uploads/cv.txt"},
        )
        req = privacy.open_request(s, tenant_id=tid, kind="delete", now=NOW)
        report = privacy.execute_deletion(s, tenant_id=tid, request_id=req.id, now=NOW)
    assert f"tenants/{tid}/uploads/cv.pdf" in report.storage_keys_to_purge
    assert f"tenants/{tid}/uploads/cv.txt" in report.storage_keys_to_purge


def test_deletion_purges_funnel_reports_via_storage_prefix(
    two_tenants: tuple[str, str], tmp_path
) -> None:
    """Audit fix: funnel report PDFs have no DB row — deletion collects the
    whole tenant storage prefix when a storage adapter is injected, and the
    funnel session row itself is deleted."""
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    storage.put(f"tenants/{tid}/funnel_reports/2026-07-19-120000.pdf",
                b"%PDF- report", content_type="application/pdf")
    storage.put(f"tenants/{tid}/tailored_cvs/joburl-x.pdf", b"%PDF- cv",
                content_type="application/pdf")
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        req = privacy.open_request(s, tenant_id=tid, kind="delete", now=NOW)
        report = privacy.execute_deletion(
            s, tenant_id=tid, request_id=req.id, now=NOW, storage=storage
        )
    assert report.deleted["funnel_sessions"] == 1
    assert any("funnel_reports" in k for k in report.storage_keys_to_purge)
    assert any("tailored_cvs" in k for k in report.storage_keys_to_purge)


# ── pause: suspends without extending (§05: لا يمدد) ─────────────────────────


def test_pause_and_resume_never_extend_the_period(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        before = s.execute(
            sql_text("SELECT current_period_end FROM subscriptions")
        ).scalar_one()
        privacy.pause_subscription(s, tenant_id=tid)
        assert s.execute(sql_text("SELECT status FROM subscriptions")).scalar_one() == "PAUSED"
        privacy.resume_subscription(s, tenant_id=tid)
        after_row = s.execute(
            sql_text("SELECT status, current_period_end FROM subscriptions")
        ).one()
    assert after_row.status == "ACTIVE"
    assert after_row.current_period_end == before  # لا يمدد


# ── subscription status: an honest Arabic one-liner ──────────────────────────


def test_status_summary_names_state_and_days_left(two_tenants: tuple[str, str]) -> None:
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        summary = privacy.subscription_status_summary(s, tenant_id=tid, now=NOW)
    assert "نشط" in summary
    assert "21" in summary  # days remaining until period end


def test_the_export_is_as_wide_as_the_deletion(owner_session, clean_billing):
    """§12 grants «الحصول على نسخة منها» over everything personal we hold, and
    the yardstick is our own deletion list: a table personal enough to DELETE
    is personal enough to HAND OVER. Seven were missing — the customer got no
    record of which jobs we sent, which CVs were generated in their name, what
    they uploaded, or their own messages, while the request was still marked
    fulfilled.

    This guard is structural on purpose: adding a table to the deletion order
    without exporting it fails here.
    """
    from career.onboarding import privacy as _p

    #: deletion table -> the export section that carries it. A new personal
    #: table must be given a home here (or an explicit, argued exemption).
    covered = {
        "forbidden_claims": "forbidden_claims",
        "profile_facts": "facts",
        "customer_profiles": "profile",
        "career_path_assessments": "assessments",
        "search_policies": "search_policies",
        "cv_uploads": "cv_uploads",
        "documents": "generated_cvs",
        "funnel_sessions": "funnel_analyses",
        "delivery_messages": "messages_we_sent",
        "deliveries": "delivery_days",
        "inbound_messages": "messages_you_sent",
        "customer_channels": "channels",
        # the journey row is process state (which question we were on), not
        # data ABOUT the customer — its content is already exported as the
        # profile and the facts it produced.
        "onboarding_sessions": None,
    }

    deleted_tables = {m.__tablename__ for m in _p._PERSONAL_DELETION_ORDER}
    assert deleted_tables <= set(covered), (
        "a personal table is deleted but has no declared export home: "
        f"{sorted(deleted_tables - set(covered))}"
    )

    tid = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-X{uuid.uuid4().int % 100_000:05d}"})
    owner_session.commit()
    try:
        bundle = _p.export_bundle(owner_session, tenant_id=tid)
        for table, section in covered.items():
            if section is None:
                continue
            assert section in bundle, (
                f"{table} is deleted on request but never exported ({section})"
            )
    finally:
        owner_session.rollback()
        owner_session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                              {"t": str(tid)})
        owner_session.commit()


def test_pausing_twice_never_breaks_the_conversation(owner_session, clean_billing):
    """The unguarded transition raised InvalidTransition into the WhatsApp
    worker for anyone already paused, expired or cancelled — a customer who
    typed «وقف مؤقت» a second time crashed their own turn and was answered
    with nothing. Repeating a command is not an error."""
    from career.onboarding import privacy as _p
    from career.salla import subscriptions as _st

    tid = uuid.uuid4()
    sid = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-P{uuid.uuid4().int % 100_000:05d}"})
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency)"
        " VALUES (:i, :t, 'professional', 'ACTIVE', :o, 279, 'SAR')"),
        {"i": str(sid), "t": str(tid), "o": f"ORD-{uuid.uuid4()}"})
    owner_session.commit()
    try:
        first = _p.pause_subscription(owner_session, tenant_id=tid)
        assert first.status == _st.PAUSED

        again = _p.pause_subscription(owner_session, tenant_id=tid)   # no raise
        assert again.status == _st.PAUSED

        # and from a state where pausing means nothing at all
        owner_session.execute(sql_text(
            "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = :i"),
            {"i": str(sid)})
        owner_session.commit()
        expired = _p.pause_subscription(owner_session, tenant_id=tid)
        assert expired.status == "EXPIRED"        # untouched, not crashed
    finally:
        owner_session.rollback()
        for table in ("subscription_events", "privacy_requests", "subscriptions"):
            owner_session.execute(sql_text(
                f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": str(tid)})  # noqa: S608
        owner_session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                              {"t": str(tid)})
        owner_session.commit()


def test_the_renewal_button_meta_really_sends_is_understood() -> None:
    """Meta's APPROVED renewal and recovery templates carry the button label
    «تجديد الاشتراك», and a template's buttons cannot be changed once
    approved. The customer taps it at the exact moment they intend to pay."""
    from career.onboarding.orchestrator import _PRIVACY_COMMANDS

    assert _PRIVACY_COMMANDS["تجديد الاشتراك"] == "status"
    assert _PRIVACY_COMMANDS["حالة اشتراكي"] == "status"


# ── the pause the state machine never agreed to (AUDIT, 5 August) ────────────


@contextmanager
def _tenant_in_status(owner_session, status: str):
    """A tenant holding one pass subscription in the given state.

    The shape every pause/resume question needs, and the cleanup the repo
    lesson demands: an open transaction wedges the clean_billing teardown.
    """
    tid = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-S{uuid.uuid4().int % 100_000:05d}"})
    owner_session.execute(sql_text(
        "INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
        " salla_order_id, amount_sar, currency)"
        " VALUES (:i, :t, 'professional', :s, :o, 279, 'SAR')"),
        {"i": str(uuid.uuid4()), "t": str(tid), "s": status,
         "o": f"ORD-{uuid.uuid4()}"})
    owner_session.commit()
    try:
        yield tid
    finally:
        owner_session.rollback()
        for table in ("subscription_events", "privacy_requests", "subscriptions"):
            owner_session.execute(sql_text(
                f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": str(tid)})  # noqa: S608
        owner_session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                              {"t": str(tid)})
        owner_session.commit()


def _db_status(owner_session, tenant_id: uuid.UUID) -> str:
    return owner_session.execute(sql_text(
        "SELECT status FROM subscriptions WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()


def test_pausing_in_grace_answers_the_customer_instead_of_crashing(
    owner_session, clean_billing
):
    """INCIDENT: «وقف مؤقت» from GRACE raised InvalidTransition.

    ``_PAUSABLE`` was written by hand and counted GRACE as pausable while the
    state machine has no GRACE→PAUSED edge, so the transition raised straight
    through the standing privacy command into the WhatsApp worker: the inbound
    row was rolled back, the event was marked failed — a terminal status
    nothing re-reads — and the customer, whose paid period had just ended and
    who is therefore exactly the person most likely to type it, was answered
    with nothing at all.
    """
    from career.onboarding import privacy as _p
    from career.salla import subscriptions as _st

    with _tenant_in_status(owner_session, _st.GRACE) as tid:
        result = _p.pause_subscription(owner_session, tenant_id=tid)  # no raise
        assert result.outcome is _p.ActionOutcome.NOT_ELIGIBLE
        assert result.changed is False
        assert result.status == _st.GRACE
        assert _db_status(owner_session, tid) == _st.GRACE  # untouched


def test_the_pausable_set_belongs_to_the_state_machine(owner_session):
    """The drift itself, not one instance of it. Two modules agreed about
    which states may be paused only by coincidence, and the coincidence
    lapsed. Restating the set anywhere is now a failing test."""
    from career.onboarding import privacy as _p
    from career.salla import subscriptions as _st

    derived = frozenset(
        state for state in _st.ALL_STATES
        if _st.can_transition(state, _st.PAUSED)
    )
    assert _p._PAUSABLE == derived
    assert _st.GRACE not in _p._PAUSABLE
    assert {_st.ACTIVE, _st.ONBOARDING} <= _p._PAUSABLE  # still the real ones


def test_a_refusal_is_never_shaped_like_a_success(owner_session, clean_billing):
    """AUDIT: both actions returned the subscription row UNCHANGED when they
    declined, so «paused it» and «refused to pause it» were the same value.
    Every caller had to re-read the status to tell them apart and one did not
    — which is how «✅ استأنفنا الخدمة للعميل» was printed over an account
    that was never resumed. The outcome now travels with the answer."""
    from career.onboarding import privacy as _p
    from career.salla import subscriptions as _st

    with _tenant_in_status(owner_session, _st.ACTIVE) as tid:
        first = _p.pause_subscription(owner_session, tenant_id=tid)
        assert first.outcome is _p.ActionOutcome.CHANGED
        assert first.changed and first.status == _st.PAUSED

        again = _p.pause_subscription(owner_session, tenant_id=tid)
        assert again.outcome is _p.ActionOutcome.ALREADY
        assert not again.changed              # idempotent, and says so

        back = _p.resume_subscription(owner_session, tenant_id=tid)
        assert back.outcome is _p.ActionOutcome.CHANGED
        assert _db_status(owner_session, tid) == _st.ACTIVE

        nothing = _p.resume_subscription(owner_session, tenant_id=tid)
        assert nothing.outcome is _p.ActionOutcome.ALREADY
        assert not nothing.changed

    with _tenant_in_status(owner_session, _st.EXPIRED) as tid:
        refused = _p.resume_subscription(owner_session, tenant_id=tid)
        assert refused.outcome is _p.ActionOutcome.NOT_ELIGIBLE
        assert not refused.changed and refused.status == _st.EXPIRED
        assert _db_status(owner_session, tid) == _st.EXPIRED


def test_a_tenant_with_no_subscription_is_answered_not_silenced(
    owner_session, clean_billing
):
    """«حالة اشتراكي» is the most advertised command in the product, and for a
    tenant with no live row — the shells a §04 upgrade leaves behind, an order
    still being provisioned — it raised RequestNotFound: a privacy-REQUEST
    exception standing in for «no subscription», thrown on the customer path.
    Their whole turn died with no reply at all."""
    from career.onboarding import privacy as _p

    tid = uuid.uuid4()
    owner_session.execute(sql_text(
        "INSERT INTO tenants (id, code) VALUES (:i, :c)"),
        {"i": str(tid), "c": f"TEN-N{uuid.uuid4().int % 100_000:05d}"})
    owner_session.commit()
    try:
        summary = _p.subscription_status_summary(
            owner_session, tenant_id=tid, now=NOW)      # no raise
        assert "دعم" in summary                        # never a dead end
        assert "حالة اشتراكي" in summary

        # the two actions on the same path answer instead of raising too
        paused = _p.pause_subscription(owner_session, tenant_id=tid)
        assert paused.outcome is _p.ActionOutcome.NO_SUBSCRIPTION
        assert paused.subscription is None and not paused.changed
        resumed = _p.resume_subscription(owner_session, tenant_id=tid)
        assert resumed.outcome is _p.ActionOutcome.NO_SUBSCRIPTION
    finally:
        owner_session.rollback()
        owner_session.execute(sql_text("DELETE FROM tenants WHERE id = :t"),
                              {"t": str(tid)})
        owner_session.commit()


def test_every_state_has_an_arabic_name_and_no_line_mixes_directions():
    """PENDING_PAYMENT was missing from the map, so «حالة اشتراكي» answered
    with an Arabic sentence carrying a raw Latin token in the middle of it —
    a line Fahad's client scrambles on delivery (§16), and one that means
    nothing to the reader either way. The guard is structural: a twelfth
    subscription state with no Arabic name fails here."""
    from career.onboarding import privacy as _p
    from career.salla import subscriptions as _st

    assert _st.ALL_STATES <= set(_p._STATUS_AR)

    arabic = re.compile(r"[؀-ۿ]")
    latin = re.compile(r"[A-Za-z]")
    bodies: list[tuple[str, str]] = []
    for name, value in vars(_p).items():
        if name.startswith("__"):
            continue
        if isinstance(value, str) and arabic.search(value):
            bodies.append((name, value))
        elif isinstance(value, dict):
            bodies.extend((name, v) for v in value.values()
                          if isinstance(v, str) and arabic.search(v))
    assert bodies
    for name, body in bodies:
        for line in body.splitlines():
            if arabic.search(line) and latin.search(line):
                raise AssertionError(
                    f"{name}: mixed-direction line would scramble: {line!r}"
                )


# ── webhook_events: the table a deletion request could not reach ─────────────
#
# The oldest unclosed compliance item (closed 6 أغسطس, migration 0024). The
# raw provider body carries the buyer's name, mobile and email from Salla and
# the customer's phone, profile name and typed text from Meta, and the table
# had no tenant column at all — so it was in no deletion list, no export and
# no pruning, and the nightly backup carried it for months. These tests are
# the four halves of the fix: the link, the erasure, the expiry, and the
# guarantee the erasure must not break.


@pytest.fixture()
def purge_webhooks(owner_engine):
    """Delete whatever webhook_events rows a test creates.

    webhook_events deliberately has no foreign key to tenants — an order
    arrives before its tenant exists — so the two_tenants fixture's cascade
    does not reach these rows and career_test would accumulate them.
    """
    from sqlalchemy.orm import Session as _Session

    def _ids() -> set[str]:
        with _Session(owner_engine) as s:
            return {str(r[0]) for r in s.execute(
                sql_text("SELECT id::text FROM webhook_events"))}

    before = _ids()
    yield
    new = _ids() - before
    if new:
        with _Session(owner_engine) as s:
            s.execute(sql_text("DELETE FROM webhook_events WHERE id::text = ANY(:i)"),
                      {"i": list(new)})
            s.commit()


def _wa_body(*phones: str) -> dict:
    """A Meta POST shaped the way Meta actually sends one — note that `entry`
    is a list, which is how one body comes to carry two customers."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": "WABA", "changes": [{"value": {
                "contacts": [{"wa_id": p.lstrip("+"), "profile": {"name": "فهد"}}],
                "messages": [{"from": p.lstrip("+"), "id": f"wamid.{p}",
                              "type": "text", "text": {"body": "وش الأخبار"}},
                             ],
            }}]}
            for p in phones
        ],
    }


def _seed_channel(session, tenant_id: uuid.UUID, phone: str) -> None:
    session.execute(
        sql_text("INSERT INTO customer_channels (id, tenant_id, provider, phone_e164)"
                 " VALUES (:id, :tid, 'whatsapp', :p)"),
        {"id": str(uuid.uuid4()), "tid": str(tenant_id), "p": phone},
    )


def test_intake_links_a_whatsapp_body_to_the_customer_it_is_about(
    two_tenants: tuple[str, str], purge_webhooks
) -> None:
    from career.db.session import SessionLocal
    from career.webhooks import intake

    a, _ = two_tenants
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    with tenant_session(a) as s:
        _seed_channel(s, uuid.UUID(a), phone)

    with SessionLocal() as s:   # the unscoped app session main.py uses
        event_id = intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=f"wa:{uuid.uuid4()}", payload=_wa_body(phone),
            signature_valid=True,
        )
        subject = s.execute(
            sql_text("SELECT subject_tenant_id::text FROM webhook_events WHERE id = :i"),
            {"i": event_id},
        ).scalar_one()
    assert subject == a


def test_a_batch_that_names_two_customers_is_attributed_to_neither(
    two_tenants: tuple[str, str], purge_webhooks
) -> None:
    """One Meta POST can carry two people's messages, and stamping it with one
    of them would be wrong in both directions at once: her deletion request
    would erase his forensic record, and her data export would point at a body
    containing his message. Ambiguity stays NULL and expires on the clock."""
    from career.db.session import SessionLocal
    from career.webhooks import intake

    a, b = two_tenants
    phone_a = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    phone_b = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    with tenant_session(a) as s:
        _seed_channel(s, uuid.UUID(a), phone_a)
    with tenant_session(b) as s:
        _seed_channel(s, uuid.UUID(b), phone_b)

    with SessionLocal() as s:
        assert intake.resolve_subject_tenant(
            s, provider="whatsapp", payload=_wa_body(phone_a, phone_b)
        ) is None
        assert intake.resolve_subject_tenant(
            s, provider="whatsapp", payload=_wa_body(phone_a)
        ) == a


def test_intake_still_records_the_event_when_the_link_cannot_be_resolved(
    two_tenants: tuple[str, str], purge_webhooks, monkeypatch
) -> None:
    """The 200 is worth more than the link. A database at an older migration
    has no app.tenant_for_phone at all, and an exception there would poison
    the transaction and lose the event — so resolution runs in a SAVEPOINT."""
    from career.db.session import SessionLocal
    from career.webhooks import intake

    def _explode(*_a, **_k):
        raise RuntimeError("function app.tenant_for_phone(text) does not exist")

    monkeypatch.setattr(intake, "tenant_for_phone", _explode)
    with SessionLocal() as s:
        event_id = intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=f"wa:{uuid.uuid4()}", payload=_wa_body("+966500000001"),
            signature_valid=True,
        )
        assert event_id is not None
        assert s.execute(
            sql_text("SELECT subject_tenant_id FROM webhook_events WHERE id = :i"),
            {"i": event_id},
        ).scalar_one() is None
    assert two_tenants


def test_deletion_erases_the_raw_bodies_and_keeps_the_fingerprint(
    two_tenants: tuple[str, str], purge_webhooks
) -> None:
    """«حذف بياناتي» now reaches the wire record. The row survives redacted —
    it is the idempotency record — and the customer's own words are gone."""
    from career.webhooks import intake

    a, _ = two_tenants
    tid = uuid.UUID(a)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    fingerprint = f"wa:{uuid.uuid4()}"
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        _seed_channel(s, tid, phone)
    with tenant_session(a) as s:
        intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=fingerprint, payload=_wa_body(phone),
            signature_valid=True,
        )

    with tenant_session(a) as s:
        req = privacy.open_request(s, tenant_id=tid, kind="delete", now=NOW)
        report = privacy.execute_deletion(s, tenant_id=tid, request_id=req.id, now=NOW)
        row = s.execute(
            sql_text("SELECT payload::text, event_fingerprint, payload_redacted_at "
                     "FROM webhook_events WHERE event_fingerprint = :f"),
            {"f": fingerprint},
        ).one()
    assert report.redacted["webhook_events"] == 1
    assert "webhook_events" in privacy.REDACTED_TABLES
    assert row.event_fingerprint == fingerprint, "the idempotency record must survive"
    assert row.payload_redacted_at is not None
    assert "وش الأخبار" not in row[0] and phone.lstrip("+") not in row[0]
    assert "_redacted" in row[0]


def test_a_replayed_webhook_is_still_a_no_op_after_its_body_was_erased(
    two_tenants: tuple[str, str], purge_webhooks
) -> None:
    """Why redaction and not deletion. Salla retries; a webhook whose
    fingerprint we had thrown away would provision the same customer twice."""
    from career.db.session import SessionLocal
    from career.webhooks import intake

    a, _ = two_tenants
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    fingerprint = f"wa:{uuid.uuid4()}"
    with tenant_session(a) as s:
        _seed_channel(s, uuid.UUID(a), phone)
    with SessionLocal() as s:
        first = intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=fingerprint, payload=_wa_body(phone),
            signature_valid=True,
        )
        intake.redact_for_tenant(s, tenant_id=uuid.UUID(a), now=NOW)
        s.commit()
        replay = intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=fingerprint, payload=_wa_body(phone),
            signature_valid=True,
        )
    assert first is not None
    assert replay is None, "the replay was accepted as a fresh event"


def test_the_nightly_prune_expires_bodies_and_links_what_became_linkable(
    two_tenants: tuple[str, str], purge_webhooks
) -> None:
    """Both halves of the sweep, in the order it runs them.

    The late link is not decoration: the order that pays for a subscription
    arrives BEFORE the tenant it creates, so at intake there was nothing to
    resolve against. Linking before redacting is what makes the count in a
    deletion report honest for the customer who signed up last month.
    """
    from career.db.session import SessionLocal
    from career.webhooks import intake

    a, _ = two_tenants
    tid = uuid.UUID(a)
    order_id = f"O-{uuid.uuid4()}"
    old = NOW - timedelta(days=intake.RAW_PAYLOAD_RETENTION_DAYS + 1)
    fresh_fp, old_fp = f"wa:{uuid.uuid4()}", f"sl:{uuid.uuid4()}"

    with tenant_session(a) as s:
        # the order webhook lands first; the subscription that names it is
        # created afterwards, exactly as provisioning does it
        s.execute(
            sql_text("INSERT INTO webhook_events (id, provider, event_type, "
                     "event_fingerprint, signature_valid, salla_order_id, payload, "
                     "processing_status, received_at) VALUES (gen_random_uuid(), "
                     "'salla', 'order.created', :f, true, :o, "
                     "'{\"data\": {\"customer\": {\"mobile\": \"0501234567\"}}}'::jsonb,"
                     " 'processed', :t)"),
            {"f": old_fp, "o": order_id, "t": old},
        )
        s.execute(
            sql_text("INSERT INTO webhook_events (id, provider, event_type, "
                     "event_fingerprint, signature_valid, payload, "
                     "processing_status, received_at) VALUES (gen_random_uuid(), "
                     "'whatsapp', 'messages', :f, true, '{}'::jsonb, 'processed', :t)"),
            {"f": fresh_fp, "t": NOW},
        )
        s.execute(
            sql_text("INSERT INTO subscriptions (id, tenant_id, plan_code, status,"
                     " salla_order_id, amount_sar, currency) VALUES (:id, :tid,"
                     " 'basic', 'ACTIVE', :o, 149, 'SAR')"),
            {"id": str(uuid.uuid4()), "tid": str(tid), "o": order_id},
        )

    with SessionLocal() as s:
        counts = intake.prune_webhook_payloads(s, now=NOW)
        s.commit()
        old_row = s.execute(
            sql_text("SELECT subject_tenant_id::text, payload::text, "
                     "payload_redacted_at FROM webhook_events "
                     "WHERE event_fingerprint = :f"), {"f": old_fp}).one()
        fresh_row = s.execute(
            sql_text("SELECT payload_redacted_at FROM webhook_events "
                     "WHERE event_fingerprint = :f"), {"f": fresh_fp}).one()
    assert counts["linked"] >= 1 and counts["redacted"] >= 1
    assert old_row[0] == a, "the late link never happened"
    assert "0501234567" not in old_row[1] and old_row[2] is not None
    assert fresh_row[0] is None, "a body inside the window was expired early"


def test_the_export_names_the_wire_record_without_handing_over_the_body(
    two_tenants: tuple[str, str], purge_webhooks
) -> None:
    """§12 grants a copy of everything personal we hold — and one Meta POST can
    hold two customers, so the body itself is not what is handed over. What
    they typed is already exported in full under `messages_you_sent`."""
    from career.webhooks import intake

    a, _ = two_tenants
    tid = uuid.UUID(a)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        _seed_channel(s, tid, phone)
    with tenant_session(a) as s:
        intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=f"wa:{uuid.uuid4()}", payload=_wa_body(phone),
            signature_valid=True,
        )
    with tenant_session(a) as s:
        bundle = privacy.export_bundle(s, tenant_id=tid)
    events = bundle["provider_events"]
    assert [e["provider"] for e in events] == ["whatsapp"]
    assert events[0]["raw_body_still_held"] is True
    assert "وش الأخبار" not in json.dumps(bundle, ensure_ascii=False)


def test_a_real_postgres_error_in_the_link_does_not_poison_the_intake(
    two_tenants: tuple[str, str], purge_webhooks, monkeypatch
) -> None:
    """The staging shape, with a real aborted transaction rather than a stub.

    staging is at migration 0020 as this ships, so `app.tenant_for_phone` does
    not exist there yet. A missing function is not a Python error that can be
    caught and shrugged off — it aborts the Postgres transaction, and every
    statement after it fails too. Without the SAVEPOINT the INSERT that follows
    would fail as well and the webhook would be lost, which is the one outcome
    intake exists to prevent.
    """
    from career.db.session import SessionLocal
    from career.webhooks import intake

    def _missing_function(session, _phone):
        return session.execute(
            sql_text("SELECT app.a_function_this_database_does_not_have()")
        ).scalar_one()

    monkeypatch.setattr(intake, "tenant_for_phone", _missing_function)
    with SessionLocal() as s:
        event_id = intake.persist_deduped_event(
            s, provider="whatsapp", event_type="messages",
            fingerprint=f"wa:{uuid.uuid4()}", payload=_wa_body("+966500000002"),
            signature_valid=True,
        )
        assert event_id is not None, "the event was lost to a failed lookup"
    assert two_tenants


# ── the audit trail's own reason for existing ────────────────────────────────


def _audit_rows(tenant: str) -> list:
    with tenant_session(tenant) as s:
        return list(s.execute(sql_text(
            "SELECT action, actor, resource_type, details FROM audit_events"
            " ORDER BY created_at")).all())


def test_deletion_writes_the_row_the_retention_exemption_exists_for(
    two_tenants: tuple[str, str],
) -> None:
    """`audit_events` is in RETAINED_TABLES: we tell the customer we delete
    their data and then keep this table, and the justification is that it holds
    the record they or a regulator could ask us to produce. The deletion itself
    was the one event nobody wrote — so the exemption was vacuous, and the row
    that survives is the row that proves the erasure happened.

    It survives BECAUSE it is written in the same transaction as the deletion
    and its table is not in the deletion order — asserted here rather than
    assumed, since «written, then deleted by the very act it records» is the
    obvious way for this to be wrong.
    """
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
        req = privacy.open_request(s, tenant_id=tid, kind="delete", now=NOW)
        privacy.execute_deletion(s, tenant_id=tid, request_id=req.id, now=NOW)

    rows = [r for r in _audit_rows(a) if r.action == "privacy_deletion_executed"]
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "customer"
    assert row.resource_type == "privacy_request"
    # counts and table names — never a phone, a name or a storage key's contents
    assert row.details["deleted"]["customer_profiles"] == 1
    assert row.details["deleted"]["customer_channels"] == 1
    assert isinstance(row.details["storage_objects_purged"], int)
    assert "audit_events" in row.details["retained_tables"]
    blob = json.dumps(row.details, ensure_ascii=False)
    assert "+9665" not in blob and "Fahad" not in blob


def test_the_deletion_audit_row_is_atomic_with_the_deletion(
    two_tenants: tuple[str, str],
) -> None:
    """The opposite call from the export beside it, and worth pinning.

    Every delete is inside the caller's transaction, so a row written in its
    OWN transaction could outlive a rollback and claim an erasure that never
    happened — a worse lie than a missing row, because this one would be
    believed. Roll the turn back and nothing is left saying we deleted anything.
    """
    a, _ = two_tenants
    tid = uuid.UUID(a)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)
    with pytest.raises(RuntimeError, match="turn blew up"):
        with tenant_session(a) as s:
            req = privacy.open_request(s, tenant_id=tid, kind="delete", now=NOW)
            privacy.execute_deletion(s, tenant_id=tid, request_id=req.id, now=NOW)
            raise RuntimeError("turn blew up after the deletion")

    assert _audit_rows(a) == []
    with tenant_session(a) as s:
        # and the customer's data is still there — nothing happened at all
        assert s.execute(sql_text(
            "SELECT count(*) FROM customer_profiles")).scalar_one() == 1


def test_export_is_audited_even_when_the_turn_that_asked_for_it_rolls_back(
    two_tenants: tuple[str, str], tmp_path,
) -> None:
    """The export's audit row is written in its OWN transaction, on purpose.

    `storage.put` has already written a complete copy of everything personal we
    hold, and object storage is not in this transaction. If the WhatsApp turn
    that asked for it then fails, the request row, its `storage_key` and any
    in-transaction audit row all vanish — while the bundle sits in storage
    exactly where we put it. A disclosure whose only record can disappear while
    the disclosure survives is indistinguishable from a leak.
    """
    a, _ = two_tenants
    tid = uuid.UUID(a)
    storage = FilesystemStorageAdapter(tmp_path)
    with tenant_session(a) as s:
        _seed_personal_data(s, tid)

    with pytest.raises(RuntimeError, match="turn blew up"):
        with tenant_session(a) as s:
            req = privacy.open_request(s, tenant_id=tid, kind="export", now=NOW)
            privacy.fulfill_export(
                s, tenant_id=tid, request_id=req.id, storage=storage, now=NOW
            )
            raise RuntimeError("turn blew up after the bundle was written")

    rows = [r for r in _audit_rows(a) if r.action == "privacy_export_fulfilled"]
    assert len(rows) == 1
    assert rows[0].details["storage_key"].startswith(f"tenants/{tid}/exports/")
    with tenant_session(a) as s:
        # the request itself went with the rollback — the audit row is all that
        # is left, which is exactly why it must not be in that transaction
        assert s.execute(sql_text(
            "SELECT count(*) FROM privacy_requests")).scalar_one() == 0


def test_an_unregistered_action_is_still_refused(
    two_tenants: tuple[str, str],
) -> None:
    """The vocabulary stays closed. Four actions were added, not the door."""
    from career.audit import UnregisteredAuditAction, record_audit

    a, _ = two_tenants
    with pytest.raises(UnregisteredAuditAction):
        with tenant_session(a) as s:
            record_audit(s, tenant_id=a, actor="operator",
                         action="customer_opened_the_app")
