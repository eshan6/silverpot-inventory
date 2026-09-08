// Every date in this app is an America/New_York calendar day, because that is
// how the ingestion buckets them. Formatting goes through here so no component
// can accidentally reintroduce the browser's local timezone and shift a day.

export const money = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD" });

export const int = (n: number) => n.toLocaleString("en-US");

/** A metric with no answer renders as a dash, never as zero. */
export const ratio = (n: number | null | undefined, prefix = "$") =>
  n === null || n === undefined ? "—" : `${prefix}${n.toFixed(2)}`;

export const pct = (n: number | null | undefined) =>
  n === null || n === undefined ? "—" : `${n.toFixed(1)}%`;

/** "2026-09-07" -> "Sep 7". Parsed as parts, never through Date(string). */
export const shortDay = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[m - 1]} ${d}`;
};

/** Today in the seller's timezone, as YYYY-MM-DD. */
export const todayET = (): string => {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
  return parts;
};

export const addDays = (iso: string, delta: number): string => {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1));
  dt.setUTCDate(dt.getUTCDate() + delta);
  return dt.toISOString().slice(0, 10);
};

/** How stale a timestamp is, in plain words. */
export const agoWords = (iso: string | null): string => {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
};
