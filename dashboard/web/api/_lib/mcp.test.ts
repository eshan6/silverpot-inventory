// The connector's logic, tested without a network or a database.
//
// Run: npm run test:api  (node --test, no test framework installed)
//
// What is worth pinning here is the security surface, because every mistake in
// it is silent: a redirect allow-list that accepts a lookalike domain, a
// constant-time compare that returns early, a date argument that reaches a
// PostgREST filter unvalidated. None of those fail loudly in testing.

import assert from "node:assert/strict";
import test from "node:test";

process.env["MCP_ACCESS_PASSWORD"] = "correct horse battery staple";

const { passwordMatches, pkceMatches, sha256, base64url } = await import("./oauth.js");
const { TOOLS, ToolError } = await import("./tools.js");

test("the password compare accepts only the exact password", () => {
  assert.equal(passwordMatches("correct horse battery staple"), true);
  assert.equal(passwordMatches("correct horse battery stapl"), false);
  assert.equal(passwordMatches("correct horse battery staple "), false);
  assert.equal(passwordMatches(""), false);
  assert.equal(passwordMatches("x"), false);
});

test("an unset password refuses everything rather than allowing everything", () => {
  const saved = process.env["MCP_ACCESS_PASSWORD"];
  delete process.env["MCP_ACCESS_PASSWORD"];
  try {
    // The failure that matters: an empty expected value must not make an empty
    // submission match. That is the difference between "not configured" and
    // "open to the internet".
    assert.equal(passwordMatches(""), false);
    assert.equal(passwordMatches("anything"), false);
  } finally {
    process.env["MCP_ACCESS_PASSWORD"] = saved;
  }
});

test("PKCE accepts a real verifier and rejects a wrong one", async () => {
  const verifier = "a-verifier-of-reasonable-length-1234567890";
  const challenge = await sha256(verifier);
  assert.equal(await pkceMatches(verifier, challenge), true);
  assert.equal(await pkceMatches("not-it", challenge), false);
  assert.equal(await pkceMatches("", challenge), false);
});

test("no challenge means nothing to verify, but an empty one is not a pass", async () => {
  assert.equal(await pkceMatches("anything", null), true);
  assert.equal(await pkceMatches("", null), true);
});

test("base64url output carries no characters that need escaping in a URL", () => {
  const encoded = base64url(new Uint8Array([251, 255, 190, 0, 1, 2]));
  assert.equal(/^[A-Za-z0-9_-]+$/.test(encoded), true);
});

test("every tool is described and takes a closed schema", () => {
  assert.ok(TOOLS.length >= 6);
  for (const tool of TOOLS) {
    assert.ok(tool.description.length > 30, `${tool.name} needs a real description`);
    const schema = tool.inputSchema as Record<string, unknown>;
    // additionalProperties:false is what stops a model inventing a filter that
    // silently does nothing.
    assert.equal(schema["additionalProperties"], false, tool.name);
    assert.equal(schema["type"], "object", tool.name);
  }
});

test("no tool offers a write or a free-text query", () => {
  // A run_query tool would hand anyone who reached this endpoint the roster,
  // the invites and the audit log. The absence is the security property, so
  // it gets a test rather than a comment.
  const forbidden = /query|sql|insert|update|delete|write|exec/i;
  for (const tool of TOOLS) {
    assert.equal(forbidden.test(tool.name), false, `${tool.name} looks like a write`);
  }
});

test("ToolError is thrown for a date that is not a date", async () => {
  const { callTool } = await import("./tools.js");
  await assert.rejects(
    () => callTool("sales_summary", { from: "last tuesday", to: "2026-01-01" }),
    (err: unknown) => err instanceof ToolError,
  );
  // A filter injection attempt must be refused by the shape check, before any
  // query is built.
  await assert.rejects(
    () => callTool("sales_summary", { from: "2026-01-01,marketplace.eq.x", to: "2026-02-01" }),
    (err: unknown) => err instanceof ToolError,
  );
});

test("a backwards range is refused rather than silently returning nothing", async () => {
  const { callTool } = await import("./tools.js");
  await assert.rejects(
    () => callTool("sales_summary", { from: "2026-06-01", to: "2026-01-01" }),
    (err: unknown) => err instanceof ToolError,
  );
});

test("an unknown marketplace is refused", async () => {
  const { callTool } = await import("./tools.js");
  await assert.rejects(
    () => callTool("sales_summary", {
      from: "2026-01-01", to: "2026-02-01", marketplace: "ebay",
    }),
    (err: unknown) => err instanceof ToolError,
  );
});

test("an unknown tool name is refused", async () => {
  const { callTool } = await import("./tools.js");
  await assert.rejects(
    () => callTool("drop_everything", {}),
    (err: unknown) => err instanceof ToolError,
  );
});

test("the redirect allow-list accepts Claude and refuses lookalikes", async () => {
  const { redirectAllowed } = await import("../oauth/register.js");
  assert.equal(redirectAllowed("https://claude.ai/api/mcp/auth_callback"), true);
  assert.equal(redirectAllowed("https://console.anthropic.com/cb"), true);

  // Each of these is a real way an allow-list gets defeated when it is written
  // as a substring or a `startsWith` check rather than a hostname comparison.
  for (const bad of [
    "https://claude.ai.evil.com/cb",        // suffix attack
    "https://notclaude.ai/cb",              // prefix attack
    "https://evil.com/?x=claude.ai",        // it appears in the query
    "https://evil.com#claude.ai",           // and in the fragment
    "http://claude.ai/cb",                  // downgraded to http
    "javascript:alert(1)",                  // not even a URL scheme we allow
    "not a url",
  ]) {
    assert.equal(redirectAllowed(bad), false, `should refuse ${bad}`);
  }
});

test("every file Vercel will treat as a function is actually one", async () => {
  // Vercel turns each file directly under api/ into a deployed function unless
  // its name starts with an underscore. A helper or a .d.ts left at that level
  // is built as an entrypoint, has no handler, and fails the deploy - while CI
  // stays green, because nothing in the test suite or the typecheck notices.
  // That happened once (api/env.d.ts), so it is pinned rather than remembered.
  const { readdir } = await import("node:fs/promises");
  const { readFile } = await import("node:fs/promises");
  const path = await import("node:path");

  // Compiled to .api-build/_lib, so the real sources are two levels up.
  const root = path.join(import.meta.dirname, "..", "..", "api");
  const walk = async (dir: string): Promise<string[]> => {
    const out: string[] = [];
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      if (entry.name.startsWith("_")) continue; // ignored by Vercel
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) out.push(...(await walk(full)));
      else out.push(full);
    }
    return out;
  };

  const routed = await walk(root);
  assert.ok(routed.length > 0, "expected some function files");
  for (const file of routed) {
    assert.ok(
      file.endsWith(".ts") && !file.endsWith(".d.ts"),
      `${file} would be deployed as a function but is not a .ts module`,
    );
    const source = await readFile(file, "utf8");
    assert.match(
      source,
      /export default (async )?function/,
      `${file} would be deployed as a function but exports no default handler`,
    );
  }
});
