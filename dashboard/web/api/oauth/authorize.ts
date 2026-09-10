// The one place a human is involved: the access password.
//
// GET renders a small form. POST checks the password and, if it matches,
// redirects back to Claude with a single-use code. Everything else about the
// request - client id, redirect URI, PKCE challenge - is validated before the
// form is even shown, so a malformed flow fails on a page that explains why
// rather than after someone has typed a password.

import { findClient, issueCode, passwordConfigured, passwordMatches } from "../_lib/oauth.js";
import { missingEnv } from "../_lib/db.js";

export const config = { runtime: "edge" };

interface Ask {
  clientId: string;
  redirectUri: string;
  state: string | null;
  codeChallenge: string | null;
  resource: string | null;
}

function readAsk(params: URLSearchParams): Ask {
  return {
    clientId: params.get("client_id") ?? "",
    redirectUri: params.get("redirect_uri") ?? "",
    state: params.get("state"),
    codeChallenge: params.get("code_challenge"),
    resource: params.get("resource"),
  };
}

/**
 * Validate before showing anything. Returns an error string, or null to
 * proceed.
 *
 * The redirect URI is checked against what the client registered, not merely
 * parsed. That check is the one that matters: without it, anyone could start a
 * flow naming their own callback and collect a code for this dashboard.
 */
async function problemWith(ask: Ask, params: URLSearchParams): Promise<string | null> {
  if (params.get("response_type") !== "code") {
    return "Only response_type=code is supported.";
  }
  if (!ask.clientId) return "Missing client_id.";
  if (!ask.redirectUri) return "Missing redirect_uri.";
  if (params.get("code_challenge_method") &&
      params.get("code_challenge_method") !== "S256") {
    return "Only PKCE S256 is supported.";
  }
  const client = await findClient(ask.clientId);
  if (!client) return "Unknown client. Remove the connector and add it again.";
  if (!client.redirect_uris.includes(ask.redirectUri)) {
    return "redirect_uri does not match this client's registration.";
  }
  return null;
}

export default async function handler(request: Request): Promise<Response> {
  const url = new URL(request.url);

  const missing = [
    ...missingEnv(),
    ...(passwordConfigured() ? [] : ["MCP_ACCESS_PASSWORD"]),
  ];
  if (missing.length > 0) {
    return page(
      "Not configured",
      `<p>This connector is not set up yet. Missing: <code>${missing
        .map(escape)
        .join("</code>, <code>")}</code>.</p>`,
      503,
    );
  }

  if (request.method === "GET") {
    const ask = readAsk(url.searchParams);
    const problem = await problemWith(ask, url.searchParams);
    if (problem) return page("Cannot continue", `<p>${escape(problem)}</p>`, 400);
    return page("Connect to Silverpot", form(url.searchParams, null));
  }

  if (request.method !== "POST") {
    return page("Cannot continue", "<p>Method not allowed.</p>", 405);
  }

  const submitted = new URLSearchParams(await request.text());
  const ask = readAsk(submitted);
  const problem = await problemWith(ask, submitted);
  if (problem) return page("Cannot continue", `<p>${escape(problem)}</p>`, 400);

  if (!passwordMatches(submitted.get("password") ?? "")) {
    return page(
      "Connect to Silverpot",
      form(submitted, "That password is not right."),
      401,
    );
  }

  let code: string;
  try {
    code = await issueCode({
      clientId: ask.clientId,
      redirectUri: ask.redirectUri,
      codeChallenge: ask.codeChallenge,
      resource: ask.resource,
    });
  } catch (err) {
    console.error("could not issue code", err);
    return page("Cannot continue", "<p>The database did not answer.</p>", 500);
  }

  const back = new URL(ask.redirectUri);
  back.searchParams.set("code", code);
  if (ask.state) back.searchParams.set("state", ask.state);
  return Response.redirect(back.toString(), 302);
}

/** Carry every parameter forward, so the POST is validated as strictly as the GET. */
function form(params: URLSearchParams, error: string | null): string {
  const carry = [
    "response_type", "client_id", "redirect_uri", "state",
    "code_challenge", "code_challenge_method", "resource", "scope",
  ];
  const hidden = carry
    .filter((k) => params.get(k))
    .map(
      (k) =>
        `<input type="hidden" name="${k}" value="${escape(params.get(k) ?? "")}">`,
    )
    .join("");
  return `
    ${error ? `<p class="error">${escape(error)}</p>` : ""}
    <p>Claude is asking to read Silverpot's sales and advertising figures.</p>
    <form method="post">
      ${hidden}
      <label for="password">Access password</label>
      <input id="password" name="password" type="password" autocomplete="current-password"
             autofocus required>
      <button type="submit">Allow access</button>
    </form>
    <p class="fine">Read-only. This grants no ability to change anything.</p>`;
}

function page(title: string, inner: string, status = 200): Response {
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escape(title)}</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.55 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI",
         Roboto, sans-serif; background: #f7f3ec; color: #1b1a17;
         display: grid; place-items: center; min-height: 100vh; margin: 0; padding: 24px; }
  main { background: #fffdf9; border: 1px solid #e7dfd2; border-radius: 10px;
         padding: 28px; max-width: 26rem; width: 100%; }
  h1 { font-size: 1.15rem; margin: 0 0 .75rem; }
  label { display: block; font-weight: 600; margin: 1rem 0 .35rem; }
  input { width: 100%; padding: .6rem .7rem; border: 1px solid #d9cfbd;
          border-radius: 6px; font: inherit; background: #fff; color: inherit; box-sizing: border-box; }
  button { margin-top: 1rem; width: 100%; padding: .65rem; border: 0; border-radius: 6px;
           background: #6f6139; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  .error { color: #a33a3a; font-weight: 600; }
  .fine { color: #6e6a61; font-size: .85rem; margin-top: 1rem; }
  code { background: #f0e9dd; padding: .1rem .3rem; border-radius: 4px; }
  @media (prefers-color-scheme: dark) {
    body { background: #171613; color: #f2ede4; }
    main { background: #201e1a; border-color: #3a352c; }
    input { background: #171613; border-color: #4a4438; }
    code { background: #2c2822; }
  }
</style></head><body><main><h1>${escape(title)}</h1>${inner}</main></body></html>`,
    {
      status,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        // No indexing, no framing, and no referrer - the URL carries the
        // client's state parameter.
        "X-Robots-Tag": "noindex, nofollow",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
      },
    },
  );
}

function escape(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
