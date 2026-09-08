import { createClient } from "@supabase/supabase-js";

// The anon key is meant to be public. It identifies the project; row level
// security decides what the signed-in user may read. The service key never
// appears in this bundle - it lives in GitHub Actions secrets and is used
// only by ingestion.
const url = import.meta.env.VITE_SUPABASE_URL;
const anon = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!url || !anon) {
  throw new Error(
    "VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY must be set at build time. " +
      "Copy .env.example to .env and fill them in.",
  );
}

export const supabase = createClient(url, anon, {
  auth: { persistSession: true, autoRefreshToken: true },
});
