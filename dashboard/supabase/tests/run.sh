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

# ------------------------------------------------------- the paste-once file
#
# setup.sql is what actually gets run against the real project, once, by hand.
# Proving the migrations apply is not the same as proving that file applies:
# it adds a bootstrap block of its own, and a generated file that nobody
# executes before the one time it matters is a file nobody has tested.

echo ''
echo 'setup.sql: the single paste'

# Unedited, it must refuse: shipping with a live example address would hand
# the super admin slot to whoever signs up as it first. A separate database,
# because the refusal happens after the tables are created and re-running the
# file over them would fail for the uninteresting reason instead.
psql -q -tAc "drop database if exists silverpot_test_setup_raw" postgres
psql -q -tAc "create database silverpot_test_setup_raw" postgres
psql -q -v ON_ERROR_STOP=1 -d silverpot_test_setup_raw -f "$HERE/00_local_auth_stub.sql"

if psql -q -v ON_ERROR_STOP=1 -d silverpot_test_setup_raw \
        -f "$HERE/../setup.sql" 2>"$HERE/.setup_err"; then
    echo 'FAILED: setup.sql ran with the placeholder email still in it'
    exit 1
fi
if ! grep -q 'marked line' "$HERE/.setup_err"; then
    echo 'FAILED: setup.sql failed, but not on the bootstrap guard:'
    cat "$HERE/.setup_err"
    exit 1
fi
rm -f "$HERE/.setup_err"
echo '  ok: setup.sql refuses to run until the bootstrap email is set'

db=silverpot_test_setup
psql -q -tAc "drop database if exists $db" postgres
psql -q -tAc "create database $db" postgres
psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$HERE/00_local_auth_stub.sql"

sed 's/you@example\.com/eshan@dcgnorthamerica.com/g' "$HERE/../setup.sql" \
    > "$HERE/.setup_edited.sql"
psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$HERE/.setup_edited.sql"
rm -f "$HERE/.setup_edited.sql"

psql -q -v ON_ERROR_STOP=1 -d "$db" -c "create schema tests"
psql -q -v ON_ERROR_STOP=1 -d "$db" \
    -c "grant usage on schema tests to anon, authenticated, service_role"
psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$HERE/_helpers.sql"
psql -v ON_ERROR_STOP=1 -d "$db" -f "$HERE/setup_check.sql"
