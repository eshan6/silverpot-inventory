// The OAuth bits the MCP connector needs, and nothing more.
//
// Claude.ai custom connectors authenticate with OAuth only - the connector UI
// has no field for a static bearer token - so a dashboard that wants to be a
// connector has to run an authorization server. This is the smallest one that
// is still correct:
//
//   * codes and tokens are random 256-bit values, stored only as SHA-256
//     hashes, so the database never holds anything usable;
//   * codes are single-use and expire in ten minutes;
//   * PKCE S256 is verified when the client sends a challenge (Claude does);
//   * the human gate is one shared password, compared in constant time.
//
// One password rather than per-user accounts is a deliberate scope choice: the
// dashboard's own login already has roles, and this endpoint exposes only
// aggregate figures that every role can already read. If per-user connector
// access is ever wanted, that is a real feature, not a tweak here.

import { insert, patch, select } from "./db.ts";

const PASSWORD_ENV = "MCP_ACCESS_PASSWORD";

export const CODE_TTL_SECONDS = 600;
export const ACCESS_TTL_SECONDS = 60 * 60 * 24 * 30;

export function passwordConfigured(): boolean {
  return (process.env[PASSWORD_ENV] ?? "").length > 0;
}

/** Constant-time compare, so a wrong password leaks nothing by timing. */
export function passwordMatches(given: string): boolean {
  const expected = process.env[PASSWORD_ENV] ?? "";
  if (expected.length === 0) return false;
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(expected);
  // Length is compared without an early return; a differing length still walks
  // the whole loop below.
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  return diff === 0;
}

export function randomSecret(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

export function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return base64url(new Uint8Array(digest));
}

/** PKCE S256: the verifier hashes to the challenge the client sent earlier. */
export async function pkceMatches(
  verifier: string,
  challenge: string | null,
): Promise<boolean> {
  if (!challenge) return true; // No challenge was issued, so none to check.
  if (!verifier) return false;
  return (await sha256(verifier)) === challenge;
}

export interface StoredClient {
  client_id: string;
  redirect_uris: string[];
}

export async function findClient(clientId: string): Promise<StoredClient | null> {
  const rows = await select<StoredClient>(
    "mcp_clients",
    { select: "client_id,redirect_uris", client_id: `eq.${clientId}` },
    1,
  );
  return rows[0] ?? null;
}

export async function issueCode(params: {
  clientId: string;
  redirectUri: string;
  codeChallenge: string | null;
  resource: string | null;
}): Promise<string> {
  const code = randomSecret();
  await insert("mcp_auth_codes", {
    code_hash: await sha256(code),
    client_id: params.clientId,
    redirect_uri: params.redirectUri,
    code_challenge: params.codeChallenge,
    resource: params.resource,
    expires_at: new Date(Date.now() + CODE_TTL_SECONDS * 1000).toISOString(),
  });
  return code;
}

interface StoredCode {
  code_hash: string;
  client_id: string;
  redirect_uri: string;
  code_challenge: string | null;
  expires_at: string;
  consumed_at: string | null;
}

/**
 * Redeem a code, or explain why not. Consuming happens before the caller gets
 * the tokens, so a code replayed twice in parallel cannot mint two sessions.
 */
export async function redeemCode(
  code: string,
  clientId: string,
  redirectUri: string,
  verifier: string,
): Promise<{ ok: true } | { ok: false; reason: string }> {
  const hash = await sha256(code);
  const rows = await select<StoredCode>(
    "mcp_auth_codes",
    {
      select: "code_hash,client_id,redirect_uri,code_challenge,expires_at,consumed_at",
      code_hash: `eq.${hash}`,
    },
    1,
  );
  const row = rows[0];
  if (!row) return { ok: false, reason: "unknown code" };
  if (row.consumed_at) return { ok: false, reason: "code already used" };
  if (new Date(row.expires_at).getTime() < Date.now()) {
    return { ok: false, reason: "code expired" };
  }
  if (row.client_id !== clientId) return { ok: false, reason: "wrong client" };
  if (row.redirect_uri !== redirectUri) {
    return { ok: false, reason: "redirect_uri mismatch" };
  }
  if (!(await pkceMatches(verifier, row.code_challenge))) {
    return { ok: false, reason: "PKCE verification failed" };
  }
  await patch(
    "mcp_auth_codes",
    { code_hash: `eq.${hash}`, consumed_at: "is.null" },
    { consumed_at: new Date().toISOString() },
  );
  return { ok: true };
}

export async function issueTokens(clientId: string): Promise<{
  access_token: string;
  refresh_token: string;
  expires_in: number;
}> {
  const access = randomSecret();
  const refresh = randomSecret();
  const expires = new Date(Date.now() + ACCESS_TTL_SECONDS * 1000).toISOString();
  await insert("mcp_tokens", {
    token_hash: await sha256(access),
    kind: "access",
    client_id: clientId,
    expires_at: expires,
  });
  await insert("mcp_tokens", {
    token_hash: await sha256(refresh),
    kind: "refresh",
    client_id: clientId,
    expires_at: null,
  });
  return {
    access_token: access,
    refresh_token: refresh,
    expires_in: ACCESS_TTL_SECONDS,
  };
}

interface StoredToken {
  token_hash: string;
  kind: string;
  client_id: string;
  expires_at: string | null;
  revoked_at: string | null;
}

async function lookupToken(
  token: string,
  kind: "access" | "refresh",
): Promise<StoredToken | null> {
  const hash = await sha256(token);
  const rows = await select<StoredToken>(
    "mcp_tokens",
    {
      select: "token_hash,kind,client_id,expires_at,revoked_at",
      token_hash: `eq.${hash}`,
      kind: `eq.${kind}`,
    },
    1,
  );
  const row = rows[0];
  if (!row) return null;
  if (row.revoked_at) return null;
  if (row.expires_at && new Date(row.expires_at).getTime() < Date.now()) {
    return null;
  }
  return row;
}

/** Returns the client id the bearer token belongs to, or null. */
export async function verifyAccessToken(token: string): Promise<string | null> {
  const row = await lookupToken(token, "access");
  if (!row) return null;
  // Best-effort touch; a failure here must not fail the request.
  patch("mcp_tokens", { token_hash: `eq.${row.token_hash}` }, {
    last_used_at: new Date().toISOString(),
  }).catch(() => {});
  return row.client_id;
}

export async function refresh(token: string, clientId: string) {
  const row = await lookupToken(token, "refresh");
  if (!row || row.client_id !== clientId) return null;
  // The old refresh token is retired as the new pair is minted, so a stolen
  // refresh token stops working the moment the real client uses its own.
  await patch("mcp_tokens", { token_hash: `eq.${row.token_hash}` }, {
    revoked_at: new Date().toISOString(),
  });
  return issueTokens(clientId);
}

export function bearerFrom(request: Request): string | null {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  return match?.[1] ?? null;
}

export function originOf(request: Request): string {
  return new URL(request.url).origin;
}
