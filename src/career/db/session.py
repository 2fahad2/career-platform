"""Database engines and the tenant-scoped session.

Every application query runs as the non-superuser ``career_app`` role with a
transaction-local ``app.tenant_id`` GUC set. RLS policies read that GUC, so a
query can only ever see its own tenant's rows (§15.10). The Worker/request layer
must set the tenant explicitly — there is no ambient default.
"""

from __future__ import annotations

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


def _set_tenant(session: Session, tenant_id: str) -> None:
    """Bind this transaction to a tenant via a transaction-local GUC.

    SET LOCAL scopes the setting to the current transaction, so a pooled
    connection never leaks one tenant's id into another's query.
    """
    session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
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
