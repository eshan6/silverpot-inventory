import { useEffect, useState } from "react";
import { supabase } from "../lib/supabase";
import { agoWords } from "../lib/format";
import type { IngestRun, Marketplace } from "../lib/types";

/**
 * When the numbers on this page were last refreshed, and whether that went
 * wrong.
 *
 * The inventory pipeline in this same project published a stale storefront for
 * 31 hours after a Google Sheets outage and nothing on the page said so. A
 * dashboard that silently shows old figures is worse than one that admits it,
 * so this is on every section rather than tucked into a settings screen.
 *
 * It reports per *source*, not per marketplace. Sales and advertising are
 * separate jobs with separate credentials and separate ways to fail: the Ads
 * API can be denied for a week while Orders keeps working. Showing the newest
 * run of either would let a healthy sales pull vouch for advertising figures
 * that stopped updating - exactly the reassurance this component exists to
 * refuse. The Spend page reads both and so declares both.
 */
const LABEL: Record<string, string> = {
  "sp-api-orders": "Sales",
  "ads-api": "Advertising",
};

const MARKET: Record<string, string> = {
  amazon: "Amazon",
  walmart: "Walmart",
};

function Line({ marketplace, source }: { marketplace: Marketplace; source: string }) {
  const [run, setRun] = useState<IngestRun | null>(null);
  const [loaded, setLoaded] = useState(false);
  const what = LABEL[source] ?? source;
  const where = MARKET[marketplace] ?? marketplace;

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    supabase
      .from("ingest_runs")
      .select("source, status, covers_from, covers_to, started_at, finished_at, error")
      .eq("marketplace", marketplace)
      .eq("source", source)
      .order("started_at", { ascending: false })
      .limit(1)
      .maybeSingle()
      .then(({ data }) => {
        if (!alive) return;
        setRun((data as IngestRun) ?? null);
        setLoaded(true);
      });
    return () => { alive = false; };
  }, [marketplace, source]);

  if (!loaded) return null;

  if (!run) {
    return (
      <div className="banner bad">
        {what} data has never been ingested for {where}. Every figure below is
        absent rather than zero.
      </div>
    );
  }

  if (run.status === "failed") {
    return (
      <div className="banner bad">
        Last {where} {what.toLowerCase()} sync failed{" "}
        {agoWords(run.finished_at ?? run.started_at)}
        {run.error ? ` — ${run.error}` : ""}. Figures below are from before that.
      </div>
    );
  }

  const stamp = run.finished_at ?? run.started_at;
  const hours = (Date.now() - new Date(stamp).getTime()) / 3.6e6;
  // The job runs twice daily, so more than a day and a half without a
  // successful run means something is wrong, not merely late.
  const stale = hours > 36;

  return (
    <div className={`banner${stale ? " stale" : ""}`}>
      {where} {what.toLowerCase()} data as of {agoWords(stamp)}
      {run.covers_to ? `, covering through ${run.covers_to}` : ""}
      {stale ? " — that is older than expected for a twice-daily sync." : "."}
    </div>
  );
}

export function Freshness({
  marketplace,
  sources = ["sp-api-orders"],
}: {
  marketplace: Marketplace;
  sources?: string[];
}) {
  return (
    <>
      {sources.map((s) => (
        <Line key={s} marketplace={marketplace} source={s} />
      ))}
    </>
  );
}
