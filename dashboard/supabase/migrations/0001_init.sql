-- Silverpot marketplace dashboard: schema, roles and row-level security.
--
-- Every rule that matters is enforced here, in Postgres, rather than in the
-- React app. A viewer who opens devtools and calls the REST API directly must
-- get the same answer as a viewer clicking around the UI, and the only way to
-- guarantee that is to make the database the authority.
--
-- Ingestion runs as the service role, which bypasses RLS by design. Nothing in
-- the browser ever holds a service key or a marketplace credential.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- enums

create type app_role as enum ('viewer', 'admin', 'super_admin');

-- Marketplaces are an enum, and every fact table carries one. Amazon is the
-- only ingestion built today; Walmart orders are reachable with credentials
-- the inventory collector already holds, and Walmart Connect (their ads) is a
-- separate approval we have deliberately postponed. Carrying the column now
-- costs nothing and keeps "never show the two combined" enforceable in the
-- query layer instead of relying on the UI to remember.
create type marketplace as enum ('amazon', 'walmart');

-- ---------------------------------------------------------------- profiles

create table public.profiles (
    id          uuid primary key references auth.users on delete cascade,
    email       text        not null unique,
    full_name   text,
    role        app_role    not null default 'viewer',
    is_active   boolean     not null default true,
    invited_by  uuid        references public.profiles (id),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

comment on table public.profiles is
    'One row per human. Deactivate rather than delete, so the audit trail keeps '
    'pointing at a real name.';

-- ---------------------------------------------------------------- helpers
--
-- SECURITY DEFINER so they can read profiles without tripping the very
-- policies that call them. A policy on profiles that itself selects from
-- profiles recurses forever otherwise.

create or replace function public.auth_role()
returns app_role
language sql
stable
security definer
set search_path = public
as $$
    select p.role
    from public.profiles p
    where p.id = auth.uid()
      and p.is_active
$$;

create or replace function public.is_active_user()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from public.profiles p
        where p.id = auth.uid() and p.is_active
    )
$$;

-- Admin OR super admin. Spelled once so a later edit cannot make the two
-- checks disagree.
create or replace function public.is_staff()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select public.auth_role() in ('admin', 'super_admin')
$$;

create or replace function public.is_super()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select public.auth_role() = 'super_admin'
$$;

-- ---------------------------------------------------------------- audit log

