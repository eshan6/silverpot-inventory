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
from datetime import datetime, timezone

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

# Reports in flight, carried between runs.
#
# Amazon took over ten minutes, and then over twenty-five, to build
# Silverpot's first report - two runs, two timeouts, both on the first report
# of the set. Waiting inside the job means racing a queue nobody here
# controls, and losing that race fails a run that did nothing wrong.
#
# So a run asks for its reports, keeps whatever is ready, and writes the rest
# down as pending. The next run collects them. A report that takes an hour
# costs nothing but an hour; the job itself always finishes.
PENDING_SETTING = "ads_pending_reports"

# One sweep costs a few seconds. This is how long a run will keep sweeping
# before parking what is left - long enough to finish in one go when Amazon is
# quick, short enough that a slow queue does not hold the runner open.
COLLECT_BUDGET_SECONDS = 8 * 60

# A report id Amazon never finished is not worth carrying forever.
PENDING_MAX_AGE_HOURS = 36

# Advertising history is far shorter-lived than order history: Amazon keeps
# roughly a quarter of it available for reporting, against two years of orders.
# Like the sales horizon this is a ceiling rather than a claim - a request for
# a range Amazon will not serve is caught and ends the walk, so being wrong
# here costs one skipped chunk and nothing else.
ADS_HORIZON_DAYS = 95
ADS_CHUNK_DAYS = 30


def probe_collect(token, keys, start, end):
    """The probe's own collect: raw rows, same carry-over as a real run."""
    saved = dashboard_db.get_setting(PENDING_SETTING) or {}
    pending = dict(saved.get("reports") or {})
    window = (saved.get("start"), saved.get("end"))
    asked_at = saved.get("asked_at")

    if pending and _too_old(asked_at):
        print(f"Abandoning {len(pending)} stale report(s) from {asked_at}.")
        pending, window, asked_at = {}, (None, None), None

    if pending:
        print(f"Collecting {len(pending)} report(s) from {window[0]}..{window[1]}")
    else:
        pending = ads.request_all(token, keys, start, end)
        window = (start.isoformat(), end.isoformat())
        asked_at = _now_iso()
        print(f"Requested {len(pending)} report(s) for {window[0]}..{window[1]}")

    sess = net.session()
    raw_by_key = {}
    remaining = dict(pending)
    import time as _time
    started = _time.monotonic()
    deadline = started + COLLECT_BUDGET_SECONDS
    sweeps = 0
    while True:
        sweeps += 1
        for key in list(remaining):
            status, url = ads.poll_report(token, remaining[key], sess=sess)
            if status == "done":
                elapsed = _time.monotonic() - started
                print(f"    {key}: ready after {elapsed:.0f}s")
                raw_by_key[key] = ads.download_report(url, sess=sess)
                del remaining[key]
        if not remaining or _time.monotonic() >= deadline:
            break
        _time.sleep(ads.POLL_SECONDS)
    # One line, not one per key per sweep: an eight-minute wait at ten-second
    # intervals is fifty sweeps, and four keys each printing every time buries
    # the answer in two hundred identical lines.
    print(f"    {sweeps} sweep(s) over {_time.monotonic() - started:.0f}s")

    if remaining:
        dashboard_db.set_setting(PENDING_SETTING, {
            "reports": remaining, "start": window[0], "end": window[1],
            "asked_at": asked_at,
        })
    elif saved:
        dashboard_db.set_setting(PENDING_SETTING, {})
    return raw_by_key, remaining, window


