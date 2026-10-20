// What the connector can actually ask for.
//
// Every tool is a read of an aggregate fact table. There is no write path, no
// tool that names `profiles`, `invites`, `app_settings` or `audit_log`, and no
// free-text SQL - a `run_query` tool would hand anyone who reached this
// endpoint the whole database, including the roster and the audit trail.
//
// Aggregation happens here rather than in SQL because the volume is small
// (sales_daily holds a few thousand rows for the whole history) and because a
// view would be a second place to keep the same definitions correct. Every
// query is bounded by a date range and a row cap.
//
// Figures are returned as JSON text. The freshness stamp rides along with the
// sales tools on purpose: `ingest_runs` is what tells a reader whether zero
// means "no sales" or "the pipeline did not run", and this project has
// already published stale numbers once without saying so.

import { select } from "./db.js";

const ROW_CAP = 20000;
const MARKETPLACES = ["amazon", "walmart"] as const;

export interface ToolDef {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

const dateRange = {
  from: { type: "string", description: "First day, YYYY-MM-DD (inclusive)." },
  to: { type: "string", description: "Last day, YYYY-MM-DD (inclusive)." },
  marketplace: {
    type: "string",
    enum: [...MARKETPLACES],
    description: "Omit for both marketplaces combined, reported separately.",
  },
};

export const TOOLS: ToolDef[] = [
  {
    name: "sales_summary",
    description:
      "Total units, orders and revenue for a date range, split by marketplace. " +
      "Start here for questions like 'how did we do last month'.",
    inputSchema: {
      type: "object",
      properties: dateRange,
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "sales_by_sku",
    description:
      "Units and revenue per SKU for a date range, best sellers first. Use for " +
      "'which teas sold most' or to find a slow mover.",
    inputSchema: {
      type: "object",
      properties: {
        ...dateRange,
        limit: { type: "integer", description: "How many SKUs (default 25)." },
      },
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "sales_timeseries",
    description:
      "Units and revenue per day for a date range. Use for trends, spikes, and " +
      "spotting days with no sales at all (which may mean a stockout).",
    inputSchema: {
      type: "object",
      properties: dateRange,
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "ads_summary",
    description:
      "Advertising totals for a date range: impressions, clicks, spend, " +
      "attributed sales and units, split by marketplace and ad program.",
    inputSchema: {
      type: "object",
      properties: dateRange,
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "spend_summary",
    description:
      "Ad spend against units actually sold, per day: cost per unit and ad " +
      "spend as a percentage of revenue (TACoS). Units come from orders, not " +
      "from ad attribution.",
    inputSchema: {
      type: "object",
      properties: dateRange,
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "ads_by_campaign",
    description:
      "Sponsored Products metrics per campaign per day, summed over a date " +
      "range, biggest spender first. Totals here match ads_summary for the " +
      "same range. Do not add this to ads_by_asin: they are the same spend " +
      "cut two ways, not two different spends.",
    inputSchema: {
      type: "object",
      properties: {
        ...dateRange,
        limit: { type: "integer", description: "How many campaigns (default 50)." },
      },
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "ads_by_asin",
    description:
      "Sponsored Products metrics per advertised ASIN per day, summed over a " +
      "date range, biggest spender first. The same spend as ads_by_campaign, " +
      "cut by product instead; never sum the two together. Campaign-level " +
      "spend that is not attributable to one advertised product does not " +
      "appear here, so this can total slightly less than ads_summary.",
    inputSchema: {
      type: "object",
      properties: {
        ...dateRange,
        limit: { type: "integer", description: "How many ASINs (default 50)." },
      },
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "ad_change_history",
    description:
      "Who changed what in the Amazon Ads account, and when: campaigns, ad " +
      "groups, ads, keywords, negative keywords and product targets, with " +
      "the old and new value of each change. Use it to explain a step change " +
      "in spend or sales. Amazon keeps about ninety days and deletes the " +
      "rest, so an empty answer for an older range means the window closed, " +
      "not that nothing changed.",
    inputSchema: {
      type: "object",
      properties: {
        from: { type: "string", description: "First day, YYYY-MM-DD (inclusive)." },
        to: { type: "string", description: "Last day, YYYY-MM-DD (inclusive)." },
        campaign_id: {
          type: "string",
          description: "Only changes to this campaign, and to things inside it.",
        },
        asin: {
          type: "string",
          description: "Only changes naming this ASIN, such as a product target.",
        },
        limit: { type: "integer", description: "How many changes (default 200)." },
      },
      required: ["from", "to"],
      additionalProperties: false,
    },
  },
  {
    name: "data_freshness",
    description:
      "When each marketplace last ingested successfully and what range it " +
      "covered. Call this before trusting a zero: it distinguishes 'no sales' " +
      "from 'the pipeline has not run'.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
];

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

export class ToolError extends Error {}

function day(value: unknown, field: string): string {
  if (typeof value !== "string" || !ISO_DAY.test(value)) {
    throw new ToolError(`${field} must be a date as YYYY-MM-DD`);
  }
  // PostgREST filters are built by string concatenation below, so a value that
  // reached a query un-validated could inject a filter. The regex above is
  // what makes that impossible; keep it.
  return value;
}

function marketplaceFilter(args: Record<string, unknown>): string | null {
  const value = args["marketplace"];
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string" || !MARKETPLACES.includes(value as never)) {
    throw new ToolError(`marketplace must be one of ${MARKETPLACES.join(", ")}`);
  }
  return value;
}

/**
 * Both bounds of a date range plus the optional marketplace, as PostgREST
 * filters. The two bounds go in one `and=(...)` group because a plain object
 * cannot carry the same key twice, and a range needs `gte` and `lte` on the
 * one column.
 */
function range(args: Record<string, unknown>, column: string) {
  const from = day(args["from"], "from");
  const to = day(args["to"], "to");
  if (from > to) throw new ToolError("`from` is after `to`");
  const query: Record<string, string> = {
    and: `(${column}.gte.${from},${column}.lte.${to})`,
  };
  const market = marketplaceFilter(args);
  if (market) query["marketplace"] = `eq.${market}`;
  return { from, to, query };
}

interface SalesRow {
  marketplace: string;
  sale_date: string;
  sku: string;
  product_name: string | null;
  internal_code: string | null;
  units: number;
  orders: number;
  revenue: number;
}

async function salesRows(args: Record<string, unknown>): Promise<SalesRow[]> {
  const { query } = range(args, "sale_date");
  return select<SalesRow>(
    "sales_daily",
    {
      ...query,
      select:
        "marketplace,sale_date,sku,product_name,internal_code,units,orders,revenue",
    },
    ROW_CAP,
  );
}

function totals(rows: SalesRow[]) {
  const by = new Map<string, { units: number; orders: number; revenue: number }>();
  for (const r of rows) {
    const cur = by.get(r.marketplace) ?? { units: 0, orders: 0, revenue: 0 };
    cur.units += Number(r.units) || 0;
    cur.orders += Number(r.orders) || 0;
    cur.revenue += Number(r.revenue) || 0;
    by.set(r.marketplace, cur);
  }
  return [...by.entries()].map(([marketplace, t]) => ({
    marketplace,
    units: t.units,
    orders: t.orders,
    revenue: round(t.revenue),
  }));
}

function round(n: number): number {
  return Math.round(n * 100) / 100;
}

interface FreshnessRow {
  marketplace: string;
  source: string;
  status: string;
  covers_from: string | null;
  covers_to: string | null;
  rows_written: number;
  started_at: string;
  finished_at: string | null;
}

interface AdGrainRow {
  marketplace: string;
  ad_program: string;
  campaign_id?: string;
  campaign_name?: string | null;
  asin?: string;
  sku?: string;
  impressions: number;
  clicks: number;
  spend: number;
  attributed_sales: number;
  attributed_units: number;
}

interface ChangeRow {
  changed_at: string;
  entity_type: string;
  entity_id: string;
  entity_name: string | null;
  campaign_id: string;
  campaign_name: string | null;
  change_type: string;
  field_name: string;
  old_value: string | null;
  new_value: string | null;
  changed_by: string | null;
}

/** An optional free-text filter, validated so it cannot inject a filter. */
function text(value: unknown, field: string): string | null {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string") throw new ToolError(`${field} must be a string`);
  // Same reasoning as `day`: these are concatenated into PostgREST filters,
  // where a comma, parenthesis or dot is syntax. Ids and ASINs need none of
  // those characters, so the safe set is small and the check is a whitelist
  // rather than an attempt to escape.
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(value)) {
    throw new ToolError(`${field} must be letters, digits, dash or underscore`);
  }
  return value;
}

function cap(args: Record<string, unknown>, fallback: number): number {
  const raw = args["limit"];
  if (raw === undefined || raw === null || raw === "") return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) {
    throw new ToolError("limit must be a positive whole number");
  }
  return Math.min(n, ROW_CAP);
}

/** The day after `iso`, so a date range can be closed with a `lt` on an instant. */
function nextDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/**
 * Sum metric rows into one row per key, biggest spender first.
 *
 * Shared by the two grain tools so that "spend" means the same arithmetic in
 * both. They read different tables on purpose - see 0005_ads_history.sql -
 * and the one thing that must not differ is how the numbers are added up.
 */
function rollUp(
  rows: AdGrainRow[],
  key: (r: AdGrainRow) => string,
  label: (r: AdGrainRow) => Record<string, string | null>,
  limit: number,
) {
  const by = new Map<string, Record<string, unknown>>();
  for (const r of rows) {
    const k = `${r.marketplace}\u0000${r.ad_program}\u0000${key(r)}`;
    let cur = by.get(k);
    if (!cur) {
      cur = {
        marketplace: r.marketplace,
        ad_program: r.ad_program,
        ...label(r),
        impressions: 0, clicks: 0, spend: 0,
        attributed_sales: 0, attributed_units: 0,
      };
      by.set(k, cur);
    }
    cur["impressions"] = (cur["impressions"] as number) + (Number(r.impressions) || 0);
    cur["clicks"] = (cur["clicks"] as number) + (Number(r.clicks) || 0);
    cur["spend"] = (cur["spend"] as number) + (Number(r.spend) || 0);
    cur["attributed_sales"] =
      (cur["attributed_sales"] as number) + (Number(r.attributed_sales) || 0);
    cur["attributed_units"] =
      (cur["attributed_units"] as number) + (Number(r.attributed_units) || 0);
  }
  return [...by.values()]
    .map((r) => ({
      ...r,
      // Rounded once, at the end. Rounding each row first and then adding
      // would drift away from ads_summary, which rounds the same way.
      spend: round(r["spend"] as number),
      attributed_sales: round(r["attributed_sales"] as number),
    }))
    .sort((a, b) => (b["spend"] as number) - (a["spend"] as number))
    .slice(0, limit);
}

async function freshness() {
  const rows = await select<FreshnessRow>(
    "ingest_runs",
    {
      select:
        "marketplace,source,status,covers_from,covers_to,rows_written,started_at,finished_at",
      status: "eq.ok",
      order: "started_at.desc",
    },
    200,
  );
  const latest = new Map<string, FreshnessRow>();
  for (const r of rows) {
    const key = `${r.marketplace}:${r.source}`;
    if (!latest.has(key)) latest.set(key, r);
  }
  return [...latest.values()];
}

export async function callTool(
  name: string,
  args: Record<string, unknown>,
): Promise<unknown> {
  switch (name) {
    case "sales_summary": {
      const rows = await salesRows(args);
      return {
        range: { from: args["from"], to: args["to"] },
        by_marketplace: totals(rows),
        note:
          rows.length === 0
            ? "No rows in this range. Call data_freshness to tell 'no sales' from 'not ingested'."
            : undefined,
      };
    }

    case "sales_by_sku": {
      const rows = await salesRows(args);
      const limitRaw = args["limit"];
      const limit =
        typeof limitRaw === "number" && limitRaw > 0
          ? Math.min(Math.floor(limitRaw), 200)
          : 25;
      const by = new Map<
        string,
        { sku: string; product_name: string | null; internal_code: string | null;
          marketplace: string; units: number; revenue: number }
      >();
      for (const r of rows) {
        const key = `${r.marketplace}:${r.sku}`;
        const cur = by.get(key) ?? {
          sku: r.sku,
          product_name: r.product_name,
          internal_code: r.internal_code,
          marketplace: r.marketplace,
          units: 0,
          revenue: 0,
        };
        cur.units += Number(r.units) || 0;
        cur.revenue += Number(r.revenue) || 0;
        by.set(key, cur);
      }
      const sorted = [...by.values()].sort((a, b) => b.units - a.units);
      return {
        range: { from: args["from"], to: args["to"] },
        skus_returned: Math.min(sorted.length, limit),
        skus_total: sorted.length,
        skus: sorted.slice(0, limit).map((s) => ({ ...s, revenue: round(s.revenue) })),
      };
    }

    case "sales_timeseries": {
      const rows = await salesRows(args);
      const by = new Map<string, { units: number; orders: number; revenue: number }>();
      for (const r of rows) {
        const key = `${r.sale_date}:${r.marketplace}`;
        const cur = by.get(key) ?? { units: 0, orders: 0, revenue: 0 };
        cur.units += Number(r.units) || 0;
        cur.orders += Number(r.orders) || 0;
        cur.revenue += Number(r.revenue) || 0;
        by.set(key, cur);
      }
      const days = [...by.entries()]
        .map(([key, t]) => {
          const [date, marketplace] = key.split(":");
          return { date, marketplace, ...t, revenue: round(t.revenue) };
        })
        .sort((a, b) => (a.date ?? "").localeCompare(b.date ?? ""));
      return { range: { from: args["from"], to: args["to"] }, days };
    }

    case "ads_summary": {
      const { query } = range(args, "ad_date");
      const rows = await select<{
        marketplace: string; ad_program: string; impressions: number;
        clicks: number; spend: number; attributed_sales: number;
        attributed_units: number;
      }>(
        "ads_daily",
        {
          ...query,
          select:
            "marketplace,ad_program,impressions,clicks,spend,attributed_sales,attributed_units",
        },
        ROW_CAP,
      );
      const by = new Map<string, Record<string, number>>();
      for (const r of rows) {
        const key = `${r.marketplace}:${r.ad_program}`;
        const cur = by.get(key) ?? {
          impressions: 0, clicks: 0, spend: 0,
          attributed_sales: 0, attributed_units: 0,
        };
        cur["impressions"] = (cur["impressions"] ?? 0) + (Number(r.impressions) || 0);
        cur["clicks"] = (cur["clicks"] ?? 0) + (Number(r.clicks) || 0);
        cur["spend"] = (cur["spend"] ?? 0) + (Number(r.spend) || 0);
        cur["attributed_sales"] =
          (cur["attributed_sales"] ?? 0) + (Number(r.attributed_sales) || 0);
        cur["attributed_units"] =
          (cur["attributed_units"] ?? 0) + (Number(r.attributed_units) || 0);
        by.set(key, cur);
      }
      const programs = [...by.entries()].map(([key, t]) => {
        const [marketplace, ad_program] = key.split(":");
        return {
          marketplace, ad_program,
          impressions: t["impressions"] ?? 0,
          clicks: t["clicks"] ?? 0,
          spend: round(t["spend"] ?? 0),
          attributed_sales: round(t["attributed_sales"] ?? 0),
          attributed_units: t["attributed_units"] ?? 0,
        };
      });
      return {
        range: { from: args["from"], to: args["to"] },
        programs,
        note:
          rows.length === 0
            ? "No advertising rows in this range. Amazon Ads is live, so check data_freshness before reading this as no spend; Walmart advertising has no available API route."
            : undefined,
      };
    }

    case "spend_summary": {
      const { query } = range(args, "day");
      const rows = await select<{
        marketplace: string; day: string; ad_spend: number; units_sold: number;
        revenue: number; cost_per_unit: number | null;
        ad_pct_of_revenue: number | null;
      }>(
        "spend_daily",
        {
          ...query,
          select:
            "marketplace,day,ad_spend,units_sold,revenue,cost_per_unit,ad_pct_of_revenue",
          order: "day.asc",
        },
        ROW_CAP,
      );
      return { range: { from: args["from"], to: args["to"] }, days: rows };
    }

    case "ads_by_campaign": {
      // Arguments are validated before anything is fetched: a bad limit
      // should be a refusal, not a refusal after a database round trip.
      const limit = cap(args, 50);
      const { query } = range(args, "ad_date");
      const rows = await select<AdGrainRow>(
        "ads_daily",
        {
          ...query,
          select:
            "marketplace,ad_program,campaign_id,campaign_name," +
            "impressions,clicks,spend,attributed_sales,attributed_units",
        },
        ROW_CAP,
      );
      return {
        range: { from: args["from"], to: args["to"] },
        campaigns: rollUp(
          rows,
          (r) => r.campaign_id ?? "",
          (r) => ({
            campaign_id: r.campaign_id ?? "",
            campaign_name: r.campaign_name ?? null,
          }),
          limit,
        ),
      };
    }

    case "ads_by_asin": {
      const limit = cap(args, 50);
      const { query } = range(args, "ad_date");
      const rows = await select<AdGrainRow>(
        "ads_advertised_product",
        {
          ...query,
          select:
            "marketplace,ad_program,asin,sku,campaign_id,campaign_name," +
            "impressions,clicks,spend,attributed_sales,attributed_units",
        },
        ROW_CAP,
      );
      return {
        range: { from: args["from"], to: args["to"] },
        // Keyed by ASIN and SKU together, because one ASIN is advertised
        // under more than one seller SKU here - the stickerless conversion
        // issued a second one - and collapsing them would hide that split.
        asins: rollUp(
          rows,
          (r) => `${r.asin ?? ""}\u0000${r.sku ?? ""}`,
          (r) => ({ asin: r.asin ?? "", sku: r.sku ?? "" }),
          limit,
        ),
      };
    }

    case "ad_change_history": {
      const limit = Math.min(cap(args, 200), ROW_CAP);
      const from = day(args["from"], "from");
      const to = day(args["to"], "to");
      if (from > to) throw new ToolError("`from` is after `to`");
      // changed_at is an instant, not a day, so the upper bound has to reach
      // the end of `to`. A lte on the bare date would silently drop every
      // change made after midnight on the last day of the range.
      const query: Record<string, string> = {
        and: `(changed_at.gte.${from}T00:00:00Z,changed_at.lt.${nextDay(to)}T00:00:00Z)`,
        order: "changed_at.desc",
      };
      const campaign = text(args["campaign_id"], "campaign_id");
      if (campaign) query["campaign_id"] = `eq.${campaign}`;
      const asin = text(args["asin"], "asin");
      if (asin) {
        // An ASIN is not a column: it turns up as the target of a product
        // target or the subject of an ad, so it is matched against the
        // entity id and the values on either side of the change.
        query["or"] =
          `(entity_id.eq.${asin},old_value.eq.${asin},new_value.eq.${asin})`;
      }
      const rows = await select<ChangeRow>(
        "ads_change_history",
        {
          ...query,
          select:
            "changed_at,entity_type,entity_id,entity_name,campaign_id," +
            "campaign_name,change_type,field_name,old_value,new_value,changed_by",
        },
        limit,
      );
      return {
        range: { from, to },
        filters: {
          campaign_id: campaign ?? undefined,
          asin: asin ?? undefined,
        },
        changes: rows,
        note:
          rows.length === 0
            ? "No changes recorded in this range. Amazon keeps roughly ninety days of change history and deletes the rest, so an older range being empty means the window closed. Call data_freshness to see how far back this table actually reaches."
            : undefined,
      };
    }

    case "data_freshness":
      return { last_successful_runs: await freshness() };

    default:
      throw new ToolError(`unknown tool: ${name}`);
  }
}
