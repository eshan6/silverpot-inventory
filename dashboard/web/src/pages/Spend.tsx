import { useEffect, useMemo, useState } from "react";
import { supabase } from "../lib/supabase";
import { Freshness } from "../components/Freshness";
import { RangeControls } from "../components/RangeControls";
import { SavedViews } from "../components/SavedViews";
import { int, money, pct, ratio, shortDay } from "../lib/format";
import { rangeFor } from "../lib/range";
import type { Preset, Range } from "../lib/range";
import type { Marketplace, SpendRow } from "../lib/types";

/**
 * Ad spend over units actually sold. The question this dashboard was built to
 * answer: are we paying $4 for every sale, or more?
 *
 * The arithmetic lives in the spend_daily view rather than here, so that spend
 * covers every ad program and units come from Orders rather than from
 * attribution. Both mistakes flatter the number in the same direction - a
 * short numerator or an inflated denominator makes overspending look fine -
 * which is the direction it must not be wrong in.
 *
 * The headline figure is computed over the whole range, not averaged from the
 * daily column. A day with two sales and a day with two hundred are not worth
 * the same, and averaging their ratios would say they were.
 */
type ViewConfig = {
  preset: Preset;
  from: string;
  to: string;
  target: number;
};

/** What Eshan is checking against: "$4 for every sale, not more". */
const DEFAULT_TARGET = 4;

