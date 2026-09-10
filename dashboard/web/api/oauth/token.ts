// Code for tokens, and refresh for a new pair.
//
// Two things the spec is strict about and clients quietly depend on:
// the body arrives as application/x-www-form-urlencoded, and errors come back
// as the documented `error` codes rather than prose. Both are honoured here.

import { findClient, issueTokens, redeemCode, refresh } from "../_lib/oauth.ts";

export const config = { runtime: "edge" };

export default async function handler(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
      },
    });
  }
  if (request.method !== "POST") return fail("invalid_request", "POST only", 405);

  const form = new URLSearchParams(await request.text());
  const grant = form.get("grant_type") ?? "";
  const clientId = form.get("client_id") ?? "";

  if (!clientId) return fail("invalid_client", "client_id is required");
  const client = await findClient(clientId).catch(() => null);
  if (!client) return fail("invalid_client", "Unknown client");

  if (grant === "authorization_code") {
    const code = form.get("code") ?? "";
    const redirectUri = form.get("redirect_uri") ?? "";
    const verifier = form.get("code_verifier") ?? "";
    if (!code) return fail("invalid_request", "code is required");

    const outcome = await redeemCode(code, clientId, redirectUri, verifier).catch(
      (err) => {
        console.error("code redemption failed", err);
        return { ok: false as const, reason: "server error" };
      },
    );
    if (!outcome.ok) return fail("invalid_grant", outcome.reason);

    try {
      return ok(await issueTokens(clientId));
    } catch (err) {
      console.error("could not issue tokens", err);
      return fail("server_error", "Could not issue tokens", 500);
    }
  }

  if (grant === "refresh_token") {
    const token = form.get("refresh_token") ?? "";
    if (!token) return fail("invalid_request", "refresh_token is required");
    const issued = await refresh(token, clientId).catch((err) => {
      console.error("refresh failed", err);
      return null;
    });
    if (!issued) return fail("invalid_grant", "Refresh token is not valid");
    return ok(issued);
  }

  return fail("unsupported_grant_type", `Unsupported grant_type: ${grant}`);
}

function ok(tokens: {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}): Response {
  return new Response(
    JSON.stringify({ ...tokens, token_type: "Bearer", scope: "read" }),
    {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        // Tokens must never sit in a shared cache.
        "Cache-Control": "no-store",
        Pragma: "no-cache",
        "Access-Control-Allow-Origin": "*",
      },
    },
  );
}

function fail(error: string, description: string, status = 400): Response {
  return new Response(
    JSON.stringify({ error, error_description: description }),
    {
      status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
      },
    },
  );
}
