// The two discovery documents Claude fetches before it will authenticate.
//
// One function serves both, chosen by `?doc=`, because they are four lines
// each and a second function is a second cold start. vercel.json rewrites the
// well-known paths here.

export const config = { runtime: "edge" };

export default function handler(request: Request): Response {
  const url = new URL(request.url);
  const origin = url.origin;
  const doc = url.searchParams.get("doc");

  const body =
    doc === "resource"
      ? {
          // Which resource this protects, and who issues tokens for it.
          resource: `${origin}/api/mcp`,
          authorization_servers: [origin],
          bearer_methods_supported: ["header"],
        }
      : {
          issuer: origin,
          authorization_endpoint: `${origin}/api/oauth/authorize`,
          token_endpoint: `${origin}/api/oauth/token`,
          registration_endpoint: `${origin}/api/oauth/register`,
          response_types_supported: ["code"],
          grant_types_supported: ["authorization_code", "refresh_token"],
          // S256 only. `plain` is in the spec and is not worth supporting: it
          // gives a challenge no attacker has to work to satisfy.
          code_challenge_methods_supported: ["S256"],
          token_endpoint_auth_methods_supported: ["none"],
          scopes_supported: ["read"],
        };

  return new Response(JSON.stringify(body, null, 2), {
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "public, max-age=300",
    },
  });
}
