import { useCallback, useEffect, useState } from "react";
import { supabase } from "./supabase";
import { useSession } from "./session";
import type { Marketplace, SavedView, Section } from "./types";

/**
 * Saved views: a named set of controls, kept per person across logins.
 *
 * Stored in Postgres rather than in localStorage, because "across logins" was
 * the actual request - a view saved on a laptop has to be there on a phone.
 * The saved_views policies make each row private to its owner, so this hook
 * never filters by owner itself: asking for every row returns only your own.
 *
 * The config is opaque jsonb on purpose. Each section decides what its own
 * controls mean, and adding a control later does not need a migration. The
 * cost is that a view saved before a control existed comes back without it,
 * which is why applying a view merges over the section's defaults rather than
 * replacing them wholesale.
 */
export function useSavedViews<T extends Record<string, unknown>>(
  section: Section,
  marketplace: Marketplace,
) {
  const { profile } = useSession();
  const [views, setViews] = useState<SavedView[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(async () => {
    const { data, error } = await supabase
      .from("saved_views")
      .select("id, section, marketplace, name, config, is_default")
      .eq("section", section)
      .eq("marketplace", marketplace)
      .order("name");
    if (error) setErr(error.message);
    else setViews((data ?? []) as SavedView[]);
    setLoaded(true);
  }, [section, marketplace]);

  useEffect(() => {
    if (!profile) return;
    void reload();
  }, [profile, reload]);

  const save = useCallback(
    async (name: string, config: T, isDefault: boolean) => {
      if (!profile) return;
      setErr(null);
      // One default per section per marketplace is a unique index, so an
      // existing default has to stand down first. Doing it here rather than
      // relying on the insert failing keeps the error reserved for real
      // problems.
      if (isDefault) {
        await supabase
          .from("saved_views")
          .update({ is_default: false })
          .eq("section", section)
          .eq("marketplace", marketplace)
          .eq("is_default", true);
      }
      const { error } = await supabase.from("saved_views").upsert(
        {
          owner_id: profile.id,
          section,
          marketplace,
          name: name.trim(),
          config,
          is_default: isDefault,
        },
        { onConflict: "owner_id,section,marketplace,name" },
      );
      if (error) setErr(error.message);
      await reload();
    },
    [profile, section, marketplace, reload],
  );

  const remove = useCallback(
    async (id: string) => {
      const { error } = await supabase.from("saved_views").delete().eq("id", id);
      if (error) setErr(error.message);
      await reload();
    },
    [reload],
  );

  const fallback = views.find((v) => v.is_default) ?? null;

  return { views, defaultView: fallback, loaded, err, save, remove };
}
