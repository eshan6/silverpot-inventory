import { useState } from "react";
import { useSavedViews } from "../lib/views";
import type { Marketplace, Section } from "../lib/types";

/**
 * The saved-views strip that sits under a section's controls.
 *
 * Applying a view hands the stored config back to the page, which merges it
 * over its own defaults. A view saved before a control existed therefore
 * still applies cleanly, with the new control left at its default rather than
 * arriving as undefined.
 */
export function SavedViews<T extends Record<string, unknown>>({
  section,
  marketplace,
  current,
  onApply,
}: {
  section: Section;
  marketplace: Marketplace;
  current: T;
  onApply: (config: Partial<T>) => void;
}) {
  const { views, loaded, err, save, remove } = useSavedViews<T>(section, marketplace);
  const [name, setName] = useState("");
  const [asDefault, setAsDefault] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!loaded) return null;

  const submit = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    await save(name, current, asDefault);
    setBusy(false);
    setName("");
    setAsDefault(false);
  };

  return (
    <div className="views">
      <span className="views-label">Saved views</span>

      {views.length === 0 && <span className="views-none">none yet</span>}

      {views.map((v) => (
        <span key={v.id} className="chip">
          <button onClick={() => onApply(v.config as Partial<T>)}>
            {v.name}
            {v.is_default ? " ·" : ""}
          </button>
          <button
            className="x"
            title={`Delete "${v.name}"`}
            onClick={() => void remove(v.id)}
          >
            ×
          </button>
        </span>
      ))}

      <span className="views-save">
        <input
          value={name}
          placeholder="Save these settings as…"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
        />
        <label title="Open this section with these settings">
          <input
            type="checkbox"
            checked={asDefault}
            onChange={(e) => setAsDefault(e.target.checked)}
          />
          default
        </label>
        <button className="btn ghost" disabled={!name.trim() || busy}
                onClick={() => void submit()}>
          Save
        </button>
      </span>

      {err && <span className="views-err">{err}</span>}
    </div>
  );
}
