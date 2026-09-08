-- Local-only stand-in for what Supabase provides: the auth schema, auth.uid()
-- reading the JWT subject claim, and the anon / authenticated / service_role
-- roles. Never applied to the real project - Supabase already has all of this.
--
-- auth.uid() is defined exactly as Supabase defines it, so a policy that
-- behaves here behaves there.

create schema if not exists auth;

create table if not exists auth.users (
    id    uuid primary key,
    email text
);

create or replace function auth.uid()
returns uuid
language sql
stable
as $$
    select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
    -- Ingestion connects as this. BYPASSRLS mirrors Supabase's service role:
    -- the daily job must be able to write facts no browser session may write.
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin bypassrls;
    end if;
end $$;

grant usage on schema public, auth to anon, authenticated, service_role;
