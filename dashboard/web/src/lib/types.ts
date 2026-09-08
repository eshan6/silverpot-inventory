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
