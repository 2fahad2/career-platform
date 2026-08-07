"""The sd_notify writer, proven against a REAL AF_UNIX datagram socket.

No systemd here and none needed: the protocol is «send these bytes to that
socket», so the test binds the socket itself and reads what arrives. What is
actually being defended:

  * the exact bytes — a watchdog that receives ``WATCHDOG=1 `` or
    ``b'READY=1\\n'`` is a watchdog that never resets, and the failure looks
    identical to a wedge (systemd kills a healthy worker every WatchdogSec);
  * the abstract-namespace address, which is the form systemd hands to any
    unit with a private mount namespace and the form a naive implementation
    gets wrong (``str`` with an embedded NUL is rejected by Python's path
    conversion, so it must be sent as bytes);
  * the no-op outside systemd — these loops are also run by hand and by the
    logging-wiring probes, and a heartbeat that raised where nobody listens
    would be a new way to kill the worker instead of a way to notice it died.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from career import notify as sd


@pytest.fixture()
def listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[socket.socket]:
    """A bound datagram socket standing in for systemd's own."""
    path = str(tmp_path / "notify.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(2.0)
    monkeypatch.setenv(sd.NOTIFY_SOCKET_ENV, path)
    yield sock
    sock.close()


class TestDatagram:
    def test_ready_sends_exactly_ready_one(self, listener: socket.socket) -> None:
        assert sd.ready() is True
        assert listener.recv(64) == b"READY=1"

    def test_watchdog_sends_exactly_watchdog_one(self, listener: socket.socket) -> None:
        assert sd.watchdog() is True
        assert listener.recv(64) == b"WATCHDOG=1"

    def test_every_beat_is_its_own_datagram(self, listener: socket.socket) -> None:
        # systemd resets the deadline per datagram; a coalesced or buffered
        # write would be one beat where the loop thinks it sent three.
        for _ in range(3):
            assert sd.watchdog() is True
        assert [listener.recv(64) for _ in range(3)] == [b"WATCHDOG=1"] * 3

    def test_abstract_namespace_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        name = f"career-notify-test-{uuid.uuid4().hex[:12]}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(b"\0" + name.encode())
        sock.settimeout(2.0)
        try:
            monkeypatch.setenv(sd.NOTIFY_SOCKET_ENV, "@" + name)
            assert sd.ready() is True
            assert sock.recv(64) == b"READY=1"
        finally:
            sock.close()


class TestNoOpOutsideSystemd:
    def test_absent_socket_is_a_silent_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(sd.NOTIFY_SOCKET_ENV, raising=False)
        assert sd.notify("READY=1") is False
        assert sd.ready() is False
        assert sd.watchdog() is False

    def test_empty_socket_variable_is_a_silent_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(sd.NOTIFY_SOCKET_ENV, "")
        assert sd.watchdog() is False

    def test_dead_socket_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd gone, path still in the environment — the loop must not die
        because the thing watching it did."""
        monkeypatch.setenv(sd.NOTIFY_SOCKET_ENV, str(tmp_path / "not-there.sock"))
        assert sd.watchdog() is False

    def test_nonsense_address_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(sd.NOTIFY_SOCKET_ENV, "tcp://systemd")
        assert sd.watchdog() is False


class TestWatchdogInterval:
    """Read once at boot so the journal states the armed deadline in seconds —
    the operator's proof after a deploy that the watchdog is actually on."""

    def test_reports_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(sd.WATCHDOG_USEC_ENV, "300000000")
        monkeypatch.delenv(sd.WATCHDOG_PID_ENV, raising=False)
        assert sd.watchdog_interval_s() == 300.0

    def test_absent_means_no_watchdog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(sd.WATCHDOG_USEC_ENV, raising=False)
        assert sd.watchdog_interval_s() is None

    def test_belongs_to_the_named_pid_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An inherited environment must not make a child believe it owns the
        # heartbeat (sd_watchdog_enabled(3)).
        monkeypatch.setenv(sd.WATCHDOG_USEC_ENV, "300000000")
        monkeypatch.setenv(sd.WATCHDOG_PID_ENV, str(os.getpid() + 1))
        assert sd.watchdog_interval_s() is None
        monkeypatch.setenv(sd.WATCHDOG_PID_ENV, str(os.getpid()))
        assert sd.watchdog_interval_s() == 300.0

    def test_garbage_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(sd.WATCHDOG_PID_ENV, raising=False)
        for value in ("", "  ", "n/a", "-1", "0"):
            monkeypatch.setenv(sd.WATCHDOG_USEC_ENV, value)
            assert sd.watchdog_interval_s() is None
