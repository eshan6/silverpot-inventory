// Vercel's edge runtime exposes `process.env` and nothing else from Node.
//
// Declaring just that is deliberate: pulling in @types/node would also bring
// Node's `fetch`, `Request` and `Response` types, which are not the Web ones
// these functions actually run against, and the mismatch surfaces as puzzling
// type errors rather than as a missing dependency.

declare const process: {
  env: Record<string, string | undefined>;
};
