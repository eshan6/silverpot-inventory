import { useState } from "react";
import type { FormEvent } from "react";
import { supabase } from "../lib/supabase";
import { useSession } from "../lib/session";

/**
 * Invite-only. There is deliberately no sign-up form: accounts are created by
 * a super admin or an admin, and someone who reaches this page without one
 * cannot make themselves an account by filling anything in.
 */
export default function Login() {
  const { unprovisioned, signOut } = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
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
        <p className="note">Ask a dashboard administrator to add you.</p>
      </div>
    );
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) {
      // Deliberately not distinguishing "no such account" from "wrong
      // password": telling an anonymous visitor which emails exist is free
      // reconnaissance.
      setErr("That email and password did not match an account.");
      setBusy(false);
    }
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
          <input id="password" type="password" autoComplete="current-password"
                 required value={password}
                 onChange={(e) => setPassword(e.target.value)} />
        </div>
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="note">Access is by invitation. There is no sign-up.</p>
    </div>
  );
}
