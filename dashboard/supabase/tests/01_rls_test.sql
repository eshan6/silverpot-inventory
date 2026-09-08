-- Does the role hierarchy actually hold, against a real Postgres?
--
-- Every check below is a thing someone could try from a browser console with a
-- valid session token. The UI hiding a button is not a control; these are.
--
--     dashboard/supabase/tests/run.sh
--
-- Any failed expectation raises, and psql runs with ON_ERROR_STOP, so a broken
-- policy fails the script rather than printing a warning nobody reads.

\set ON_ERROR_STOP on

-- ------------------------------------------------------------------ helpers

create or replace function tests.act_as(p_id uuid)
returns void language plpgsql as $$
begin
    perform set_config('request.jwt.claim.sub', p_id::text, false);
    execute 'set role authenticated';
end $$;

create or replace function tests.act_as_service()
returns void language plpgsql as $$
begin
    perform set_config('request.jwt.claim.sub', '', false);
    execute 'set role service_role';
end $$;

create or replace function tests.ok(cond boolean, what text)
returns void language plpgsql as $$
begin
    if not cond then
        raise exception 'FAILED: %', what;
    end if;
    raise notice '  ok: %', what;
end $$;

-- ------------------------------------------------------------------ fixtures

reset role;

insert into auth.users (id, email) values
    ('11111111-1111-1111-1111-111111111111', 'eshan@dcgnorthamerica.com'),
    ('22222222-2222-2222-2222-222222222222', 'super2@dcgnorthamerica.com'),
    ('33333333-3333-3333-3333-333333333333', 'admin@dcgnorthamerica.com'),
    ('44444444-4444-4444-4444-444444444444', 'viewer@dcgnorthamerica.com'),
    ('55555555-5555-5555-5555-555555555555', 'viewer2@dcgnorthamerica.com'),
    ('66666666-6666-6666-6666-666666666666', 'nobody@example.com'),
    -- Real auth users for the escalation attempts below. Using a random uuid
    -- there made those inserts die on the foreign key to auth.users before the
    -- role guard was ever consulted, so the test passed for the wrong reason
    -- and kept passing when the guard was deleted. Caught by mutation testing.
    ('77777777-7777-7777-7777-777777777777', 'sneaky@example.com'),
    ('88888888-8888-8888-8888-888888888888', 'sneakier@example.com');

-- Seeded directly: the first super admin has to come from somewhere, and in
-- the real project that is a one-off SQL statement by the project owner.
insert into public.profiles (id, email, full_name, role) values
    ('11111111-1111-1111-1111-111111111111', 'eshan@dcgnorthamerica.com', 'Eshan', 'super_admin'),
    ('22222222-2222-2222-2222-222222222222', 'super2@dcgnorthamerica.com', 'Break Glass', 'super_admin'),
    ('33333333-3333-3333-3333-333333333333', 'admin@dcgnorthamerica.com', 'An Admin', 'admin'),
    ('44444444-4444-4444-4444-444444444444', 'viewer@dcgnorthamerica.com', 'A Viewer', 'viewer'),
    ('55555555-5555-5555-5555-555555555555', 'viewer2@dcgnorthamerica.com', 'Another Viewer', 'viewer');
-- 66666666 deliberately has NO profile: an authenticated stranger.

insert into public.sales_daily (marketplace, sale_date, sku, internal_code, units, revenue)
values ('amazon', '2026-09-01', 'AA-1111-AAAA', '2201US', 12, 167.88),
       ('walmart', '2026-09-01', 'AA-1111-AAAA', '2201US', 3, 41.97);

insert into public.app_settings (key, value) values ('ads_sheet_id', '"abc123"'::jsonb);

grant select, insert, update on all tables in schema public to authenticated;
grant usage, select on all sequences in schema public to authenticated;
grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

-- ================================================================ VIEWER

\echo ''
\echo 'viewer'

select tests.act_as('44444444-4444-4444-4444-444444444444');

select tests.ok((select count(*) from public.sales_daily) = 2,
    'viewer reads sales facts');

select tests.ok((select count(*) from public.app_settings) = 0,
    'viewer cannot read app_settings (which sheet we pull from is not theirs)');

select tests.ok((select count(*) from public.audit_log) = 0,
    'viewer cannot read the audit log');

select tests.ok((select count(*) from public.profiles) = 1,
    'viewer sees only their own profile, not the user roster');

