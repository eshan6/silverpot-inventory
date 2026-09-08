import { useState } from "react";
import type { FormEvent } from "react";
import { supabase } from "../lib/supabase";
import { useSession } from "../lib/session";

/**
 * Sign in, or claim an invite.
 *
 * There is a sign-up form, and it is not a hole. Supabase can only create
 * users with the service key, and that key must never reach a browser, so
 * "the admin creates the account" is not something a static app can do. What
 * happens instead is that an admin records an invite and the invited person
 * signs up themselves; a trigger on auth.users matches the address and
 * provisions the profile.
 *
 * Signing up without an invite therefore produces an authenticated user with
 * no profile, which every policy in the database treats as no access at all -
 * they land on the "No access" panel below and can read nothing. That is
 * deliberately indistinguishable from an invite that has not been recorded
 * yet: refusing the signup outright would tell a stranger which addresses are
 * known, which is free reconnaissance.
 */
export default function Login() {
  const { unprovisioned, signOut } = useSession();
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  if (unprovisioned) {
    // Authenticated but not provisioned - or deactivated. Bouncing them back
    // to a login form they already completed would be a lie about what is
    // wrong, and they would try the same password forever.
    return (
      <div className="login">
        <h1>No access</h1>
        <p>
          You are signed in, but this account has not been given access to the
          dashboard, or its access has been withdrawn.
        </p>
        <button className="btn ghost" onClick={signOut}>Sign out</button>
        <p className="note">
          If you were invited, check that you signed up with exactly the address
          the invite was sent to. Otherwise, ask a dashboard administrator to
          add you.
        </p>
      </div>
    );
  }

  if (sent) {
    return (
      <div className="login">
        <h1>Check your email</h1>
        <p>
          If confirmation is switched on for this project, there is a link
          waiting at {email}. Open it, then sign in.
        </p>
        <button className="btn ghost" onClick={() => { setSent(false); setMode("in"); }}>
          Back to sign in
        </button>
      </div>
    );
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);

    if (mode === "in") {
      const { error } = await supabase.auth.signInWithPassword({ email, password });
      if (error) {
        // Deliberately not distinguishing "no such account" from "wrong
        // password": telling an anonymous visitor which emails exist is free
        // reconnaissance.
        setErr("That email and password did not match an account.");
        setBusy(false);
      }
      return;
    }

    const { data, error } = await supabase.auth.signUp({ email, password });
    if (error) {
      setErr(error.message);
      setBusy(false);
      return;
    }
    // No session back means Supabase is waiting on an email confirmation.
    if (!data.session) setSent(true);
    setBusy(false);
  };

  return (
    <div className="login">
      <h1>Silverpot</h1>
      <p>Marketplace sales, advertising and spend.</p>
      <form onSubmit={submit}>
        {err && <div className="err">{err}</div>}
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" required
                 value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" required value={password}
                 autoComplete={mode === "in" ? "current-password" : "new-password"}
                 minLength={mode === "up" ? 8 : undefined}
                 onChange={(e) => setPassword(e.target.value)} />
        </div>
        <button className="btn" type="submit" disabled={busy}>
          {busy
            ? (mode === "in" ? "Signing in…" : "Creating…")
            : (mode === "in" ? "Sign in" : "Create account")}
        </button>
      </form>
      <p className="note">
        {mode === "in" ? (
          <>
            Invited but have not set a password yet?{" "}
            <button className="linkish" onClick={() => { setMode("up"); setErr(null); }}>
              Create your account
            </button>
            . Access is by invitation; an account without one sees nothing.
          </>
        ) : (
          <>
            Use the exact address your invite was recorded against.{" "}
            <button className="linkish" onClick={() => { setMode("in"); setErr(null); }}>
              Back to sign in
            </button>
            .
          </>
        )}
      </p>
    </div>
  );
}
