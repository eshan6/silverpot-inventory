-- The invite flow, and the first super admin.
--
-- This is a second route into the system, so it needs the same escalation
-- checks the profiles table already has. An admin who could record an invite
-- for role 'super_admin' would simply be waiting one signup to own the
-- account, which is the profile guard defeated by going around it.

\set ON_ERROR_STOP on

\echo ''
\echo 'invites: bootstrap'

reset role;

-- Nobody exists yet. This is the real starting condition: an empty project
-- where someone has to become the first administrator without a hand-run SQL
-- statement leaving a window with no one in charge.
insert into public.app_settings (key, value)
values ('bootstrap_super_admin_email', '"eshan@dcgnorthamerica.com"');

insert into auth.users (id, email)
values ('aaaaaaaa-0000-0000-0000-000000000001', 'eshan@dcgnorthamerica.com');

select tests.ok(
    (select role from public.profiles
     where id = 'aaaaaaaa-0000-0000-0000-000000000001') = 'super_admin',
    'the bootstrap address becomes super admin on signup');

-- A stranger signing up is not refused - refusing would reveal which
-- addresses are known - but gets nothing.
insert into auth.users (id, email)
values ('aaaaaaaa-0000-0000-0000-000000000002', 'stranger@example.com');

select tests.ok(
    (select count(*) from public.profiles
     where id = 'aaaaaaaa-0000-0000-0000-000000000002') = 0,
    'an uninvited signup gets no profile, so no access');

-- The bootstrap is a way in, not a permanent back door.
insert into auth.users (id, email)
values ('aaaaaaaa-0000-0000-0000-000000000003', 'eshan@dcgnorthamerica.com');

select tests.ok(
    (select count(*) from public.profiles where role = 'super_admin') = 1,
    'the bootstrap is inert once an active super admin exists');

\echo ''
\echo 'invites: who may invite whom'

select tests.act_as('aaaaaaaa-0000-0000-0000-000000000001');   -- super admin

insert into public.invites (email, role) values ('newadmin@dcgnorthamerica.com', 'admin');
select tests.ok((select role from public.invites
                 where email = 'newadmin@dcgnorthamerica.com') = 'admin',
    'a super admin can invite an admin');

insert into public.invites (email, role) values ('Watcher@DCGnorthamerica.com', 'viewer');
select tests.ok((select count(*) from public.invites
                 where email = 'watcher@dcgnorthamerica.com') = 1,
    'invited addresses are lower-cased, so case cannot create a duplicate');

reset role;
insert into auth.users (id, email)
values ('aaaaaaaa-0000-0000-0000-000000000004', 'newadmin@dcgnorthamerica.com');

select tests.ok(
    (select role from public.profiles
     where id = 'aaaaaaaa-0000-0000-0000-000000000004') = 'admin',
    'signing up against an invite provisions the invited role');

select tests.ok(
    (select claimed_at is not null from public.invites
     where email = 'newadmin@dcgnorthamerica.com'),
    'the invite is marked claimed, so it cannot be reused');

\echo ''
\echo 'invites: an admin cannot escalate through them'

select tests.act_as('aaaaaaaa-0000-0000-0000-000000000004');   -- the new admin

insert into public.invites (email, role) values ('viewer1@dcgnorthamerica.com', 'viewer');
select tests.ok((select count(*) from public.invites
                 where email = 'viewer1@dcgnorthamerica.com') = 1,
    'an admin can invite a viewer');

-- The whole point of this file. Without the invite trigger, an admin could
-- invite themselves a second identity as super admin and sign up as it.
do $$ begin
    begin
        insert into public.invites (email, role)
        values ('sneaky2@example.com', 'admin');
        raise exception 'FAILED: admin invited an admin';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot invite an admin';
    end;
end $$;

do $$ begin
    begin
        insert into public.invites (email, role)
        values ('sneaky3@example.com', 'super_admin');
        raise exception 'FAILED: admin invited a super admin';
    exception when insufficient_privilege then
        raise notice '  ok: admin cannot invite a super admin';
    end;
end $$;

\echo ''
\echo 'invites: viewers'

reset role;
insert into auth.users (id, email)
values ('aaaaaaaa-0000-0000-0000-000000000005', 'viewer1@dcgnorthamerica.com');
select tests.act_as('aaaaaaaa-0000-0000-0000-000000000005');

select tests.ok((select count(*) from public.invites) = 0,
    'a viewer cannot see the invite list');

do $$ begin
    begin
        insert into public.invites (email, role)
        values ('friend@example.com', 'viewer');
        raise exception 'FAILED: viewer created an invite';
    exception when insufficient_privilege then
        raise notice '  ok: viewer cannot invite anyone';
    end;
end $$;

\echo ''
\echo 'invites: the trail'

reset role;
select tests.act_as('aaaaaaaa-0000-0000-0000-000000000001');

select tests.ok(
    (select count(*) from public.audit_log where action = 'invite.created') >= 3,
    'invites are logged when created');
select tests.ok(
    (select count(*) from public.audit_log where action = 'invite.claimed') >= 1,
    'claiming an invite is logged');
select tests.ok(
    (select count(*) from public.audit_log where action = 'bootstrap.claimed') = 1,
    'the bootstrap claim is logged, since it is the one unprompted promotion');

reset role;
\echo ''
\echo 'ALL INVITE CHECKS PASSED'
