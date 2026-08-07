"""The Salla credential: consuming ``app.store.authorize`` (CHANGELOG §29).

Salla's «Easy Mode» never displays the merchant access token — it is handed to
us exactly once, inside the signed install webhook. The code used to define
that event and deliberately not consume it, so it fell to the ``else`` that
marks a row ``ignored``; the token captured by hand on 2026-07-15 expired on
2026-07-29 and the sale path died with it.

Every test here is written against the payload SALLA ACTUALLY SENDS, read off
the live 2026-07-15 rows in staging rather than from memory:

    {"event": "app.store.authorize", "merchant": <int>, "created_at": "...",
     "data": {"access_token": …, "refresh_token": …, "expires": <epoch>,
              "scope": …, "token_type": "bearer",
              "id": <app id>, "app_name": …, "app_type": …,
              "app_description": …}}

Two field facts that a guess would have got wrong, and that the tests pin:
``data.expires`` is an ABSOLUTE unix epoch (1785334959 → 2026-07-29T14:22:39Z,
the exact expiry the operator recorded that day), and the store is ``merchant``
at the TOP level — ``data.id`` is the APP id.

Every token literal below is an obvious fake. No test writes to the real
secrets file: ``tokens.ENV_FILE`` is redirected into tmp_path by an autouse
fixture, which also restores the process environment afterwards.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from career import logging_filters
from career.config import get_settings
from career.db.models import WebhookEvent
from career.salla import tokens
from career.salla.client import FakeSallaClient
from career.salla.provisioning import (
    process_pending_webhooks,
    reset_salla_backoff,
)

# Obvious fakes. Shaped like Salla's Ory tokens so the module sees realistic
# input, but no part of any is a real credential.
FAKE_ACCESS = "ory_at_FAKE-ACCESS-TOKEN-FOR-TESTS-ONLY-0000000001"
FAKE_REFRESH = "ory_rt_FAKE-REFRESH-TOKEN-FOR-TESTS-ONLY-000000001"
FAKE_ACCESS_2 = "ory_at_FAKE-ACCESS-TOKEN-FOR-TESTS-ONLY-0000000002"
FAKE_REFRESH_2 = "ory_rt_FAKE-REFRESH-TOKEN-FOR-TESTS-ONLY-000000002"
FAKE_STORE = "855028708"

#: The real value from the live delivery — a timestamp, not a secret. It is
#: here because it is the evidence for `expires` being absolute.
LIVE_EXPIRES_EPOCH = 1785334959

CATALOG = {"prod_pro": "professional"}
PRICING: dict[str, tuple] = {}


def authorize_payload(
    *,
    access: str = FAKE_ACCESS,
    refresh: str | None = FAKE_REFRESH,
    expires: object = None,
    merchant: object = FAKE_STORE,
) -> dict:
    """The live shape, including the app metadata Salla really sends — a
    payload trimmed to the two fields under test would not prove that the
    reader picks `merchant` over `data.id`."""
    data: dict = {
        "access_token": access,
        "expires": (
            expires if expires is not None
            else int((datetime.now(UTC) + timedelta(days=14)).timestamp())
        ),
        "scope": "settings.read customers.read orders.read products.read "
                 "offline_access",
        "token_type": "bearer",
        "id": 1410006361,                      # the APP id, never the store's
        "app_name": "تيستفهد",
        "app_type": "private",
        "app_description": "a private partner app",
    }
    if refresh is not None:
        data["refresh_token"] = refresh
    payload: dict = {
        "event": "app.store.authorize",
        "created_at": "2026-08-07 12:00:00",
        "data": data,
    }
    if merchant is not None:
        payload["merchant"] = merchant
    return payload


UNINSTALL_PAYLOAD = {
    "event": "app.uninstalled",
    "merchant": FAKE_STORE,
    "created_at": "2026-08-07 12:00:00",
    "data": {
        "id": 1410006361, "app_name": "تيستفهد", "app_type": "private",
        "app_description": "a private partner app",
        "installation_date": "2026-07-15 17:22:39",
        "uninstallation_date": "2026-08-07 12:00:00",
        "refunded": False, "store_type": "live",
    },
}


class RawAdmin:
    """Records admin text VERBATIM.

    Deliberately not ``FakeTelegramAdminClient``: that one runs every message
    through ``sanitize_secret_text`` before storing it, so a leak test using it
    would be asserting on laundered evidence and would pass even if the alert
    had been built with the token in it.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_admin(self, text: str) -> str:
        self.messages.append(text)
        return "raw.1"


