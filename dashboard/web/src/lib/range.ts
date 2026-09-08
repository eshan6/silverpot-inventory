import { addDays, todayET } from "./format";

export type Preset = "7d" | "30d" | "mtd" | "custom";

export const PRESETS: { key: Preset; label: string }[] = [
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "mtd", label: "Month" },
  { key: "custom", label: "Custom" },
];

export interface Range {
  from: string;
  to: string;
}

/**
 * Ranges end yesterday. Today is a partial Eastern day everywhere in this
 * app - ingestion buckets by America/New_York and runs in the morning - so
 * including it would draw a slump every single day and make advertising look
 * like it stopped working by lunchtime.
 */
export function rangeFor(preset: Preset): Range {
  const to = addDays(todayET(), -1);
  if (preset === "7d") return { from: addDays(to, -6), to };
  if (preset === "30d") return { from: addDays(to, -29), to };
  if (preset === "mtd") return { from: `${to.slice(0, 7)}-01`, to };
  return { from: addDays(to, -29), to };
}

export const dayCount = (r: Range): number => {
  const [fy, fm, fd] = r.from.split("-").map(Number);
  const [ty, tm, td] = r.to.split("-").map(Number);
  const a = Date.UTC(fy ?? 1970, (fm ?? 1) - 1, fd ?? 1);
  const b = Date.UTC(ty ?? 1970, (tm ?? 1) - 1, td ?? 1);
  return Math.max(0, Math.round((b - a) / 86400000) + 1);
};
