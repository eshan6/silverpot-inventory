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

from . import ads, backfill, dashboard_db, net, orders

# Attribution windows mean recent days keep moving after the fact: a click
# today can be credited with a sale for the next 7 or 14 days. So the window
# re-read each run is wider than the sales one, and rows are upserted.
TRAILING_DAYS = 15

TABLE_FOR_GRAIN = {
    "campaign": "ads_daily",
    "search_term": "ads_search_terms",
}

BACKFILL_SETTING = "ads_backfill"

# Advertising history is far shorter-lived than order history: Amazon keeps
# roughly a quarter of it available for reporting, against two years of orders.
# Like the sales horizon this is a ceiling rather than a claim - a request for
# a range Amazon will not serve is caught and ends the walk, so being wrong
# here costs one skipped chunk and nothing else.
ADS_HORIZON_DAYS = 95
ADS_CHUNK_DAYS = 30


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=TRAILING_DAYS)
    ap.add_argument("--probe", action="store_true",
                    help="print raw report rows and exit, writing nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="one key from ads.REPORTS, for debugging")
    ap.add_argument("--no-backfill", action="store_true",
                    help="only re-read the trailing window, do not extend "
                         "the archive further back")
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

    if not args.no_backfill:
        extend_history(token, keys, start)
    return 0


def extend_history(token: str, keys: list[str], oldest_covered) -> int:
    """Reach one chunk further into advertising history.

    Total, like its sales counterpart: this runs after the day's figures are
    written and the run is recorded as ok, so a chunk Amazon declines to serve
    is reported and skipped rather than turning a good run red. A declined
    chunk is also the most likely way this walk ends - it is how the real
    retention window announces itself, rather than being guessed at here.
    """
    try:
        state = backfill.State.from_settings(
            dashboard_db.get_setting(BACKFILL_SETTING))
        chunk = backfill.next_chunk(state, start_from=oldest_covered,
                                    today=orders.today_et(),
                                    chunk_days=ADS_CHUNK_DAYS,
                                    horizon_days=ADS_HORIZON_DAYS)
        print(backfill.describe(state, chunk))
        if not chunk:
            if not state.done:
                dashboard_db.set_setting(
                    BACKFILL_SETTING,
                    backfill.advance(state, chunk, 0,
                                     horizon_days=ADS_HORIZON_DAYS).as_settings())
            return 0

        written = 0
        found = 0
        for key in keys:
            spec = ads.REPORTS[key]
            rows = ads.fetch(token, key, chunk[0], chunk[-1])
            found += len(rows)
            written += dashboard_db.upsert(TABLE_FOR_GRAIN[spec["grain"]], rows)

        print(f"Backfill: {written} row(s) for {chunk[0]}..{chunk[-1]}")
        after = backfill.advance(state, chunk, found,
                                 horizon_days=ADS_HORIZON_DAYS,
                                 today=orders.today_et())
        dashboard_db.set_setting(BACKFILL_SETTING, after.as_settings())
        if after.done:
            print(f"Backfill: finished - advertising history reaches {after.cursor}")
        return written
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"Backfill skipped this run: {net.describe_error(exc)}",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
