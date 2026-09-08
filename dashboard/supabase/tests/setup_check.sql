-- What the single paste is supposed to leave behind.
--
-- Run against a database built from setup.sql rather than from the migrations,
-- so this proves the file the project owner actually pastes - including its
-- bootstrap block, which no migration contains.

\set ON_ERROR_STOP on

reset role;

select tests.ok(
    (select value #>> '{}' from public.app_settings
     where key = 'bootstrap_super_admin_email') = 'eshan@dcgnorthamerica.com',
    'the bootstrap address is recorded, lower-cased');

select tests.ok(
    (select count(*) from public.profiles) = 0,
    'setup creates no users: the first one arrives by signing up');

-- The whole point of the paste: sign up, and be in charge, with no second step.
insert into auth.users (id, email)
values ('bbbbbbbb-0000-0000-0000-000000000001', 'eshan@dcgnorthamerica.com');

select tests.ok(
    (select role from public.profiles
     where id = 'bbbbbbbb-0000-0000-0000-000000000001') = 'super_admin',
    'signing up as the bootstrap address gives super admin, with no follow-up SQL');

-- Every table the app reads must have RLS on. A table created without it is
-- readable by anyone holding the anon key, which is public by design.
select tests.ok(
    (select count(*) from pg_tables t
     join pg_class c on c.relname = t.tablename
     where t.schemaname = 'public' and not c.relrowsecurity) = 0,
    'every public table has row level security enabled');

select tests.ok(
    (select count(*) from pg_views where schemaname = 'public'
     and viewname = 'spend_daily') = 1,
    'the spend view exists, so the Spend page has something to read');

reset role;
\echo ''
\echo 'SETUP.SQL CHECKS PASSED'
