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


-- ============================================== 0001_init.sql

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


-- ============================================== 0002_ad_programs.sql

-- Carry all three Amazon ad programs from the start.
--
-- Silverpot runs only Sponsored Products today. Sponsored Brands and Sponsored
-- Display are modelled anyway so that switching one on later is a settings
-- change and a new report request, never a migration and a rewrite of every
-- query. An empty program costs one enum value and nothing else.

create type ad_program as enum (
    'sponsored_products',
    'sponsored_brands',
    'sponsored_display'
);

-- Program-specific metrics live in `extra` rather than in columns, because the
-- three programs do not report the same things: Sponsored Display has viewable
-- impressions and Sponsored Brands has video quartiles, neither of which means
-- anything for Sponsored Products. Widening the table for every metric one
-- program has would leave most columns null most of the time, and would need a
-- migration the first time Amazon adds a metric. The five columns below are
-- the ones all three genuinely share, so they stay first-class and queryable.

alter table public.ads_daily
    add column ad_program ad_program not null default 'sponsored_products',
    add column extra jsonb not null default '{}'::jsonb;

alter table public.ads_search_terms
    add column ad_program ad_program not null default 'sponsored_products',
    add column extra jsonb not null default '{}'::jsonb;

-- The program is part of a row's identity: the same campaign id could appear
-- under two programs, and summing them without distinction would double-count
-- spend, which is the numerator of the one metric this dashboard exists for.
alter table public.ads_daily drop constraint ads_daily_pkey;
alter table public.ads_daily add primary key
    (marketplace, ad_program, ad_date, campaign_id, ad_group, sku);

alter table public.ads_search_terms drop constraint ads_search_terms_pkey;
alter table public.ads_search_terms add primary key
    (marketplace, ad_program, ad_date, campaign_id, search_term, match_type);

-- Sponsored Display targets audiences and products rather than search queries,
-- so it will not populate ads_search_terms in practice. That is left as a fact
-- about the data rather than a check constraint: Amazon has changed what each
-- program reports before, and a constraint encoding today's behaviour is a
-- migration waiting to happen.

create index ads_daily_program_idx
    on public.ads_daily (marketplace, ad_program, ad_date);

-- ---------------------------------------------------------------- spend
--
-- The number this dashboard exists to answer: what did we pay, in advertising,
-- for each unit we actually sold - not each unit advertising takes credit for.
--
-- Two things make that easy to get wrong, so the view does them once here
-- rather than in whichever page happens to ask.
--
-- Spend is summed across every ad program. If Sponsored Brands is running and
-- only Sponsored Products is counted, the numerator is short while the
-- denominator is complete, and cost per unit reads better than reality - the
-- exact direction that would let overspending look fine.
--
-- Units come from sales_daily, which is every unit sold from the Orders API,
-- not attributed_units. Ad-attributed units answer a different question.

create or replace view public.spend_daily as
select
    coalesce(s.marketplace, a.marketplace)   as marketplace,
    coalesce(s.sale_date, a.ad_date)         as day,
    coalesce(a.spend, 0)                     as ad_spend,
    coalesce(s.units, 0)                     as units_sold,
    coalesce(s.revenue, 0)                   as revenue,
    coalesce(a.attributed_sales, 0)          as attributed_sales,
    coalesce(a.attributed_units, 0)          as attributed_units,
    -- Null rather than zero on a day with no sales: dividing by nothing has no
    -- answer, and 0 would plot as a perfect day.
    case when coalesce(s.units, 0) > 0
         then round(coalesce(a.spend, 0) / s.units, 2) end as cost_per_unit,
    -- TACoS. $4 on a $13.99 tin is 29% of revenue; $4 on a $21.99 pouch is
    -- 18%. The ratio and the percentage say different things and the Spend
    -- page shows both.
    case when coalesce(s.revenue, 0) > 0
         then round(100 * coalesce(a.spend, 0) / s.revenue, 2) end as ad_pct_of_revenue
