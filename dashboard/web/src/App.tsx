import { useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { SessionProvider, useSession } from "./lib/session";
import { Shell } from "./components/Shell";
import Login from "./pages/Login";
import Sales from "./pages/Sales";
import Ads from "./pages/Ads";
import Spend from "./pages/Spend";
import Admin from "./pages/Admin";
import { isStaff } from "./lib/types";
import type { Marketplace } from "./lib/types";

// Walmart is modelled throughout but not ingested yet, so its switcher is
// present and disabled rather than absent: it says "later", not "never".
const WALMART_READY = false;

function Routed() {
  const { session, profile, loading, unprovisioned } = useSession();
  const [marketplace, setMarketplace] = useState<Marketplace>("amazon");

  if (loading && session) return <div className="loading">Loading…</div>;
  if (!session || unprovisioned || !profile) return <Login />;

  return (
    <Routes>
      <Route
        element={
          <Shell
            marketplace={marketplace}
            setMarketplace={setMarketplace}
            walmartReady={WALMART_READY}
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
