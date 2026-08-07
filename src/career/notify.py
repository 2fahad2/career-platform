"""sd_notify — the forty lines that let systemd see a WEDGE.

Both long-running loops (``scripts/run_worker_loop.py``,
``scripts/run_admin_bot.py``) catch every exception and continue, which is the
right shape for a process that must survive a bad message — and the reason
monitoring could not see them die. A dead database, an expired Meta token or a
poll that never returns leaves the process ALIVE and the unit ACTIVE while
nothing at all is processed. systemd is watching the process, and the process
is fine.

Two incidents, both with ``systemctl`` reporting ``active``, ``Result=success``
and ``NRestarts=0`` the whole time, and no alert on anybody's phone:

  * 2026-08-04 09:39:08 → 09:40:48 — 34 consecutive cycles failed against a
    Postgres that was being restarted underneath the worker (100 seconds).
  * 2026-08-06 06:58:04 → the worker was restarted onto code that reads
    ``webhook_events.next_attempt_at``, a column the staging database does not
    have. Every cycle since has failed on the same query. Nine hours, ten
    thousand identical tracebacks, zero WhatsApp messages processed, and the
    unit green in every screen the operator has.

The cure is a heartbeat that only beats when work actually COMPLETED, and a
watchdog that kills the process when the beat stops. This module is the
heartbeat's writer: the sd_notify protocol is a single datagram of ASCII
``KEY=value`` lines to the ``AF_UNIX`` socket systemd hands us in
``NOTIFY_SOCKET``, so it needs no dependency — nothing is added to
requirements.lock for it, which matters for a control that must not itself be
a reason the worker fails to start.

Silent no-op when ``NOTIFY_SOCKET`` is absent. Everything that runs these
loops outside systemd — the C7.8 canary by hand, the logging-wiring probes,
a developer's terminal — must behave exactly as it did before, and a
heartbeat that raised when nobody was listening would be a new way to kill
the worker rather than a way to notice it died.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger("career.notify")

#: systemd's contract with a ``Type=notify`` service (sd_notify(3)).
NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
WATCHDOG_USEC_ENV = "WATCHDOG_USEC"
WATCHDOG_PID_ENV = "WATCHDOG_PID"


def _socket_address() -> bytes | None:
    """The notification socket as sendto() wants it, or None if unset.

    ``NOTIFY_SOCKET`` is either a filesystem path (``/run/systemd/notify``) or
    an abstract-namespace name introduced by ``@``. The abstract form has to be
    handed to the kernel as BYTES beginning with a NUL: Python's path
    conversion rejects a ``str`` with an embedded null byte, so encoding here
    (rather than letting ``sendto`` do it) is what makes the abstract case work
    at all — and systemd uses the abstract form whenever the unit runs with a
    private mount namespace.
    """
    raw = os.environ.get(NOTIFY_SOCKET_ENV, "")
    if not raw:
        return None
    if raw.startswith("@"):
        return b"\0" + raw[1:].encode("utf-8")
    if raw.startswith("/"):
        return raw.encode("utf-8")
    # Anything else is not an address sd_notify defines. Refusing is safer
    # than guessing: a wrong address only ever produces a silent heartbeat.
    logger.warning("NOTIFY_SOCKET is not a path or an abstract name — "
                   "heartbeat disabled")
    return None


def notify(state: str) -> bool:
    """Send one sd_notify datagram; return whether systemd was told.

    Never raises. False means «nobody was listening» (no socket, or the send
    failed), which every caller treats as normal: outside systemd there is no
    watchdog to feed, and inside it a lost datagram is covered by the next one.
    """
    address = _socket_address()
    if address is None:
        return False
    try:
        with socket.socket(
            socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC
        ) as sock:
            sock.sendto(state.encode("utf-8"), address)
    except OSError as exc:
        # Warning, not error: the watchdog deadline is the real backstop. If
        # the socket is genuinely gone, systemd stops hearing the beat and
        # restarts us, which is exactly the behaviour we are buying.
        logger.warning("sd_notify %r failed: %s", state.split("=")[0], exc)
        return False
    return True


def ready() -> bool:
    """READY=1 — «start-up finished, the watchdog deadline starts now».

    Sent ONCE, after the boot checks, by a ``Type=notify`` service. Until it
    arrives systemd holds the unit in ``activating`` and kills it at
    ``TimeoutStartSec``; that is why the code must be deployed before the unit
    file that asks for it (see ops/systemd/career-worker.service).
    """
    return notify("READY=1")


def watchdog() -> bool:
    """WATCHDOG=1 — «one full cycle of work COMPLETED».

    The placement of this call is the entire mechanism. It belongs at the end
    of the loop body, after the cycle's work has returned, and never in a
    ``finally`` and never in the ``except`` — a wedged or failing cycle that
    still pets the dog would have systemd certify both incidents above as
    healthy, which is the state we already had for free.
    """
    return notify("WATCHDOG=1")


def watchdog_interval_s() -> float | None:
    """The deadline systemd is holding us to, in seconds, or None.

    ``WATCHDOG_USEC`` is set by ``WatchdogSec=``; ``WATCHDOG_PID`` (when
    present) names the one process the deadline belongs to, so an inherited
    environment in a child never makes a subprocess think it owns the
    heartbeat — sd_watchdog_enabled(3). Read at boot only, to log the armed
    interval: after a deploy the operator can prove from the journal that the
    watchdog is real and what it is set to, instead of trusting the unit file.
    """
    raw = os.environ.get(WATCHDOG_USEC_ENV, "").strip()
    if not raw:
        return None
    owner = os.environ.get(WATCHDOG_PID_ENV, "").strip()
    if owner and owner != str(os.getpid()):
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    return usec / 1_000_000 if usec > 0 else None