@pytest.fixture(autouse=True)
def isolated_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the module at a throwaway secrets file and put the process
    environment back afterwards.

    ``store_credentials`` publishes what it wrote into ``os.environ`` and
    clears the Settings cache on purpose (so the expiry warner in a live
    worker stops reporting a token that has just been replaced). In a test
    process that would leak a fake credential into every later test, so it is
    snapshotted and restored here.
    """
    env_file = tmp_path / ".env.staging"
    monkeypatch.setattr(tokens, "ENV_FILE", env_file)
    keys = (tokens.KEY_ACCESS, tokens.KEY_REFRESH, tokens.KEY_EXPIRES,
            tokens.KEY_STORE)
    before = {k: os.environ.get(k) for k in keys}
    reset_salla_backoff()
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        reset_salla_backoff()


def seed(
    session: Session, *, payload: dict, event_type: str = "app.store.authorize",
    signature_valid: bool = True, provider: str = "salla",
) -> uuid.UUID:
    """One raw intake row, exactly as ``career.webhooks.intake`` writes it."""
    event = WebhookEvent(
        id=uuid.uuid4(), provider=provider, event_type=event_type,
        event_fingerprint=f"test:{uuid.uuid4()}",
        signature_valid=signature_valid, payload=payload,
        processing_status="received",
    )
    session.add(event)
    session.commit()
    return event.id


def row(session: Session, event_id: uuid.UUID) -> WebhookEvent:
    session.expire_all()
    return session.execute(
        select(WebhookEvent).where(WebhookEvent.id == event_id)
    ).scalar_one()


def sweep(session: Session, admin: RawAdmin | None = None) -> None:
    reset_salla_backoff()
    process_pending_webhooks(
        session, salla_client=FakeSallaClient({}), product_catalog=CATALOG,
        expected_pricing=PRICING, admin_client=admin,
    )


# ── the file itself ──────────────────────────────────────────────────────────


class TestTheEnvFileIsTheStore:
    def test_round_trip_with_closed_permissions(self, tmp_path) -> None:
        path = tmp_path / ".env.staging"
        # The seeded file names the store it holds. Amended 2026-08-07 with the
        # identity fix (TestTheStoreIsTheIdentity): a file holding a token whose
        # store was never recorded cannot answer «is this the same grant?», and
        # a credential arriving for a NAMED store is now an operator decision
        # rather than a silent replacement. That is the whole point of the fix,
        # so this test — which is about the round trip and about leaving
        # unrelated lines alone — states the ordinary case instead of
        # accidentally standing on the ambiguous one.
        path.write_text(
            "DB_PASSWORD=unrelated\n# a comment\n"
            f"SALLA_API_KEY=old\nSALLA_STORE_ID={FAKE_STORE}\n"
        )
        expires = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)

        outcome = tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH, expires_at=expires,
            store_id=FAKE_STORE, env_file=path,
        )

        assert outcome is tokens.StoreOutcome.STORED
        creds = tokens.current(path)
        assert creds.access_token == FAKE_ACCESS
        assert creds.refresh_token == FAKE_REFRESH
        assert creds.expires_at == expires
        assert creds.store_id == FAKE_STORE
        assert creds.present
        # 0600 — the whole reason §29 chose a file over a row.
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # every unrelated line survives, in place
        body = path.read_text()
        assert "DB_PASSWORD=unrelated" in body
        assert "# a comment" in body
        assert body.index("DB_PASSWORD") < body.index("# a comment")

    def test_a_backup_is_taken_before_the_replace(self, tmp_path) -> None:
        path = tmp_path / ".env.staging"
        path.write_text("DB_PASSWORD=unrelated\n")

        tokens.store_credentials(FAKE_ACCESS, env_file=path)

        backup = tmp_path / ".env.staging.bak"
        assert "DB_PASSWORD=unrelated" in backup.read_text()
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600


    def test_a_newline_in_a_token_is_refused(self, tmp_path) -> None:
        """An env file is parsed one ``KEY=value`` line at a time, so a value
        carrying a newline is not a corrupt token — it is an injected
        VARIABLE. Nothing may be written."""
        path = tmp_path / ".env.staging"
        path.write_text("DB_PASSWORD=unrelated\n")

        with pytest.raises(tokens.CredentialError):
            tokens.store_credentials(
                f"{FAKE_ACCESS}\nDB_PASSWORD=injected", env_file=path,
            )

        assert path.read_text() == "DB_PASSWORD=unrelated\n"
        assert not tokens.current(path).present

    @pytest.mark.parametrize("bad", ["", "   ", "short"])
    def test_a_missing_or_stunted_token_is_refused(self, tmp_path, bad) -> None:
        path = tmp_path / ".env.staging"
        with pytest.raises(tokens.CredentialError):
            tokens.store_credentials(bad, env_file=path)
        assert not path.exists()

    def test_a_write_failure_raises_and_names_no_value(self, tmp_path) -> None:
        """The retryable half of §29's fourth condition: a disk problem is
        reported as one, and the message carries a path and nothing else."""
        path = tmp_path / "nonexistent-dir" / ".env.staging"

        with pytest.raises(tokens.CredentialWriteError) as caught:
            tokens.store_credentials(FAKE_ACCESS, env_file=path)

        assert FAKE_ACCESS not in str(caught.value)
        assert FAKE_ACCESS not in repr(caught.value)

    def test_credentials_never_render_their_values(self) -> None:
        creds = tokens.SallaCredentials(
            access_token=FAKE_ACCESS, refresh_token=FAKE_REFRESH,
            expires_at=datetime(2026, 8, 21, tzinfo=UTC), store_id=FAKE_STORE,
        )
        # Every way a value reaches a log line: repr in a traceback frame, str
        # in an f-string, and the %-substitution stdlib does for
        # ``logger.info("%s", creds)`` — built as a real LogRecord rather than
        # imitated, because that is the path that would actually leak.
        record = logging.LogRecord(
            "career.salla", logging.INFO, __file__, 1, "%s", (creds,), None,
        )
        for rendered in (repr(creds), str(creds), f"{creds}",
                         record.getMessage()):
            assert FAKE_ACCESS not in rendered
            assert FAKE_REFRESH not in rendered
        assert "set" in repr(creds)


class TestTheBackupHasALifecycle:
    """2026-08-07. `.env.staging.bak` on this host held a DIFFERENT access
    token from the live file — the demo store's, rotated that morning, valid
    until the 21st — and nothing rotated, pruned, shredded or even knew about
    it. It was a second live credential with no owner.

    The policy: ONE generation, carrying no credential, shredded rather than
    unlinked, and dead after fourteen days whether or not anything writes
    again. What a superseded credential is recoverable FROM is the signed
    authorize row in `webhook_events` (see `TestTheRefusedCredentialSurvives`),
    not a shadow copy of the secrets file.
    """

    def _rotate_twice(self, path) -> None:
        tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH,
            expires_at=datetime.now(UTC) + timedelta(days=14),
            store_id=FAKE_STORE, env_file=path,
        )
        tokens.store_credentials(
            FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
            expires_at=datetime.now(UTC) + timedelta(days=14),
            store_id=FAKE_STORE, env_file=path,
        )

    def test_the_backup_never_holds_the_credential_it_displaced(
        self, tmp_path
    ) -> None:
        """THE DEFECT. After a rotation the previous access token is still
        live until its own expiry — up to fourteen days — and it was sitting
        in a file no code had ever heard of."""
        path = tmp_path / ".env.staging"
        path.write_text("DB_PASSWORD=unrelated\n")

        self._rotate_twice(path)

        body = tokens._backup_path(path).read_text()
        for secret in (FAKE_ACCESS, FAKE_REFRESH, FAKE_ACCESS_2, FAKE_REFRESH_2):
            assert secret not in body
        # …and the reason it exists is untouched: the lines this module does
        # not own, so a bad merge is recoverable.
        assert "DB_PASSWORD=unrelated" in body
        # The key stays, so a human reading it can see what was removed.
        assert f"{tokens.KEY_ACCESS}=" in body

    def test_only_one_generation_is_kept_and_the_old_one_is_shredded(
        self, tmp_path
    ) -> None:
        path = tmp_path / ".env.staging"
        path.write_text("DB_PASSWORD=first\n")
        tokens.store_credentials(FAKE_ACCESS, env_file=path)
        path.write_text("DB_PASSWORD=second\n")
        tokens.store_credentials(FAKE_ACCESS_2, env_file=path)

        siblings = sorted(p.name for p in tmp_path.iterdir())
        assert siblings.count(".env.staging.bak") == 1
        assert [n for n in siblings if n.startswith(".env.staging")] == [
            ".env.staging", ".env.staging.bak", ".env.staging.lock",
        ]
        assert "DB_PASSWORD=first" not in tokens._backup_path(path).read_text()

    def test_a_superseded_backup_is_overwritten_not_merely_unlinked(
        self, tmp_path
    ) -> None:
        """`unlink` drops a name and leaves the bytes for whatever reads the
        device next. The whole file is a copy of every secret on the host."""
        path = tmp_path / ".env.bak-victim"
        path.write_text("DB_PASSWORD=unrelated\n")

        tokens._shred(path)

        assert not path.exists()

    def test_the_last_backup_expires_even_if_nothing_ever_writes_again(
        self, tmp_path
    ) -> None:
        """The July state: the credential died, nothing rotated for nine days,
        and a full copy of the secrets file would have sat there through all of
        it. The daily timer sweeps it (`refresh_salla_token._sweep_backups`)."""
        path = tmp_path / ".env.staging"
        path.write_text("DB_PASSWORD=unrelated\n")
        tokens.store_credentials(FAKE_ACCESS, env_file=path)
        backup = tokens._backup_path(path)
        assert backup.exists()

        now = datetime.now(UTC) + timedelta(days=tokens.BACKUP_MAX_AGE_DAYS)
        assert tokens.prune_credential_backups(path, now=now) is True

        assert not backup.exists()
        # …and the live file is not touched by any of it.
        assert tokens.current(path).access_token == FAKE_ACCESS

    def test_a_fresh_backup_is_left_alone(self, tmp_path) -> None:
        path = tmp_path / ".env.staging"
        path.write_text("DB_PASSWORD=unrelated\n")
        tokens.store_credentials(FAKE_ACCESS, env_file=path)

        assert tokens.prune_credential_backups(path) is False
        assert tokens._backup_path(path).exists()

    def test_the_sweep_is_silent_when_there_is_no_backup(self, tmp_path) -> None:
        assert tokens.prune_credential_backups(tmp_path / "absent") is False


class TestIdempotenceAndOrdering:
    def test_a_redelivery_writes_nothing(self, tmp_path) -> None:
        path = tmp_path / ".env.staging"
        expires = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
        tokens.store_credentials(FAKE_ACCESS, refresh_token=FAKE_REFRESH,
                                 expires_at=expires, env_file=path)
        before = path.read_text()

        outcome = tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH, expires_at=expires,
            env_file=path,
        )

        assert outcome is tokens.StoreOutcome.UNCHANGED
        assert path.read_text() == before

    def test_an_older_credential_never_overwrites_a_newer_one(self, tmp_path) -> None:
        """Salla redelivers install events — four arrived for one install on
        2026-07-15 — and the refresh timer writes between deliveries. A
        yesterday's-token redelivery must not take a live sale path down."""
        path = tmp_path / ".env.staging"
        fresh = datetime(2026, 8, 21, tzinfo=UTC)
        tokens.store_credentials(FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
                                 expires_at=fresh, env_file=path)

        outcome = tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH,
            expires_at=datetime(2026, 7, 29, tzinfo=UTC), env_file=path,
        )

        assert outcome is tokens.StoreOutcome.STALE
        creds = tokens.current(path)
        assert creds.access_token == FAKE_ACCESS_2
        assert creds.refresh_token == FAKE_REFRESH_2
        assert creds.expires_at == fresh

    def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(
        self, tmp_path
    ) -> None:
        """What the refresh timer needs: an access-only rotation must not
        erase the credential that buys the NEXT rotation."""
        path = tmp_path / ".env.staging"
        tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH, store_id=FAKE_STORE,
            expires_at=datetime(2026, 8, 1, tzinfo=UTC), env_file=path,
        )

        tokens.store_credentials(
            FAKE_ACCESS_2, expires_at=datetime(2026, 8, 21, tzinfo=UTC),
            env_file=path,
        )

        creds = tokens.current(path)
        assert creds.access_token == FAKE_ACCESS_2
        assert creds.refresh_token == FAKE_REFRESH
        assert creds.store_id == FAKE_STORE


