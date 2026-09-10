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

import { select } from "./db.ts";

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
            ? "No advertising rows. Amazon Ads is pending API approval and Walmart advertising has no available API route."
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

    case "data_freshness":
      return { last_successful_runs: await freshness() };

    default:
      throw new ToolError(`unknown tool: ${name}`);
  }
}