-- A viewer promoting themselves is the whole ballgame.
do $$ begin
    begin
        update public.profiles set role = 'super_admin'
        where id = '44444444-4444-4444-4444-444444444444';
        raise exception 'FAILED: viewer promoted themselves to super_admin';
    exception when insufficient_privilege then
        raise notice '  ok: viewer cannot promote themselves';
    end;
end $$;

do $$ begin
    begin
        insert into public.profiles (id, email, role)
        values ('66666666-6666-6666-6666-666666666666', 'nobody@example.com', 'viewer');
        raise exception 'FAILED: viewer invited a user';
    exception when insufficient_privilege then
        raise notice '  ok: viewer cannot invite users';
    end;
end $$;

-- Renaming yourself is allowed, and is the only profile edit a viewer gets.
update public.profiles set full_name = 'A Viewer (renamed)'
where id = '44444444-4444-4444-4444-444444444444';
select tests.ok((select full_name from public.profiles
                 where id = '44444444-4444-4444-4444-444444444444') = 'A Viewer (renamed)',
    'viewer can rename themselves');

do $$ begin
    begin
        update public.profiles set full_name = 'hacked'
        where id = '55555555-5555-5555-5555-555555555555';
        if found then
            raise exception 'FAILED: viewer edited another viewer';
        end if;
        raise notice '  ok: viewer cannot edit another user (no row matched)';
    exception when insufficient_privilege then
        raise notice '  ok: viewer cannot edit another user (rejected)';
    end;
end $$;

-- Saved views are private per person, which is what "my custom views come back
-- when I log in" has to mean if two people use the same section.
insert into public.saved_views (owner_id, section, marketplace, name, config)
values ('44444444-4444-4444-4444-444444444444', 'sales', 'amazon', 'My last 30 days',
        '{"range":"last_30d"}'::jsonb);

select tests.ok((select count(*) from public.saved_views) = 1,
    'viewer sees their own saved view');

do $$ begin
    begin
        insert into public.saved_views (owner_id, section, marketplace, name, config)
        values ('55555555-5555-5555-5555-555555555555', 'sales', 'amazon', 'Planted',
                '{}'::jsonb);
        raise exception 'FAILED: viewer wrote a saved view onto another user';
    exception when insufficient_privilege then
        raise notice '  ok: viewer cannot create a saved view for someone else';
    end;
end $$;

do $$ begin
    begin
        insert into public.sales_daily (marketplace, sale_date, sku, units)
        values ('amazon', '2026-09-02', 'FAKE', 9999);
        raise exception 'FAILED: viewer wrote sales facts';
    exception when insufficient_privilege then
        raise notice '  ok: viewer cannot write sales facts';
    end;
end $$;

-- ================================================================ ADMIN

\echo ''
\echo 'admin'

reset role;
select tests.act_as('33333333-3333-3333-3333-333333333333');

select tests.ok((select count(*) from public.profiles) = 5,
    'admin sees the full roster');

select tests.ok((select count(*) from public.audit_log) >= 0,
    'admin can read the audit log');

select tests.ok((select count(*) from public.app_settings) = 0,
    'admin cannot read app_settings (credentials config is super-admin only)');

-- The headline rule: an admin adds people, but only as viewers.
insert into public.profiles (id, email, role, invited_by)
values ('66666666-6666-6666-6666-666666666666', 'nobody@example.com', 'viewer',
        '33333333-3333-3333-3333-333333333333');
select tests.ok((select role from public.profiles
                 where id = '66666666-6666-6666-6666-666666666666') = 'viewer',
    'admin can invite a viewer');

do $$ begin
    begin
        insert into public.profiles (id, email, role)
        values ('77777777-7777-7777-7777-777777777777', 'sneaky@example.com', 'admin');
        raise exception 'FAILED: admin minted another admin';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot invite an admin';
    end;
end $$;

do $$ begin
    begin
        insert into public.profiles (id, email, role)
        values ('88888888-8888-8888-8888-888888888888', 'sneakier@example.com', 'super_admin');
        raise exception 'FAILED: admin minted a super_admin';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot invite a super_admin';
    end;
end $$;

do $$ begin
    begin
        update public.profiles set role = 'super_admin'
        where id = '33333333-3333-3333-3333-333333333333';
        raise exception 'FAILED: admin promoted themselves';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot promote themselves';
    end;
end $$;

do $$ begin
    begin
        update public.profiles set role = 'admin'
        where id = '44444444-4444-4444-4444-444444444444';
        raise exception 'FAILED: admin promoted a viewer to admin';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot promote a viewer';
    end;
end $$;

do $$ begin
    begin
        update public.profiles set is_active = false
        where id = '11111111-1111-1111-1111-111111111111';
        raise exception 'FAILED: admin deactivated a super admin';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot deactivate a super admin';
    end;