class TestTheStoreIsTheIdentity:
    """2026-08-07, live. The ordering guard above treated EXPIRY as identity,
    and expiry is not identity — the store is.

    What actually happened: the file held the demo store's credential, rotated
    minutes earlier by the refresh path to expire 09:37Z. Fahad's real store
    had authorised the app that morning and its credential — the one the
    business runs on — expires 07:24Z, two hours and thirteen minutes EARLIER.
    Compared on expiry alone the real store's token is «an old redelivery» and
    the platform keeps serving from the demo store forever.

    Both merchant numbers and both timestamps below are the real ones. Neither
    is a secret; the credentials themselves never appear here.
    """

    DEMO_STORE = "855028708"
    REAL_STORE = "1275699954"
    DEMO_EXPIRY = datetime(2026, 8, 21, 9, 37, 12, tzinfo=UTC)
    REAL_EXPIRY = datetime(2026, 8, 21, 7, 24, 22, tzinfo=UTC)

    def _held(self, tmp_path, store: str | None):
        path = tmp_path / ".env.staging"
        tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH,
            expires_at=self.DEMO_EXPIRY, store_id=store, env_file=path,
        )
        return path

    def test_another_stores_credential_is_never_stale(self, tmp_path) -> None:
        """THE BUG. An earlier expiry from a DIFFERENT store is not a late
        redelivery — the comparison is meaningless across grants — so the
        verdict must not be STALE, silently keeping the wrong merchant."""
        path = self._held(tmp_path, self.DEMO_STORE)

        with pytest.raises(tokens.ForeignStoreCredential) as caught:
            tokens.store_credentials(
                FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
                expires_at=self.REAL_EXPIRY, store_id=self.REAL_STORE,
                env_file=path,
            )

        # Refused, not silently discarded: both stores are named so the
        # operator can decide, and no credential is.
        assert self.REAL_STORE in str(caught.value)
        assert self.DEMO_STORE in str(caught.value)
        for secret in (FAKE_ACCESS, FAKE_ACCESS_2, FAKE_REFRESH, FAKE_REFRESH_2):
            assert secret not in str(caught.value)
        # nothing written — the incumbent is intact
        assert tokens.current(path).access_token == FAKE_ACCESS
        assert tokens.current(path).store_id == self.DEMO_STORE

    def test_the_operator_can_take_the_other_store_earlier_expiry_and_all(
        self, tmp_path
    ) -> None:
        """The other half: once a human confirms it, the earlier expiry must
        NOT stand in the way. This is the call the consume script makes."""
        path = self._held(tmp_path, self.DEMO_STORE)

        outcome = tokens.store_credentials(
            FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
            expires_at=self.REAL_EXPIRY, store_id=self.REAL_STORE,
            env_file=path, allow_store_change=True,
        )

        assert outcome is tokens.StoreOutcome.STORED
        creds = tokens.current(path)
        assert creds.access_token == FAKE_ACCESS_2
        assert creds.store_id == self.REAL_STORE
        assert creds.expires_at == self.REAL_EXPIRY

    def test_a_genuine_redelivery_from_the_same_store_is_still_refused(
        self, tmp_path
    ) -> None:
        """The reason the guard exists, and it must survive the fix: Salla sent
        four deliveries for one install on 2026-07-15. A week-old redelivery
        from the store we ARE serving still loses to what we hold."""
        path = self._held(tmp_path, self.DEMO_STORE)

        outcome = tokens.store_credentials(
            FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
            expires_at=datetime(2026, 7, 29, 14, 22, 39, tzinfo=UTC),
            store_id=self.DEMO_STORE, env_file=path,
        )

        assert outcome is tokens.StoreOutcome.STALE
        assert tokens.current(path).access_token == FAKE_ACCESS

    def test_an_unrecorded_store_is_a_question_not_a_permission(
        self, tmp_path
    ) -> None:
        """The state this host is actually in: a credential predating
        ``SALLA_STORE_ID``, so the file cannot say whose it is. «I cannot tell»
        is resolved by asking, never by assuming either answer."""
        path = self._held(tmp_path, None)
        assert tokens.current(path).store_id is None

        with pytest.raises(tokens.ForeignStoreCredential):
            tokens.store_credentials(
                FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
                expires_at=self.REAL_EXPIRY, store_id=self.REAL_STORE,
                env_file=path,
            )
        assert tokens.current(path).access_token == FAKE_ACCESS

    def test_a_first_credential_needs_no_permission(self, tmp_path) -> None:
        """Nothing stored means nothing to protect — an install on a fresh host
        must not need a human to press a button."""
        path = tmp_path / ".env.staging"

        outcome = tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH,
            expires_at=self.REAL_EXPIRY, store_id=self.REAL_STORE,
            env_file=path,
        )

        assert outcome is tokens.StoreOutcome.STORED
        assert tokens.current(path).store_id == self.REAL_STORE

    def test_the_refresh_path_claims_no_store_and_is_not_blocked(
        self, tmp_path
    ) -> None:
        """``refresh_salla_token._persist`` passes the store id it read back
        from the file — ``None`` while none was ever recorded. If «no claim»
        were read as «foreign», every rotation would fail three times and tell
        Fahad to reinstall the app."""
        path = self._held(tmp_path, None)

        outcome = tokens.store_credentials(
            FAKE_ACCESS_2, refresh_token=FAKE_REFRESH_2,
            expires_at=self.DEMO_EXPIRY + timedelta(days=14), env_file=path,
        )

        assert outcome is tokens.StoreOutcome.STORED
        assert tokens.current(path).access_token == FAKE_ACCESS_2

    def test_the_same_token_is_the_same_grant_however_it_is_labelled(
        self, tmp_path
    ) -> None:
        """A redelivery of the credential we already hold, now carrying a
        merchant number the file never recorded. The shared secret settles it:
        this is not a decision to put in front of anyone."""
        path = self._held(tmp_path, None)

        outcome = tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH,
            expires_at=self.DEMO_EXPIRY, store_id=self.DEMO_STORE,
            env_file=path,
        )

        assert outcome is tokens.StoreOutcome.STORED   # it records the store
        assert tokens.current(path).store_id == self.DEMO_STORE

    def test_a_store_change_does_not_inherit_the_old_refresh_token(
        self, tmp_path
    ) -> None:
        """Keeping it would pair merchant B's access token with merchant A's
        refresh token, and the next rotation would quietly bring the store we
        just left back as the live credential."""
        path = self._held(tmp_path, self.DEMO_STORE)

        tokens.store_credentials(
            FAKE_ACCESS_2, expires_at=self.REAL_EXPIRY,
            store_id=self.REAL_STORE, env_file=path, allow_store_change=True,
        )

        creds = tokens.current(path)
        assert creds.access_token == FAKE_ACCESS_2
        assert creds.refresh_token == ""      # cleared, not inherited
        assert not creds.refresh_token

    @pytest.mark.parametrize(
        "held_store, offered, expected",
        [
            (DEMO_STORE, DEMO_STORE, tokens.StoreIdentity.SAME),
            (DEMO_STORE, REAL_STORE, tokens.StoreIdentity.DIFFERENT),
            (DEMO_STORE, None, tokens.StoreIdentity.UNCLAIMED),
            (DEMO_STORE, "", tokens.StoreIdentity.UNCLAIMED),
            (None, REAL_STORE, tokens.StoreIdentity.UNKNOWN),
        ],
    )
    def test_the_identity_ladder_is_readable_without_writing(
        self, held_store, offered, expected
    ) -> None:
        held = tokens.SallaCredentials(
            access_token=FAKE_ACCESS, store_id=held_store,
        )
        assert tokens.store_identity(held, offered, FAKE_ACCESS_2) is expected
        # nothing stored at all is its own answer
        assert tokens.store_identity(
            tokens.SallaCredentials(), offered, FAKE_ACCESS_2
        ) is tokens.StoreIdentity.NO_INCUMBENT


