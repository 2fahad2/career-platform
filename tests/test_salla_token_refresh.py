"""The credential must never die of old age again.

On 2026-07-29 the Salla access token expired. Nothing crashed, nothing was
logged, and nobody found out for nine days — until paid orders stopped
provisioning. Everything here exists so that exact silence cannot recur:

  * something renews the credential BEFORE the margin runs out
    (scripts/refresh_salla_token.py, ops/systemd/career-salla-token.*), and
  * something says so out loud when the renewal is not working
    (career.engine.cli's boot check, which measures what is LEFT rather than
    reading a date a human typed into a file).

Every token value in this file is an obvious fake. The real ones are never
printed anywhere — CHANGELOG §29's second condition — and one of the tests
below proves it for the whole successful path.
"""

from __future__ import annotations

import dataclasses
import fcntl
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from career.engine import cli

REPO = Path(__file__).resolve().parents[1]
UNITS = REPO / "ops" / "systemd"


def _load_refresher() -> Any:
    """The script is the deliverable, so it is loaded from its real path —
    the same way tests/test_whatsapp_worker.py loads the ops entry points."""
    path = REPO / "scripts" / "refresh_salla_token.py"
    spec = importlib.util.spec_from_file_location("career_refresh_salla", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the module defines dataclasses, and
    # `@dataclass` resolves annotations through `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


refresher = _load_refresher()

# Obvious fakes. Nothing here resembles a Salla token except its shape.
OLD_REFRESH = "FAKE-ory-rt-OLD-refresh-value"
NEW_ACCESS = "FAKE-ory-at-NEW-access-value"
NEW_REFRESH = "FAKE-ory-rt-NEW-refresh-value"
NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

#: Salla's own documented success body (docs.salla.dev/doc-421118), with the
#: values replaced. 1209599 seconds is the number the documentation prints.
DOCUMENTED_OK = {
    "access_token": NEW_ACCESS,
    "expires_in": 1209599,
    "refresh_token": NEW_REFRESH,
    "scope": "offline_access settings.read",
    "token_type": "bearer",
}


class FakeStore:
    """Agent T1's `career.salla.tokens`, reduced to the three published calls."""

    def __init__(
        self, days: int | None, *, refresh_token: str = OLD_REFRESH,
        write_error: Exception | None = None,
    ) -> None:
        self._days = days
        self._refresh = refresh_token
        self._write_error = write_error
        self.writes: list[dict[str, Any]] = []

    def current(self) -> SimpleNamespace:
        return SimpleNamespace(
            access_token="FAKE-ory-at-CURRENT",
            refresh_token=self._refresh,
            expires_at=NOW + timedelta(days=self._days or 0),
            store_id="STORE-FAKE-1",
        )

    def days_left(self) -> int | None:
        return self._days

    def store_credentials(self, **kwargs: Any) -> None:
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(kwargs)


class FakeAdmin:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_admin(self, text: str) -> str:
        self.sent.append(text)
        return "fake"


class FakePoster:
    """One recorded call. Raises whatever the test wants raised."""

    def __init__(
        self, status: int = 200, payload: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload if payload is not None else dict(DOCUMENTED_OK)
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(
        self, url: str, data: dict[str, str], timeout: float
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, dict(data), timeout))
        if self.raises is not None:
            raise self.raises
        return self.status, self.payload


def _store(store: FakeStore) -> Any:
    return refresher.TokenStore(
        current=store.current,
        days_left=store.days_left,
        store_credentials=store.store_credentials,
    )


@pytest.fixture()
def app_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALLA_CLIENT_ID", "FAKE-client-id")
    monkeypatch.setenv("SALLA_CLIENT_SECRET", "FAKE-client-secret")


def _run(
    store: FakeStore, poster: FakePoster, admin: FakeAdmin, tmp_path: Path,
    *, now: datetime = NOW,
) -> int:
    return int(refresher.run(
        poster=poster, admin=admin, store=_store(store), now=now,
        state=tmp_path / "last-alert",
    ))


# ── the decision: when to refresh at all ────────────────────────────────────


class TestTheMargin:
    """Five days, and the reason is arithmetic, not taste: the timer is daily,
    so the margin IS the retry budget — six attempts before the credential
    dies. A one-day margin survives one bad night; this survives five."""

    def test_a_token_outside_the_margin_is_left_alone(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store, poster, admin = FakeStore(6), FakePoster(), FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_OK
        assert poster.calls == []      # Salla was never asked
        assert store.writes == []      # nothing was rotated
        assert admin.sent == []        # and nobody was disturbed

    def test_a_token_inside_the_margin_is_refreshed(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store, poster, admin = FakeStore(5), FakePoster(), FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_OK
        assert len(poster.calls) == 1
        assert len(store.writes) == 1

    def test_an_already_expired_token_is_still_refreshed(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """The refresh token outlives the access token by weeks, so the nine
        silent days of the incident were recoverable the whole time — the
        margin check must not exclude negative days and quietly give up on
        exactly the case it exists for."""
        store, poster, admin = FakeStore(-9), FakePoster(), FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_OK
        assert len(store.writes) == 1

    def test_the_boot_check_never_shouts_about_a_token_the_timer_will_renew(
        self,
    ) -> None:
        """The two thresholds are one number on purpose: the refresher acts at
        09:00 and the boot check speaks at 11:00, so any morning that still
        shows five days is a morning the automation did not work."""
        assert cli.TOKEN_EXPIRY_WARN_DAYS == refresher.REFRESH_MARGIN_DAYS
        assert cli.verify_environment(
            _settings(), approved_prices=APPROVED, sale_plans=ON_SALE,
            today=TODAY,
            token_life=cli.TokenLife.measured(refresher.REFRESH_MARGIN_DAYS + 1),
        ) == []


class TestTheSupersededSecretsFileHasAnOwner:
    """`.env.staging.bak` held a DIFFERENT live access token from the live file
    on 2026-08-07 — the demo store's, valid for another fortnight — and nothing
    rotated, pruned, shredded or knew about it.

    The credential store bounds it to one generation and strips the credential
    out of it (tests/test_salla_tokens.TestTheBackupHasALifecycle). What a
    module that only runs when something is stored cannot do is expire it on a
    host where nothing is being stored, which is the July state exactly. This
    timer runs daily whatever happens, so it is the one that can.
    """

    def _store_with_sweep(self, store: FakeStore, calls: list[str]) -> Any:
        return refresher.TokenStore(
            current=store.current, days_left=store.days_left,
            store_credentials=store.store_credentials,
            prune_backups=lambda: calls.append("swept") or True,
        )

    def test_it_sweeps_on_a_day_it_refreshes_nothing(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """THE defect's shape: the healthy day. A margin check that returns
        early is exactly when the last rotation's copy is sitting there."""
        calls: list[str] = []
        poster = FakePoster()
        code = refresher.run(
            poster=poster, admin=FakeAdmin(),
            store=self._store_with_sweep(FakeStore(30), calls), now=NOW,
            state=tmp_path / "stamp",
        )
        assert code == refresher.EXIT_OK
        assert poster.calls == []       # nothing was refreshed…
        assert calls == ["swept"]       # …and the backup was still swept

    def test_a_sweep_that_fails_never_costs_a_refresh(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """Housekeeping is not allowed to stand between a dying credential and
        its renewal."""
        def _boom() -> bool:
            raise OSError("read-only filesystem")

        store = FakeStore(1)
        code = refresher.run(
            poster=FakePoster(), admin=FakeAdmin(),
            store=refresher.TokenStore(
                current=store.current, days_left=store.days_left,
                store_credentials=store.store_credentials, prune_backups=_boom,
            ),
            now=NOW, state=tmp_path / "stamp",
        )
        assert code == refresher.EXIT_OK
        assert len(store.writes) == 1

    def test_the_real_store_publishes_the_sweep_this_timer_calls(self) -> None:
        """The wiring, against the real module rather than a fake: the only
        optional member of the store, and it is present."""
        from career.salla import tokens

        resolved = refresher.load_store(tokens)
        assert resolved.prune_backups is tokens.prune_credential_backups

    def test_a_store_without_a_sweep_still_refreshes(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store = FakeStore(1)
        assert _run(store, FakePoster(), FakeAdmin(), tmp_path) == refresher.EXIT_OK
        assert len(store.writes) == 1


# ── the exchange, against Salla's own documentation ─────────────────────────


class TestTheDocumentedExchange:
    def test_it_posts_exactly_what_salla_documents(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store, poster, admin = FakeStore(3), FakePoster(), FakeAdmin()
        _run(store, poster, admin, tmp_path)
        url, data, _timeout = poster.calls[0]
        assert url == "https://accounts.salla.sa/oauth2/token"
        assert data == {
            "grant_type": "refresh_token",
            "client_id": "FAKE-client-id",
            "client_secret": "FAKE-client-secret",
            "refresh_token": OLD_REFRESH,
        }

    def test_expires_in_is_read_as_a_duration_not_as_a_timestamp(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """`expires_in` in the refresh body is seconds (1209599 = fourteen
        days); the `expires` field of the app.store.authorize webhook is a
        unix timestamp. Reading one as the other lands the expiry in 1970 —
        which would refresh forever — or in 2065, which would never refresh
        again."""
        store, poster, admin = FakeStore(4), FakePoster(), FakeAdmin()
        _run(store, poster, admin, tmp_path)
        stored = store.writes[0]["expires_at"]
        assert timedelta(days=13) < (stored - NOW) < timedelta(days=15)

    def test_the_new_pair_is_what_gets_stored(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store, poster, admin = FakeStore(4), FakePoster(), FakeAdmin()
        _run(store, poster, admin, tmp_path)
        written = store.writes[0]
        assert written["access_token"] == NEW_ACCESS
        assert written["refresh_token"] == NEW_REFRESH
        assert written["store_id"] == "STORE-FAKE-1"

    def test_it_calls_the_store_interface_agent_t1_publishes(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """A tripwire, not a style rule. This script was written against
        `store_credentials/current/days_left` before that module existed; if
        the published shape differs, it has to fail in CI rather than at
        03:00 with a rotated token in memory and nowhere to put it."""
        store, poster, admin = FakeStore(4), FakePoster(), FakeAdmin()
        _run(store, poster, admin, tmp_path)
        assert set(store.writes[0]) == {
            "access_token", "refresh_token", "expires_at", "store_id",
        }

    def test_an_unusable_expires_in_still_stores_the_rotated_pair(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """Once Salla answers 200 the OLD refresh token is spent, so the pair
        in memory is the only credential that exists. Nothing — least of all
        an opinion about a malformed number — may be allowed to drop it."""
        payload = dict(DOCUMENTED_OK, expires_in="not-a-number")
        store = FakeStore(3)
        poster, admin = FakePoster(payload=payload), FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_OK
        life = store.writes[0]["expires_at"] - NOW
        assert life == timedelta(days=refresher.FALLBACK_LIFETIME_DAYS)

    def test_a_200_without_a_credential_is_treated_as_a_lost_answer(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store = FakeStore(3)
        poster = FakePoster(payload={"token_type": "bearer"})
        admin = FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_TRANSIENT
        assert store.writes == []
        assert "أعد تثبيت التطبيق" in admin.sent[0]


# ── failure, and the difference between kinds of it ─────────────────────────


class TestARejectedRefreshTokenIsUnmistakable:
    """Salla's documentation is explicit: a refresh token used twice makes its
    OAuth server invalidate the token, revoke the access tokens it issued, and
    require the merchant to REINSTALL THE APPLICATION. Easy Mode never shows
    the credential to a human, so there is nothing to paste and nothing this
    host can do — the operator is the only recovery path in existence."""

    def _refused(self, tmp_path: Path, days: int = 3) -> tuple[int, FakeAdmin]:
        store = FakeStore(days)
        poster = FakePoster(status=400, payload={"error": "invalid_grant"})
        admin = FakeAdmin()
        code = _run(store, poster, admin, tmp_path)
        assert poster.calls != []
        return code, admin

    def test_it_exits_with_its_own_code_and_says_the_exact_recovery(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        code, admin = self._refused(tmp_path)
        assert code == refresher.EXIT_NEEDS_REINSTALL
        assert len(admin.sent) == 1
        message = admin.sent[0]
        assert message.startswith("🔴")
        assert "أعد تثبيت التطبيق على متجرك من لوحة سلة" in message

    def test_it_is_never_retried_inside_one_run(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """A refusal is not a blip. Retrying it presents the same single-use
        token again, which is the documented way to make the refusal
        permanent."""
        store = FakeStore(3)
        poster = FakePoster(status=401, payload={"error": "invalid_grant"})
        _run(store, poster, FakeAdmin(), tmp_path)
        assert len(poster.calls) == 1

    def test_it_speaks_even_with_the_whole_margin_still_left(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        """The tiered ladder stays silent above three days for a transient
        failure — four more attempts are coming. A refusal has no attempts
        coming, so it must not inherit that silence."""
        _code, admin = self._refused(tmp_path, days=5)
        assert len(admin.sent) == 1


class TestFailuresThatWaitForTomorrow:
    def test_a_server_error_waits_for_the_timer_instead_of_retrying(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store = FakeStore(4)
        poster = FakePoster(status=503, payload={})
        assert _run(store, poster, FakeAdmin(), tmp_path) == refresher.EXIT_TRANSIENT
        assert len(poster.calls) == 1

    def test_only_a_connection_that_never_opened_is_retried(self) -> None:
        """The one failure that CANNOT have rotated the token: nothing was
        sent. Everything else waits, because a lost answer may mean Salla has
        already issued the replacement we never received."""
        poster = FakePoster(raises=refresher.RefreshFailed(
            "no route", reason="unavailable"))
        with pytest.raises(refresher.RefreshFailed):
            refresher.exchange(
                poster=poster, client_id="a", client_secret="b",
                refresh_token=OLD_REFRESH, now=NOW, attempts=3,
            )
        assert len(poster.calls) == 3

    def test_a_lost_answer_is_asked_only_once_and_is_loud(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store = FakeStore(5)
        poster = FakePoster(raises=refresher.RefreshFailed(
            "read timeout", reason="lost"))
        admin = FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_TRANSIENT
        assert len(poster.calls) == 1
        assert len(admin.sent) == 1              # loud even at full margin
        assert "أعد تثبيت التطبيق" in admin.sent[0]


class TestItRefusesToExchangeWhatItCannotKeep:
    """A refresh whose result cannot be persisted is strictly worse than no
    refresh: it spends the single-use token, receives the replacement and
    drops it, leaving the installation dead with no way back."""

    def test_a_store_missing_any_of_the_three_calls_is_refused_up_front(
        self,
    ) -> None:
        half = SimpleNamespace(current=lambda: None, days_left=lambda: 3)
        with pytest.raises(refresher.RefreshFailed) as caught:
            refresher.load_store(half)
        assert caught.value.reason == "unconfigured"
        assert "store_credentials" in str(caught.value)

    def test_a_complete_store_is_accepted(self) -> None:
        whole = SimpleNamespace(
            current=lambda: None, days_left=lambda: 3,
            store_credentials=lambda **kw: None,
        )
        assert refresher.load_store(whole).days_left() == 3

    def test_a_write_that_fails_after_the_exchange_is_a_red_alert(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store = FakeStore(3, write_error=OSError("read-only file system"))
        admin = FakeAdmin()
        code = _run(store, FakePoster(), admin, tmp_path)
        assert code == refresher.EXIT_NEEDS_REINSTALL
        assert admin.sent[0].startswith("🔴")
        assert "أعد تثبيت التطبيق على متجرك من لوحة سلة" in admin.sent[0]

    def test_no_stored_credential_at_all_says_so_and_asks_for_nothing(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        store = FakeStore(None, refresh_token="")
        poster, admin = FakePoster(), FakeAdmin()
        code = _run(store, poster, admin, tmp_path)
        assert code == refresher.EXIT_NOT_CONFIGURED
        assert poster.calls == []
        assert "أعد تثبيت التطبيق" in admin.sent[0]

    def test_a_missing_app_identity_names_the_variables_on_their_own_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("SALLA_CLIENT_ID", raising=False)
        monkeypatch.delenv("SALLA_CLIENT_SECRET", raising=False)
        store, poster, admin = FakeStore(3), FakePoster(), FakeAdmin()
        assert _run(store, poster, admin, tmp_path) == refresher.EXIT_NOT_CONFIGURED
        assert poster.calls == []
        assert "SALLA_CLIENT_ID" in admin.sent[0]


# ── the alarm that does not get muted ───────────────────────────────────────


class TestItEscalatesInsteadOfRepeating:
    """Today's lesson, in a test. An alarm that fires flatly every day for
    weeks is muted by week two, and a muted channel is how nine days pass."""

    def _slide(self, tmp_path: Path, days: list[int]) -> list[str]:
        state = tmp_path / "last-alert"
        admin = FakeAdmin()
        for day in days:
            refresher.run(
                poster=FakePoster(status=503, payload={}), admin=admin,
                store=_store(FakeStore(day)), now=NOW, state=state,
            )
        return admin.sent

    def test_the_first_days_inside_the_margin_are_the_timers_own_business(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        assert self._slide(tmp_path, [5, 4]) == []

    def test_each_step_down_says_something_new_and_worse(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        sent = self._slide(tmp_path, [5, 4, 3, 2, 1, 0, -1])
        # warn (3), urgent (1), dead (-1) — three messages for seven days, and
        # never the same sentence twice.
        assert len(sent) == 3
        assert len(set(sent)) == 3
        assert sent[0].startswith("⚠️")
        assert sent[1].startswith("🔴")
        assert "انتهى" in sent[2]

    def test_the_same_tier_twice_running_is_not_repeated(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        assert len(self._slide(tmp_path, [3, 3, 2, 2])) == 1

    def test_a_success_makes_the_next_failure_news_again(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        state = tmp_path / "last-alert"
        admin = FakeAdmin()
        refresher.run(poster=FakePoster(status=503, payload={}), admin=admin,
                      store=_store(FakeStore(2)), now=NOW, state=state)
        refresher.run(poster=FakePoster(), admin=admin,
                      store=_store(FakeStore(2)), now=NOW, state=state)
        refresher.run(poster=FakePoster(status=503, payload={}), admin=admin,
                      store=_store(FakeStore(2)), now=NOW, state=state)
        assert len(admin.sent) == 2

    def test_an_unreadable_stamp_sends_rather_than_stays_quiet(
        self, tmp_path: Path
    ) -> None:
        """Fails open, deliberately and in the same direction as
        scripts/alert_unit_failure.sh: the cost of a wrong guess here is a
        noisy hour, and the cost in the other direction is the incident."""
        assert refresher.read_last_key(tmp_path / "nothing-here") is None
        assert refresher.should_speak("unavailable", refresher.TIER_WARN, None)

    def test_a_dead_admin_channel_does_not_swallow_the_exit_code(
        self, app_identity: None, tmp_path: Path
    ) -> None:
        class Dead:
            def send_admin(self, text: str) -> str:
                raise RuntimeError("telegram down")

        store = FakeStore(1)
        poster = FakePoster(status=400, payload={"error": "invalid_grant"})
        code = refresher.run(
            poster=poster, admin=Dead(), store=_store(store), now=NOW,
            state=tmp_path / "last-alert",
        )
        assert code == refresher.EXIT_NEEDS_REINSTALL


class TestTheOperatorCanReadIt:
    def test_every_alert_line_is_direction_pure(self) -> None:
        """His client reverses a line that mixes Arabic with Latin — and a
        Latin digit or a minus sign is enough on its own (CHANGELOG §28)."""
        arabic = re.compile(r"[؀-ۿ]")
        latin = re.compile(r"[A-Za-z0-9]")
        texts = [refresher.MISSING_APP_IDENTITY]
        for reason in ("refused", "lost", "unstorable", "unconfigured",
                       "unavailable"):
            for days in (5, 3, 1, 0, -9, None):
                texts.append(refresher.alert_text(
                    reason, refresher.tier_for(days), days))
        for text in texts:
            for line in text.splitlines():
                assert not (arabic.search(line) and latin.search(line)), line

    def test_a_negative_runway_never_prints_a_latin_minus(self) -> None:
        line = refresher._runway_line(-9)
        assert "-" not in line and "٩" in line

    def test_every_alert_ends_in_an_instruction(self) -> None:
        for reason in ("refused", "lost", "unstorable", "unconfigured",
                       "unavailable"):
            text = refresher.alert_text(reason, refresher.TIER_URGENT, 1)
            assert "أعد تثبيت التطبيق على متجرك من لوحة سلة" in text


class TestNoCredentialEverReachesAJournal:
    """§15.13 and CHANGELOG §29's second condition. Asserted with the redaction
    filter NOT armed, so this proves the code never writes the value rather
    than proving the scrubber catches it afterwards."""

    def test_a_successful_refresh_logs_no_token(
        self, app_identity: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store, poster, admin = FakeStore(3), FakePoster(), FakeAdmin()
        with caplog.at_level(logging.DEBUG):
            _run(store, poster, admin, tmp_path)
        blob = "\n".join(r.getMessage() for r in caplog.records) + "\n".join(admin.sent)
        for secret in (OLD_REFRESH, NEW_ACCESS, NEW_REFRESH):
            assert secret not in blob

    def test_a_refusal_logs_no_body_beyond_a_short_error_code(
        self, app_identity: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = {"error": "invalid_grant", "error_description": OLD_REFRESH}
        store = FakeStore(2)
        with caplog.at_level(logging.DEBUG):
            _run(store, FakePoster(status=400, payload=payload), FakeAdmin(),
                 tmp_path)
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "invalid_grant" in blob
        assert OLD_REFRESH not in blob

    def test_an_error_field_that_is_not_a_short_code_is_dropped(self) -> None:
        assert refresher._error_code({"error": "Bearer " + NEW_ACCESS}) == ""
        assert refresher._error_code({"error": "invalid_client"}) == "invalid_client"


class TestNoDataclassCarriesAToken:
    """`NewCredential` held both live values behind a GENERATED repr, in a file
    whose docstring says NO TOKEN VALUE IS EVER PRINTED, while its sibling
    `SallaCredentials` deliberately hides its fields. Nothing rendered it on
    2026-08-07 — which is missing armour, not a live leak, and «no caller does
    it today» is not a property of an object.

    The second test is the guard that stops the next one: it is the ratchet,
    and it is why this class is not one assertion about one class.
    """

    #: Field names that mean «this is a secret». Deliberately broad: the cost
    #: of a false positive is one explicit `__repr__`, and the cost of a false
    #: negative is a credential in a traceback.
    SECRET_FIELDS = ("token", "secret", "password", "credential", "key")

    def _dataclasses(self, module: Any) -> list[type]:
        import dataclasses
        import inspect

        return [
            obj for _, obj in vars(module).items()
            if inspect.isclass(obj) and dataclasses.is_dataclass(obj)
            and obj.__module__ == module.__name__
        ]

    def test_new_credential_renders_neither_of_its_values(self) -> None:
        credential = refresher.NewCredential(
            access_token=NEW_ACCESS, refresh_token=NEW_REFRESH,
            expires_at=NOW,
        )
        # Every path a value takes to a log line: a traceback frame's repr, an
        # f-string, and the %-substitution stdlib does for
        # `logger.info("%s", credential)` — built as a real LogRecord, because
        # that is the one that would actually leak.
        record = logging.LogRecord(
            "career.salla", logging.INFO, __file__, 1, "%s", (credential,), None,
        )
        for rendered in (repr(credential), str(credential), f"{credential}",
                         record.getMessage()):
            assert NEW_ACCESS not in rendered
            assert NEW_REFRESH not in rendered
        assert "set" in repr(credential)     # it still says something useful

    def test_no_dataclass_in_either_module_can_print_a_secret_field(
        self,
    ) -> None:
        """The ratchet. A dataclass with a generated repr puts every field in
        every traceback frame; the two modules that handle the credential may
        not contain one that would print a token."""
        from career.salla import tokens

        offenders = []
        for module in (refresher, tokens):
            for klass in self._dataclasses(module):
                secret = [
                    f.name for f in dataclasses.fields(klass)
                    if any(word in f.name for word in self.SECRET_FIELDS)
                    # A field holding a FUNCTION named `store_credentials` is
                    # not a field holding a credential (`TokenStore`).
                    and "Callable" not in str(f.type)
                ]
                if not secret:
                    continue
                if klass.__dataclass_params__.repr or "__repr__" not in vars(klass):
                    offenders.append(f"{module.__name__}.{klass.__name__} "
                                     f"would render {secret}")
        assert not offenders, (
            "these dataclasses hold a secret-shaped field and would print it: "
            + "; ".join(offenders)
        )


class TestSingleFlight:
    """Salla invalidates everything when one refresh token is presented twice,
    so this is the one place in the ops tooling that fails CLOSED."""

    def test_a_second_run_refuses_while_the_first_holds_the_lock(
        self, app_identity: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lock = tmp_path / "refresh.lock"
        monkeypatch.setenv("CAREER_SALLA_TOKEN_LOCK", str(lock))
        monkeypatch.setenv("CAREER_SALLA_TOKEN_STATE", str(tmp_path / "stamp"))
        held = lock.open("w")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            poster = FakePoster()
            code = refresher.main(
                [], poster=poster, admin=FakeAdmin(),
                store=_store(FakeStore(1)), now=NOW,
            )
        finally:
            held.close()
        assert code == refresher.EXIT_TRANSIENT   # non-zero ⇒ OnFailure speaks
        assert poster.calls == []

    def test_a_lock_that_cannot_be_taken_at_all_stops_the_refresh(
        self, app_identity: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        blocked = tmp_path / "not-a-dir" / "refresh.lock"
        (tmp_path / "not-a-dir").write_text("i am a file", encoding="utf-8")
        monkeypatch.setenv("CAREER_SALLA_TOKEN_LOCK", str(blocked))
        poster = FakePoster()
        code = refresher.main(
            [], poster=poster, admin=FakeAdmin(),
            store=_store(FakeStore(1)), now=NOW,
        )
        assert code == refresher.EXIT_TRANSIENT
        assert poster.calls == []

    def test_the_free_lock_is_taken_and_the_work_happens(
        self, app_identity: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CAREER_SALLA_TOKEN_LOCK",
                           str(tmp_path / "refresh.lock"))
        monkeypatch.setenv("CAREER_SALLA_TOKEN_STATE", str(tmp_path / "stamp"))
        store, poster = FakeStore(1), FakePoster()
        code = refresher.main(
            [], poster=poster, admin=FakeAdmin(), store=_store(store), now=NOW,
        )
        assert code == refresher.EXIT_OK
        assert len(store.writes) == 1


# ── the boot check: what is LEFT, not what was configured ───────────────────

APPROVED = {
    "basic": Decimal("149"), "cv_analysis": Decimal("29"),
    "professional": Decimal("199"), "executive": Decimal("449"),
}
ON_SALE = {
    "cv_analysis": Decimal("29.00"), "professional": Decimal("199.00"),
    "executive": Decimal("449.00"),
}
TODAY = NOW.date()


def _settings(**overrides: str) -> SimpleNamespace:
    base = {
        "salla_product_catalog": '{"1": "professional"}',
        "salla_product_pricing": '{"1": [199.00, "SAR"]}',
        # A date somebody wrote down, still comfortably in the future — the
        # exact shape of the 29 July lie.
        "salla_token_expires_at": "2026-12-31",
        "salla_store_url": "https://store.example/lammah",
        "whatsapp_access_token": "FAKE-wa-token",
        "whatsapp_phone_number_id": "123456789",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _check(token_life: cli.TokenLife, **overrides: str) -> list[cli.EnvProblem]:
    return cli.verify_environment(
        _settings(**overrides), approved_prices=APPROVED, sale_plans=ON_SALE,
        today=TODAY, token_life=token_life,
    )


def _measured(days_left: int, **overrides: str) -> list[cli.EnvProblem]:
    return _check(cli.TokenLife.measured(days_left), **overrides)


class TestTheBootCheckMeasuresWhatRemains:
    def test_the_live_token_beats_the_date_in_the_file(self) -> None:
        """The whole incident in one assertion: the file says December and the
        credential has two days to live. The file is not the answer."""
        problems = _measured(2)
        assert [p.key for p in problems] == ["SALLA_TOKEN_EXPIRES_AT"]
        assert "expires in 2 days" in problems[0].english

    def test_it_warns_BEFORE_expiry_and_says_what_to_do(self) -> None:
        problems = _measured(3)
        assert problems
        assert "reinstall the app on the store" in problems[0].english
        assert "أعد تثبيت التطبيق على متجرك من لوحة سلة" in problems[0].arabic

    def test_the_sentence_escalates_as_the_margin_shrinks(self) -> None:
        rungs = [_measured(days)[0].arabic for days in (5, 1, -1)]
        assert len(set(rungs)) == 3
        assert "قارب" in rungs[0]
        assert "يوشك" in rungs[1]
        assert "منتهي" in rungs[2]

    def test_a_healthy_token_is_silent(self) -> None:
        assert _measured(13) == []

    def test_a_store_that_holds_nothing_is_louder_than_any_date(self) -> None:
        """Amended 2026-08-07: this used to be written `TokenLife(days_left=
        None)`, which was ALSO how a live-but-undated credential arrived here.
        The state is now named, and naming it is the fix — see
        `TestTheBootCheckDoesNotCallALiveCredentialNothing`."""
        problems = _check(cli.TokenLife.absent())
        assert [p.key for p in problems] == ["SALLA_TOKEN_EXPIRES_AT"]
        assert "NO Salla token" in problems[0].english


class TestTheBootCheckDoesNotCallALiveCredentialNothing:
    """2026-08-07. `tokens.days_left()` returns None for two unrelated facts —
    «no credential» and «a credential whose expiry we never recorded» — and
    this check documented the second as the first.

    What that produced, with a perfectly good token on disk: «the credential
    store holds NO Salla token … only reinstalling the app on the store issues
    a new one». Reinstalling fires `app.uninstalled`, which revokes the
    credential that was working and raises the red «البيع واقف» alert. The
    instruction for the emptiest state was being given for a working one, and
    the refresher — reading the SAME None — was saying «we do not know».
    """

    def test_an_undated_credential_is_never_reported_as_no_credential(
        self,
    ) -> None:
        problems = _check(cli.TokenLife.undated())

        assert len(problems) == 1
        assert "holds NO Salla token" not in problems[0].english
        assert "IS stored" in problems[0].english

    def test_it_does_not_send_the_operator_to_revoke_a_working_credential(
        self,
    ) -> None:
        problems = _check(cli.TokenLife.undated())

        assert "reinstall the app on the store (that re-fires" not in \
            problems[0].english
        assert "Do NOT reinstall" in problems[0].english
        assert "لا داعي لإعادة" in problems[0].arabic
        assert "أعد تثبيت التطبيق على متجرك من لوحة سلة" not in problems[0].arabic

    def test_the_store_and_the_refresher_now_agree_on_what_none_means(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two modules read one value in opposite directions; that
        disagreement WAS the defect. Same credential, same file, both readers.
        """
        from career.salla import tokens

        # `store_credentials` publishes what it wrote into os.environ on
        # purpose (a live worker's expiry warner must stop quoting a replaced
        # token). Registering the keys with monkeypatch FIRST is what puts the
        # process environment back afterwards — without it this test leaks a
        # fake credential into every later test in the session, and
        # `tokens.current()` falls back to the environment when a file has
        # none.
        for key in (tokens.KEY_ACCESS, tokens.KEY_REFRESH, tokens.KEY_EXPIRES,
                    tokens.KEY_STORE):
            monkeypatch.setenv(key, os.environ.get(key, ""))
        path = tmp_path / ".env.staging"
        tokens.store_credentials(
            "ory_at_FAKE-UNDATED-CREDENTIAL-0000000000000",
            refresh_token="ory_rt_FAKE-UNDATED-CREDENTIAL-000000000",
            expires_at=None, store_id="1275699954", env_file=path,
        )

        life = cli.TokenLife.from_store(tokens.life(env_file=path))

        assert life.state is cli.TokenState.UNDATED
        # …and the whole boot check, from that file, says the true thing —
        # this is the adversarial repro's scenario end to end.
        problems = _check(life)
        assert len(problems) == 1
        assert "holds NO Salla token" not in problems[0].english
        # the refresher's own sentence for an unknown runway — unchanged, and
        # now the same fact the boot check is looking at
        assert refresher._runway_line(tokens.days_left(env_file=path)) == \
            "لا نعرف كم بقي من عمر الاعتماد"

    def test_the_states_that_mean_nothing_cannot_be_constructed(self) -> None:
        """A boolean pair can express «unreadable, with four days left». This
        cannot: the ladder never has to ask which half to believe."""
        with pytest.raises(ValueError):
            cli.TokenLife(cli.TokenState.UNREADABLE, 4)
        with pytest.raises(ValueError):
            cli.TokenLife(cli.TokenState.MEASURED, None)
        with pytest.raises(ValueError):
            cli.TokenLife(cli.TokenState.ABSENT, -3)

    def test_an_unreadable_store_falls_back_to_the_configured_date(
        self,
    ) -> None:
        """A host where the credential store is not deployed keeps exactly the
        behaviour it had — degraded, announced, never silently green."""
        problems = cli.verify_environment(
            _settings(salla_token_expires_at="2026-08-04"),
            approved_prices=APPROVED, sale_plans=ON_SALE, today=TODAY,
            token_life=cli.TokenLife.unreadable(),
        )
        assert "EXPIRED 3 days ago" in problems[0].english

    def test_no_token_life_argument_at_all_keeps_the_old_behaviour(
        self,
    ) -> None:
        assert cli.verify_environment(
            _settings(), approved_prices=APPROVED, sale_plans=ON_SALE,
            today=TODAY,
        ) == []

    def test_reading_the_store_never_raises_and_never_lies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `career.salla.tokens` on this host yet — and a boot check that
        raises on an absent module is a boot check that stops a boot."""
        life = cli.read_token_life()
        assert isinstance(life, cli.TokenLife)
        assert life.readable in (True, False)


class TestTheBootCheckNeverRefusesToBoot:
    """The rule this file inherits and must not break: every condition here
    degrades the NEW-ORDER path only, and refusing to start would turn «sales
    are stopped» into «the product is down for the people who already paid»."""

    def test_a_dead_credential_is_reported_and_the_process_continues(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
        monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
        monkeypatch.setattr(cli, "read_token_life",
                            lambda: cli.TokenLife.measured(-9))
        admin = FakeAdmin()
        with caplog.at_level("ERROR"):
            problems = cli.report_environment(
                settings=_settings(), session=None, admin_client=admin,
                today=TODAY,
            )
        assert [p.key for p in problems] == ["SALLA_TOKEN_EXPIRES_AT"]
        assert any("BOOT CHECK SALLA_TOKEN_EXPIRES_AT" in r.message
                   for r in caplog.records)
        assert len(admin.sent) == 1

    def test_a_store_that_explodes_does_not_stop_the_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> cli.TokenLife:
            raise RuntimeError("credential file unreadable")

        monkeypatch.setattr(cli, "approved_plan_prices", lambda session: APPROVED)
        monkeypatch.setattr(cli, "canonical_sale_plans", lambda: ON_SALE)
        monkeypatch.setattr(cli, "read_token_life", _boom)
        assert cli.report_environment(
            settings=_settings(), session=None, admin_client=FakeAdmin(),
            today=TODAY,
        ) == []


# ── the units ───────────────────────────────────────────────────────────────


def _directives(name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (UNITS / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


class TestTheUnits:
    def test_systemd_accepts_both_files(self) -> None:
        binary = shutil.which("systemd-analyze")
        if binary is None:  # pragma: no cover — CI images carry systemd
            pytest.skip("systemd-analyze not installed")
        done = subprocess.run(  # noqa: S603 — resolved path, fixed argv
            [binary, "verify", "./career-salla-token.service",
             "./career-salla-token.timer"],
            cwd=UNITS, capture_output=True, text=True, check=False,
        )
        assert done.returncode == 0, done.stderr
        assert done.stderr.strip() == ""

    def test_the_service_matches_the_conventions_of_its_neighbours(
        self,
    ) -> None:
        service = _directives("career-salla-token.service")
        assert service["Type"] == "oneshot"
        # The same alert hook every other scheduled job here uses: a run that
        # exits non-zero reaches the operator once, deduplicated.
        assert service["OnFailure"] == "career-alert@%n.service"
        assert service["EnvironmentFile"] == "/root/career/.env.staging"
        assert service["ExecStart"].endswith("scripts/refresh_salla_token.py")
        assert Path(service["ExecStart"].split()[0]).name == "python"

    def test_the_timer_is_daily_and_catches_up_after_a_dark_night(
        self,
    ) -> None:
        timer = _directives("career-salla-token.timer")
        assert timer["OnCalendar"] == "*-*-* 09:00:00 Asia/Riyadh"
        # A machine that was off at 09:00 is exactly the machine whose
        # credential aged another day unattended.
        assert timer["Persistent"] == "true"
        assert timer["WantedBy"] == "timers.target"

    def test_the_schedule_leaves_room_before_the_nightly_run(self) -> None:
        """Renew at 09:00, deliver at 11:00: the credential is in place before
        the run that spends it, and the boot check at 11:00 is a second,
        independent witness on the same day rather than the next one."""
        refresh_hour = int(
            _directives("career-salla-token.timer")["OnCalendar"].split()[1][:2]
        )
        nightly_hour = int(
            _directives("career-engine-nightly.timer")["OnCalendar"].split()[1][:2]
        )
        assert refresh_hour < nightly_hour

    def test_the_script_the_unit_runs_exists_and_is_the_one_tested(
        self,
    ) -> None:
        exec_start = _directives("career-salla-token.service")["ExecStart"]
        assert (REPO / "scripts" / "refresh_salla_token.py").exists()
        assert exec_start.split()[-1] == "/root/career/scripts/refresh_salla_token.py"
