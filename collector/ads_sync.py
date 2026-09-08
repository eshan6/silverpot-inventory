"""Daily Amazon Ads ingestion into the dashboard database.

    python -m collector.ads_sync                # trailing window
    python -m collector.ads_sync --days 30      # backfill
    python -m collector.ads_sync --probe        # dump raw rows, write nothing
    python -m collector.ads_sync --dry-run      # normalize and print

--probe is the important one before trusting any figure. It prints Amazon's
raw report rows so the column names in `ads.FIELDS` can be confirmed against
reality rather than assumed, which is how walmart.py's field names were found
after nine wrong guesses.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import ads, dashboard_db, net, orders

# Attribution windows mean recent days keep moving after the fact: a click
# today can be credited with a sale for the next 7 or 14 days. So the window
# re-read each run is wider than the sales one, and rows are upserted.
TRAILING_DAYS = 15

TABLE_FOR_GRAIN = {
    "campaign": "ads_daily",
    "search_term": "ads_search_terms",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=TRAILING_DAYS)
    ap.add_argument("--probe", action="store_true",
                    help="print raw report rows and exit, writing nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="one key from ads.REPORTS, for debugging")
    args = ap.parse_args()

    if not ads.configured():
        print(f"Amazon Ads not configured: {', '.join(ads.missing())}",
              file=sys.stderr)
        return 1
    if not (args.probe or args.dry_run) and not dashboard_db.configured():
        print(f"Dashboard database not configured: "
              f"{', '.join(dashboard_db.missing())}", file=sys.stderr)
        return 1

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    days = orders.trailing_days(count=args.days)
    start, end = days[0], days[-1]
    print(f"Ads window: {start} to {end} ({len(days)} days, America/New_York)")

    keys = [args.only] if args.only else list(ads.REPORTS)
    run_id = None
    if not (args.probe or args.dry_run):
        run_id = dashboard_db.start_run("amazon", "ads-api", start, end)

    written = 0
    try:
        token = ads.get_access_token()
        for key in keys:
            spec = ads.REPORTS[key]
            if args.probe:
                # Raw, before normalization: the whole point is to see what
                # Amazon actually calls things.
                report_id = ads.request_report(token, spec, start, end)
                url = ads.wait_for_report(token, report_id)
                raw = ads.download_report(url)
                print(f"\n=== {key} ({len(raw)} rows) ===")
                print(json.dumps(raw[:3], indent=2)[:4000])
                if raw:
                    print(f"keys: {sorted(raw[0])}")
                continue

            rows = ads.fetch(token, key, start, end)
            print(f"  {key}: {len(rows)} row(s)")
            if args.dry_run:
                for r in rows[:5]:
                    print(f"    DRY {r.get('ad_date')} {r.get('campaign_name')} "
                          f"spend={r.get('spend')} clicks={r.get('clicks')}")
                continue
            written += dashboard_db.upsert(TABLE_FOR_GRAIN[spec["grain"]], rows)

    except ads.AdsDenied as exc:
        print(f"Amazon Ads denied: {exc}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=str(exc))
        return 1
    except Exception as exc:
        print(f"Amazon Ads FAILED: {net.describe_error(exc)}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=net.describe_error(exc))
        return 1

    if args.probe or args.dry_run:
        return 0

    dashboard_db.finish_run(run_id, "ok", rows_written=written)
    print(f"Wrote {written} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
