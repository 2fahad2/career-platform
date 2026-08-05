"""RLS fails closed, and cross-tenant sweeps get a narrow named capability

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-05

AUDIT (constant 10, «العزل بين المستأجرين»): the schema half of RLS was real —
30 tables ENABLE, 29 FORCE, a meta-test that fails when a migration forgets it,
adversarial cross-tenant tests in CI. The *runtime* half was not there at all.
Every long-running process (`scripts/run_worker_loop.py`, `scripts/run_admin_bot.py`,
`career.engine.cli`) opens `create_engine(settings.owner_database_url)`, and
`career_owner` is the Postgres bootstrap superuser: `rolsuper=t`, `rolbypassrls=t`.
A superuser bypasses RLS unconditionally, so FORCE is decorative for the three
processes that actually serve customers. Isolation in the deployed system rested
entirely on hand-written `WHERE tenant_id = …` in ~22 modules, including
`onboarding/privacy.py`, where a single missed predicate exports one customer's
file to another. It works today; nothing would catch the regression.

This migration builds the database half of the fix. It does NOT move any process
onto the application role — that is a call-site change tracked in DEVIATIONS D20.
It is deliberately safe to land first: `career_app` is today used only by the
FastAPI webhook intake, which writes `webhook_events` (a table with no RLS), so
the blast radius of the stricter policies against the running product is nil.
Landing the database side first means the call-site migration lands onto rules
that are already proven, rather than the two changing together.

Three things change.

1. FAIL CLOSED. Measured on career_test against the old policy expression
   `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`:
   with no tenant bound, SELECT returned 0 rows *silently*, UPDATE and DELETE
   touched 0 rows *silently*, and only INSERT raised. Silence is the dangerous
   answer: a query that lost its scope reports «nothing found», and the caller
   takes the branch for a customer who has no subscription, no dedupe row, no
   pending delivery. The policy now calls `app.current_tenant()`, which RAISES
   42501 when nothing is bound. The only case that still returns quietly is a
   physically empty table, where the qual is never evaluated — and an empty
   table leaks nothing.

2. A SWEEP CAPABILITY THAT IS NOT A BLANKET. Routing an inbound message by
   phone, provisioning by Salla order id, the nightly fan-out and the retention
   sweep genuinely have to span tenants; the tenant is not known yet, or the
   whole point is every tenant. Handing those a role with cross-tenant SELECT
   would only rename the problem. Instead `career_sweep` is a NOLOGIN role that
   nobody is a member of and nobody can log in as. It owns four SECURITY DEFINER
   functions whose bodies are fixed here and whose return type is a tenant id —
   never a row. Its SELECT grants are COLUMN level, so even the author of a
   future sweep function cannot reach `onboarding_sessions.context`,
   `outbox_events.payload` or a message body: the ceiling is in the catalog, not
   in the code review. The sweep answers «which tenant», and the caller then
   opens a scoped session and does the work.

3. BREAK GLASS IS NAMED AND LOUD. `app.break_glass(tenant, reason)` demands a
   reason, writes `break_glass_log`, and emits a server-log line — the log line
   because the table row is in the caller's transaction and a ROLLBACK would
   erase it. It is not what any daily service calls.

Expected lock profile: catalog-only. 30 × (DROP POLICY + CREATE POLICY), each
taking a brief ACCESS EXCLUSIVE lock on its table; one CREATE TABLE; no ALTER
TABLE that rewrites, no index build, no data scan, no row touched. Total work is
milliseconds. The risk is not duration, it is queueing: an ACCESS EXCLUSIVE
request parked behind a long-running SELECT blocks every later reader of that
table. `lock_timeout` is therefore set to 3s so the migration aborts and can be
retried instead of stalling the worker loop.

Downgrade restores the previous inline expressions exactly and is
non-destructive: `break_glass_log` is deliberately left in place, because an
audit trail that a downgrade deletes is not an audit trail.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "career_app"
SWEEP_ROLE = "career_sweep"

#: The expression every policy carried before this migration. Kept verbatim so
#: downgrade() restores the exact catalog state, not an approximation of it.
_OLD_GUC = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_NEW_GUC = "app.current_tenant()"

#: (table, policy name, keyed column). Names are NOT derivable: 0001 called the
#: documents policy `document_tenant_isolation` (singular) and 0002 called the
#: outbox one `outbox_tenant_isolation`, and `tenants` keys on `id` because the
#: tenant IS the row. Read from pg_policies on the live database, not guessed.
_POLICIES: tuple[tuple[str, str, str], ...] = (
    ("activation_tokens", "activation_tokens_tenant_isolation", "tenant_id"),
    ("audit_events", "audit_events_tenant_isolation", "tenant_id"),
    ("career_path_assessments", "career_path_assessments_tenant_isolation", "tenant_id"),
    ("consent_events", "consent_events_tenant_isolation", "tenant_id"),
    ("cost_allocations", "cost_allocations_tenant_isolation", "tenant_id"),
    ("customer_channels", "customer_channels_tenant_isolation", "tenant_id"),
    ("customer_profiles", "customer_profiles_tenant_isolation", "tenant_id"),
    ("cv_uploads", "cv_uploads_tenant_isolation", "tenant_id"),
    ("deliveries", "deliveries_tenant_isolation", "tenant_id"),
    ("delivery_messages", "delivery_messages_tenant_isolation", "tenant_id"),
    ("documents", "document_tenant_isolation", "tenant_id"),
    ("forbidden_claims", "forbidden_claims_tenant_isolation", "tenant_id"),
    ("funnel_sessions", "funnel_sessions_tenant_isolation", "tenant_id"),
    ("inbound_messages", "inbound_messages_tenant_isolation", "tenant_id"),
    ("onboarding_sessions", "onboarding_sessions_tenant_isolation", "tenant_id"),
    ("outbox_events", "outbox_tenant_isolation", "tenant_id"),
    ("outcome_events", "outcome_events_tenant_isolation", "tenant_id"),
    ("privacy_requests", "privacy_requests_tenant_isolation", "tenant_id"),
    ("processed_messages", "processed_messages_tenant_isolation", "tenant_id"),
    ("profile_facts", "profile_facts_tenant_isolation", "tenant_id"),
    ("role_enrichments", "role_enrichments_tenant_isolation", "tenant_id"),
    ("search_policies", "search_policies_tenant_isolation", "tenant_id"),
    ("subscription_events", "subscription_events_tenant_isolation", "tenant_id"),
    ("subscriptions", "subscriptions_tenant_isolation", "tenant_id"),
    ("support_events", "support_events_tenant_isolation", "tenant_id"),
    ("tenant_day_states", "tenant_day_states_tenant_isolation", "tenant_id"),
    ("tenant_job_decisions", "tenant_job_decisions_tenant_isolation", "tenant_id"),
    ("tenant_job_suppressions", "tenant_job_suppressions_tenant_isolation", "tenant_id"),
    ("tenants", "tenant_self_isolation", "id"),
    ("usage_events", "usage_events_tenant_isolation", "tenant_id"),
)

#: table → the ONLY columns the sweep role may read. Every omission is
#: deliberate: `onboarding_sessions.context`, `outbox_events.payload` and
#: `deliveries.bundle` carry the customer's own words and their CV, and a
#: routing question never needs them. Column grants are what stop this design
#: from being «a second role with cross-tenant SELECT» wearing a new name.
_SWEEP_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tenants", ("id",)),
    ("customer_channels", ("tenant_id", "phone_e164", "created_at")),
    ("subscriptions", ("tenant_id", "salla_order_id", "status")),
    ("outbox_events", ("tenant_id", "published_at")),
    ("onboarding_sessions", ("tenant_id", "completed_at")),
    ("deliveries", ("tenant_id", "completed_at")),
)


def _policy_sql(table: str, policy: str, column: str, guc: str) -> str:
    return (
        f"CREATE POLICY {policy} ON {table} "
        f"USING ({column} = {guc}) WITH CHECK ({column} = {guc})"
    )


def upgrade() -> None:
    # A catalog-only migration should never wait: ACCESS EXCLUSIVE queued behind
    # one long SELECT blocks every reader that arrives after it. Fail and retry
    # beats stalling the worker loop on a live database.
    op.execute("SET lock_timeout = '3s'")

    # ---------------------------------------------------------------- roles --
    # CREATE ROLE is CLUSTER-global, not database-local: career_test and
    # career_staging share one Postgres instance, so this runs once per cluster
    # and must be idempotent. No password and NOLOGIN — a credential does not
    # belong in a migration (it would sit in git and in the alembic log), and
    # this role is never meant to be connected as. It is reached only through
    # the SECURITY DEFINER functions below.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{SWEEP_ROLE}') THEN
                CREATE ROLE {SWEEP_ROLE}
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS NOINHERIT;
            END IF;
        END
        $$
        """
    )

    # --------------------------------------------------------------- schema --
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    # USAGE to PUBLIC because every policy in the database now calls a function
    # that lives here; a role without USAGE would get «permission denied for
    # schema app» on any tenant table, which is still closed but says the wrong
    # thing. The individual functions carry their own EXECUTE grants.
    op.execute("GRANT USAGE ON SCHEMA app TO PUBLIC")

    # ---------------------------------------------------- the fail-closed fn --
    # Split in two on purpose. `current_tenant()` is a plain SQL function so the
    # planner can inline it into the policy qual — a plpgsql call per row on
    # every table in the database is a cost we do not need. COALESCE is
    # guaranteed to short-circuit, so the raising branch is only reached when
    # the GUC really is unset.
    op.execute(
        f"""
        CREATE FUNCTION app.no_tenant_context() RETURNS uuid
        LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
        BEGIN
            -- The sweep role reaches rows through its own narrow FOR SELECT
            -- policies. Two PERMISSIVE policies are combined with OR and
            -- Postgres does not promise which side it evaluates first, so
            -- raising here would abort a sweep that was entitled to the row.
            -- Returning NULL makes the tenant policy simply false instead.
            IF pg_has_role(current_user, '{SWEEP_ROLE}', 'USAGE') THEN
                RETURN NULL;
            END IF;
            RAISE EXCEPTION
                'no app.tenant_id bound: refusing a tenant-scoped query'
                USING ERRCODE = '42501',
                      HINT = 'open the transaction through '
                             'career.db.session.tenant_session(tenant_id)';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.current_tenant() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT COALESCE(
                NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                app.no_tenant_context()
            )
        $$
        """
    )
    op.execute(
        "COMMENT ON FUNCTION app.current_tenant() IS "
        "'The tenant bound to this transaction. Raises 42501 when nothing is "
        "bound — a scoped query must never silently return the empty set.'"
    )

    # ------------------------------------------------------- rewrite policies --
    for table, policy, column in _POLICIES:
        op.execute(f"DROP POLICY {policy} ON {table}")
        op.execute(_policy_sql(table, policy, column, _NEW_GUC))

    # -------------------------------------------------- the sweep capability --
    op.execute(f"GRANT USAGE ON SCHEMA public TO {SWEEP_ROLE}")
    for table, columns in _SWEEP_COLUMNS:
        cols = ", ".join(columns)
        op.execute(f"GRANT SELECT ({cols}) ON {table} TO {SWEEP_ROLE}")
        # RLS applies to career_sweep too (it is neither owner nor BYPASSRLS),
        # so the column grant alone would return nothing. This second PERMISSIVE
        # policy is what lets it across tenants — and it is SELECT-only, so the
        # sweep role can never write anything, anywhere.
        op.execute(
            f"CREATE POLICY {table}_sweep_read ON {table} "
            f"FOR SELECT TO {SWEEP_ROLE} USING (true)"
        )

    # Every one of these carries `SET search_path = pg_catalog, public`, spelled
    # out rather than shared through a variable: without it, a caller who can
    # create objects in a schema earlier on the path chooses which
    # `customer_channels` a SECURITY DEFINER function reads, and that clause is
    # too important to be somewhere else on the screen. Plain strings, no
    # interpolation — the bodies are fixed here and that is the whole point.
    op.execute(
        """
        CREATE FUNCTION app.tenant_for_phone(p_phone text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT c.tenant_id FROM customer_channels c
            WHERE c.phone_e164 = p_phone
            ORDER BY c.created_at DESC
            LIMIT 1
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.tenant_for_salla_order(p_order_id text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT s.tenant_id FROM subscriptions s
            WHERE s.salla_order_id = p_order_id
            LIMIT 1
        $$
        """
    )
    # Statuses are passed in rather than written here: the plan/status
    # vocabulary is product policy and it has already changed twice (0018, 0020).
    # A migration is the worst place to keep a copy of it.
    op.execute(
        """
        CREATE FUNCTION app.tenant_ids_with_status(p_statuses text[])
        RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT DISTINCT s.tenant_id FROM subscriptions s
            WHERE s.status = ANY(p_statuses)
        $$
        """
    )
    # Deliberately expressed as «unfinished», not as a status literal, for the
    # same reason — and because completed_at is the column the sweep role is
    # granted, so the function cannot drift onto a wider one without a migration.
    op.execute(
        """
        CREATE FUNCTION app.tenants_with_pending_work() RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT tenant_id FROM outbox_events WHERE published_at IS NULL
            UNION
            SELECT tenant_id FROM onboarding_sessions WHERE completed_at IS NULL
            UNION
            SELECT tenant_id FROM deliveries WHERE completed_at IS NULL
        $$
        """
    )

    for fn in (
        "app.tenant_for_phone(text)",
        "app.tenant_for_salla_order(text)",
        "app.tenant_ids_with_status(text[])",
        "app.tenants_with_pending_work()",
    ):
        # The function must RUN as career_sweep for its policies and column
        # grants to be the ones that apply.
        op.execute(f"ALTER FUNCTION {fn} OWNER TO {SWEEP_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")

    # ---------------------------------------------------------- break glass --
    # target_tenant_id, not tenant_id: this table is the record of crossing the
    # boundary, so it must not itself be confined by it — and the RLS meta-test
    # keys on a column literally named tenant_id, which would otherwise demand a
    # policy that makes the log invisible to the person auditing it.
    #
    # IF NOT EXISTS because downgrade() deliberately leaves this table behind
    # (an audit trail a downgrade deletes is not an audit trail), so a
    # down-then-up cycle finds it already there. Caught on the round-trip test.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS break_glass_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            at timestamptz NOT NULL DEFAULT now(),
            db_user name NOT NULL,
            target_tenant_id uuid NOT NULL,
            reason text NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.break_glass(p_tenant uuid, p_reason text)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_reason IS NULL OR length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION
                    'break-glass requires a reason of at least 10 characters'
                    USING ERRCODE = '22023';
            END IF;
            INSERT INTO break_glass_log (db_user, target_tenant_id, reason)
            VALUES (session_user, p_tenant, p_reason);
            -- The row above lives in the caller's transaction, and a caller who
            -- rolls back erases it. Postgres has no autonomous transaction, so
            -- the durable half of the trail is the server log, which a ROLLBACK
            -- cannot reach.
            RAISE LOG 'BREAK-GLASS: % opened tenant % — %',
                session_user, p_tenant, p_reason;
            PERFORM set_config('app.tenant_id', p_tenant::text, true);
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION app.break_glass(uuid, text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION app.break_glass(uuid, text) TO {APP_ROLE}")
    # No grant of any kind on break_glass_log to the app role: the process that
    # can break the glass must not be able to read, edit or delete the record of
    # having done it.


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")

    op.execute("DROP FUNCTION IF EXISTS app.break_glass(uuid, text)")
    for fn in (
        "app.tenant_for_phone(text)",
        "app.tenant_for_salla_order(text)",
        "app.tenant_ids_with_status(text[])",
        "app.tenants_with_pending_work()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")

    for table, columns in _SWEEP_COLUMNS:
        op.execute(f"DROP POLICY IF EXISTS {table}_sweep_read ON {table}")
        cols = ", ".join(columns)
        # The S608 suppression below is safe: `cols` and `table` come from
        # _SWEEP_COLUMNS above — module constants in this file, never input.
        op.execute(f"REVOKE SELECT ({cols}) ON {table} FROM {SWEEP_ROLE}")  # noqa: S608
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {SWEEP_ROLE}")

    for table, policy, column in _POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(_policy_sql(table, policy, column, _OLD_GUC))

    op.execute("DROP FUNCTION IF EXISTS app.current_tenant()")
    op.execute("DROP FUNCTION IF EXISTS app.no_tenant_context()")
    op.execute("DROP SCHEMA IF EXISTS app")

    # break_glass_log survives on purpose — see the module docstring. The role is
    # cluster-global and may be referenced by another database on the same
    # instance, so it is not dropped either.