class TestTheConsumeScript:
    """``scripts/consume_stored_authorize.py``, rehearsed end to end.

    It exists because the row that was waiting on 2026-08-07 could not be
    consumed by the worker: it belongs to another store, so the worker defers
    it forever and a human has to say yes. That «yes» is typed once, against
    a live credential, on a script whose ``--apply`` branch would otherwise
    never have been executed before. So it is executed here — against
    ``career_test`` and a throwaway env file, never the real ones.
    """

    REAL_STORE = "1275699954"

    def _run(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import importlib.util
        import io

        spec = importlib.util.spec_from_file_location(
            "consume_stored_authorize",
            "/root/career/scripts/consume_stored_authorize.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # Registered BEFORE execution, the same way
        # tests/test_salla_token_refresh._load_refresher does it: `@dataclass`
        # resolves its annotations through `sys.modules[cls.__module__]`, so a
        # script that grows a dataclass would otherwise fail to import here
        # with a bare «'NoneType' object has no attribute '__dict__'».
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        buffer = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["consume_stored_authorize.py", *argv]
        try:
            with contextlib.redirect_stdout(buffer):
                code = module.main()
        finally:
            sys.argv = old_argv
        return code, buffer.getvalue()

    def test_the_dry_run_refuses_a_foreign_store_and_writes_nothing(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        tokens.store_credentials(FAKE_ACCESS, refresh_token=FAKE_REFRESH,
                                 expires_at=datetime(2026, 8, 21, 9, 37,
                                                     tzinfo=UTC),
                                 env_file=tokens.ENV_FILE)
        before = tokens.ENV_FILE.read_text()
        # A DIFFERENT credential — the same one would be the same grant, which
        # is exactly the case the identity ladder lets through untouched.
        event_id = seed(owner_session, payload=authorize_payload(
            access=FAKE_ACCESS_2, refresh=FAKE_REFRESH_2,
            merchant=self.REAL_STORE))

        code, out = self._run(["--env-file", str(tokens.ENV_FILE)])

        assert code == 3
        assert "REFUSING" in out
        assert self.REAL_STORE in out
        assert FAKE_ACCESS_2 not in out and FAKE_ACCESS not in out
        assert tokens.ENV_FILE.read_text() == before
        assert row(owner_session, event_id).processing_status == "received"

    def test_apply_with_the_operators_confirmation_stores_and_processes(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The exact live shape: a credential from another store whose expiry
        is EARLIER than the one held. It lands, and the row is closed."""
        tokens.store_credentials(FAKE_ACCESS, refresh_token=FAKE_REFRESH,
                                 expires_at=datetime(2026, 8, 21, 9, 37,
                                                     tzinfo=UTC),
                                 env_file=tokens.ENV_FILE)
        expires = int(datetime(2026, 8, 21, 7, 24, 22, tzinfo=UTC).timestamp())
        event_id = seed(owner_session, payload=authorize_payload(
            access=FAKE_ACCESS_2, refresh=FAKE_REFRESH_2,
            expires=expires, merchant=self.REAL_STORE))

        code, out = self._run([
            "--env-file", str(tokens.ENV_FILE), "--allow-store-change",
            "--apply",
        ])

        assert code == 0, out
        creds = tokens.current(tokens.ENV_FILE)
        assert creds.access_token == FAKE_ACCESS_2
        assert creds.refresh_token == FAKE_REFRESH_2
        assert creds.store_id == self.REAL_STORE
        assert row(owner_session, event_id).processing_status == "processed"
        # the operator alert is shown, not sent, and carries no credential
        assert "systemctl restart" in out
        for secret in (FAKE_ACCESS, FAKE_ACCESS_2, FAKE_REFRESH, FAKE_REFRESH_2):
            assert secret not in out

    def test_it_reports_nothing_to_do_rather_than_inventing_work(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        code, out = self._run(["--env-file", str(tokens.ENV_FILE)])
        assert code == 0
        assert "nothing to do" in out


class TestTheRefusedCredentialSurvives:
    """`ForeignStoreCredential` promises the operator that a refused credential
    «is still in the webhook_events row … so the operator can consume it
    deliberately the moment they have decided».

    It was not true. `intake.prune_webhook_payloads` replaced the payload of
    any row past the retention window whatever its `processing_status`, so the
    refusal destroyed the only copy of the thing it promised to keep and
    `scripts/consume_stored_authorize.py` silently stopped working. The row
    survived — it is the idempotency record — and the credential did not.

    The rule now: the body is kept while the credential in it can still be
    USED, and not one day longer. The adversarial repro asserted survival for a
    credential that had expired twenty-six days earlier; that is deliberately
    not what this does, because there is nothing left to consume then and an
    unbounded exemption would turn `webhook_events` into the credential store
    §29 refused to make it. The promise was narrowed in the same commit to say
    exactly this, so what the operator reads is true.

    2026-08-07: «while it can still be USED» was measured entirely from
    `data.expires` — a number Salla sends us — so a body claiming a life Salla
    does not issue deferred its own redaction for as long as it liked. There is
    a ceiling now (`intake._AUTHORIZE_MAX_LIFE_DAYS`), and the first test below
    was written against the unbounded behaviour: it used a FORTY-day-old row
    whose payload claimed ten more days, i.e. a fifty-day access token, and
    asserted the body survived. That is the defect, in the shape of a test. It
    now states the same rule — no window makes it right to destroy a credential
    nobody has decided about yet — with an age the credential could actually
    have. `tests/test_webhook_intake_authority.py` holds the other half: the
    year-2100 payload, redacted on schedule.
    """

    DEMO_STORE = "855028708"

    def _authorize_row(
        self, session: Session, *, age_days: int, expires: datetime,
        status: str = "received", signature_valid: bool = True,
    ) -> uuid.UUID:
        event = WebhookEvent(
            id=uuid.uuid4(), provider="salla",
            event_type="app.store.authorize",
            event_fingerprint=f"test:{uuid.uuid4()}",
            signature_valid=signature_valid,
            payload=authorize_payload(
                merchant=self.DEMO_STORE, expires=int(expires.timestamp())
            ),
            processing_status=status,
            received_at=datetime.now(UTC) - timedelta(days=age_days),
        )
        session.add(event)
        session.commit()
        return event.id

    def _sweep(self, session: Session, **kwargs) -> dict[str, int]:
        from career.webhooks import intake

        counts = intake.prune_webhook_payloads(
            session, now=datetime.now(UTC), **kwargs
        )
        session.commit()
        session.expire_all()
        return counts

    def _token_in(self, session: Session, event_id: uuid.UUID) -> str | None:
        payload = row(session, event_id).payload
        data = payload.get("data") if isinstance(payload, dict) else None
        return data.get("access_token") if isinstance(data, dict) else None

    def test_a_deferred_credential_outlives_the_retention_window(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The clock is brought all the way forward (`retention_days=0`) so the
        test states the RULE and not an arithmetic coincidence: no window makes
        it right to destroy a credential nobody has decided about yet.

        The row is three days old and its credential has ten days left — a
        shape Salla actually delivers. Forty days old with ten days left, which
        is what this used to seed, is not: it claims a fifty-day access token,
        and asserting THAT body survived was asserting the unbounded exemption
        (see the class docstring)."""
        event_id = self._authorize_row(
            owner_session, age_days=3,
            expires=datetime.now(UTC) + timedelta(days=10),
        )

        counts = self._sweep(owner_session, retention_days=0)

        assert self._token_in(owner_session, event_id) == FAKE_ACCESS
        assert counts["deferred_credentials"] == 1
        assert row(owner_session, event_id).payload_redacted_at is None

    def test_it_goes_the_moment_it_can_no_longer_be_consumed(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The other half, and the reason this is a deferral and not an
        exemption: an expired credential is not a credential, so the body loses
        every claim to a longer life than any other body — and the operator is
        told, because a store's install is now unconnected with no way back
        except a reinstall."""
        event_id = self._authorize_row(
            owner_session, age_days=40,
            expires=datetime.now(UTC) - timedelta(days=1),
        )

        counts = self._sweep(owner_session)

        assert self._token_in(owner_session, event_id) is None
        assert counts["expired_credentials"] == 1
        assert row(owner_session, event_id).payload_redacted_at is not None

    def test_an_unreadable_expiry_is_bounded_by_sallas_longest_lifetime(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """`data.expires` absent or renamed — the same shape that makes
        `store_credentials` record no expiry at all. Unknown must not mean
        forever: fourteen days is the longest life Salla documents."""
        payload = authorize_payload(merchant=self.DEMO_STORE)
        del payload["data"]["expires"]
        young = WebhookEvent(
            id=uuid.uuid4(), provider="salla",
            event_type="app.store.authorize",
            event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
            payload=payload, processing_status="received",
            received_at=datetime.now(UTC) - timedelta(days=3),
        )
        old = WebhookEvent(
            id=uuid.uuid4(), provider="salla",
            event_type="app.store.authorize",
            event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
            payload=payload, processing_status="received",
            received_at=datetime.now(UTC) - timedelta(days=20),
        )
        owner_session.add_all([young, old])
        owner_session.commit()

        self._sweep(owner_session, retention_days=0)

        assert self._token_in(owner_session, young.id) == FAKE_ACCESS
        assert self._token_in(owner_session, old.id) is None

    def test_a_consumed_authorize_gets_no_exemption(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """The deferral is for the row waiting on a human. Once the decision
        was taken — the credential is on disk, or it was refused terminally —
        the body is ordinary scaffolding again."""
        processed = self._authorize_row(
            owner_session, age_days=40,
            expires=datetime.now(UTC) + timedelta(days=10), status="processed",
        )
        forged = self._authorize_row(
            owner_session, age_days=40,
            expires=datetime.now(UTC) + timedelta(days=10),
            status="received", signature_valid=False,
        )

        counts = self._sweep(owner_session)

        assert self._token_in(owner_session, processed) is None
        assert self._token_in(owner_session, forged) is None
        assert counts["deferred_credentials"] == 0

    def test_a_body_with_a_customer_in_it_is_never_deferred(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """§25 is not weakened by any of this. The exemption is keyed on the
        event TYPE, and an authorize body carries a credential and the app's
        own metadata — no name, no mobile, no email. A row that does carry
        those expires on its own clock, stuck or not."""
        order = WebhookEvent(
            id=uuid.uuid4(), provider="salla", event_type="order.created",
            event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
            payload={"event": "order.created",
                     "data": {"customer": {"mobile": "0501234567"},
                              "access_token": FAKE_ACCESS}},
            processing_status="received",
            received_at=datetime.now(UTC) - timedelta(days=40),
        )
        owner_session.add(order)
        owner_session.commit()

        counts = self._sweep(owner_session)

        assert "0501234567" not in str(row(owner_session, order.id).payload)
        assert counts["deferred_credentials"] == 0


class TestExpiry:
    def test_salla_expires_is_an_absolute_epoch(self) -> None:
        """The live-verified fact. Read as a duration (``expires_in``) this
        would have dated the token to 1970 and made every renewal overdue."""
        parsed = tokens.parse_expiry(LIVE_EXPIRES_EPOCH)
        assert parsed == datetime(2026, 7, 29, 14, 22, 39, tzinfo=UTC)

    @pytest.mark.parametrize("value", ["", None, "not-a-date", {}, True])
    def test_unknown_expiry_is_none_not_zero(self, value) -> None:
        assert tokens.parse_expiry(value) is None

    def test_days_left_floors_and_goes_negative(self, tmp_path) -> None:
        path = tmp_path / ".env.staging"
        now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        tokens.store_credentials(
            FAKE_ACCESS, expires_at=now + timedelta(hours=11), env_file=path,
        )
        assert tokens.days_left(now, env_file=path) == 0

        # A second file, not a second write: storing an EARLIER expiry over a
        # later one is exactly what the stale guard refuses (see
        # TestIdempotenceAndOrdering), so it cannot be used to set this up.
        expired = tmp_path / ".env.expired"
        tokens.store_credentials(
            FAKE_ACCESS_2, expires_at=now - timedelta(days=3), env_file=expired,
        )
        assert tokens.days_left(now, env_file=expired) == -3

    def test_days_left_is_none_when_nothing_is_stored(self, tmp_path) -> None:
        assert tokens.days_left(env_file=tmp_path / "absent") is None


class TestPresenceIsNotRunway:
    """`days_left()` answers None for two unrelated facts, and a caller that
    has to tell them apart cannot. `life()` is the answer that carries both.

    This is not a taxonomy exercise: `career.engine.cli` read «a credential we
    cannot date» as «no credential at all» and printed «only reinstalling the
    app on the store issues a new one» — an instruction that fires
    `app.uninstalled` and revokes the credential that was working.
    """

    def test_an_undated_credential_is_not_an_absent_one(self, tmp_path) -> None:
        """`store_credentials` CREATES this state on purpose: `expires_at=None`
        clears the expiry rather than keeping a wrong date beside a new token,
        which is what an authorize payload with no readable `data.expires`
        produces."""
        path = tmp_path / ".env.staging"
        tokens.store_credentials(
            FAKE_ACCESS, refresh_token=FAKE_REFRESH, expires_at=None,
            store_id=FAKE_STORE, env_file=path,
        )

        held = tokens.life(env_file=path)

        assert held.present and held.undated
        assert held.days_left is None
        # the same file, asked the old way, cannot tell you any of that
        assert tokens.days_left(env_file=path) is None

    def test_an_empty_store_says_so_in_the_type(self, tmp_path) -> None:
        held = tokens.life(env_file=tmp_path / "absent")
        assert held.present is False
        assert held.undated is False
        assert held.days_left is None

    def test_a_dated_credential_measures(self, tmp_path) -> None:
        path = tmp_path / ".env.staging"
        now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        tokens.store_credentials(
            FAKE_ACCESS, expires_at=now + timedelta(days=9), env_file=path,
        )

        held = tokens.life(now, env_file=path)

        assert held.present and not held.undated
        assert held.days_left == 9

    def test_the_meaningless_pair_cannot_be_built(self) -> None:
        """«Nothing is stored, and it has four days left» is not a state; a
        type that can express it is a type a caller can be handed."""
        with pytest.raises(ValueError):
            tokens.CredentialLife(present=False, days_left=4)


# ── the worker path ──────────────────────────────────────────────────────────


class TestTheWorkerConsumesAuthorize:
    def test_a_signed_authorize_stores_the_credential(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        admin = RawAdmin()
        event_id = seed(owner_session, payload=authorize_payload())

        sweep(owner_session, admin)

        creds = tokens.current()
        assert creds.access_token == FAKE_ACCESS
        assert creds.refresh_token == FAKE_REFRESH
        assert creds.store_id == FAKE_STORE          # `merchant`, not data.id
        assert creds.store_id != "1410006361"
        assert row(owner_session, event_id).processing_status == "processed"
        assert admin.messages, "a landed credential must not be silent"
        assert "systemctl restart" in "\n".join(admin.messages)

    def test_an_unsigned_authorize_is_refused_and_stores_nothing(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """§29's first condition. An unsigned body carrying a live credential
        is an attempted credential injection, so it is refused before the
        payload is read — and the row is terminal, because a forgery does not
        become genuine on retry."""
        admin = RawAdmin()
        event_id = seed(owner_session, payload=authorize_payload(),
                        signature_valid=False)

        sweep(owner_session, admin)

        assert not tokens.current().present
        assert not tokens.ENV_FILE.exists()
        assert row(owner_session, event_id).processing_status == "failed"
        assert admin.messages

    def test_a_foreign_provider_row_can_never_reach_the_credential_path(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """``webhook_events`` is one shared table; a WhatsApp row wearing this
        event name must not be a way into the credential writer."""
        event = WebhookEvent(
            id=uuid.uuid4(), provider="whatsapp",
            event_type="app.store.authorize",
            event_fingerprint=f"test:{uuid.uuid4()}", signature_valid=True,
            payload=authorize_payload(), processing_status="received",
        )
        owner_session.add(event)
        owner_session.commit()

        sweep(owner_session, RawAdmin())

        assert not tokens.current().present
        # untouched: it was never selected — it is the WhatsApp worker's row
        assert row(owner_session, event.id).processing_status == "received"

    def test_a_redelivered_authorize_is_idempotent(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        payload = authorize_payload()
        first = seed(owner_session, payload=payload)
        sweep(owner_session, RawAdmin())
        stored = tokens.ENV_FILE.read_text()

        # Salla redelivers with a different created_at, so the fingerprint
        # differs and intake persists a SECOND row — dedupe does not save us
        # here, the writer has to be idempotent by itself.
        second_payload = dict(payload, created_at="2026-08-07 12:00:05")
        second = seed(owner_session, payload=second_payload)
        sweep(owner_session, RawAdmin())

        assert tokens.ENV_FILE.read_text() == stored
        assert tokens.current().access_token == FAKE_ACCESS
        assert row(owner_session, first).processing_status == "processed"
        assert row(owner_session, second).processing_status == "processed"

    def test_a_payload_without_a_token_fails_loudly_and_terminally(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        admin = RawAdmin()
        payload = authorize_payload()
        del payload["data"]["access_token"]
        event_id = seed(owner_session, payload=payload)

        sweep(owner_session, admin)

        assert not tokens.current().present
        assert row(owner_session, event_id).processing_status == "failed"
        assert admin.messages

    def test_a_persistence_failure_is_neither_silent_nor_processed(
        self, owner_session: Session, monkeypatch: pytest.MonkeyPatch,
        clean_billing: None
    ) -> None:
        """§29's fourth condition. The credential is still in the row, so the
        row stays ``received`` — retryable — and the operator hears about it,
        because a missed authorize means the sale path is dead and nothing
        else in the system would say so."""
        admin = RawAdmin()

        def explode(*_args, **_kwargs):
            raise tokens.CredentialWriteError("could not persist to /x/y")

        monkeypatch.setattr(tokens, "store_credentials", explode)
        event_id = seed(owner_session, payload=authorize_payload())

        sweep(owner_session, admin)

        after = row(owner_session, event_id)
        assert after.processing_status == "received"   # NOT processed
        assert after.attempt_count == 1                # and visibly waiting
        assert admin.messages

    def test_an_unexpected_crash_keeps_the_event_instead_of_burning_it(
        self, owner_session: Session, monkeypatch: pytest.MonkeyPatch,
        clean_billing: None
    ) -> None:
        """A crash on any other event is quarantined ``failed``. Not this one:
        the row may hold the only copy of a live credential."""
        def explode(*_args, **_kwargs):
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(tokens, "store_credentials", explode)
        event_id = seed(owner_session, payload=authorize_payload())

        sweep(owner_session, RawAdmin())

        assert row(owner_session, event_id).processing_status == "received"


class TestUninstall:
    def test_uninstall_is_loud_and_destroys_nothing(
        self, owner_session: Session, clean_billing: None
    ) -> None:
        """An uninstall means the token is already dead — but deleting our
        copy would let an uninstall REDELIVERED after a reinstall wipe the
        credential the reinstall just brought. It shouts; it does not wipe."""
        tokens.store_credentials(FAKE_ACCESS, refresh_token=FAKE_REFRESH,
                                 env_file=tokens.ENV_FILE)
        admin = RawAdmin()
        event_id = seed(owner_session, payload=UNINSTALL_PAYLOAD,
                        event_type="app.uninstalled")

        sweep(owner_session, admin)

        assert tokens.current().access_token == FAKE_ACCESS
        assert row(owner_session, event_id).processing_status == "processed"
        assert any("🔴" in m for m in admin.messages)


class TestZeroLeakage:
    def test_no_token_substring_survives_a_full_authorize(
        self, owner_session: Session, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, clean_billing: None
    ) -> None:
        """Constant 13, proven with the safety net switched OFF.

        ``install_secret_redaction`` scrubs log records process-wide, which
        means a naive version of this test would pass even if the worker had
        logged the token in full — the filter would have deleted the evidence.
        So ``sanitize_secret_text`` is replaced by the identity function for
        the duration: what is asserted here is that the code never EMITS the
        value, not that something downstream removed it.
        """
        monkeypatch.setattr(logging_filters, "sanitize_secret_text",
                            lambda value: value if isinstance(value, str)
                            else str(value))
        admin = RawAdmin()
        seed(owner_session, payload=authorize_payload())

        with caplog.at_level(logging.DEBUG):
            sweep(owner_session, admin)

        haystacks = [caplog.text, "\n".join(admin.messages)]
        haystacks += [str(r.msg) + str(r.args) for r in caplog.records]
        for secret in (FAKE_ACCESS, FAKE_REFRESH):
            for haystack in haystacks:
                assert secret not in haystack
        # the credential really did land — otherwise this proves nothing
        assert tokens.current().access_token == FAKE_ACCESS

    def test_no_token_substring_survives_a_failure(
        self, owner_session: Session, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, clean_billing: None
    ) -> None:
        """The failing paths are where secrets usually escape: an exception
        message, a traceback, an «alert with the details» line."""
        monkeypatch.setattr(logging_filters, "sanitize_secret_text",
                            lambda value: value if isinstance(value, str)
                            else str(value))
        admin = RawAdmin()
        # A directory where the file should be: every write fails, for real,
        # inside the real code path rather than through a stubbed raise.
        monkeypatch.setattr(tokens, "ENV_FILE", tokens.ENV_FILE.parent / "as_dir")
        tokens.ENV_FILE.mkdir()
        seed(owner_session, payload=authorize_payload())

        with caplog.at_level(logging.DEBUG):
            sweep(owner_session, admin)

        for secret in (FAKE_ACCESS, FAKE_REFRESH):
            assert secret not in caplog.text
            assert secret not in "\n".join(admin.messages)
        assert admin.messages, "a failure to store the credential must be told"