create table public.audit_log (
    id          bigserial primary key,
    actor_id    uuid references public.profiles (id),
    actor_email text,
    action      text        not null,
    target_type text,
    target_id   text,
    detail      jsonb       not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

create index audit_log_created_at_idx on public.audit_log (created_at desc);

comment on table public.audit_log is
    'Append-only. Written by triggers and by the server, never by the browser. '
    'With a super-admin model this is what you read when something looks wrong.';

-- ---------------------------------------------------------------- saved views

create table public.saved_views (
    id          uuid primary key default gen_random_uuid(),
    owner_id    uuid        not null references public.profiles (id) on delete cascade,
    section     text        not null check (section in ('sales', 'ads', 'spend')),
    marketplace marketplace not null default 'amazon',
    name        text        not null,
    config      jsonb       not null,
    is_default  boolean     not null default false,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (owner_id, section, marketplace, name)
);

-- At most one default per person per section per marketplace.
create unique index saved_views_one_default_idx
    on public.saved_views (owner_id, section, marketplace)
    where is_default;

-- ---------------------------------------------------------------- settings

create table public.app_settings (
    key        text primary key,
    value      jsonb       not null,
    updated_by uuid references public.profiles (id),
    updated_at timestamptz not null default now()
);

comment on table public.app_settings is
    'Super-admin only: which Google Sheet the ads export is read from, and the '
    'like. Never secrets - credentials live in GitHub Actions secrets and are '
    'read only by the ingestion job, never by anything the browser can reach.';

-- ---------------------------------------------------------------- ingestion

create table public.ingest_runs (
    id           bigserial primary key,
    marketplace  marketplace not null,
    source       text        not null,
    status       text        not null check (status in ('running', 'ok', 'failed')),
    covers_from  date,
    covers_to    date,
    rows_written integer     not null default 0,
    error        text,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz
);

create index ingest_runs_recent_idx
    on public.ingest_runs (marketplace, source, started_at desc);

comment on table public.ingest_runs is
    'Freshness. Every page stamps "as of" from here, and a range with no '
    'successful run says so rather than rendering zeros - showing 0 units when '
    'ingestion failed is the plausible-but-wrong failure this project already '
    'shipped once.';

-- ---------------------------------------------------------------- facts

create table public.sales_daily (
    marketplace   marketplace not null,
    sale_date     date        not null,
    sku           text        not null,
    internal_code text,
    asin          text,
    product_name  text,
    units         integer     not null default 0,
    orders        integer     not null default 0,
    revenue       numeric(12, 2) not null default 0,
    updated_at    timestamptz not null default now(),
    primary key (marketplace, sale_date, sku)
);

create index sales_daily_date_idx on public.sales_daily (marketplace, sale_date);
create index sales_daily_code_idx on public.sales_daily (marketplace, internal_code);

create table public.ads_daily (
    marketplace      marketplace not null,
    ad_date          date        not null,
    campaign_id      text        not null,
    campaign_name    text,
    ad_group         text        not null default '',
    sku              text        not null default '',
    asin             text,
    impressions      bigint      not null default 0,
    clicks           bigint      not null default 0,
    spend            numeric(12, 2) not null default 0,
    attributed_sales numeric(12, 2) not null default 0,
    attributed_units integer     not null default 0,
    updated_at       timestamptz not null default now(),
    primary key (marketplace, ad_date, campaign_id, ad_group, sku)
);

create index ads_daily_date_idx on public.ads_daily (marketplace, ad_date);

create table public.ads_search_terms (
    marketplace      marketplace not null,
    ad_date          date        not null,
    campaign_id      text        not null,
    search_term      text        not null,
    match_type       text        not null default '',
    impressions      bigint      not null default 0,
    clicks           bigint      not null default 0,
    spend            numeric(12, 2) not null default 0,
    attributed_sales numeric(12, 2) not null default 0,
    attributed_units integer     not null default 0,
    updated_at       timestamptz not null default now(),
    primary key (marketplace, ad_date, campaign_id, search_term, match_type)
);

create index ads_search_terms_date_idx on public.ads_search_terms (marketplace, ad_date);

-- CPC and cost-per-unit are derived, never stored: a stored ratio goes stale
-- the moment either side is corrected by a re-ingest.

-- ---------------------------------------------------------------- guards
--
-- The role hierarchy is enforced by trigger rather than by policy alone,
-- because an UPDATE policy's WITH CHECK sees only the resulting row. Telling
-- "an admin edited a viewer's name" from "an admin promoted themselves"
-- requires comparing OLD with NEW, which only a trigger can do.

create or replace function public.enforce_profile_rules()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    actor      app_role := public.auth_role();
    supers_left integer;
begin
    -- The service role has no profile row, so auth_role() is null. Ingestion
    -- and migrations must not be second-guessed by these rules.
    if actor is null then
        return new;
    end if;

    if tg_op = 'INSERT' then
        -- An admin may add people, but only as viewers. Without this, "can add
        -- users" quietly becomes "can mint themselves a second super admin".
        if actor = 'admin' and new.role <> 'viewer' then
            raise exception 'admins may only invite viewers (attempted role: %)', new.role
                using errcode = 'insufficient_privilege';
        end if;
        if actor = 'viewer' then
            raise exception 'viewers may not invite users'
                using errcode = 'insufficient_privilege';
        end if;
        return new;
    end if;

    -- UPDATE from here.

    -- Nobody edits their own role, super admins included. Changing your own
    -- level is always someone else's decision.
    if new.role is distinct from old.role and new.id = auth.uid() then
        raise exception 'you cannot change your own role'
            using errcode = 'insufficient_privilege';
    end if;

    if actor = 'admin' then
        if old.role <> 'viewer' then
            raise exception 'admins may only modify viewers'
                using errcode = 'insufficient_privilege';
        end if;
        if new.role is distinct from old.role then
            raise exception 'admins may not change roles'
                using errcode = 'insufficient_privilege';
        end if;
    elsif actor = 'viewer' then
        -- A viewer may edit their own display name and nothing else.
        if new.id <> auth.uid()
           or new.role is distinct from old.role
           or new.is_active is distinct from old.is_active
           or new.email is distinct from old.email then
            raise exception 'viewers may only edit their own name'
                using errcode = 'insufficient_privilege';
        end if;
    end if;

    -- Never strand the account with nobody able to administer it. A second
    -- super admin is the break-glass; this stops the first one being removed
    -- before the second exists.
    if old.role = 'super_admin'
       and (new.role <> 'super_admin' or new.is_active = false) then
        select count(*) into supers_left
        from public.profiles
        where role = 'super_admin' and is_active and id <> old.id;

        if supers_left = 0 then
            raise exception 'refusing to remove the last active super admin'
                using errcode = 'insufficient_privilege';
        end if;
    end if;

    new.updated_at := now();
    return new;
end;
$$;

create trigger profiles_enforce_rules
    before insert or update on public.profiles
    for each row execute function public.enforce_profile_rules();

-- Every privileged change writes itself down. Doing this in a trigger rather
-- than in application code means a change made through any route is logged.
create or replace function public.log_profile_change()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    actor_mail text;
begin
    select email into actor_mail from public.profiles where id = auth.uid();

    if tg_op = 'INSERT' then
        insert into public.audit_log (actor_id, actor_email, action, target_type, target_id, detail)
        values (auth.uid(), actor_mail, 'user.invited', 'profile', new.id::text,
                jsonb_build_object('email', new.email, 'role', new.role));
    elsif new.role is distinct from old.role then
        insert into public.audit_log (actor_id, actor_email, action, target_type, target_id, detail)
        values (auth.uid(), actor_mail, 'user.role_changed', 'profile', new.id::text,
                jsonb_build_object('email', new.email, 'from', old.role, 'to', new.role));
    elsif new.is_active is distinct from old.is_active then
        insert into public.audit_log (actor_id, actor_email, action, target_type, target_id, detail)
        values (auth.uid(), actor_mail,
                case when new.is_active then 'user.reactivated' else 'user.deactivated' end,
                'profile', new.id::text, jsonb_build_object('email', new.email));
    end if;
    return new;
end;
$$;

create trigger profiles_audit
    after insert or update on public.profiles
    for each row execute function public.log_profile_change();

create or replace function public.log_setting_change()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    actor_mail text;
begin
    select email into actor_mail from public.profiles where id = auth.uid();
    insert into public.audit_log (actor_id, actor_email, action, target_type, target_id, detail)
    values (auth.uid(), actor_mail, 'setting.changed', 'app_settings', new.key,
            jsonb_build_object('value', new.value));
    return new;
end;
$$;

create trigger app_settings_audit
    after insert or update on public.app_settings
    for each row execute function public.log_setting_change();

-- ---------------------------------------------------------------- RLS

alter table public.profiles         enable row level security;
alter table public.audit_log        enable row level security;
alter table public.saved_views      enable row level security;
alter table public.app_settings     enable row level security;
alter table public.ingest_runs      enable row level security;
alter table public.sales_daily      enable row level security;
alter table public.ads_daily        enable row level security;
alter table public.ads_search_terms enable row level security;

-- profiles: you always see yourself; staff see the roster.
create policy profiles_select_self on public.profiles
    for select using (id = auth.uid() or public.is_staff());

create policy profiles_insert_staff on public.profiles
    for insert with check (public.is_staff());

create policy profiles_update on public.profiles
    for update using (public.is_staff() or id = auth.uid())
    with check (public.is_staff() or id = auth.uid());

-- No delete policy at all: deactivation is the only route out.

-- audit_log: staff read, nobody writes from a browser session.
create policy audit_select_staff on public.audit_log
    for select using (public.is_staff());

-- saved_views: entirely private to their owner.
create policy saved_views_owner on public.saved_views
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- app_settings: super admin only, read included. Which sheet the numbers come
-- from is not a viewer's business.
create policy settings_super on public.app_settings
    for all using (public.is_super()) with check (public.is_super());

-- Facts and freshness: any active user may read. Writes have no policy, so
-- only the service role (which bypasses RLS) can perform them.
create policy ingest_runs_read on public.ingest_runs
    for select using (public.is_active_user());

create policy sales_read on public.sales_daily
    for select using (public.is_active_user());

create policy ads_read on public.ads_daily
    for select using (public.is_active_user());

create policy search_terms_read on public.ads_search_terms
    for select using (public.is_active_user());

-- ---------------------------------------------------------------- new signups
--
-- Invite-only. A Supabase auth user with no profile row can authenticate but
-- sees nothing, because every policy above keys off an active profile. Profiles
-- are created by a super admin or an admin, never by the person signing up.

revoke all on public.profiles from anon;
revoke all on public.app_settings from anon;
revoke all on public.audit_log from anon;
