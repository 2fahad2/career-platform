"""Create the non-superuser application role in CI.

In Compose the role is created by docker/initdb/10-app-role.sh; the GitHub
Actions Postgres service container cannot run that init script, so CI creates
the role explicitly here — connecting as the owner/superuser role. Idempotent:
safe to run against a database that already has the role.

Reads DB_* from the environment (same names as Settings). No secrets in code.
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg import sql


def main() -> int:
    host = os.environ["DB_HOST"]
    port = os.environ.get("DB_PORT", "5432")
    dbname = os.environ["DB_NAME"]
    owner_user = os.environ["DB_OWNER_USER"]
    owner_password = os.environ["DB_OWNER_PASSWORD"]
    app_user = os.environ["DB_USER"]
    app_password = os.environ["DB_PASSWORD"]

    conninfo = (
        f"host={host} port={port} dbname={dbname} "
        f"user={owner_user} password={owner_password}"
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,)
        ).fetchone()
        if exists:
            print(f"role {app_user} already exists")
        else:
            # Values come from CI env, not user input; identifiers are our own
            # fixed config. psycopg.sql handles identifier/literal quoting.
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {role} LOGIN PASSWORD {pw} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(role=sql.Identifier(app_user), pw=sql.Literal(app_password))
            )
            print(f"created non-superuser role {app_user}")

        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=sql.Identifier(dbname), role=sql.Identifier(app_user)
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
