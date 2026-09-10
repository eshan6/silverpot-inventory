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
