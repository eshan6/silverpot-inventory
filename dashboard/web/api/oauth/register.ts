// Dynamic client registration (RFC 7591).
//
// Claude registers itself here the first time the connector is added, and
// again if it ever loses its client id. Registration is deliberately open -
// anyone can obtain a client id - because a client id is not a credential.
// The gate is the access password on the authorize page, which a registration
// does not get anyone past.
//
// What is *not* open: redirect URIs. A registration that could name any
// redirect would let someone start a flow that mails the code elsewhere, so
// only Claude's callbacks are accepted.

import { insert } from "../_lib/db.ts";
import { randomSecret } from "../_lib/oauth.ts";

export const config = { runtime: "edge" };

const ALLOWED_REDIRECT_HOSTS = new Set([
  "claude.ai",
  "claude.com",
  "console.anthropic.com",
]);

export function redirectAllowed(uri: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:") return false;
  const host = parsed.hostname.toLowerCase();
  return [...ALLOWED_REDIRECT_HOSTS].some(
    (allowed) => host === allowed || host.endsWith(`.${allowed}`),
  );
}

export default async function handler(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    });
  }
  if (request.method !== "POST") {
    return json({ error: "invalid_request" }, 405);
  }

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return json({ error: "invalid_client_metadata" }, 400);
  }

  const uris = Array.isArray(body["redirect_uris"])
    ? (body["redirect_uris"] as unknown[]).filter(
        (u): u is string => typeof u === "string",
      )
    : [];
  if (uris.length === 0) {
    return json(
      { error: "invalid_redirect_uri", error_description: "redirect_uris is required." },
      400,
    );
  }
  const rejected = uris.filter((u) => !redirectAllowed(u));
  if (rejected.length > 0) {
    return json(
      {
        error: "invalid_redirect_uri",
        error_description:
          "Only https callbacks on claude.ai, claude.com or console.anthropic.com are accepted.",
      },
      400,
    );
  }

  const clientId = `mcp_${randomSecret().slice(0, 24)}`;
  const name =
    typeof body["client_name"] === "string" ? body["client_name"] : "Claude";

  try {
    await insert("mcp_clients", {
      client_id: clientId,
      client_name: name,
      redirect_uris: uris,
    });
  } catch (err) {
    console.error("client registration failed", err);
    return json({ error: "server_error" }, 500);
  }

  return json(
    {
      client_id: clientId,
      client_name: name,
      redirect_uris: uris,
      // A public client: no secret, PKCE instead. A secret stored in a hosted
      // client is not a secret, and PKCE is what actually binds the code to
      // the client that asked for it.
      token_endpoint_auth_method: "none",
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
    },
    201,
  );
}

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