from (
    select marketplace, sale_date, sum(units) units, sum(revenue) revenue
    from public.sales_daily group by 1, 2
) s
full outer join (
    select marketplace, ad_date, sum(spend) spend,
           sum(attributed_sales) attributed_sales,
           sum(attributed_units) attributed_units
    from public.ads_daily group by 1, 2
) a on a.marketplace = s.marketplace and a.ad_date = s.sale_date;

comment on view public.spend_daily is
    'Ad spend over units actually sold, per marketplace per day. Spend covers '
    'every ad program; units come from Orders, not from attribution.';

-- A view runs as its caller, so the RLS on sales_daily and ads_daily still
-- applies through it: a deactivated user reads nothing here either.
grant select on public.spend_daily to authenticated;


-- ============================================== 0003_invites.sql

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


-- ============================================== 0004_mcp.sql

-- Storage for the MCP connector's OAuth flow.
--
-- Claude.ai custom connectors authenticate with OAuth and nothing else - the
-- UI has no field for a static bearer token - so the dashboard has to be an
-- OAuth authorization server as well as a resource server. These three tables
-- are that server's whole state.
--
-- Serverless functions keep nothing between invocations, so this cannot live
-- in memory. It lives here because the dashboard already has a Postgres and
-- adding a second store for three small tables would be a second thing to
-- secure.
--
-- **Every row here is service-role only.** RLS is enabled and no policy is
-- ever written, which in Postgres means: deny. The anon key the browser holds
-- can read none of it. That is deliberate rather than an oversight, and the
-- comment is here so nobody "fixes" it by adding a policy.
--
-- Nothing stores a usable secret. Codes and tokens are kept as SHA-256
-- hashes, so a dump of this schema does not let anyone call the connector.

create table public.mcp_clients (
    client_id      text primary key,
    client_name    text,
    redirect_uris  text[] not null,
    created_at     timestamptz not null default now(),
    last_seen_at   timestamptz
);

comment on table public.mcp_clients is
    'Clients registered through OAuth dynamic client registration. Claude '
    'registers itself here the first time the connector is added.';

create table public.mcp_auth_codes (
    code_hash      text primary key,
    client_id      text not null references public.mcp_clients(client_id) on delete cascade,
    redirect_uri   text not null,
    code_challenge text,
    resource       text,
    expires_at     timestamptz not null,
    consumed_at    timestamptz,
    created_at     timestamptz not null default now()
);

comment on table public.mcp_auth_codes is
    'Short-lived authorization codes, stored as hashes and single-use. '
    'consumed_at is set on redemption rather than the row being deleted, so a '
    'replayed code is distinguishable from an unknown one.';

create table public.mcp_tokens (
    token_hash   text primary key,
    kind         text not null check (kind in ('access', 'refresh')),
    client_id    text not null references public.mcp_clients(client_id) on delete cascade,
    expires_at   timestamptz,
    revoked_at   timestamptz,
    created_at   timestamptz not null default now(),
    last_used_at timestamptz
);

create index mcp_tokens_client_idx on public.mcp_tokens (client_id, kind);

comment on table public.mcp_tokens is
    'Access and refresh tokens as SHA-256 hashes. A refresh token has no '
    'expiry and is revoked rather than deleted, so revoking access is one '
    'update and the audit trail survives it.';

alter table public.mcp_clients    enable row level security;
alter table public.mcp_auth_codes enable row level security;
alter table public.mcp_tokens     enable row level security;

-- No policies, on purpose. See the header.

-- Revoking every connector session, when that is ever needed:
--
--   update public.mcp_tokens set revoked_at = now() where revoked_at is null;
--
-- Claude then re-runs the OAuth flow and asks for the access password again.

-- ============================================================== BOOTSTRAP

-- Last, so that if anything above failed this is never reached and the
-- project is not left half-built with a claimable super admin slot.
do $bootstrap$
declare
    -- ------------------------------------------------------------------
    bootstrap_email text := 'you@example.com';   -- <<< PUT YOUR EMAIL HERE
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
