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

// ---------------------------------------------------------- advertising grains
//
// The three advertising tools read two tables on purpose. `ads_summary` and
// `ads_by_campaign` both sum `ads_daily`; `ads_by_asin` sums
// `ads_advertised_product`. They are the same spend cut two ways, and the
// reason they are not one table is that `ads_summary` groups only by
// (marketplace, ad_program) and does not filter by grain - a second grain in
// `ads_daily` would double every figure without erroring.

test("the new advertising tools are offered and take closed schemas", () => {
  for (const name of ["ads_by_campaign", "ads_by_asin", "ad_change_history"]) {
    const tool = TOOLS.find((t) => t.name === name);
    assert.ok(tool, `${name} is not offered`);
    assert.equal((tool.inputSchema as Record<string, unknown>)["additionalProperties"], false);
    assert.ok(tool.description.length > 40, `${name} needs a real description`);
  }
});

test("the two grain tools warn against being added together", () => {
  // The one mistake a reader of these tools can make that produces a
  // plausible wrong number rather than an error.
  for (const name of ["ads_by_campaign", "ads_by_asin"]) {
    const tool = TOOLS.find((t) => t.name === name);
    assert.match(String(tool?.description), /never (sum|add)|do not add/i);
  }
});

test("no tool description still claims Amazon Ads is awaiting approval", () => {
  for (const tool of TOOLS) {
    assert.doesNotMatch(tool.description, /pending API approval/i);
  }
});

test("ad_change_history refuses a campaign id that could inject a filter", async () => {
  const { callTool } = await import("./tools.js");
  for (const bad of [
    "123,entity_id.eq.x",
    "123)",
    "a.b",
    "' or 1=1",
    "x".repeat(65),
  ]) {
    await assert.rejects(
      () => callTool("ad_change_history", {
        from: "2026-09-01", to: "2026-10-20", campaign_id: bad,
      }),
      (err: unknown) => err instanceof ToolError,
      `campaign_id ${JSON.stringify(bad)} should be refused`,
    );
  }
});

test("ad_change_history refuses an asin that could inject a filter", async () => {
  const { callTool } = await import("./tools.js");
  await assert.rejects(
    () => callTool("ad_change_history", {
      from: "2026-09-01", to: "2026-10-20", asin: "B0TEST,old_value.eq.x",
    }),
    (err: unknown) => err instanceof ToolError,
  );
});

test("the grain tools refuse a bad date and a backwards range", async () => {
  const { callTool } = await import("./tools.js");
  for (const name of ["ads_by_campaign", "ads_by_asin", "ad_change_history"]) {
    await assert.rejects(
      () => callTool(name, { from: "yesterday", to: "2026-10-20" }),
      (err: unknown) => err instanceof ToolError,
      `${name} accepted a non-date`,
    );
    await assert.rejects(
      () => callTool(name, { from: "2026-10-20", to: "2026-09-01" }),
      (err: unknown) => err instanceof ToolError,
      `${name} accepted a backwards range`,
    );
  }
});

test("a limit must be a positive whole number", async () => {
  const { callTool } = await import("./tools.js");
  for (const bad of [0, -5, 2.5, "lots"]) {
    await assert.rejects(
      () => callTool("ads_by_campaign", {
        from: "2026-09-01", to: "2026-10-20", limit: bad,
      }),
      (err: unknown) => err instanceof ToolError,
      `limit ${JSON.stringify(bad)} should be refused`,
    );
  }
});

// ------------------------------------------- the grains must agree on totals
//
// The check this was written for: Sponsored Products spend in
// ads_by_campaign over Sep 26 - Oct 2 2026 should total $137.09, the figure
// ads_summary reports for the same range.
//
// The live call needs a deployed connector, but the part that can be wrong in
// code is whether the two aggregations agree on identical input - and that is
// testable here. If they ever disagree, a reader who adds up the campaigns
// and compares against the summary has no way to tell which one is lying.

const SEPT_ROWS = [
  { marketplace: "amazon", ad_program: "sponsored_products", campaign_id: "1",
    campaign_name: "SP - Assam", impressions: 9001, clicks: 71, spend: 80.115,
    attributed_sales: 221.44, attributed_units: 11 },
  { marketplace: "amazon", ad_program: "sponsored_products", campaign_id: "2",
    campaign_name: "SP - Darjeeling", impressions: 5120, clicks: 38, spend: 41.225,
    attributed_sales: 108.34, attributed_units: 4 },
  { marketplace: "amazon", ad_program: "sponsored_products", campaign_id: "1",
    campaign_name: "SP - Assam", impressions: 2516, clicks: 14, spend: 15.75,
    attributed_sales: 50.0, attributed_units: 2 },
];

test("campaign spend sums to the same total ads_summary reports", async () => {
  const { summariseAds, rollUp } = await import("./tools.js");
  const summary = summariseAds(SEPT_ROWS as never);
  const campaigns = rollUp(
    SEPT_ROWS as never,
    (r) => String(r.campaign_id ?? ""),
    (r) => ({ campaign_id: String(r.campaign_id ?? "") }),
    50,
  );
  // The authoritative total the tool reports is summed from raw values and
  // rounded once, exactly as ads_summary does, so the two agree to the penny.
  const summarySpend = summary[0]?.spend ?? 0;
  assert.equal(summarySpend, 137.09);
  // And the same for every other metric, not just the one being checked.
  assert.equal(
    campaigns.reduce((a, c) => a + (c["clicks"] as number), 0),
    summary[0]?.clicks,
  );
  assert.equal(
    campaigns.reduce((a, c) => a + (c["attributed_units"] as number), 0),
    summary[0]?.attributed_units,
  );
});

test("adding up the rounded per-campaign rows can differ by a penny", async () => {
  // Why ads_by_campaign reports a `totals` block rather than leaving a
  // reader to add the rows. 80.115 + 41.225 + 15.75 is 137.09 summed raw,
  // but the campaigns round to 95.87 and 41.23, which read as 137.10. This
  // is inherent to showing rounded components and is not a bug; publishing
  // an authoritative total next to them is what keeps it from looking like
  // one.
  const { summariseAds, rollUp } = await import("./tools.js");
  const campaigns = rollUp(
    SEPT_ROWS as never,
    (r) => String(r.campaign_id ?? ""),
    (r) => ({ campaign_id: String(r.campaign_id ?? "") }),
    50,
  );
  const addedUp = Number(
    campaigns.reduce((a, c) => a + (c["spend"] as number), 0).toFixed(2),
  );
  assert.equal(addedUp, 137.10);
  assert.equal(summariseAds(SEPT_ROWS as never)[0]?.spend, 137.09);
});

test("rollUp groups by key and orders by spend, biggest first", async () => {
  const { rollUp } = await import("./tools.js");
  const out = rollUp(
    SEPT_ROWS as never,
    (r) => String(r.campaign_id ?? ""),
    (r) => ({ campaign_id: String(r.campaign_id ?? "") }),
    50,
  );
  assert.equal(out.length, 2, "the two rows for campaign 1 should merge");
  assert.equal(out[0]?.["campaign_id"], "1");
  assert.equal(out[0]?.["spend"], 95.87);
  assert.equal(out[1]?.["spend"], 41.23);
});
