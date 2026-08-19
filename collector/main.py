"""Silverpot unified inventory collector.

Availability contract:

    published_available(sku) = FBA.fulfillableQuantity
                             + WFS.availableToSell
                             - safety_buffer

Only those two pools are sellable through multi-channel fulfillment. Inbound,
reserved (customer order / transshipment / FC processing), unfulfillable and
researching quantities are recorded in the snapshot table for forecasting and
never published to the website.

Usage:
    python -m collector.main            # full run
    python -m collector.main --probe    # print raw API shapes, write nothing
    python -m collector.main --dry-run  # compute and print, write nothing
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone

from . import amazon, walmart
from .config import BUFFER_PERCENT, DEFAULT_SAFETY_BUFFER, PUBLIC_DIR, load_sku_map


def compute_buffer(row, raw_available: int) -> int:
    if row.safety_buffer is not None:
        return row.safety_buffer
    if raw_available <= 0:
        return 0
    return max(DEFAULT_SAFETY_BUFFER, math.ceil(raw_available * BUFFER_PERCENT))


def build(sku_map, fba_by_sku: dict, wfs_by_sku: dict) -> list[dict]:
    results = []
    for row in sku_map.rows:
        if not row.active:
            continue
        fba = fba_by_sku.get(row.amazon_sku.strip().upper(), {})
        wfs = wfs_by_sku.get(row.walmart_sku.strip().upper(), {})

        fba_fulfillable = fba.get("fulfillable", 0)
        wfs_ats = wfs.get("available_to_sell", 0)
        raw_available = fba_fulfillable + wfs_ats
        buffer = compute_buffer(row, raw_available)
        published = max(0, raw_available - buffer)

        results.append({
            "internal_code": row.internal_code,
            "product_name": row.product_name,
            "sku": row.sku,
            "walmart_sku": row.walmart_sku,
            "sku_diverged": bool(row.walmart_sku_override),
            "asin": row.asin,
            "website_product_id": row.website_product_id,
            "fba_fulfillable": fba_fulfillable,
            "wfs_available": wfs_ats,
            "raw_available": raw_available,
            "safety_buffer": buffer,
            "published_available": published,
            "fba_inbound": (fba.get("inbound_working", 0) + fba.get("inbound_shipped", 0)
                            + fba.get("inbound_receiving", 0)),
            "fba_reserved": fba.get("reserved_total", 0),
            "fba_unfulfillable": fba.get("unfulfillable", 0),
            "fba_researching": fba.get("researching", 0),
            "matched_fba": bool(fba),
            "matched_wfs": bool(wfs),
        })
    return results


def snapshot_rows(date: str, results: list[dict]) -> list[list]:
    rows = []
    for r in results:
        pairs = [
            ("FBA", "fulfillable", r["fba_fulfillable"]),
            ("FBA", "inbound", r["fba_inbound"]),
            ("FBA", "reserved", r["fba_reserved"]),
            ("FBA", "unfulfillable", r["fba_unfulfillable"]),
            ("FBA", "researching", r["fba_researching"]),
            ("WFS", "available_to_sell", r["wfs_available"]),
            ("PUBLISHED", "available", r["published_available"]),
        ]
        for node, state, qty in pairs:
            rows.append([date, r["internal_code"], r["product_name"], node, state, qty])
    return rows


def depletion(series: list[tuple[str, int]]) -> tuple[float | None, float | None]:
    """Average daily decline in published availability, and days of cover."""
    if len(series) < 2:
        return None, None
    drops, spans = [], 0
    for (_, a), (_, b) in zip(series, series[1:]):
        if b <= a:  # ignore restock jumps
            drops.append(a - b)
            spans += 1
    if not spans:
        return None, None
    rate = sum(drops) / spans
    latest = series[-1][1]
    cover = (latest / rate) if rate > 0 else None
    return round(rate, 2), (round(cover, 1) if cover is not None else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="Print raw API payloads and exit")
    ap.add_argument("--dry-run", action="store_true", help="Compute but write nothing")
    args = ap.parse_args()

    sku_map = load_sku_map()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    az_token = amazon.get_access_token()
    fba_raw = amazon.fetch_fba_summaries(az_token)
    fba_parsed = [amazon.parse_summary(s) for s in fba_raw]
    fba_by_sku = {p["seller_sku"].upper(): p for p in fba_parsed if p["seller_sku"]}

    wm_token = walmart.get_access_token()
    wfs_raw, wfs_first_page = walmart.fetch_wfs_inventory(wm_token)
    wfs_parsed = [walmart.parse_record(r) for r in wfs_raw]
    wfs_by_sku = {p["sku"].upper(): p for p in wfs_parsed if p["sku"]}

    if args.probe:
        print("=== FBA raw sample ===")
        print(json.dumps(fba_raw[:2], indent=2))
        print("=== WFS raw first page ===")
        print(json.dumps(wfs_first_page, indent=2)[:6000])
        print("=== WFS parsed sample ===")
        print(json.dumps(wfs_parsed[:5], indent=2))
        print(f"\nFBA SKUs seen: {len(fba_by_sku)} | WFS SKUs seen: {len(wfs_by_sku)}")
        return 0

    results = build(sku_map, fba_by_sku, wfs_by_sku)

    unmatched_fba = [r["internal_code"] for r in results if not r["matched_fba"]]
    unmatched_wfs = [r["internal_code"] for r in results if not r["matched_wfs"]]
    diverged = [r["internal_code"] for r in results if r["sku_diverged"]]

    if unmatched_fba:
        print(f"WARN no FBA match ({len(unmatched_fba)}): {', '.join(unmatched_fba)}",
              file=sys.stderr)
    if unmatched_wfs:
        print(f"WARN no WFS match ({len(unmatched_wfs)}): {', '.join(unmatched_wfs)}",
              file=sys.stderr)
    if diverged:
        print(f"NOTE Walmart SKU overridden for: {', '.join(diverged)}", file=sys.stderr)

    # A SKU that vanishes from one marketplace but not the other is almost
    # always a listing problem, not an inventory problem. Surface it loudly.
    half_matched = [r["internal_code"] for r in results
                    if r["matched_fba"] != r["matched_wfs"]]
    if half_matched:
        print(f"WARN present on one marketplace only: {', '.join(half_matched)}",
              file=sys.stderr)

    history = {}
    sh = None
    if not args.dry_run:
        from . import sheets
        sh = sheets.open_sheet()
        history = sheets.read_prior_published(sh)

    for r in results:
        series = history.get(r["internal_code"], []) + [(today, r["published_available"])]
        rate, cover = depletion(series)
        r["daily_depletion"] = rate
        r["days_of_cover"] = cover

    total = sum(r["published_available"] for r in results)
    oos = [r["internal_code"] for r in results if r["published_available"] == 0]
    print(f"{today}: {len(results)} SKUs | {total} publishable units | "
          f"{len(oos)} at zero")
    if oos:
        print(f"  zero: {', '.join(oos)}")

    if args.dry_run:
        print(json.dumps(results, indent=2))
        return 0

    from . import sheets
    sheets.append_snapshots(sh, snapshot_rows(today, results))
    sheets.write_current(sh, [[
        r["internal_code"], r["product_name"], r["amazon_seller_sku"], r["walmart_sku"],
        r["asin"], r["fba_fulfillable"], r["wfs_available"], r["raw_available"],
        r["safety_buffer"], r["published_available"], r["fba_inbound"], r["fba_reserved"],
        r["fba_unfulfillable"], r["daily_depletion"] or "", r["days_of_cover"] or "",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    ] for r in results])

    PUBLIC_DIR.mkdir(exist_ok=True)
    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "FBA fulfillable + WFS available-to-sell, less safety buffer",
        "sku_count": len(results),
        "items": [{
            "internal_code": r["internal_code"],
            "name": r["product_name"],
            "sku": r["sku"],
            "asin": r["asin"],
            "website_product_id": r["website_product_id"],
            "available": r["published_available"],
            "in_stock": r["published_available"] > 0,
            "fba": r["fba_fulfillable"],
            "wfs": r["wfs_available"],
            "buffer": r["safety_buffer"],
            "days_of_cover": r["days_of_cover"],
            "inbound": r["fba_inbound"],
        } for r in results],
    }
    (PUBLIC_DIR / "inventory.json").write_text(json.dumps(feed, indent=2))
    print(f"Wrote {PUBLIC_DIR / 'inventory.json'}")

    # Push into the website database, if it has been configured yet.
    from . import website
    if website.configured():
        summary = website.push(results)
        print(f"Website: {summary['updated']} products updated")
        if summary["no_row_updated"]:
            print(f"  no row updated for: {', '.join(summary['no_row_updated'])}",
                  file=sys.stderr)
    else:
        print(f"Website push skipped (not configured yet: "
              f"{', '.join(website.missing())})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
