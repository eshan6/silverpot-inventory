import { useEffect, useMemo, useState } from "react";
import { supabase } from "../lib/supabase";
import { Freshness } from "../components/Freshness";
import { RangeControls } from "../components/RangeControls";
import { SavedViews } from "../components/SavedViews";
import { int, money, shortDay } from "../lib/format";
import { rangeFor } from "../lib/range";
import type { Preset, Range } from "../lib/range";
import type { Marketplace, SalesRow } from "../lib/types";

type ViewConfig = {
  preset: Preset;
  from: string;
  to: string;
};

export default function Sales({ marketplace }: { marketplace: Marketplace }) {
  const [preset, setPreset] = useState<Preset>("30d");
  const [range, setRange] = useState<Range>(() => rangeFor("30d"));
  const [rows, setRows] = useState<SalesRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const apply = (c: Partial<ViewConfig>) => {
    if (c.preset && c.preset !== "custom") {
      setPreset(c.preset);
      setRange(rangeFor(c.preset));
    } else if (c.from && c.to) {
      setPreset("custom");
      setRange({ from: c.from, to: c.to });
    }
  };

  useEffect(() => {
    let alive = true;
    setRows(null);
    setErr(null);
    supabase
      .from("sales_daily")
      .select("sale_date, sku, internal_code, product_name, units, orders, revenue")
      .eq("marketplace", marketplace)
      .gte("sale_date", range.from)
      .lte("sale_date", range.to)
      .then(({ data, error }) => {
        if (!alive) return;
        if (error) setErr(error.message);
        else setRows((data ?? []) as SalesRow[]);
      });
    return () => { alive = false; };
  }, [marketplace, range.from, range.to]);

  /** One line per product, because "which ones sold" is the question here. */
  const byProduct = useMemo(() => {
    if (!rows) return [];
    const acc = new Map<string, {
      key: string; code: string | null; name: string;
      units: number; orders: number; revenue: number;
    }>();
    for (const r of rows) {
      // Group on internal_code so a stickerless twin folds into its product.
      // An unattributed SKU keeps its own line rather than being merged into
      // something it might not be.
      const key = r.internal_code ?? `sku:${r.sku}`;
      const at = acc.get(key) ?? {
        key,
        code: r.internal_code,
        name: r.product_name || r.sku,
        units: 0, orders: 0, revenue: 0,
      };
      at.units += r.units;
      at.orders += r.orders;
      at.revenue += Number(r.revenue);
      acc.set(key, at);
    }
    return [...acc.values()].sort((a, b) => b.units - a.units);
  }, [rows]);

  const byDay = useMemo(() => {
    if (!rows) return [];
    const acc = new Map<string, number>();
    for (const r of rows) acc.set(r.sale_date, (acc.get(r.sale_date) ?? 0) + r.units);
    return [...acc.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [rows]);

  const totals = useMemo(() => ({
    units: byProduct.reduce((n, p) => n + p.units, 0),
    revenue: byProduct.reduce((n, p) => n + p.revenue, 0),
    skus: byProduct.length,
  }), [byProduct]);

  const peak = Math.max(1, ...byDay.map(([, u]) => u));
  const days = byDay.length;

  return (
    <>
      <Freshness marketplace={marketplace} sources={["sp-api-orders"]} />

      <RangeControls
        preset={preset}
        range={range}
        onChange={(p, r) => { setPreset(p); setRange(r); }}
      />

      <SavedViews<ViewConfig>
        section="sales"
        marketplace={marketplace}
        current={{ preset, from: range.from, to: range.to }}
        onApply={apply}
      />

      {err && <div className="banner bad">Could not load sales: {err}</div>}

      {rows === null && !err && <div className="loading">Loading…</div>}

      {rows !== null && rows.length === 0 && (
        // Explicitly "no data", never a grid of zeros. A zero here would read
        // as "you sold nothing", which is a different and probably false claim.
        <div className="empty">
          <strong>No sales data for this range.</strong>
          Either nothing sold between {range.from} and {range.to}, or ingestion
          has not covered those days yet — check the banner above.
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <>
          <div className="totals">
            <div className="stat">
              <div className="n">{int(totals.units)}</div>
              <div className="l">Units sold</div>
              <div className="d">{range.from} → {range.to}</div>
            </div>
            <div className="stat">
              <div className="n">{money(totals.revenue)}</div>
              <div className="l">Revenue</div>
            </div>
            <div className="stat">
              <div className="n">{totals.skus}</div>
              <div className="l">Products sold</div>
            </div>
            <div className="stat">
              <div className="n">{days ? (totals.units / days).toFixed(1) : "—"}</div>
              <div className="l">Units per day</div>
              <div className="d">Over {days} day{days === 1 ? "" : "s"}</div>
            </div>
          </div>

          <table>
            <thead>
              <tr>
                <th>Product</th>
                <th className="bar-col" style={{ textAlign: "left", width: "26%" }}>Share</th>
                <th>Units</th>
                <th>Orders</th>
                <th>Revenue</th>
              </tr>
            </thead>
            <tbody>
              {byProduct.map((p) => (
                <tr key={p.key}>
                  <td className="name">
                    {p.name}
                    <small>{p.code ?? "Unattributed SKU"}</small>
                  </td>
                  <td>
                    <div className="bar">
                      <span className="seg-a"
                            style={{ width: `${(p.units / (totals.units || 1)) * 100}%` }} />
                    </div>
                  </td>
                  <td className="num">{int(p.units)}</td>
                  <td className="num">{int(p.orders)}</td>
                  <td className="num">{money(p.revenue)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h2 className="section-head">Units per day</h2>
          <table>
            <tbody>
              {byDay.map(([day, units]) => (
                <tr key={day}>
                  <td className="name" style={{ width: "6rem" }}>{shortDay(day)}</td>
                  <td>
                    <div className="bar">
                      <span className="seg-a" style={{ width: `${(units / peak) * 100}%` }} />
                    </div>
                  </td>
                  <td className="num" style={{ width: "5rem" }}>{int(units)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
