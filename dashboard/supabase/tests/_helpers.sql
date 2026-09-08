-- Shared assertion helpers. Applied to every test database by run.sh, after
-- the migrations, so each test file starts from the same vocabulary without
-- depending on another file having run first.

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
