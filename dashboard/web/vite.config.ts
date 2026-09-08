import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// A plain static build. No server-side anything: auth and data come from
// Supabase, ingestion from GitHub Actions. That is what lets this deploy
// unchanged to Cloudflare Pages, Netlify or Vercel, and lets the host be a
// reversible decision rather than an architectural one.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
});