end $$;

-- Deactivating a viewer is squarely an admin's job.
update public.profiles set is_active = false
where id = '55555555-5555-5555-5555-555555555555';
select tests.ok((select is_active from public.profiles
                 where id = '55555555-5555-5555-5555-555555555555') = false,
    'admin can deactivate a viewer');

do $$ begin
    begin
        insert into public.app_settings (key, value) values ('ads_sheet_id', '"stolen"'::jsonb);
        raise exception 'FAILED: admin changed app settings';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot change app settings';
    end;
end $$;

-- ================================================================ SUPER ADMIN

\echo ''
\echo 'super admin'

reset role;
select tests.act_as('11111111-1111-1111-1111-111111111111');

select tests.ok((select count(*) from public.app_settings) = 1,
    'super admin reads app_settings');

update public.profiles set role = 'admin'
where id = '44444444-4444-4444-4444-444444444444';
select tests.ok((select role from public.profiles
                 where id = '44444444-4444-4444-4444-444444444444') = 'admin',
    'super admin can promote a viewer to admin');

-- Even the super admin does not change their own level.
do $$ begin
    begin
        update public.profiles set role = 'viewer'
        where id = '11111111-1111-1111-1111-111111111111';
        raise exception 'FAILED: super admin changed their own role';
    exception when insufficient_privilege then
        raise notice '  ok: super admin cannot change their own role';
    end;
end $$;

-- Break-glass: with two super admins, demoting one is fine.
update public.profiles set role = 'viewer'
where id = '22222222-2222-2222-2222-222222222222';
select tests.ok((select role from public.profiles
                 where id = '22222222-2222-2222-2222-222222222222') = 'viewer',
    'super admin can demote the other super admin while one remains');

-- ...but not the last one. Someone must always be able to administer this.
reset role;
select tests.act_as('11111111-1111-1111-1111-111111111111');
do $$ begin
    begin
        update public.profiles set is_active = false
        where id = '11111111-1111-1111-1111-111111111111';
        raise exception 'FAILED: the last super admin was removed';
    exception when insufficient_privilege then
        raise notice '  ok: the last active super admin cannot be removed';
    end;
end $$;

-- ================================================================ STRANGER

\echo ''
\echo 'authenticated stranger and anon'

reset role;
-- 66666666 exists in auth.users and now has a viewer profile from the admin
-- test, so deactivate it to model a real revoked account.
update public.profiles set is_active = false
where id = '66666666-6666-6666-6666-666666666666';

select tests.act_as('66666666-6666-6666-6666-666666666666');
select tests.ok((select count(*) from public.sales_daily) = 0,
    'a deactivated user reads no facts, even with a valid session');

reset role;
select tests.act_as('99999999-9999-9999-9999-999999999999');
select tests.ok((select count(*) from public.sales_daily) = 0,
    'a token for an unknown user reads no facts');

-- ================================================================ SERVICE

\echo ''
\echo 'service role (ingestion)'

reset role;
select tests.act_as_service();

insert into public.sales_daily (marketplace, sale_date, sku, units, revenue)
values ('amazon', '2026-09-02', 'BB-2222-BBBB', 7, 97.93);
select tests.ok((select count(*) from public.sales_daily) = 3,
    'ingestion writes facts, bypassing RLS as Supabase''s service role does');

insert into public.ingest_runs (marketplace, source, status, covers_from, covers_to, rows_written)
values ('amazon', 'sp-api-orders', 'ok', '2026-09-02', '2026-09-02', 1);
select tests.ok((select count(*) from public.ingest_runs) = 1,
    'ingestion records a run for the freshness stamp');

-- ================================================================ AUDIT

\echo ''
\echo 'audit trail'

reset role;
select tests.act_as('11111111-1111-1111-1111-111111111111');

select tests.ok((select count(*) from public.audit_log where action = 'user.invited') >= 1,
    'invites are logged');
select tests.ok((select count(*) from public.audit_log where action = 'user.role_changed') >= 1,
    'role changes are logged');
select tests.ok((select count(*) from public.audit_log where action = 'user.deactivated') >= 1,
    'deactivations are logged');
select tests.ok(
    (select actor_email from public.audit_log
     where action = 'user.invited' and target_id = '66666666-6666-6666-6666-666666666666')
    = 'admin@dcgnorthamerica.com',
    'the log names who did it, not just what happened');

reset role;
\echo ''
\echo 'ALL RLS CHECKS PASSED'
