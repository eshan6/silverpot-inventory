"""Build setup.sql: every migration, in order, as one paste.

Supabase's SQL editor takes one script at a time, and running four files in
the right order by hand is four chances to run them in the wrong one. This
concatenates them into a single file with one line to edit.

    python dashboard/supabase/build_setup.py

The result is checked in, and tests/test_setup_sql.py fails if it stops
matching the migrations - which is the only thing that makes a generated file
safe to trust. Adding a migration means running this again.

Nothing here uses psql backslash commands. The Supabase SQL editor is not
psql: it sends the text straight to Postgres, so `\\set` would be a syntax
error rather than a variable. The bootstrap address is therefore a plpgsql
local in a DO block, which both psql and the editor run identically.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE / "migrations"
TARGET = HERE / "setup.sql"

PLACEHOLDER = "you@example.com"

HEADER = """\
-- Silverpot dashboard: the whole schema, in one paste.
--
-- GENERATED FILE - do not edit. It is every file in
-- dashboard/supabase/migrations/ concatenated in order by build_setup.py,
-- and a test fails if the two drift apart. Edit the migrations, then rerun
-- that script.
--
-- ------------------------------------------------------------------ HOW TO
--
--   1. Scroll to the bottom and put your own email on the one marked line.
--   2. Paste this whole file into the Supabase SQL editor and run it.
--   3. Sign up in the dashboard with that same address. You become the first
--      super admin automatically - no follow-up SQL.
--
-- Step 3 works only for that address, and only while no active super admin
-- exists. Once one does, the bootstrap does nothing, so the row left behind
-- is not a way in for anyone later.
--
-- Running this file twice fails on the second run ("type app_role already
-- exists"). That is deliberate: a setup script that quietly re-runs over a
-- live database is how policies get reset without anyone noticing.
"""

FOOTER = f"""
-- ============================================================== BOOTSTRAP

-- Last, so that if anything above failed this is never reached and the
-- project is not left half-built with a claimable super admin slot.
do $bootstrap$
declare
    -- ------------------------------------------------------------------
    bootstrap_email text := '{PLACEHOLDER}';   -- <<< PUT YOUR EMAIL HERE
    -- ------------------------------------------------------------------
begin
    -- Matched on the domain, not on the placeholder string. Someone editing
    -- this with a find-and-replace across the whole file would rewrite both
    -- sides of a literal comparison and defeat the check. example.com is
    -- reserved by IANA and can never be a real mailbox, so this is exact.
    if bootstrap_email like '%@example.com' then
        raise exception
            'Put your own email on the marked line near the end of this file, '
            'then run it again. Left as it is, whoever signs up as that '
            'example address first would become super admin.';
    end if;

    insert into public.app_settings (key, value)
    values ('bootstrap_super_admin_email', to_jsonb(lower(trim(bootstrap_email))))
    on conflict (key) do update set value = excluded.value;

    raise notice 'Setup complete. Sign up as % to become super admin.',
        bootstrap_email;
end
$bootstrap$;
"""


def build() -> str:
    parts = [HEADER]
    for path in sorted(MIGRATIONS.glob("*.sql")):
        parts.append(
            f"\n\n-- ============================================== {path.name}\n\n")
        parts.append(path.read_text())
    parts.append(FOOTER)
    return "".join(parts)


if __name__ == "__main__":
    TARGET.write_text(build())
    print(f"Wrote {TARGET} ({len(TARGET.read_text().splitlines())} lines)")
