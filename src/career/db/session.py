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
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from career.config import get_settings

_settings = get_settings()

# Application engine — connects as career_app (RLS applies).
app_engine: Engine = create_engine(
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
