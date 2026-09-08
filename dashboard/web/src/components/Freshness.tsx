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
 */
export function Freshness({ marketplace }: { marketplace: Marketplace }) {
  const [run, setRun] = useState<IngestRun | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    supabase
      .from("ingest_runs")
      .select("source, status, covers_from, covers_to, started_at, finished_at, error")
      .eq("marketplace", marketplace)
      .order("started_at", { ascending: false })
      .limit(1)
      .maybeSingle()
      .then(({ data }) => {
        if (!alive) return;
        setRun((data as IngestRun) ?? null);
        setLoaded(true);
      });
    return () => { alive = false; };
  }, [marketplace]);

  if (!loaded) return null;

  if (!run) {
    return (
      <div className="banner bad">
        No ingestion has ever run for {marketplace}. Every figure below is
        absent rather than zero.
      </div>
    );
  }

  if (run.status === "failed") {
    return (
      <div className="banner bad">
        Last {marketplace} sync failed {agoWords(run.finished_at ?? run.started_at)}
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
      {marketplace} data as of {agoWords(stamp)}
      {run.covers_to ? `, covering through ${run.covers_to}` : ""}
      {stale ? " — that is older than expected for a twice-daily sync." : "."}
    </div>
  );
}
