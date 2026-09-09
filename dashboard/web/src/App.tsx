import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { SessionProvider, useSession } from "./lib/session";
import { supabase } from "./lib/supabase";
import { Shell } from "./components/Shell";
import Login from "./pages/Login";
import Sales from "./pages/Sales";
import Ads from "./pages/Ads";
import Spend from "./pages/Spend";
import Admin from "./pages/Admin";
import { isStaff } from "./lib/types";
import type { Marketplace } from "./lib/types";

/**
 * Walmart's switcher enables itself the first time Walmart ingestion succeeds.
 *
 * A hardcoded flag would mean the tab is either offered before there is
 * anything behind it, or stays disabled until someone remembers to flip it and
 * redeploy. Asking the run ledger is the same question the Freshness banner
 * already asks, and it answers itself.
 */
function useWalmartReady(): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let alive = true;
    supabase
      .from("ingest_runs")
      .select("id")
      .eq("marketplace", "walmart")
      .eq("status", "ok")
      .limit(1)
      .then(({ data }) => {
        if (alive) setReady((data ?? []).length > 0);
      });
    return () => { alive = false; };
  }, []);
  return ready;
}

function Routed() {
  const { session, profile, loading, unprovisioned } = useSession();
  const [marketplace, setMarketplace] = useState<Marketplace>("amazon");
  const walmartReady = useWalmartReady();

  if (loading && session) return <div className="loading">Loading…</div>;
  if (!session || unprovisioned || !profile) return <Login />;

  return (
    <Routes>
      <Route
        element={
          <Shell
            marketplace={marketplace}
            setMarketplace={setMarketplace}
            walmartReady={walmartReady}
          />
        }
      >
        <Route path="/sales" element={<Sales marketplace={marketplace} />} />
        <Route path="/ads" element={<Ads marketplace={marketplace} />} />
        <Route path="/spend" element={<Spend marketplace={marketplace} />} />
        <Route
          path="/admin"
          element={isStaff(profile.role) ? <Admin /> : <Navigate to="/sales" replace />}
        />
        <Route path="*" element={<Navigate to="/sales" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <SessionProvider>
      <BrowserRouter>
        <Routed />
      </BrowserRouter>
    </SessionProvider>
  );
}