def collect_or_request(token, keys, start, end, dry_run=False):
    """Collect reports already in flight, or ask for a new set.

    Returns (rows_by_key, still_pending, window).

    The rule that keeps this honest: a run never asks for a second set while
    the first is still building. Requesting again every twelve hours would
    queue reports faster than Amazon retires them, and the pending list would
    grow without anything ever being collected.

    A pending set older than PENDING_MAX_AGE_HOURS is abandoned rather than
    waited on forever. Amazon's report ids do expire, and a stuck set that is
    never dropped means the pipeline never asks again.
    """
    saved = dashboard_db.get_setting(PENDING_SETTING) or {}
    pending = dict(saved.get("reports") or {})
    window = (saved.get("start"), saved.get("end"))
    asked_at = saved.get("asked_at")

    if pending and _too_old(asked_at):
        print(f"Abandoning {len(pending)} report(s) requested at {asked_at}: "
              f"older than {PENDING_MAX_AGE_HOURS}h.")
        pending, window = {}, (None, None)

    if pending:
        print(f"Collecting {len(pending)} report(s) from {window[0]}..{window[1]}")
    elif dry_run:
        # A dry run neither requests nor consumes. Requesting would leave a
        # report building that nothing is going to collect - the ids are not
        # saved on a dry run - and twenty minutes of Amazon's queue would be
        # spent on output nobody keeps.
        print("Nothing pending, and a dry run does not request. "
              "Run without --dry-run to ask for a set.")
        return {}, {}, window
    else:
        pending = ads.request_all(token, keys, start, end)
        window = (start.isoformat(), end.isoformat())
        asked_at = _now_iso()
        print(f"Requested {len(pending)} report(s) for {window[0]}..{window[1]}")

    rows_by_key, still_pending, statuses = ads.collect(
        token, pending, budget_seconds=COLLECT_BUDGET_SECONDS)

    for key in sorted(statuses):
        if statuses[key] != "done":
            print(f"    {key}: {statuses[key]}")

    # A dry run must leave the carry-over exactly as it found it, or it would
    # consume reports the next real run was going to write.
    if not dry_run:
        if still_pending:
            dashboard_db.set_setting(PENDING_SETTING, {
                "reports": still_pending,
                "start": window[0],
                "end": window[1],
                "asked_at": asked_at,
            })
        elif saved:
            dashboard_db.set_setting(PENDING_SETTING, {})

    return rows_by_key, still_pending, window


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _too_old(asked_at) -> bool:
    if not asked_at:
        return False
    try:
        when = datetime.fromisoformat(asked_at)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return age > PENDING_MAX_AGE_HOURS * 3600


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

        if args.probe:
            # Raw, before normalization: the whole point is to see what Amazon
            # actually calls things.
            #
            # The probe carries reports between runs like the real path does,
            # and for the same reason - it was the probe that sat for
            # twenty-five minutes and failed, twice, teaching us nothing. A
            # probe that reports "still building" is more use than one that
            # dies waiting.
            raw_by_key, still, window = probe_collect(token, keys, start, end)
            for key, raw in raw_by_key.items():
                print(f"\n=== {key} ({len(raw)} rows) ===")
                print(json.dumps(raw[:3], indent=2)[:4000])
                if raw:
                    print(f"keys: {sorted(raw[0])}")
                else:
                    print("(no rows - the report built, and it is empty)")
            if still:
                print(f"\nStill building after {COLLECT_BUDGET_SECONDS // 60}m: "
                      f"{', '.join(sorted(still))}")
                print("Run the probe again to pick them up; the ids are saved.")
            return 0

        by_key, still_pending, window = collect_or_request(
            token, keys, start, end, dry_run=args.dry_run)

        for key in keys:
            spec = ads.REPORTS[key]
            if key not in by_key:
                continue
            rows = by_key[key]
            print(f"  {key}: {len(rows)} row(s)")
            if args.dry_run:
                for r in rows[:5]:
                    print(f"    DRY {r.get('ad_date')} {r.get('campaign_name')} "
                          f"spend={r.get('spend')} clicks={r.get('clicks')}")
                continue
            written += dashboard_db.upsert(TABLE_FOR_GRAIN[spec["grain"]], rows)

        if still_pending:
            print(f"  still building, will collect next run: "
                  f"{', '.join(sorted(still_pending))} "
                  f"(window {window[0]}..{window[1]})")

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
        by_key = ads.fetch_many(token, keys, chunk[0], chunk[-1])
        for key in keys:
            spec = ads.REPORTS[key]
            rows = by_key.get(key, [])
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
