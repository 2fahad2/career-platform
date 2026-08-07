"""``scripts/consume_stored_authorize.py`` — the dry run is a PROMISE.

This file exists because the script had none. It was written, rehearsed once
against ``career_test`` from ``tests/test_salla_tokens.py::TestTheConsumeScript``
(three cases: a foreign store refused, a confirmed store change applied, an
empty queue), and then the worker's payload reader grew underneath it. The
script kept its own ``payload.get("merchant")`` and the two disagreed:

* a payload the worker attributes perfectly — ``store_id`` at the top level,
  ``data.merchant``, ``data.store_id``, or Salla's merchant OBJECT — was
  reported by the script as «store (none in payload)»;
* and a value the worker REFUSES as not-an-identifier (a dict, a boolean,
  ``"two words"``, anything with a newline) was reported by the script as a
  store, because ``str()`` renders all of them happily.

Either direction moves the ``--allow-store-change`` gate: the operator's
confirmation would have been given about a different decision from the one that
executed, on the one credential this platform has. So the tests here are not
«does the script run» — they are «does the script's report equal the writer's
action», asserted on the SAME COMPUTED VALUES, for every payload shape and
every identity verdict.

The DB-backed cases run against ``career_test`` and a throwaway env file
(``isolated_env_file``, imported below and autouse). Nothing here can reach
``career_staging`` or ``/root/career/.env.staging``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import inspect
import io
import logging
import sys
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from sqlalchemy.orm import Session

from career.db.models import WebhookEvent
from career.salla import provisioning, tokens
from tests.test_salla_tokens import (
    FAKE_ACCESS,
    FAKE_ACCESS_2,
    FAKE_REFRESH,
    FAKE_REFRESH_2,
    RawAdmin,
    isolated_env_file,  # noqa: F401 — autouse: redirects tokens.ENV_FILE
    row,
    seed,
    sweep,
)

REPO = Path(__file__).resolve().parents[1]

#: Two real merchant numbers from this project's own history — identifiers, not
#: secrets, and the pair whose confusion the store guard exists to prevent.
DEMO_STORE = "855028708"
REAL_STORE = "1275699954"
#: The APP id Salla puts in ``data.id`` on EVERY delivery, whoever the store is.
APP_ID = 1410006361


def _load_script() -> Any:
    """Load the deliverable from its real path.

    Registered in ``sys.modules`` before execution — but the script must ALSO
    load without that, because ``tests/test_salla_tokens.py`` loads it the other
    way and a module-level construct that needs the registration (a
    ``@dataclass``, whose annotations resolve through
    ``sys.modules[cls.__module__]``) would break those tests instead of this
    one. Asserted below in :class:`TestTheScriptStaysLoadable`.
    """
    path = REPO / "scripts" / "consume_stored_authorize.py"
    spec = importlib.util.spec_from_file_location("career_consume_authorize", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


# ── payload shapes ───────────────────────────────────────────────────────────


def _payload(**top: Any) -> dict:
    """An authorize body with the app metadata Salla really sends, and the
    store carried wherever the caller puts it. ``data.id`` is always the APP
    id — the value that must never be read as a store."""
    data: dict = {
        "access_token": FAKE_ACCESS_2,
        "refresh_token": FAKE_REFRESH_2,
        "expires": int((datetime.now(UTC) + timedelta(days=14)).timestamp()),
        "id": APP_ID,
        "app_name": "تيستفهد",
        "scope": "offline_access orders.read",
    }
    data.update(top.pop("data", {}))
    payload: dict = {"event": "app.store.authorize",
                     "created_at": "2026-08-07 12:00:00", "data": data}
    payload.update(top)
    return payload


def _event(payload: dict, *, signature_valid: bool = True) -> WebhookEvent:
    """A detached row — enough for every reader under test, and no database."""
    return WebhookEvent(
        id=uuid.uuid4(), provider="salla", event_type="app.store.authorize",
        event_fingerprint=f"test:{uuid.uuid4()}",
        signature_valid=signature_valid, payload=payload,
        processing_status="received",
    )


#: Every shape ``provisioning._STORE_ID_PATHS`` accepts, and what it must read.
ATTRIBUTED = [
    pytest.param({"merchant": REAL_STORE}, id="merchant-top-level"),
    pytest.param({"store_id": REAL_STORE}, id="store_id-top-level"),
    pytest.param({"data": {"merchant": REAL_STORE}}, id="data.merchant"),
    pytest.param({"data": {"store_id": REAL_STORE}}, id="data.store_id"),
    pytest.param({"merchant": {"id": REAL_STORE, "domain": "x.salla.sa"}},
                 id="merchant-object"),
    pytest.param({"merchant": int(REAL_STORE)}, id="merchant-as-json-int"),
]

#: Shapes that name NO store. Each is a way for the old reader to invent one.
UNATTRIBUTED = [
    pytest.param({}, id="nothing-but-the-app-id"),
    pytest.param({"merchant": {"domain": "x.salla.sa"}}, id="object-without-id"),
    pytest.param({"merchant": True}, id="boolean"),
    pytest.param({"merchant": "two words"}, id="two-words"),
    pytest.param({"merchant": "1275699954\nDB_PASSWORD=injected"},
                 id="newline-injection"),
    pytest.param({"merchant": ""}, id="empty-string"),
]


class TestTheStoreIdComesFromTheWorkersReader:
    """One reader, one answer. The script does not have an opinion any more."""

    @pytest.mark.parametrize("shape", ATTRIBUTED)
    def test_every_shape_the_worker_attributes_is_attributed_here(
        self, shape: dict
    ) -> None:
        payload = _payload(**shape)
        assert script._offered(_event(payload)).store_id == REAL_STORE
        assert (script._offered(_event(payload)).store_id
                == provisioning._offered_store_id(payload))

    @pytest.mark.parametrize("shape", UNATTRIBUTED)
    def test_every_shape_the_worker_refuses_is_refused_here(
        self, shape: dict
    ) -> None:
        payload = _payload(**shape)
        assert script._offered(_event(payload)).store_id is None
        assert (script._offered(_event(payload)).store_id
                == provisioning._offered_store_id(payload))

    def test_the_app_id_is_never_read_as_a_store(self) -> None:
        """``data.id`` is 1410006361 on every delivery from every store.
        Reading it would label every credential identically — an answer that is
        always present, always equal and always wrong."""
        offered = script._offered(_event(_payload()))
        assert offered.store_id is None
        assert offered.store_id != str(APP_ID)

    def test_the_two_drifts_that_made_this_file_necessary(self) -> None:
        """Named explicitly, so a re-introduction of the old line fails here
        with the reason attached rather than somewhere downstream."""
        # 1. attributed by the worker, «none in payload» to the old script
        attributed = _payload(data={"merchant": REAL_STORE})
        old = attributed.get("merchant")
        assert old is None                                   # the old reader
        assert provisioning._offered_store_id(attributed) == REAL_STORE
        assert script._offered(_event(attributed)).store_id == REAL_STORE

        # 2. refused by the worker, invented by the old script
        invented = _payload(merchant={"domain": "x.salla.sa"})
        assert str(invented.get("merchant")) not in ("", "None")   # old reader
        assert provisioning._offered_store_id(invented) is None
        assert script._offered(_event(invented)).store_id is None


class TestTheExpiryAndTheFieldsComeFromTheWorker:
    def test_the_expiry_is_the_credential_modules_parser(self) -> None:
        """``data.expires`` is an ABSOLUTE unix epoch — the live-verified fact.
        Read as a duration it would date the token to 1970."""
        epoch = 1785334959          # the real 2026-07-15 value
        offered = script._offered(_event(_payload(
            merchant=REAL_STORE, data={"expires": epoch})))
        assert offered.expires_at == tokens.parse_expiry(epoch)
        assert offered.expires_at == datetime(2026, 7, 29, 14, 22, 39, tzinfo=UTC)

    def test_an_unreadable_expiry_is_unknown_not_now(self) -> None:
        offered = script._offered(_event(_payload(
            merchant=REAL_STORE, data={"expires": "not a date"})))
        assert offered.expires_at is None

    def test_a_missing_refresh_token_means_inherit_and_an_empty_one_means_clear(
        self,
    ) -> None:
        """The distinction the writer makes and the old report flattened:
        ``None`` keeps what is on disk, ``""`` erases it."""
        payload = _payload(merchant=REAL_STORE)
        payload["data"].pop("refresh_token")
        assert script._offered(_event(payload)).refresh is None
        payload["data"]["refresh_token"] = ""
        assert script._offered(_event(payload)).refresh == ""

    def test_the_offered_fields_are_the_ones_the_apply_hands_the_writer(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """THE DRIFT TEST. Not «are they equal today» by inspection — the
        script's four values are compared with what ``_apply_authorize``
        actually passes to ``store_credentials``, captured from a real run."""
        recorded: dict[str, Any] = {}

        def recorder(access_token: str, **kwargs: Any) -> tokens.StoreOutcome:
            recorded["access"] = access_token
            recorded.update(kwargs)
            return tokens.StoreOutcome.STORED

        for shape in ({"merchant": REAL_STORE}, {"store_id": REAL_STORE},
                      {"data": {"merchant": REAL_STORE}},
                      {"merchant": {"id": REAL_STORE}}):
            recorded.clear()
            event_id = seed(owner_session, payload=_payload(**shape))
            event = row(owner_session, event_id)
            offered = script._offered(event)

            original = tokens.store_credentials
            tokens.store_credentials = recorder  # type: ignore[assignment]
            try:
                provisioning._apply_authorize(owner_session, event, RawAdmin())
            finally:
                tokens.store_credentials = original  # type: ignore[assignment]

            assert recorded["access"] == offered.access
            assert recorded["refresh_token"] == offered.refresh
            assert recorded["expires_at"] == offered.expires_at
            assert recorded["store_id"] == offered.store_id == REAL_STORE


# ── the promise: predict == apply ────────────────────────────────────────────


def _bytes_of(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _promise(
    session: Session, event_id: uuid.UUID, *, allow: bool = False,
) -> tuple[Any, str]:
    """Predict, then apply, then insist they agreed.

    Returns the prediction and the row status the apply really produced. Every
    caller below asserts on the prediction afterwards, which is the point: the
    values the operator READ are the values the machine USED.
    """
    env_file = tokens.ENV_FILE
    before = _bytes_of(env_file)
    backup = env_file.with_name(env_file.name + ".bak")
    backup_before = _bytes_of(backup)

    event = row(session, event_id)
    held = script._held(env_file)
    offered = script._offered(event)
    writer = partial(tokens.store_credentials, allow_store_change=allow)
    prediction = script._predict(event, offered, held, writer)

    # THE DRY RUN WROTE NOTHING. Asserted before the apply, on the real bytes.
    assert _bytes_of(env_file) == before
    assert _bytes_of(backup) == backup_before
    assert row(session, event_id).processing_status == "received"

    original = tokens.store_credentials
    tokens.store_credentials = writer  # type: ignore[assignment]
    try:
        provisioning._apply_authorize(session, event, RawAdmin())
    finally:
        tokens.store_credentials = original  # type: ignore[assignment]

    status = row(session, event_id).processing_status
    after = script._held(env_file)
    assert status == prediction.row_status
    assert script.fingerprint(after) == script.fingerprint(prediction.after)
    # …and «writes» meant what it said.
    assert (_bytes_of(env_file) != before) == prediction.writes
    return prediction, status


def _hold(access: str = FAKE_ACCESS, *, store: str | None = DEMO_STORE,
          refresh: str = FAKE_REFRESH, expires: datetime | None = None) -> None:
    tokens.store_credentials(
        access, refresh_token=refresh,
        expires_at=expires or datetime(2026, 8, 21, 9, 37, tzinfo=UTC),
        store_id=store, env_file=tokens.ENV_FILE, allow_store_change=True,
    )


class TestTheDryRunPredictsExactlyWhatTheApplyDoes:
    """Every branch of ``_apply_authorize``, predicted by the same code."""

    @pytest.mark.parametrize("shape", ATTRIBUTED)
    def test_on_a_fresh_host_every_store_id_shape_lands_identically(
        self, owner_session: Session, clean_billing: None, shape: dict
    ) -> None:
        event_id = seed(owner_session, payload=_payload(**shape))
        prediction, _ = _promise(owner_session, event_id)
        assert prediction.row_status == "processed"
        assert prediction.outcome is tokens.StoreOutcome.STORED
        assert prediction.writes
        assert prediction.after.store_id == REAL_STORE

    def test_a_redelivery_of_what_we_already_hold_changes_nothing(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        expires = datetime(2026, 8, 21, 9, 37, tzinfo=UTC)
        _hold(FAKE_ACCESS, store=REAL_STORE, expires=expires)
        event_id = seed(owner_session, payload=_payload(
            merchant=REAL_STORE,
            data={"access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH,
                  "expires": int(expires.timestamp())},
        ))
        prediction, _ = _promise(owner_session, event_id)
        assert prediction.outcome is tokens.StoreOutcome.UNCHANGED
        assert not prediction.writes
        assert prediction.row_status == "processed"

    def test_an_older_delivery_of_the_same_grant_is_stale_and_not_written(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=REAL_STORE,
              expires=datetime(2026, 8, 21, 9, 37, tzinfo=UTC))
        event_id = seed(owner_session, payload=_payload(
            merchant=REAL_STORE,
            data={"expires": int(
                datetime(2026, 8, 10, 9, 37, tzinfo=UTC).timestamp())},
        ))
        prediction, _ = _promise(owner_session, event_id)
        assert prediction.outcome is tokens.StoreOutcome.STALE
        assert not prediction.writes
        assert prediction.after.access_token == FAKE_ACCESS   # untouched

    def test_a_foreign_store_is_deferred_and_the_row_keeps_the_credential(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE))
        prediction, status = _promise(owner_session, event_id, allow=False)
        assert prediction.refusal == "foreign_store"
        assert prediction.row_status == script.PENDING_STATUS == status
        assert not prediction.writes
        assert prediction.after.store_id == DEMO_STORE

    def test_the_same_crossing_with_the_confirmation_lands(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE))
        prediction, _ = _promise(owner_session, event_id, allow=True)
        assert prediction.refusal is None
        assert prediction.outcome is tokens.StoreOutcome.STORED
        assert prediction.after.store_id == REAL_STORE
        # the payload carried its own refresh token, so there is a renewal path
        assert prediction.after.refresh_token == FAKE_REFRESH_2

    def test_a_crossing_without_a_refresh_token_is_predicted_as_cleared(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The inheritance rule the report used to restate and now reads off
        the writer's own mapping: across a store change the held refresh token
        is not inherited, so a payload carrying none leaves NO renewal path."""
        _hold(FAKE_ACCESS, store=DEMO_STORE, refresh=FAKE_REFRESH)
        payload = _payload(merchant=REAL_STORE)
        payload["data"].pop("refresh_token")
        event_id = seed(owner_session, payload=payload)
        prediction, _ = _promise(owner_session, event_id, allow=True)
        assert prediction.after.refresh_token == ""
        assert "CLEARED" in script._refresh_line(
            tokens.SallaCredentials(access_token=FAKE_ACCESS,
                                    refresh_token=FAKE_REFRESH),
            prediction.after,
        )

    def test_a_payload_naming_no_store_is_terminal_without_a_human(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The branch the old dry run got exactly backwards: it printed «WOULD
        store this credential» for a payload the worker marks ``failed``."""
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload())
        prediction, status = _promise(owner_session, event_id, allow=False)
        assert prediction.refusal == "no_store_id"
        assert prediction.row_status == "failed" == status
        assert not prediction.writes
        assert prediction.after.access_token == FAKE_ACCESS   # nothing replaced

    def test_a_payload_naming_no_store_is_accepted_once_a_human_signs(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload())
        prediction, _ = _promise(owner_session, event_id, allow=True)
        assert prediction.outcome is tokens.StoreOutcome.STORED
        assert prediction.after.access_token == FAKE_ACCESS_2
        # unstated store means «the one we hold» — the refresh path's meaning
        assert prediction.after.store_id == DEMO_STORE

    def test_a_payload_with_no_usable_credential_is_terminal(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(
            merchant=REAL_STORE, data={"access_token": ""}))
        prediction, status = _promise(owner_session, event_id, allow=True)
        assert prediction.refusal == "no_credential"
        assert prediction.row_status == "failed" == status

    def test_an_unsigned_row_is_refused_without_its_payload_being_used(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE),
                        signature_valid=False)
        event = row(owner_session, event_id)
        prediction = script._predict(
            event, script._offered(event), script._held(tokens.ENV_FILE),
            partial(tokens.store_credentials, allow_store_change=True),
        )
        assert prediction.refusal == "unsigned"
        assert prediction.row_status == "failed"
        assert not prediction.writes


# ── the CLI: the gate, and the silence ───────────────────────────────────────


def _run(argv: list[str], session: Session) -> tuple[int, str]:
    """The script as the operator runs it, on the test session.

    ``session_factory`` is the seam that makes this possible at all: without it
    the tool could only be exercised by pointing it at a live database, which
    is why it had no tests for the branch that matters.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = script.main(
            ["--env-file", str(tokens.ENV_FILE), *argv],
            session_factory=lambda: contextlib.nullcontext(session),
        )
    return code, buffer.getvalue()


class TestTheConfirmationGateIsTheWritersOwn:
    def test_a_crossing_is_refused_without_the_flag(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        before = _bytes_of(tokens.ENV_FILE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE))

        code, out = _run([], owner_session)

        assert code == 3
        assert "REFUSING" in out and REAL_STORE in out
        assert _bytes_of(tokens.ENV_FILE) == before
        assert row(owner_session, event_id).processing_status == "received"

    def test_the_flag_alone_is_enough_to_let_it_through(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE))

        code, out = _run(["--allow-store-change"], owner_session)
        assert code == 0
        assert "store change confirmed" in out
        assert row(owner_session, event_id).processing_status == "received"

        code, out = _run(["--allow-store-change", "--apply"], owner_session)
        assert code == 0, out
        assert row(owner_session, event_id).processing_status == "processed"
        assert script._held(tokens.ENV_FILE).store_id == REAL_STORE

    def test_a_payload_with_no_store_takes_the_same_confirmation(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The other side of the same question — and the script must not burn
        the row answering it on the operator's behalf: ``--apply`` without the
        confirmation would have marked it ``failed``, terminally."""
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload())

        code, out = _run(["--apply"], owner_session)
        assert code == 3
        assert "names no store" in out
        assert row(owner_session, event_id).processing_status == "received"
        assert script._held(tokens.ENV_FILE).access_token == FAKE_ACCESS

        code, out = _run(["--allow-store-change", "--apply"], owner_session)
        assert code == 0, out
        assert row(owner_session, event_id).processing_status == "processed"
        assert script._held(tokens.ENV_FILE).access_token == FAKE_ACCESS_2

    def test_an_unsigned_row_named_by_id_is_refused_not_consumed(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE),
                        signature_valid=False)

        code, out = _run(["--event-id", str(event_id), "--apply",
                          "--allow-store-change"], owner_session)

        assert code == 2
        assert "not signature-valid" in out
        assert row(owner_session, event_id).processing_status == "received"


class TestADryRunWritesNothingAnywhere:
    @pytest.mark.parametrize(
        "shape, argv",
        [
            ({"merchant": REAL_STORE}, []),
            ({"merchant": REAL_STORE}, ["--allow-store-change"]),
            ({}, []),
            ({}, ["--allow-store-change"]),
            ({"data": {"merchant": REAL_STORE}}, ["--allow-store-change"]),
            ({"merchant": DEMO_STORE}, []),
        ],
    )
    def test_the_file_the_backup_and_the_row_are_all_untouched(
        self, owner_session: Session, clean_billing: None,
        shape: dict, argv: list[str],
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        env_file = tokens.ENV_FILE
        backup = env_file.with_name(env_file.name + ".bak")
        before, backup_before = _bytes_of(env_file), _bytes_of(backup)
        mtime = env_file.stat().st_mtime_ns
        event_id = seed(owner_session, payload=_payload(**shape))

        _run(argv, owner_session)

        assert _bytes_of(env_file) == before
        assert env_file.stat().st_mtime_ns == mtime
        assert _bytes_of(backup) == backup_before
        assert row(owner_session, event_id).processing_status == "received"

    def test_the_prediction_leaves_the_writer_exactly_as_it_found_it(
        self,
    ) -> None:
        """The seams are borrowed, not taken. A dry run that forgot to put
        ``_atomic_write`` back would disarm every later write in the process —
        including the ``--apply`` two lines further down."""
        originals = {name: getattr(tokens, name) for name in script._WRITE_SEAMS}
        with script._writes_disabled():
            for name in script._WRITE_SEAMS:
                assert getattr(tokens, name) is not originals[name]
        for name, original in originals.items():
            assert getattr(tokens, name) is original

    def test_a_renamed_seam_stops_the_script_instead_of_letting_it_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure mode this harness must never have: the writer running
        for real during a dry run because a private helper was renamed."""
        monkeypatch.delattr(tokens, "_atomic_write")
        with pytest.raises(RuntimeError, match="_atomic_write"):
            with script._writes_disabled():
                pass                                    # pragma: no cover

    def test_the_seams_the_script_borrows_all_exist(self) -> None:
        for name in script._WRITE_SEAMS:
            assert callable(getattr(tokens, name, None)), name
        # …and the readers it imports rather than copies.
        for module, name in (
            (provisioning, "_offered_store_id"), (provisioning, "_verified"),
            (provisioning, "_operator_confirmed"), (provisioning, "_RESTART_HINT"),
            (provisioning, "SALLA_PROVIDER"), (provisioning, "_AUTHORIZE_EVENT"),
            (tokens, "parse_expiry"), (tokens, "store_identity"),
            (tokens, "_credentials_from"), (tokens, "_read_env_file"),
        ):
            assert getattr(module, name, None) is not None, name


def _all_renderings(obj: object) -> list[str]:
    """Every way a value turns into text without anyone asking it to.

    ``str`` and ``repr`` are different slots; ``f"{x}"`` goes through
    ``__format__`` (which only DELEGATES to ``__str__`` for the empty format
    spec); ``"%s" % (x,)`` is the substitution ``logging`` performs, and it is
    built here as a real ``LogRecord`` because that is the one that would
    actually appear in a file; and a value inside a container is rendered by
    the container's repr, which is how an object nobody logged deliberately
    ends up in a traceback frame.
    """
    record = logging.LogRecord(
        "career.salla", logging.INFO, __file__, 1, "%s", (obj,), None,
    )
    # The `%` and `.format` spellings are the POINT here, not style: they are
    # the substitutions the stdlib performs on our behalf. `noqa` rather than
    # modernised — an f-string would test a different slot.
    return [
        repr(obj), str(obj), f"{obj}", f"{obj!r}", f"{obj!s}", format(obj),
        "{}".format(obj),  # noqa: UP032
        "%s" % (obj,),  # noqa: UP031
        "%r" % (obj,),  # noqa: UP031
        record.getMessage(), repr([obj]), repr({"k": obj}), repr((obj,)),
    ]


class TestNothingLeaks:
    def test_no_credential_reaches_the_terminal_on_any_path(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        _hold(FAKE_ACCESS, store=DEMO_STORE)
        printed = ""
        for shape, argv in (
            ({"merchant": REAL_STORE}, []),
            ({"merchant": REAL_STORE}, ["--allow-store-change"]),
            ({}, []),
            ({"merchant": REAL_STORE}, ["--allow-store-change", "--apply"]),
        ):
            seed(owner_session, payload=_payload(**shape))
            printed += _run(argv, owner_session)[1]

        assert printed  # the paths really ran
        for secret in (FAKE_ACCESS, FAKE_ACCESS_2, FAKE_REFRESH, FAKE_REFRESH_2):
            assert secret not in printed
        # the operator alert IS shown, and it is the worker's own text
        assert provisioning._RESTART_HINT in printed

    def test_the_offered_tuple_refuses_to_render_its_token(self) -> None:
        """Every rendering protocol, not just ``repr``.

        ``repr`` alone was what this asserted, and ``repr`` alone is not the
        question: a value reaches a log line through ``str``, through an
        f-string, through the ``%``-substitution ``logging`` does for
        ``logger.info("%s", offered)``, and through the repr of whatever list
        or dict it happens to be sitting in when a traceback prints a frame.
        Each of those dispatches differently, and each is checked.
        """
        offered = script._offered(_event(_payload(merchant=REAL_STORE)))
        for rendered in _all_renderings(offered):
            assert FAKE_ACCESS_2 not in rendered
            assert FAKE_REFRESH_2 not in rendered
        assert repr(offered) == str(offered)     # one story, told twice
        assert REAL_STORE in repr(offered)       # it still says something
        assert "access=set" in repr(offered)


class TestNoContainerHereCanRenderItsSecret:
    """THE RATCHET, and it is not about ``Offered``.

    ``Offered`` hid its token by writing ``__str__ = __repr__`` under the
    ``def __repr__``. Inside a ``NamedTuple`` body an assignment is the syntax
    for «field with a default», so that line reads to a type checker as a fifth
    field called ``__str__`` — mypy said so, and said it about the one line in
    this file whose whole job is keeping a live access token out of a log. It
    happened to bind (the metaclass ``setattr``s every non-field name onto the
    generated class, and ``namedtuple`` would have REFUSED a dunder field
    outright), so the redaction worked — while being invisible to the only tool
    that could have told us the day it stopped.

    «It happens to work» is not a property of an object, so the guard here does
    not read the source or trust the type checker: it BUILDS an instance of
    every secret-carrying type this script defines and renders it through every
    path, which catches a hiding method that fails to bind however it fails —
    forgotten, shadowed, defined in a scope that never reaches the class, or
    replaced by a container whose repr nobody overrode.

    Why not ``tests/test_salla_token_refresh.TestNoDataclassCarriesAToken``,
    which is the same idea: it cannot see this module, twice over. It looks at
    ``dataclasses.is_dataclass`` classes, and these are ``NamedTuple``s (a
    deliberate choice — see :class:`TestTheScriptStaysLoadable`); and its
    word list is ``token/secret/password/credential/key``, while the two fields
    holding live credentials here are called ``access`` and ``refresh``. Both
    halves are widened below rather than there, because widening the wordlist
    in that file would change which classes ITS ratchet polices — somebody
    else's test, somebody else's failure.
    """

    #: Deliberately broad, for the reason the sibling ratchet gives: a false
    #: positive costs one explicit ``__repr__``, a false negative costs a
    #: credential in a traceback.
    SECRET_WORDS = ("token", "secret", "password", "credential", "key",
                    "access", "refresh")

    SENTINEL = "ory_at_RATCHET-SENTINEL-OBVIOUSLY-NOT-A-REAL-TOKEN"

    def _secret_fields(self, klass: type) -> list[str]:
        if dataclasses.is_dataclass(klass):
            names = [f.name for f in dataclasses.fields(klass)]
        else:
            names = list(getattr(klass, "_fields", ()))
        return [n for n in names
                if any(word in n.lower() for word in self.SECRET_WORDS)]

    def carriers(self, module: Any) -> list[type]:
        """Types DEFINED BY this module that hold a secret-shaped field."""
        return [
            obj for obj in vars(module).values()
            if inspect.isclass(obj)
            and getattr(obj, "__module__", None) == module.__name__
            and self._secret_fields(obj)
        ]

    def offenders(self, classes: Iterable[type]) -> list[str]:
        """Which of these would put a secret into text, and by which path."""
        found = []
        for klass in classes:
            secret = self._secret_fields(klass)
            if dataclasses.is_dataclass(klass):
                names = [f.name for f in dataclasses.fields(klass)]
            else:
                names = list(klass._fields)  # type: ignore[attr-defined]
            # A sentinel in the secret slots, `None` everywhere else: a hiding
            # `__repr__` reports presence, so `None` is enough for it to say
            # something, and it cannot be mistaken for the sentinel.
            instance = klass(**{n: self.SENTINEL if n in secret else None
                                for n in names})
            leaked = sorted({
                rendered for rendered in _all_renderings(instance)
                if self.SENTINEL in rendered
            })
            if leaked:
                found.append(f"{klass.__name__} renders {secret} — e.g. "
                             f"{leaked[0]}")
        return found

    def test_every_secret_carrying_type_in_the_script_hides_it_everywhere(
        self,
    ) -> None:
        carriers = self.carriers(script)
        # A ratchet that matches nothing passes forever. Name the one that
        # exists today, so a rename that slips past `SECRET_WORDS` fails here
        # instead of quietly reducing this to an assertion about an empty list.
        assert script.Offered in carriers
        assert not self.offenders(carriers), (
            "these types hold a credential and would put it in a log line or a "
            "traceback: " + "; ".join(self.offenders(carriers))
        )

    def test_the_ratchet_catches_a_hiding_method_that_does_not_bind(
        self,
    ) -> None:
        """Teeth. The failure mode is silent by construction — the source says
        the token is hidden and the object says otherwise — so the detector is
        run against types that really do leak, and must report both."""

        class Forgot(NamedTuple):
            access_token: str
            store_id: str | None

        class Shadowed(NamedTuple):
            access_token: str
            store_id: str | None

            def __repr__(self) -> str:      # pragma: no cover - never bound
                return "Shadowed(access_token=set)"

        # The way it stops binding in practice: something later in the body
        # puts the generated renderer back. (Assigning `__repr__` after the
        # `def` is the assignment-shaped mistake this whole class is about.)
        Shadowed.__repr__ = tuple.__repr__  # type: ignore[method-assign]

        offenders = self.offenders([Forgot, Shadowed])
        assert len(offenders) == 2, offenders
        assert any("Forgot" in o for o in offenders)
        assert any("Shadowed" in o for o in offenders)
        # …and it does not cry wolf over the type that is actually safe.
        assert self.offenders([script.Offered]) == []

    def test_no_namedtuple_here_smuggled_a_dunder_into_its_fields(self) -> None:
        """The literal thing mypy reported, asked of the objects.

        ``namedtuple`` refuses an underscore-leading field name outright, which
        is WHY ``__str__ = __repr__`` was a method and not a field — but that
        is a fact about CPython, not a promise this file is entitled to, and it
        is one line to stop relying on it.
        """
        for obj in vars(script).values():
            fields = getattr(obj, "_fields", None)
            if not (inspect.isclass(obj) and isinstance(fields, tuple)
                    and getattr(obj, "__module__", None) == script.__name__):
                continue
            assert not [f for f in fields if f.startswith("_")], (
                f"{obj.__name__} has a private-looking field: a method written "
                "as an assignment in a NamedTuple body is a field declaration."
            )
            for name in ("__repr__", "__str__"):
                assert callable(vars(obj).get(name)) or not self._secret_fields(
                    obj
                ), f"{obj.__name__} carries a secret and inherits {name}"


class TestTheScriptAgreesWithTheWorkerAboutWhatIsPending:
    def test_the_status_it_selects_is_the_status_the_worker_sweeps(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """``PENDING_STATUS`` is a literal in this script and a literal in
        ``process_pending_webhooks``. Rather than trust that they match, seed a
        row in the script's status and let the WORKER prove it picks it up."""
        _hold(FAKE_ACCESS, store=REAL_STORE)
        event = WebhookEvent(
            id=uuid.uuid4(), provider=provisioning.SALLA_PROVIDER,
            event_type=provisioning._AUTHORIZE_EVENT,
            event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
            payload=_payload(merchant=REAL_STORE),
            processing_status=script.PENDING_STATUS,
        )
        owner_session.add(event)
        owner_session.commit()

        sweep(owner_session)

        assert row(owner_session, event.id).processing_status == "processed"

    def test_the_dry_run_reports_whether_the_body_is_still_retained(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """``ForeignStoreCredential`` promises the row keeps the credential
        «for as long as it can still be used». The script reports that promise
        from the module that keeps it (``intake``), not from a guess."""
        from career.webhooks import intake

        _hold(FAKE_ACCESS, store=DEMO_STORE)
        event_id = seed(owner_session, payload=_payload(merchant=REAL_STORE))
        assert intake.credential_is_still_consumable(
            row(owner_session, event_id), now=datetime.now(UTC)
        )
        code, out = _run([], owner_session)
        assert code == 3
        assert "still live : yes" in out

        dead = seed(owner_session, payload=_payload(
            merchant=REAL_STORE,
            data={"expires": int(
                (datetime.now(UTC) - timedelta(days=1)).timestamp())},
        ))
        code, out = _run(["--event-id", str(dead)], owner_session)
        assert "still live : NO" in out


class TestTheScriptStaysLoadable:
    def test_it_loads_without_being_registered_in_sys_modules(self) -> None:
        """``tests/test_salla_tokens.py`` loads it that way. A ``@dataclass``
        at module level would break that loader — and the break would land in
        somebody else's file."""
        path = REPO / "scripts" / "consume_stored_authorize.py"
        spec = importlib.util.spec_from_file_location("consume_unregistered", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)          # must not raise
        assert callable(module.main)
