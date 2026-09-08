"""Daily dashboard ingestion.

    python -m collector.dashboard_sync              # trailing window
    python -m collector.dashboard_sync --days 30    # backfill
    python -m collector.dashboard_sync --dry-run    # compute, write nothing

Separate from `collector.main`, which keeps the storefront's stock numbers
current. That job must not start failing because a dashboard is misconfigured,
and this one must not be skipped because inventory had a bad morning.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from . import amazon, dashboard_db, net, orders
from .config import load_sku_map

SOURCE = "sp-api-orders"


def attach_internal_codes(rows: list[dict], sku_map) -> tuple[list[dict], list[str]]:
    """Stamp each row with the internal_code that joins it to everything else.

    `internal_code` is the join key across this whole system - the inventory
    feed, the sheet history, and Eshan's sales tracker all key on it - so a row
    without one can be counted but not attributed to a tea.

    Matching goes through `amazon_skus`, so a stickerless twin resolves to the
    same product its primary SKU does. Unknown SKUs keep their row, with a null
    code and a warning: dropping the row would understate units sold, and
    understating the denominator of cost-per-unit is the direction that makes
    ad spend look better than it is.
    """
    by_sku = sku_map.by_amazon_sku
    unknown: list[str] = []
    for row in rows:
        match = by_sku.get((row.get("sku") or "").strip().upper())
        if match:
            row["internal_code"] = match.internal_code
            if not row.get("product_name"):
                row["product_name"] = match.product_name
        else:
            row["internal_code"] = None
            if row["sku"] not in unknown:
                unknown.append(row["sku"])
    return rows, unknown


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=orders.TRAILING_DAYS,
                    help="how many days back to re-read, ending yesterday")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and compute, write nothing")
    args = ap.parse_args()

    if not amazon.configured():
        print(f"Amazon not configured: {', '.join(amazon.missing())}",
              file=sys.stderr)
        return 1
    if not args.dry_run and not dashboard_db.configured():
        print(f"Dashboard database not configured: "
              f"{', '.join(dashboard_db.missing())}", file=sys.stderr)
        return 1

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    days = orders.trailing_days(count=args.days)
    print(f"Sales window: {days[0]} to {days[-1]} "
          f"({len(days)} day(s), America/New_York)")

    run_id = None
    if not args.dry_run:
        run_id = dashboard_db.start_run("amazon", SOURCE, days[0], days[-1])

    try:
        token = amazon.get_access_token()
        rows = orders.fetch_days(token, days)
    except Exception as exc:
        # Nothing partial is written: a half-ingested window would leave the
        # dashboard showing a dip that never happened.
        print(f"Amazon FAILED: {net.describe_error(exc)}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=net.describe_error(exc))
        return 1

    rows, unknown = attach_internal_codes(rows, load_sku_map())

    units = sum(r["units"] for r in rows)
    revenue = round(sum(r["revenue"] for r in rows), 2)
    print(f"{len(rows)} SKU-day row(s) | {units} units | ${revenue:,.2f}")

    if unknown:
        # Not fatal. The units are real and belong in the totals; what is
        # missing is which tea they belong to.
        print(f"WARN {len(unknown)} SKU(s) sold but are not in sku_map.csv, so "
              f"they count toward totals without being attributed to a "
              f"product: {', '.join(sorted(unknown))}", file=sys.stderr)

    if args.dry_run:
        for r in rows:
            print(f"  DRY {r['sale_date']} {r['sku']:<24} "
                  f"{r['internal_code'] or '-':<8} {r['units']:>4} units")
        return 0

    try:
        written = dashboard_db.upsert("sales_daily", rows)
    except Exception as exc:
        print(f"Write FAILED: {net.describe_error(exc)}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=net.describe_error(exc))
        return 1

    dashboard_db.finish_run(run_id, "ok", rows_written=written)
    print(f"Wrote {written} row(s) to sales_daily")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
