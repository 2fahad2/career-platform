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
import uuid
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
