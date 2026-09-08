#!/usr/bin/env bash
# Apply the migration to a throwaway Postgres and run the RLS checks against it.
#
# Needs a running Postgres 16 and nothing else - no Supabase account, no
# network, no credentials. The point is that the policies can be proved on a
# laptop and in CI, not only against the live project.
#
#     dashboard/supabase/tests/run.sh
#
# Override the connection with PGHOST / PGPORT / PGUSER if yours differs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${DB:-silverpot_rls_test}"
export PGHOST="${PGHOST:-/tmp}"
export PGPORT="${PGPORT:-5433}"
export PGUSER="${PGUSER:-postgres}"

psql -q -tAc "drop database if exists $DB" postgres
psql -q -tAc "create database $DB" postgres

# The auth schema and the anon/authenticated/service_role roles are Supabase's;
# locally we stand them up ourselves so the same policies can be exercised.
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f "$HERE/00_local_auth_stub.sql"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f "$HERE/../migrations/0001_init.sql"

psql -q -v ON_ERROR_STOP=1 -d "$DB" -c "create schema tests"
# The assertion helpers are called while acting as authenticated/service_role,
# so those roles need to reach them.
psql -q -v ON_ERROR_STOP=1 -d "$DB" \
    -c "grant usage on schema tests to anon, authenticated, service_role"
psql -v ON_ERROR_STOP=1 -d "$DB" -f "$HERE/01_rls_test.sql"
