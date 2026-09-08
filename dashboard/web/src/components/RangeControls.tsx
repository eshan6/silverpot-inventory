import type { ReactNode } from "react";
import { PRESETS, rangeFor } from "../lib/range";
import type { Preset, Range } from "../lib/range";

/**
 * Preset buttons and the two date fields, shared by all three sections.
 *
 * Shared so that "30 days" cannot come to mean one thing on Sales and another
 * on Spend. Comparing cost per unit against units sold is the whole point of
 * this dashboard, and two pages disagreeing about which days they cover would
 * make that comparison quietly wrong.
 */
export function RangeControls({
  preset,
  range,
  onChange,
  children,
}: {
  preset: Preset;
  range: Range;
  onChange: (preset: Preset, range: Range) => void;
  children?: ReactNode;
}) {
  return (
    <div className="controls">
      <div className="presets">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            className={preset === p.key ? "on" : ""}
            onClick={() => onChange(p.key, p.key === "custom" ? range : rangeFor(p.key))}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="field">
        <label htmlFor="from">From</label>
        <input
          id="from"
          type="date"
          value={range.from}
          onChange={(e) => onChange("custom", { ...range, from: e.target.value })}
        />
      </div>
      <div className="field">
        <label htmlFor="to">To</label>
        <input
          id="to"
          type="date"
          value={range.to}
          onChange={(e) => onChange("custom", { ...range, to: e.target.value })}
        />
      </div>
      {children}
    </div>
  );
}
