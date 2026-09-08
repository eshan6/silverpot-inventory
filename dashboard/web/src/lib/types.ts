export type Role = "viewer" | "admin" | "super_admin";
export type Marketplace = "amazon" | "walmart";
export type Section = "sales" | "ads" | "spend";

export interface Profile {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  is_active: boolean;
}

export interface SalesRow {
  sale_date: string;
  sku: string;
  internal_code: string | null;
  product_name: string | null;
  units: number;
  orders: number;
  revenue: number;
}

export type AdProgram = "sponsored_products" | "sponsored_brands" | "sponsored_display";

export const AD_PROGRAMS: { key: AdProgram; label: string }[] = [
  { key: "sponsored_products", label: "Sponsored Products" },
  { key: "sponsored_brands", label: "Sponsored Brands" },
  { key: "sponsored_display", label: "Sponsored Display" },
];

export interface AdsRow {
  ad_date: string;
  ad_program: AdProgram;
  campaign_id: string;
  campaign_name: string | null;
  ad_group: string;
  sku: string;
  impressions: number;
  clicks: number;
  spend: number;
  attributed_sales: number;
  attributed_units: number;
}

export interface SearchTermRow {
  ad_date: string;
  ad_program: AdProgram;
  campaign_id: string;
  search_term: string;
  match_type: string;
  impressions: number;
  clicks: number;
  spend: number;
  attributed_sales: number;
  attributed_units: number;
}

/** From the spend_daily view: spend over units actually sold, not attributed. */
export interface SpendRow {
  day: string;
  ad_spend: number;
  units_sold: number;
  revenue: number;
  attributed_sales: number;
  attributed_units: number;
  cost_per_unit: number | null;
  ad_pct_of_revenue: number | null;
}

export interface Invite {
  email: string;
  role: Role;
  created_at: string;
  claimed_at: string | null;
}

export interface AuditEntry {
  id: number;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface IngestRun {
  source: string;
  status: "running" | "ok" | "failed";
  covers_from: string | null;
  covers_to: string | null;
  finished_at: string | null;
  started_at: string;
  error: string | null;
}

export interface SavedView {
  id: string;
  section: Section;
  marketplace: Marketplace;
  name: string;
  config: Record<string, unknown>;
  is_default: boolean;
}

export const isStaff = (r: Role | undefined) => r === "admin" || r === "super_admin";
export const isSuper = (r: Role | undefined) => r === "super_admin";
