import { NavLink, Outlet } from "react-router-dom";
import { useSession } from "../lib/session";
import { isStaff } from "../lib/types";
import type { Marketplace, Role } from "../lib/types";

const ROLE_WORD: Record<Role, string> = {
  viewer: "Viewer",
  admin: "Admin",
  super_admin: "Super admin",
};

/**
 * Header, navigation and the marketplace switcher.
 *
 * Amazon and Walmart are never displayed together. One is selected, always,
 * and there is no "both" control to click - combining two marketplaces'
 * numbers into one figure would be meaningless for advertising and misleading
 * for sales.
 */
export function Shell({
  marketplace,
  setMarketplace,
  walmartReady,
}: {
  marketplace: Marketplace;
  setMarketplace: (m: Marketplace) => void;
  walmartReady: boolean;
}) {
  const { profile, signOut } = useSession();

  return (
    <div className="wrap">
      <header className="masthead">
        <div className="masthead-top">
          <h1>Silverpot — Marketplace Dashboard</h1>
          <div className="who">
            {profile?.email}
            {profile && profile.role !== "viewer" ? ` · ${ROLE_WORD[profile.role]}` : ""}
            <button onClick={signOut}>Sign out</button>
          </div>
        </div>
        <nav>
          <NavLink to="/sales" className={({ isActive }) => (isActive ? "on" : "")}>Sales</NavLink>
          <NavLink to="/ads" className={({ isActive }) => (isActive ? "on" : "")}>Ads</NavLink>
          <NavLink to="/spend" className={({ isActive }) => (isActive ? "on" : "")}>Spend</NavLink>
          {isStaff(profile?.role) && (
            <NavLink to="/admin" className={({ isActive }) => (isActive ? "on" : "")}>Admin</NavLink>
          )}
          <div className="markets">
            <button
              className={marketplace === "amazon" ? "on" : ""}
              onClick={() => setMarketplace("amazon")}
            >
              Amazon
            </button>
            <button
              className={marketplace === "walmart" ? "on" : ""}
              onClick={() => setMarketplace("walmart")}
              disabled={!walmartReady}
              title={walmartReady ? "" : "Walmart ingestion is not built yet"}
            >
              Walmart
            </button>
          </div>
        </nav>
      </header>
      <Outlet />
    </div>
  );
}
