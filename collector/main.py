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
    python -m collector.main             # full run
    python -m collector.main --diagnose  # name the missing Amazon role, write nothing
    python -m collector.main --probe     # print raw API shapes, write nothing
    python -m collector.main --dry-run   # compute and print, write nothing
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone

from . import amazon, net, walmart
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
        # A product can hold stock under several Amazon SKUs at once - the
        # original and its stickerless twin - each with its own pool. Amazon
        # reports them separately; both ship the same tea, so they are summed.
        matched = [(s, fba_by_sku[s.upper()]) for s in row.amazon_skus
                   if s.upper() in fba_by_sku]

        def fba_sum(field: str) -> int:
            return sum(rec.get(field, 0) for _s, rec in matched)

        wfs = wfs_by_sku.get(row.walmart_sku.strip().upper(), {})

        fba_fulfillable = fba_sum("fulfillable")
        wfs_ats = wfs.get("available_to_sell", 0)
        raw_available = fba_fulfillable + wfs_ats
        buffer = compute_buffer(row, raw_available)
        published = max(0, raw_available - buffer)

        results.append({
            "internal_code": row.internal_code,
            "product_name": row.product_name,
            "format": row.format,
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
            "fba_inbound": (fba_sum("inbound_working") + fba_sum("inbound_shipped")
                            + fba_sum("inbound_receiving")),
            "fba_reserved": fba_sum("reserved_total"),
            "fba_unfulfillable": fba_sum("unfulfillable"),
            "fba_researching": fba_sum("researching"),
            "fba_skus_matched": [s for s, _rec in matched],
            "wfs_reserved": wfs.get("reserved", 0),
            "wfs_inbound": wfs.get("inbound", 0),
            "wfs_aged_over_270d": wfs.get("aged_over_270d", 0),
            "wfs_stock_status": wfs.get("stock_status", ""),
            "matched_fba": bool(matched),
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
            ("WFS", "reserved", r["wfs_reserved"]),
            ("WFS", "inbound", r["wfs_inbound"]),
            ("WFS", "aged_over_270d", r["wfs_aged_over_270d"]),
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
    ap.add_argument("--diagnose", action="store_true",
                    help="Probe Amazon permissions role by role and exit")
    ap.add_argument("--dry-run", action="store_true", help="Compute but write nothing")
    args = ap.parse_args()

    sku_map = load_sku_map()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    if args.diagnose:
        if not amazon.configured():
            print(f"Amazon not configured: {', '.join(amazon.missing())}")
            return 1
        amazon.diagnose()
        return 0

    # Each marketplace is pulled independently. One failing must never take the
    # other down with it, and must never be silently reported as zero stock.
    status = {"FBA": "pending", "WFS": "pending"}
    fba_by_sku, wfs_by_sku = {}, {}
    fba_raw, wfs_first_page, wfs_parsed = [], {}, []
    fba_source = None

    if not amazon.configured():
        status["FBA"] = "skipped"
        print(f"Amazon skipped (not configured: {', '.join(amazon.missing())})")
    else:
        try:
            az_token = amazon.get_access_token()
            fba_raw, fba_source = amazon.fetch_fba_inventory(az_token)
            fba_by_sku = {p["seller_sku"].upper(): p for p in fba_raw if p["seller_sku"]}
            status["FBA"] = "ok"
            print(f"Amazon: {len(fba_by_sku)} SKUs returned via {fba_source}")
        except Exception as exc:
            status["FBA"] = "failed"
            fba_source = None
            print(f"Amazon FAILED: {net.describe_error(exc)}", file=sys.stderr)

    if not walmart.configured():
        status["WFS"] = "skipped"
        print(f"Walmart skipped (not configured: {', '.join(walmart.missing())})")
    else:
        try:
            wm_token = walmart.get_access_token()
            wfs_raw, wfs_first_page = walmart.fetch_wfs_inventory(wm_token)
            wfs_parsed = [walmart.parse_record(r) for r in wfs_raw]
            wfs_by_sku = {p["sku"].upper(): p for p in wfs_parsed if p["sku"]}
            status["WFS"] = "ok"
            print(f"Walmart: {len(wfs_by_sku)} SKUs returned")
            if wfs_by_sku and not any(p["available_to_sell"] for p in wfs_parsed):
                print("WARN Walmart returned rows but every available-to-sell is 0. "
                      "Likely an unrecognised field name - run --probe and check "
                      "ATS_FIELDS in collector/walmart.py.", file=sys.stderr)
        except Exception as exc:
            status["WFS"] = "failed"
            print(f"Walmart FAILED: {net.describe_error(exc)}", file=sys.stderr)

    healthy = [k for k, v in status.items() if v == "ok"]
    if not healthy:
        print("Both marketplaces unavailable. Nothing written - previous data left "
              "untouched.", file=sys.stderr)
        return 1
    degraded = any(v == "failed" for v in status.values())

    if args.probe:
        print(f"Status: {status}")
        print(f"=== FBA parsed sample (via {fba_source or 'nothing'}) ===")
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

    # A product whose stock arrived under more than one Amazon SKU. Worth
    # naming: it is the difference between the published number and the one
    # the primary SKU alone would have given.
    pooled = [r for r in results if len(r["fba_skus_matched"]) > 1]
    if pooled:
        print(f"NOTE FBA stock pooled across multiple Amazon SKUs "
              f"({len(pooled)}): " +
              ", ".join(f"{r['internal_code']}[{'+'.join(r['fba_skus_matched'])}]"
                        for r in pooled), file=sys.stderr)

    # The reverse of the "no FBA match" warning. An Amazon SKU holding stock
    # that no row in sku_map.csv claims is stock the storefront cannot see,
    # and nothing else in this pipeline would ever mention it.
    if status["FBA"] == "ok":
        claimed = set(sku_map.by_amazon_sku)
        unclaimed = sorted(
            (sku, rec.get("fulfillable", 0))
            for sku, rec in fba_by_sku.items() if sku not in claimed
        )
        with_stock = [(s, q) for s, q in unclaimed if q > 0]
        if with_stock:
            print(f"WARN {len(with_stock)} Amazon SKU(s) hold fulfillable stock but "
                  f"are not in sku_map.csv, so it is NOT published: " +
                  ", ".join(f"{s}={q}" for s, q in with_stock), file=sys.stderr)
        idle = len(unclaimed) - len(with_stock)
        if idle:
            print(f"NOTE {idle} further Amazon SKU(s) are unmapped but hold no "
                  f"fulfillable stock.", file=sys.stderr)

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

    if degraded:
        print(f"DEGRADED RUN - healthy sources: {', '.join(healthy)}", file=sys.stderr)
    aged = [(r["internal_code"], r["wfs_aged_over_270d"]) for r in results
            if r["wfs_aged_over_270d"] > 0]
    if aged:
        print("WFS units past 270 days (long-term storage fees apply): " +
              ", ".join(f"{c}={q}" for c, q in aged))
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
    expected = {"internal_code", "product_name", "sku", "walmart_sku", "asin",
                "fba_fulfillable", "wfs_available", "raw_available", "safety_buffer",
                "published_available", "fba_inbound", "fba_reserved",
                "fba_unfulfillable", "daily_depletion", "days_of_cover"}
    if results:
        gap = expected - set(results[0])
        if gap:
            raise KeyError(f"result rows are missing expected keys: {sorted(gap)}")

    sheets.append_snapshots(sh, snapshot_rows(today, results))
    sheets.write_current(sh, [[
        r["internal_code"], r["product_name"], r["sku"], r["walmart_sku"],
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
        "source_status": status,
        # Which Amazon route produced these numbers: the FBA Inventory API, or
        # one of the Reports API fallbacks. Kept in the feed so a published
        # figure can always be traced back to how it was obtained.
        "fba_source": fba_source,
        "degraded": degraded,
        "items": [{
            "internal_code": r["internal_code"],
            "name": r["product_name"],
            "format": r["format"],
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
    if degraded:
        print("Website push SKIPPED: a marketplace failed this run, so published "
              "numbers would understate real stock.", file=sys.stderr)
    elif website.configured():
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
