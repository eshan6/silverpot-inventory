// PostgREST access for the serverless functions, with the service key.
//
// The browser never reaches this file. The service key bypasses row-level
// security, which is exactly why it lives only in Vercel's environment and
// why every query in tools.ts is a read of an aggregate fact table - there is
// no code path here that reads `profiles`, `invites` or the audit log.

const URL_ENV = "DASHBOARD_SUPABASE_URL";
const KEY_ENV = "DASHBOARD_SUPABASE_SERVICE_KEY";

export function missingEnv(): string[] {
  return [URL_ENV, KEY_ENV].filter((k) => !process.env[k]);
}

function base(): string {
  return (process.env[URL_ENV] ?? "").replace(/\/+$/, "");
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const key = process.env[KEY_ENV] ?? "";
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

export class DbError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    // Postgres error text can name a column or a constraint. That is useful in
    // a Vercel log and must never reach an MCP client, so callers turn this
    // into a generic message; only the status travels.
    super(message);
    this.status = status;
  }
}

/**
 * One PostgREST GET. `query` is passed through as-is, so callers build their
 * own filters - `select`, `marketplace=eq.amazon`, `sale_date=gte.2026-01-01`.
 *
 * `limit` is mandatory rather than optional. A missing limit on a table that
 * grows daily is a query that works in testing and times out in a year.
 */
export async function select<T = Record<string, unknown>>(
  table: string,
  query: Record<string, string>,
  limit: number,
): Promise<T[]> {
  const params = new URLSearchParams(query);
  params.set("limit", String(limit));
  const resp = await fetch(`${base()}/rest/v1/${table}?${params}`, {
    headers: headers({ Prefer: "count=none" }),
  });
  if (!resp.ok) {
    throw new DbError(resp.status, `${table}: ${await resp.text()}`);
  }
  return (await resp.json()) as T[];
}

export async function insert(
  table: string,
  row: Record<string, unknown>,
): Promise<void> {
  const resp = await fetch(`${base()}/rest/v1/${table}`, {
    method: "POST",
    headers: headers({ Prefer: "return=minimal" }),
    body: JSON.stringify(row),
  });
  if (!resp.ok) throw new DbError(resp.status, `${table}: ${await resp.text()}`);
}

export async function patch(
  table: string,
  query: Record<string, string>,
  changes: Record<string, unknown>,
): Promise<void> {
  const params = new URLSearchParams(query);
  const resp = await fetch(`${base()}/rest/v1/${table}?${params}`, {
    method: "PATCH",
    headers: headers({ Prefer: "return=minimal" }),
    body: JSON.stringify(changes),
  });
  if (!resp.ok) throw new DbError(resp.status, `${table}: ${await resp.text()}`);
}