export default function Spend({ marketplace }: { marketplace: Marketplace }) {
  const [preset, setPreset] = useState<Preset>("30d");
  const [range, setRange] = useState<Range>(() => rangeFor("30d"));
  const [target, setTarget] = useState(DEFAULT_TARGET);
  const [rows, setRows] = useState<SpendRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setRows(null);
    setErr(null);
    supabase
      .from("spend_daily")
      .select("day, ad_spend, units_sold, revenue, attributed_sales, attributed_units, cost_per_unit, ad_pct_of_revenue")
      .eq("marketplace", marketplace)
      .gte("day", range.from)
      .lte("day", range.to)
      .order("day")
      .then(({ data, error }) => {
        if (!alive) return;
        if (error) setErr(error.message);
        else setRows((data ?? []) as SpendRow[]);
      });
    return () => { alive = false; };
  }, [marketplace, range.from, range.to]);

  const totals = useMemo(() => {
    const t = (rows ?? []).reduce(
      (n, r) => ({
        spend: n.spend + Number(r.ad_spend),
        units: n.units + r.units_sold,
        revenue: n.revenue + Number(r.revenue),
        attributedUnits: n.attributedUnits + r.attributed_units,
        attributedSales: n.attributedSales + Number(r.attributed_sales),
      }),
      { spend: 0, units: 0, revenue: 0, attributedUnits: 0, attributedSales: 0 },
    );
    return {
      ...t,
      costPerUnit: t.units > 0 ? t.spend / t.units : null,
      pctOfRevenue: t.revenue > 0 ? (100 * t.spend) / t.revenue : null,
      // The same figure counting only units advertising took credit for. Shown
      // beside the real one because the gap between them is the point: if
      // organic sales are carrying the business, the attributed figure alone
      // would say advertising is far more expensive than it is.
      costPerAttributedUnit:
        t.attributedUnits > 0 ? t.spend / t.attributedUnits : null,
    };
  }, [rows]);

  const withSpend = useMemo(
    () => (rows ?? []).filter((r) => Number(r.ad_spend) > 0 || r.units_sold > 0),
    [rows],
  );

  const peak = Math.max(
    0.01,
    ...withSpend.map((r) => r.cost_per_unit ?? 0),
    target,
  );

  const current: ViewConfig = { preset, from: range.from, to: range.to, target };

  const apply = (c: Partial<ViewConfig>) => {
    if (c.target !== undefined) setTarget(c.target);
    if (c.preset && c.preset !== "custom") {
      setPreset(c.preset);
      setRange(rangeFor(c.preset));
    } else if (c.from && c.to) {
      setPreset("custom");
      setRange({ from: c.from, to: c.to });
    }
  };

  const over = totals.costPerUnit !== null && totals.costPerUnit > target;
  const daysOver = withSpend.filter(
    (r) => r.cost_per_unit !== null && Number(r.cost_per_unit) > target,
  ).length;

  return (
    <>
      <Freshness marketplace={marketplace} sources={["sp-api-orders", "ads-api"]} />

      <RangeControls
        preset={preset}
        range={range}
        onChange={(p, r) => { setPreset(p); setRange(r); }}
      >
        <div className="field">
          <label htmlFor="target">Target $/unit</label>
          <input
            id="target"
            type="number"
            min="0"
            step="0.25"
            value={target}
            style={{ width: "5.5rem" }}
            onChange={(e) => setTarget(Math.max(0, Number(e.target.value)))}
          />
        </div>
      </RangeControls>

      <SavedViews<ViewConfig>
        section="spend"
        marketplace={marketplace}
        current={current}
        onApply={apply}
      />

      {err && <div className="banner bad">Could not load spend: {err}</div>}

      {rows === null && !err && <div className="loading">Loading…</div>}

      {rows !== null && withSpend.length === 0 && (
        <div className="empty">
          <strong>Nothing to compare in this range.</strong>
          Cost per unit needs both halves — ad spend and units sold — and at
          least one of them has no rows between {range.from} and {range.to}.
          A figure built from one half would read better than reality.
        </div>
      )}

      {rows !== null && withSpend.length > 0 && (
        <>
          <div className="totals">
            <div className={`stat ${over ? "risk" : "good"}`}>
              <div className={`n${totals.costPerUnit === null ? " none" : ""}`}>
                {ratio(totals.costPerUnit)}
              </div>
              <div className="l">Ad spend per unit sold</div>
              <div className="d">
                target {ratio(target)} · {over ? "over" : "within"}
              </div>
            </div>
            <div className="stat">
              <div className="n">{money(totals.spend)}</div>
              <div className="l">Total ad spend</div>
              <div className="d">{range.from} → {range.to}</div>
            </div>
            <div className="stat">
              <div className="n">{int(totals.units)}</div>
              <div className="l">Total units sold</div>
              <div className="d">all orders, not just attributed</div>
            </div>
            <div className="stat">
              <div className="n">{pct(totals.pctOfRevenue)}</div>
              <div className="l">Ad spend as % of revenue</div>
              <div className="d">on {money(totals.revenue)}</div>
            </div>
            <div className="stat">
              <div className={`n${totals.costPerAttributedUnit === null ? " none" : ""}`}>
                {ratio(totals.costPerAttributedUnit)}
              </div>
              <div className="l">Per attributed unit</div>
              <div className="d">{int(totals.attributedUnits)} of {int(totals.units)} claimed by ads</div>
            </div>
          </div>

          {daysOver > 0 && (
            <div className="banner stale">
              {daysOver} of {withSpend.length} day{withSpend.length === 1 ? "" : "s"} in
              this range cost more than {ratio(target)} per unit.
            </div>
          )}

          <h2 className="section-head">Cost per unit, by day</h2>
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th style={{ textAlign: "left", width: "26%" }}>vs target</th>
                <th className="num">Spend</th>
                <th className="num">Units</th>
                <th className="num">$/unit</th>
                <th className="num">% of revenue</th>
              </tr>
            </thead>
            <tbody>
              {withSpend.map((r) => {
                const cpu = r.cost_per_unit === null ? null : Number(r.cost_per_unit);
                return (
                  <tr key={r.day}>
                    <td className="name">{shortDay(r.day)}</td>
                    <td>
                      <div className="bar">
                        <span
                          className={cpu !== null && cpu > target ? "seg-over" : "seg-b"}
                          style={{ width: `${((cpu ?? 0) / peak) * 100}%` }}
                        />
                      </div>
                    </td>
                    <td className="num">{money(Number(r.ad_spend))}</td>
                    <td className="num">{int(r.units_sold)}</td>
                    <td className={`num${cpu !== null && cpu > target ? " over" : ""}`}>
                      {ratio(cpu)}
                    </td>
                    <td className="num">
                      {pct(r.ad_pct_of_revenue === null ? null : Number(r.ad_pct_of_revenue))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <p className="footnote">
            A dash means the ratio has no answer for that day — no units sold,
            so there is nothing to divide by. It is not a zero. That day's
            spend still counts towards the headline figure above, because it
            was still paid: the range total is total spend over total units,
            not the average of these rows.
          </p>
        </>
      )}
    </>
  );
}
