"""Database engines and the tenant-scoped session.

Every application query runs as the non-superuser ``career_app`` role with a
transaction-local ``app.tenant_id`` GUC set. RLS policies read that GUC through
``app.current_tenant()``, so a query can only ever see its own tenant's rows
(§15.10). The Worker/request layer must set the tenant explicitly — there is no
ambient default, and since migration 0021 there is no silent one either: a
scoped query with nothing bound raises 42501 instead of returning the empty set.

AUDIT (5 أغسطس): this module's docstring was the stated enforcement of
§15.10/§15.11 and had **zero production callers**. The three long-running
processes each built their own ``create_engine(settings.owner_database_url)``,
and ``career_owner`` is the bootstrap superuser — ``rolsuper=t``,
``rolbypassrls=t`` — so RLS was inert for every process that serves a customer.
Isolation rested on hand-written ``WHERE tenant_id = …`` alone. That is being
undone in stages (DEVIATIONS D20); this module is the destination. **There is no
owner engine here, and there must never be one** — ``tests/test_rls_runtime_role.py``
fails if one appears, and fails if a new production module reaches for
``owner_database_url``.

Since 6 أغسطس that ratchet is no longer the only thing standing there. The
first half of this module is the role guard: every engine the product builds
goes through :func:`engine_for`, and every connection any engine in the
process opens — including one a library builds for itself — passes
:func:`_check_role` before libpq is dialled. See its comment for what that
buys and what still flips it.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid as _uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from career.config import get_settings

logger = logging.getLogger("career.db")

_settings = get_settings()


# ===========================================================================
# The role guard
# ===========================================================================
#
# The AST ratchet in tests/test_rls_runtime_role.py wrote down its own limit
# honestly: a scan that looks for names can only see the names in front of it.
# It cannot resolve ``getattr(settings, "owner_" + "database_url")``, a DSN
# read out of a vault at runtime, a psycopg connection assembled from four
# separate ``os.environ`` reads with no recognisable literal, or a third-party
# library that dials the database itself. Every one of those still has to hand
# a username to libpq, and that is where this checks instead. The difference
# is the one the reviewer asked for: a bypass that is impossible rather than a
# bypass that is detected.
#
# There are two enforcement points and they are deliberately different:
#
# * :func:`engine_for` — at ENGINE CONSTRUCTION, for code we write. It fails
#   at the line that built the engine, which is the line that has to change.
# * a process-wide ``do_connect`` listener on the Engine CLASS — at the first
#   connection of any engine, however it was built. This is the one that
#   covers the library and the four-``os.environ`` cases, because it never
#   reads our source at all; it reads the credentials being dialled.
#
# The listener is installed by importing this module, and `career.db.models`
# imports it for exactly that reason (the note is there): the three D20
# services do not import this module today, and a guard that is not installed
# in the process it is meant to watch is a comment.


class ForbiddenDatabaseRole(RuntimeError):
    """A process tried to connect as a role it has no sanctioned reason to use.

    Raised at engine construction where possible and at connect otherwise. It
    is a start-up failure on purpose: a process that cannot prove which role it
    is entitled to must not serve a customer as a superuser while somebody
    reads the logs.
    """


#: The roles ordinary code may connect as. ``career_app`` is the runtime role
#: RLS applies to. ``career_sweep`` is NOLOGIN today — it exists to own the
#: ``app.*`` routing functions (0021) — and is listed here so that the day it
#: is given a login, the guard does not have to be edited in a hurry by
#: somebody who is already mid-incident.
#:
#: The owner is NOT named here, and naming it is not needed: the guard asks
#: «is this the application role?», not «is this the owner?». A vault DSN with
#: a username nobody has ever seen fails the same way `career_owner` does,
#: which is the whole point — the list of forbidden names is always one rename
#: behind, the list of permitted ones is not.
_APPLICATION_ROLES: frozenset[str] = frozenset({_settings.db_user, "career_sweep"})


@dataclass(frozen=True)
class _Escape:
    """One sanctioned way to connect as something other than the app role.

    ``why`` is not documentation. It is the thing a reviewer reads to decide
    whether the escape should still exist, so it says what would break if the
    escape were removed rather than what the process does.
    """

    why: str
    permanent: bool


#: Every escape there is. Adding one is a code change with a reviewer on it;
#: that is the entire access-control model here and it is enough, because the
#: set is small and each entry has to survive being read out loud.
_ESCAPES: dict[str, _Escape] = {
    "alembic": _Escape(
        why=(
            "Migrations create and alter the tables, and in Postgres only the "
            "role that owns a table may alter it. career_app holds CRUD grants "
            "and nothing else, so an Alembic run on the application role dies "
            "at the first ALTER. There is no version of this escape the guard "
            "could close without closing migrations, so it is permanent and "
            "there is nothing staged about it."
        ),
        permanent=True,
    ),
    "pytest": _Escape(
        why=(
            "The suite proves the guarantees FROM OUTSIDE them — that the "
            "owner is the role which bypasses RLS and the runtime role is not, "
            "that career_sweep's column grants actually refuse, that "
            "break_glass writes a row the application can neither read nor "
            "delete. None of that is assertable from inside career_app; a "
            "test that could only look from the inside is the test_rls_meta "
            "mistake (it passed throughout the whole period RLS was inert). "
            "The blast radius is already closed elsewhere: conftest's "
            "owner_engine fixture refuses any database whose name does not "
            "end in _test."
        ),
        permanent=True,
    ),
    "operator_one_shot": _Escape(
        why=(
            "A human runs these by hand, once, for the whole estate — "
            "replaying lost events, wiring the Salla products, the disaster "
            "drill, creating the app role on a fresh CI database. They have no "
            "single tenant to bind, so tenant_session has nothing to offer "
            "them, and no daemon inherits the privilege when they exit. D20 "
            "stage 4 leaves owner access exactly here and nowhere else."
        ),
        permanent=True,
    ),
    "d20_legacy_service": _Escape(
        why=(
            "The three long-running services and the nightly shim, which have "
            "connected as the owner since before RLS was runtime-real (D20). "
            "This is the only NON-permanent escape and it is the reason the "
            "guard warns instead of raising for them today: 22 call sites "
            "inside those processes still do their own WHERE tenant_id, and a "
            "guard that demanded all of them move in one deploy would either "
            "not be shipped or would take the product down on the day it was. "
            "Deleting an entry from _D20_LEGACY_ENTRYPOINTS as each process "
            "moves onto tenant_session is what retires it; when the set is "
            "empty this escape has no members and can be deleted with it."
        ),
        permanent=False,
    ),
}

#: The three services plus the nightly shim, by the file that starts them —
#: `ops/systemd/career-*.service` runs each of these by path. A process is the
#: right key here: the escape is a statement about which PROCESSES may leave
#: the app role, and a connect-time listener cannot see anything finer.
_D20_LEGACY_ENTRYPOINTS: frozenset[str] = frozenset(
    {"run_worker_loop.py", "run_admin_bot.py", "run_nightly.py"}
)

#: The by-hand scripts. Same list as the operator half of the AST ratchet's
#: allow-list, and the two are checked against each other in
#: tests/test_rls_runtime_role.py so they cannot drift apart quietly.
_OPERATOR_ONE_SHOT_ENTRYPOINTS: frozenset[str] = frozenset(
    {
        "replay_lost_events.py",
        "wire_salla_products.py",
        "demo_engine_tenants.py",
        "drill_disaster.py",
        "rehearse_enrichment.py",
        "ci_create_app_role.py",
    }
)

#: Set to ``enforce`` to turn the legacy warning into a refusal.
#:
#: THE TRIGGER, written down so it is not a matter of anyone's memory: staging
#: runs with DB_ROLE_GUARD=enforce first — that is the cheap way to find out
#: whether a process still needs the owner, since it fails at boot rather than
#: at 04:30 in the middle of the nightly. When staging survives a full week
#: including a Sunday report and a nightly run, production takes the same
#: variable, and the day the last name leaves _D20_LEGACY_ENTRYPOINTS the
#: default here becomes irrelevant because the escape has no members left.
#:
#: Read from the environment rather than from Settings deliberately: this
#: guard has to work in a process that never built a Settings object, and it
#: is read on every check so that flipping it does not need a code change.
ROLE_GUARD_ENV = "DB_ROLE_GUARD"

#: (escape, process) pairs already warned about. A warning per connection
#: would be a hundred lines an hour and would teach the operator to filter it.
_warned_escapes: set[tuple[str, str]] = set()


def _process_escape() -> str | None:
    """Which escape, if any, THIS process is entitled to.

    Alembic and pytest are recognised by their presence in ``sys.modules``
    rather than by argv, because both are launched half a dozen different ways
    (`alembic upgrade`, `python -m pytest`, a tox shim) and the module is the
    one thing all of them have in common. Everything else is recognised by the
    file that was executed, which is what systemd and the operator both name.
    """
    if "alembic" in sys.modules:
        return "alembic"
    if "pytest" in sys.modules:
        return "pytest"
    entrypoint = Path(sys.argv[0] or "").name
    if entrypoint in _D20_LEGACY_ENTRYPOINTS:
        return "d20_legacy_service"
    if entrypoint in _OPERATOR_ONE_SHOT_ENTRYPOINTS:
        return "operator_one_shot"
    return None


def _enforcing() -> bool:
    return (os.environ.get(ROLE_GUARD_ENV) or "").strip().lower() == "enforce"


def _check_role(username: str, *, escape: str | None, site: str) -> None:
    """Decide whether ``username`` may be dialled from here, and say why not.

    Three outcomes, and which one you get is the whole design:

    * an application role — nothing happens, this is the ordinary path;
    * a privileged role with NO named escape — refused, always, from the day
      this shipped. Nothing legitimate is in this branch: the entire existing
      estate is on the escape lists below, so raising here cannot take the
      product down, and it is the branch a newly-written service that reaches
      for the owner lands in;
    * a privileged role with a named escape — permitted if the escape is
      permanent, and warned-then-permitted if it is the staged D20 one, until
      DB_ROLE_GUARD says enforce.
    """
    if username in _APPLICATION_ROLES:
        return
    if escape is None:
        raise ForbiddenDatabaseRole(
            f"{site}: refusing to connect as {username!r} — the application "
            f"connects as one of {sorted(_APPLICATION_ROLES)}. If this process "
            "genuinely needs more, it needs a NAMED escape in "
            "career.db.session._ESCAPES with the reason written next to it, "
            "and a DEVIATIONS entry. For per-tenant work use tenant_session; "
            "to answer «which tenant» use sweep_session; to open one tenant "
            "on the record use break_glass_session."
        )
    rule = _ESCAPES.get(escape)
    if rule is None:
        raise ForbiddenDatabaseRole(
            f"{site}: unknown escape {escape!r} — the sanctioned set is "
            f"{sorted(_ESCAPES)}. A typo must not read as permission."
        )
    if rule.permanent:
        return
    if _enforcing():
        raise ForbiddenDatabaseRole(
            f"{site}: {ROLE_GUARD_ENV}=enforce and {escape!r} is a staged "
            f"escape, not a permanent one — {rule.why}"
        )
    key = (escape, site)
    if key not in _warned_escapes:
        _warned_escapes.add(key)
        logger.warning(
            "db role guard: %s connects as %r under the staged escape %r "
            "(D20). Set %s=enforce once this process runs on the application "
            "role.",
            site,
            username,
            escape,
            ROLE_GUARD_ENV,
        )


def engine_for(url: str, *, escape: str | None = None, **kwargs: Any) -> Engine:
    """Build an engine, refusing the ones that should not exist.

    Every ``create_engine`` in the product goes through here. The username is
    read out of the URL that was actually handed over, so it does not matter
    whether that URL came from Settings, from an f-string, from a vault or
    from four environment variables — by the time it is a DSN it has a role
    name in it, and the role name is the thing being checked.

    A URL that names no user at all is refused too. libpq would fall back to
    the operating-system user, which is root inside the container and whoever
    ran the command outside it: an unpredictable role is exactly the thing
    this guard exists to stop, and «unpredictable» is worse than «wrong».
    """
    username = make_url(url).username
    _check_role(username or "<no user in the DSN>", escape=escape, site="engine_for")
    engine = create_engine(url, **kwargs)
    if escape is not None:
        # Remember the verdict for the listener below. Without this an engine
        # the factory has ALREADY approved would be re-judged at its first
        # connection by process name alone and refused — which is how a guard
        # teaches people that the sanctioned path is the one that does not
        # work. The dialect is the key because it is per-engine and it is the
        # only object `do_connect` is handed.
        _factory_escapes[engine.dialect] = escape
    return engine


#: A libpq conninfo string, which some dialects pass positionally instead of
#: as keywords. `user=career_owner dbname=…` is the shape.
_CONNINFO_USER = re.compile(r"(?:^|\s)user\s*=\s*'?([^\s']+)")

#: Engines the factory already vetted, by the dialect instance each one owns.
#: Weak so that a disposed engine takes its entry with it.
_factory_escapes: WeakKeyDictionary[Any, str] = WeakKeyDictionary()


@event.listens_for(Engine, "do_connect")
def _guard_every_connection(
    dialect: Any, conn_rec: Any, cargs: Any, cparams: Any
) -> None:
    """The net under the factory: every DBAPI connection, whoever built it.

    Registered on the Engine CLASS, so it applies to engines this module never
    saw — the ones a library builds, the ones a future module builds with a
    bare ``create_engine`` import, the ones assembled out of environment
    variables. It reads the credentials on their way to libpq, which is the
    last place they are still true.

    Returns None in every permitted case so SQLAlchemy connects normally; the
    refusal is an exception, which surfaces at the first connection rather
    than at import for engines that did not come from :func:`engine_for`.
    """
    username = cparams.get("user") if isinstance(cparams, dict) else None
    if username is None:
        for arg in cargs or ():
            if isinstance(arg, str) and (found := _CONNINFO_USER.search(arg)):
                username = found.group(1)
                break
    if username is None:
        # No username to judge. A dialect with no roles at all (sqlite in a
        # throwaway test) is not what this guard is about, and inventing a
        # verdict for it would only teach people to disable the guard.
        return
    approved = _factory_escapes.get(dialect) if dialect is not None else None
    _check_role(
        str(username),
        escape=approved or _process_escape(),
        site=Path(sys.argv[0] or "").name or "<process>",
    )


# ===========================================================================
# Engines and sessions
# ===========================================================================

# Application engine — connects as career_app (RLS applies).
app_engine: Engine = engine_for(
    _settings.app_database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False)

#: SQLSTATE the fail-closed policy function raises. Callers that want to turn a
#: missing scope into a domain error rather than a 500 match on this, never on
#: the message text.
NO_TENANT_CONTEXT_SQLSTATE = "42501"


def _coerce_tenant_id(tenant_id: str) -> str:
    """Reject anything that is not a UUID before it reaches the GUC.

    ``set_config`` accepts any string. An empty or malformed value used to mean
    «no rows, quietly»; since 0021 it means «error at the next query», which is a
    long way from the line that actually made the mistake. Fail at the call.
    """
    try:
        return str(_uuid.UUID(str(tenant_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"tenant_id is not a UUID: {tenant_id!r}") from exc


def _set_tenant(session: Session, tenant_id: str) -> None:
    """Bind this transaction to a tenant via a transaction-local GUC.

    SET LOCAL scopes the setting to the current transaction, so a pooled
    connection never leaks one tenant's id into another's query.
    """
    session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": _coerce_tenant_id(tenant_id)},
    )


@contextmanager
def tenant_session(tenant_id: str) -> Iterator[Session]:
    """Yield a session pinned to ``tenant_id`` for the duration of one transaction.

    The tenant GUC is set inside the transaction (SET LOCAL semantics via
    set_config(..., true)); commit/rollback ends both the transaction and the
    tenant scope. The Worker re-verifies ownership from the DB and never trusts a
    queue payload (§15.11) — this helper is that enforcement boundary.
    """
    session = SessionLocal()
    try:
        session.begin()
        _set_tenant(session, tenant_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def tenant_scope(session: Session, tenant_id: str) -> Iterator[Session]:
    """Bind an ALREADY-OPEN session to one tenant, then put back what was there.

    The sweeps hold a single session and hand it to per-tenant work; this is the
    seam that lets those call sites move onto the application role one function
    at a time instead of in one commit (DEVIATIONS D20). Prefer
    :func:`tenant_session` in new code — it owns the transaction, so the scope
    cannot outlive it.
    """
    previous = session.execute(
        text("SELECT current_setting('app.tenant_id', true)")
    ).scalar_one()
    _set_tenant(session, tenant_id)
    try:
        yield session
    finally:
        # Restore rather than clear: a scope opened inside another tenant's work
        # must not hand back an unscoped session.
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": previous or ""},
        )


@contextmanager
def sweep_session() -> Iterator[Session]:
    """Yield an UNSCOPED application session, for routing questions only.

    A sweep legitimately has to span tenants — routing an inbound message by
    phone before the tenant is known, the nightly fan-out, the retention sweep,
    the seat count. It still runs as ``career_app``, so every tenant table stays
    closed to it: touching one raises 42501. The only thing it can do across
    tenants is call the ``app.*`` functions below, which are owned by the NOLOGIN
    ``career_sweep`` role, hold fixed queries, and return a tenant id — never a
    row. Ask «which tenant», then open :func:`tenant_session` and do the work.
    """
    session = SessionLocal()
    try:
        session.begin()
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# The sweep capability. Each of these is a sanctioned way to learn a tenant id
# without already having one. They are thin on purpose: the security argument
# lives in migration 0021 (column-level grants on six tables), and a wrapper
# that added a predicate of its own would move part of that argument back into
# Python, where nothing enforces it.
# --------------------------------------------------------------------------


def tenant_for_phone(session: Session, phone_e164: str) -> str | None:
    """Route an inbound message to its tenant. None for an unknown number."""
    value = session.execute(
        text("SELECT app.tenant_for_phone(:p)"), {"p": phone_e164}
    ).scalar_one()
    return str(value) if value is not None else None


def tenant_for_salla_order(session: Session, salla_order_id: str) -> str | None:
    """Resolve a Salla order to its tenant — the webhook knows the order, not us."""
    value = session.execute(
        text("SELECT app.tenant_for_salla_order(:o)"), {"o": salla_order_id}
    ).scalar_one()
    return str(value) if value is not None else None


def tenant_ids_with_status(session: Session, statuses: list[str]) -> list[str]:
    """Tenants whose subscription is in one of ``statuses`` — the nightly fan-out.

    The status vocabulary is passed in rather than written into the SQL: it is
    product policy, it has already changed twice (0018, 0020), and a migration is
    the worst place to keep a second copy of it.
    """
    rows = session.execute(
        text("SELECT app.tenant_ids_with_status(:s)"), {"s": statuses}
    ).scalars()
    return [str(r) for r in rows]


def tenants_with_pending_work(session: Session) -> list[str]:
    """Tenants with an unpublished outbox event, an open journey, or an open delivery."""
    rows = session.execute(text("SELECT app.tenants_with_pending_work()")).scalars()
    return [str(r) for r in rows]


@contextmanager
def break_glass_session(tenant_id: str, reason: str) -> Iterator[Session]:
    """Open one tenant deliberately, on the record.

    For the operator answering «ليش ما وصلت TEN-0002 فرصها اليوم» — never for
    anything on a timer. ``app.break_glass`` refuses a reason under ten
    characters, writes ``break_glass_log`` (which the application role can
    neither read nor delete), and emits a server-log line, because the table row
    is inside this transaction and a rollback would take it along.

    This buys audit, not containment. ``app.tenant_id`` is an unreserved GUC and
    Postgres 16 does not enforce ``GRANT SET ON PARAMETER`` for that namespace —
    measured on career_test, 5 أغسطس — so ``career_app`` can bind any tenant it
    likes without asking. RLS here stops the missing WHERE clause and the silent
    regression, not a process that has already been taken over. DEVIATIONS D21.
    """
    session = SessionLocal()
    try:
        session.begin()
        session.execute(
            text("SELECT app.break_glass(:tid, :reason)"),
            {"tid": _coerce_tenant_id(tenant_id), "reason": reason},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
