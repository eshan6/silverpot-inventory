#!/usr/bin/env bash
# Apply the migrations to a throwaway Postgres and run the security checks.
#
# Needs a running Postgres 16 and nothing else - no Supabase account, no
# network, no credentials. The point is that the policies can be proved on a
# laptop and in CI, not only against the live project.
#
#     dashboard/supabase/tests/run.sh
#
# Each test file gets its **own** database. They were sharing one, which made
# them order-dependent: 01 leaves an active super admin behind, and 02's
# bootstrap check is specifically about what happens when none exists. A test
# whose result depends on which file ran first is not testing what it says.
#
# Override the connection with PGHOST / PGPORT / PGUSER if yours differs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PGHOST="${PGHOST:-/tmp}"
export PGPORT="${PGPORT:-5433}"
export PGUSER="${PGUSER:-postgres}"

for test_file in "$HERE"/0[1-9]_*.sql; do
    name="$(basename "$test_file" .sql)"
    db="silverpot_test_${name//[^a-z0-9_]/_}"

    psql -q -tAc "drop database if exists $db" postgres
    psql -q -tAc "create database $db" postgres

    # The auth schema and the anon/authenticated/service_role roles are
    # Supabase's; locally we stand them up so the same policies can be run.
    psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$HERE/00_local_auth_stub.sql"

    for m in "$HERE"/../migrations/*.sql; do
        psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$m"
    done

    psql -q -v ON_ERROR_STOP=1 -d "$db" -c "create schema tests"
    # The assertion helpers are called while acting as authenticated or
    # service_role, so those roles need to reach them.
    psql -q -v ON_ERROR_STOP=1 -d "$db" \
        -c "grant usage on schema tests to anon, authenticated, service_role"

    psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$HERE/_helpers.sql"
    psql -q -v ON_ERROR_STOP=1 -d "$db" <<'GRANTS'
grant select, insert, update, delete on all tables in schema public to authenticated;
grant usage, select on all sequences in schema public to authenticated;
grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;
GRANTS

    psql -v ON_ERROR_STOP=1 -d "$db" -f "$test_file"
done
