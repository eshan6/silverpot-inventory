-- Invites, and the first super admin, without needing a server.
--
-- Two problems this solves.
--
-- **Creating users.** Supabase can only mint auth users with the service key,
-- and that key must never reach a browser. So an admin cannot "create a user"
-- from the dashboard directly. Instead they record an *invite*: an email and
-- the role it should get. When that person signs up themselves, a trigger
-- matches their address to the invite and provisions them. No server, no
-- service key in the client, and invite-only still holds - signing up without
-- an invite produces an auth user with no profile, which every policy in
-- 0001 treats as no access at all.
--
-- **The first super admin.** Someone has to be able to invite the others.
-- Rather than a hand-run SQL statement after signup - easy to get wrong, and
-- it leaves a window where the account has no administrator - one bootstrap
-- email is recorded in app_settings and claimed automatically, but only while
-- no active super admin exists. After that the bootstrap is inert.

create table public.invites (
    email       text primary key,
    role        app_role    not null default 'viewer',
    invited_by  uuid        references public.profiles (id),
    created_at  timestamptz not null default now(),
    claimed_at  timestamptz,
    claimed_by  uuid        references public.profiles (id)
);

comment on table public.invites is
    'Pending access. A row here means "this address may sign up, with this '
    'role". Rows are kept after being claimed so the audit trail shows who '
    'let whom in.';

create index invites_unclaimed_idx on public.invites (email) where claimed_at is null;

alter table public.invites enable row level security;

-- Staff see the invite list; viewers have no business knowing who is pending.
create policy invites_select_staff on public.invites
    for select using (public.is_staff());

create policy invites_insert_staff on public.invites
    for insert with check (public.is_staff());

create policy invites_delete_staff on public.invites
    for delete using (public.is_staff());

-- The same escalation rule as profiles, enforced the same way: an admin may
-- invite viewers and nothing else. Without this, "record an invite" would be
-- a back door around the profile trigger - invite yourself a second super
-- admin, sign up, done.
create or replace function public.enforce_invite_rules()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    actor app_role := public.auth_role();
begin
    if actor is null then
        return new;                       -- service role / migrations
    end if;
    if actor = 'viewer' then
        raise exception 'viewers may not invite users'
            using errcode = 'insufficient_privilege';
    end if;
    if actor = 'admin' and new.role <> 'viewer' then
        raise exception 'admins may only invite viewers (attempted role: %)', new.role
            using errcode = 'insufficient_privilege';
    end if;
    new.email := lower(trim(new.email));
    new.invited_by := coalesce(new.invited_by, auth.uid());
    return new;
end;
$$;

create trigger invites_enforce_rules
    before insert or update on public.invites
    for each row execute function public.enforce_invite_rules();

create or replace function public.log_invite()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    actor_mail text;
begin
    select email into actor_mail from public.profiles where id = auth.uid();
    insert into public.audit_log (actor_id, actor_email, action, target_type,
                                  target_id, detail)
    values (auth.uid(), actor_mail,
            case when tg_op = 'DELETE' then 'invite.revoked' else 'invite.created' end,
            'invite',
            case when tg_op = 'DELETE' then old.email else new.email end,
            jsonb_build_object('role',
                case when tg_op = 'DELETE' then old.role else new.role end));
    return case when tg_op = 'DELETE' then old else new end;
end;
$$;

create trigger invites_audit
    after insert or delete on public.invites
    for each row execute function public.log_invite();

-- ---------------------------------------------------------------- signup

-- Runs as each auth user is created. Three outcomes, in order:
--
--   1. the address matches an unclaimed invite  -> provision with that role
--   2. it is the bootstrap address and no active super admin exists
--                                               -> provision as super admin
--   3. anything else                            -> no profile, no access
--
-- Case 3 is the important one and is deliberately silent: an uninvited person
-- can complete a signup and still see nothing, because every policy keys off
-- an active profile. Refusing the signup outright would tell a stranger
-- whether an address is known.
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    addr        text := lower(trim(new.email));
    inv         public.invites%rowtype;
    bootstrap   text;
    have_super  boolean;
begin
    select * into inv from public.invites
    where email = addr and claimed_at is null;

    if found then
        insert into public.profiles (id, email, role, invited_by)
        values (new.id, addr, inv.role, inv.invited_by)
        on conflict (id) do nothing;

        update public.invites
        set claimed_at = now(), claimed_by = new.id
        where email = addr;

        insert into public.audit_log (actor_email, action, target_type,
                                      target_id, detail)
        values (addr, 'invite.claimed', 'profile', new.id::text,
                jsonb_build_object('role', inv.role));
        return new;
    end if;

    select value #>> '{}' into bootstrap
    from public.app_settings where key = 'bootstrap_super_admin_email';

    select exists (select 1 from public.profiles
                   where role = 'super_admin' and is_active)
    into have_super;

    if bootstrap is not null and addr = lower(trim(bootstrap)) and not have_super then
        insert into public.profiles (id, email, role)
        values (new.id, addr, 'super_admin')
        on conflict (id) do nothing;

        insert into public.audit_log (actor_email, action, target_type,
                                      target_id, detail)
        values (addr, 'bootstrap.claimed', 'profile', new.id::text,
                jsonb_build_object('role', 'super_admin'));
    end if;

    return new;
end;
$$;

-- Supabase owns auth.users, but a trigger on it is the supported way to react
-- to signups and is what makes invite-only work without a server.
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_auth_user();

grant select, insert, delete on public.invites to authenticated;
