/// <reference types="vite/client" />

// Declared rather than cast, so a typo in a variable name is a compile error
// instead of an undefined at runtime. Only VITE_-prefixed values reach the
// bundle, which is the guard that keeps a service key from ever being built
// into something a browser downloads.
interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL: string;
  readonly VITE_SUPABASE_ANON_KEY: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
