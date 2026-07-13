#!/usr/bin/env bash
# Runs once at Postgres first-boot (docker-entrypoint-initdb.d).
# Creates the application login role — a NON-superuser so RLS applies to it.
# The owner/superuser role is POSTGRES_USER; migrations run as that role and own
# the tables, while the app connects as ${APP_DB_USER} and is confined by RLS.
#
# APP_DB_USER / APP_DB_PASSWORD come from the environment (Compose env_file).
# Note: keep APP_DB_PASSWORD free of single quotes.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
            CREATE ROLE ${APP_DB_USER} LOGIN PASSWORD '${APP_DB_PASSWORD}'
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
        END IF;
    END
    \$\$;
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${APP_DB_USER};
EOSQL

echo "initdb: ensured non-superuser app role ${APP_DB_USER}"
