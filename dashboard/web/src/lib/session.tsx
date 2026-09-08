import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";
import type { Profile } from "./types";

/**
 * Who is signed in, and what they are allowed to do.
 *
 * The role here drives what the UI *offers*. It is not what enforces anything:
 * a viewer who edits this value in devtools still gets nothing back, because
 * the row level security policies in supabase/migrations decide what the
 * database will answer. Treat everything in this file as convenience.
 *
 * Signing in is not the same as having access. Accounts are invite-only, so a
 * valid Supabase user with no profile row - or a deactivated one - is
 * authenticated and entitled to nothing. That is a distinct state from
 * "logged out" and the UI says so rather than bouncing them to a login form
 * they have already completed.
 */
interface SessionState {
  session: Session | null;
  profile: Profile | null;
  loading: boolean;
  /** Authenticated, but with no active profile: invited but not provisioned. */
  unprovisioned: boolean;
  signOut: () => Promise<void>;
}

const Ctx = createContext<SessionState>({
  session: null,
  profile: null,
  loading: true,
  unprovisioned: false,
  signOut: async () => {},
});

export const useSession = () => useContext(Ctx);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    let alive = true;

    supabase.auth.getSession().then(({ data }) => {
      if (alive) setSession(data.session);
    });

    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => {
      if (!alive) return;
      setSession(s);
      setChecked(false); // re-resolve the profile for whoever this now is
    });

    return () => {
      alive = false;
      sub.subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    let alive = true;

    if (!session) {
      setProfile(null);
      setLoading(false);
      setChecked(true);
      return;
    }

    setLoading(true);
    supabase
      .from("profiles")
      .select("id, email, full_name, role, is_active")
      .eq("id", session.user.id)
      .maybeSingle()
      .then(({ data }) => {
        if (!alive) return;
        // An inactive profile is treated as no profile. Deactivation is how
        // access is revoked, and it has to mean the same thing here as it
        // does in the policies.
        setProfile(data && data.is_active ? (data as Profile) : null);
        setLoading(false);
        setChecked(true);
      });

    return () => {
      alive = false;
    };
  }, [session]);

  const value: SessionState = {
    session,
    profile,
    loading,
    unprovisioned: Boolean(session) && checked && !loading && !profile,
    signOut: async () => {
      await supabase.auth.signOut();
    },
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
