"""Backfill sales history from the Orders *report* rather than the Orders API.

The API spends one throttled call per day of history. Measured on 2026-09-09:
eight days, then `429 QuotaExceeded`, and about a day a minute after that. A
year of history is therefore four hours of waiting.

A report covers a whole date range in one request. Nine of them cover a year,
and the whole job fits inside a single run. `orders.py` has anticipated this
from the start - "the replacement is the flat-file orders report, which trades
immediacy for a single request" - and the report lifecycle already exists in
`amazon.py`, where it is the proven fallback for FBA inventory.

So this is for filling history once. The daily job stays on the Orders API,
which is immediate and needs no polling.

**On privacy.** An all-orders report carries buyer names, email addresses and
shipping addresses. This repository is public, which makes its Actions logs
public. Two rules follow, and they are not optional:

  1. Only the six columns this module needs are ever read out of a row. The
     rest are never touched, never stored, and never reach Supabase.
  2. Nothing derived from a row's *values* is ever printed. `--probe` prints
     column names, never a cell. A log line naming a customer would be a
     disclosure that no amount of later deletion undoes.

    python -m collector.orders_report --probe
    python -m collector.orders_report --from 2026-01-01
    python -m collector.orders_report --from 2026-01-01 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import date, datetime, timedelta, timezone

from . import amazon, dashboard_db, net, orders
from .config import load_sku_map
from .dashboard_sync import BACKFILL_SETTING, attach_internal_codes

SOURCE = "sp-api-orders-report"

# Richest first, and the second is a genuine fallback rather than a synonym:
# "by order date" is what this needs, "by last update" would file a January
# order that shipped in February under February. If only the latter exists the
# figures are still better than nothing, but the run says which was used.
REPORT_TYPES = (
    "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
    "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_LAST_UPDATE_GENERAL",
)

# Amazon rejects very wide ranges on order reports. Thirty days is the
# conventional safe window and keeps each report small enough to arrive
# quickly; the cost of being conservative is one extra request per month.
MAX_DAYS = 30

# Candidate column names, confirmed ones first. Same discipline as walmart.py:
# this repository has already lost time to nine wrong guesses at a field name,
# and a wrong guess here is silent - a missing column reads as zero units.
FIELDS = {
    "order_id": ("amazon-order-id", "order-id"),
    "purchase_date": ("purchase-date", "purchase_date"),
    "status": ("order-status", "order_status"),
    "sku": ("sku", "seller-sku", "merchant-sku"),
    "asin": ("asin",),
    "title": ("product-name", "title"),
    "units": ("quantity", "quantity-purchased", "quantity-shipped",
              "shipped-quantity"),
    "price": ("item-price", "item_price", "item-price-amount"),
}

# Columns without which the report cannot be trusted. A report missing any of
# these is refused rather than parsed into plausible-looking zeros.
ESSENTIAL = ("purchase_date", "sku", "units")


def _pick(header: list[str], key: str) -> str | None:
    lowered = {h.strip().lower(): h for h in header}
    for candidate in FIELDS[key]:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _number(raw: str) -> float:
    try:
        return float((raw or "").strip() or 0)
    except ValueError:
        return 0.0


def eastern_day(raw: str) -> date | None:
    """The seller's calendar date for a report timestamp.

    The report stamps an offset (`2026-03-04T21:15:00-08:00`), so this converts
    rather than trusting the printed date: an order placed at 21:15 Pacific is
    already the next day in UTC, and bucketing on the printed prefix would file
    a slice of every evening under the wrong day.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(orders.SELLER_TZ).date()


def column_names(text: str) -> list[str]:
    """The report's header row. Names only - never a value from a row."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    for row in reader:
        return [c.strip() for c in row]
    return []


def parse(text: str, marketplace: str = "amazon") -> list[dict]:
    """Fold an all-orders report into one row per (day, SKU).

    Only the columns in ESSENTIAL plus price, asin and title are read. Buyer
    columns are skipped entirely: they are not needed, and what is never read
    cannot be written somewhere it should not be.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in (reader.fieldnames or [])]
    if not header:
        raise RuntimeError("the report has no header row")

    cols = {key: _pick(header, key) for key in FIELDS}
    missing = [k for k in ESSENTIAL if not cols[k]]
    if missing:
        raise RuntimeError(
            f"report is missing essential column(s) {', '.join(missing)}; "
            f"it carries {len(header)} columns. Run --probe to see their names "
            f"and add the real ones to FIELDS rather than guessing.")

    bucket: dict[tuple[date, str], dict] = {}
    seen_orders: dict[tuple[date, str], set] = {}

    for row in reader:
        status = (row.get(cols["status"]) or "").strip() if cols["status"] else ""
        if status in orders.UNCOUNTED_STATUSES:
            continue

        day = eastern_day(row.get(cols["purchase_date"]) or "")
        sku = (row.get(cols["sku"]) or "").strip()
        units = int(_number(row.get(cols["units"]) or ""))
        if day is None or not sku or units <= 0:
            # Same rule as the API path: a line that resolved to nothing is
            # dropped rather than written as a zero, because a zero in
            # sales_daily means "sold none", not "could not tell".
            continue

        key = (day, sku)
        at = bucket.get(key)
        if at is None:
            at = bucket[key] = {
                "marketplace": marketplace,
                "sale_date": day.isoformat(),
                "sku": sku,
                "asin": (row.get(cols["asin"]) or "").strip() if cols["asin"] else "",
                "product_name": (row.get(cols["title"]) or "").strip() if cols["title"] else "",
                "units": 0,
                "orders": 0,
                "revenue": 0.0,
            }
            seen_orders[key] = set()

        at["units"] += units
        if cols["price"]:
            at["revenue"] = round(at["revenue"] + _number(row.get(cols["price"])), 2)

        order_id = (row.get(cols["order_id"]) or "").strip() if cols["order_id"] else ""
        if order_id:
            # Counted per distinct order, so a two-line order is one order.
            if order_id not in seen_orders[key]:
                seen_orders[key].add(order_id)
                at["orders"] += 1
        else:
            at["orders"] += 1

    return sorted(bucket.values(), key=lambda r: (r["sale_date"], r["sku"]))


def windows(start: date, end: date, span: int = MAX_DAYS) -> list[tuple[date, date]]:
    """Split a range into report-sized windows, oldest first."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=span - 1))
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def _iso(day: date, end_of_day: bool = False) -> str:
    """A report range boundary, as an instant in the seller's timezone."""
    moment = datetime.combine(day, datetime.max.time() if end_of_day
                              else datetime.min.time(), tzinfo=orders.SELLER_TZ)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_window(token: str, start: date, end: date,
                 report_types=REPORT_TYPES) -> tuple[list[dict], str]:
    """One report covering `start`..`end`. Returns (rows, report type used)."""
    sess = net.session()
    last: Exception | None = None
    for report_type in report_types:
        try:
            report_id = amazon._create_report(
                sess, token, report_type,
                start=_iso(start), end=_iso(end, end_of_day=True))
            doc_id = amazon._wait_for_report(sess, token, report_id)
            text = amazon._download_report(sess, token, doc_id)
            return parse(text), report_type
        except Exception as exc:  # noqa: BLE001 - try the next report type
            print(f"  {report_type}: {net.describe_error(exc)}", file=sys.stderr)
            last = exc
    raise RuntimeError(f"no orders report could be produced: {last}")


def probe(token: str) -> int:
    """Print each report's column names, and nothing from any row.

    The names are what the parser has to match. The values are customer data
    and this repository's logs are public, so they are never printed - not
    even one sample row, which is the usual shape of this mistake.
    """
    yesterday = orders.today_et() - timedelta(days=1)
    start = yesterday - timedelta(days=6)
    sess = net.session()

    for report_type in REPORT_TYPES:
        print(f"\n=== {report_type} ===")
        try:
            report_id = amazon._create_report(
                sess, token, report_type,
                start=_iso(start), end=_iso(yesterday, end_of_day=True))
            doc_id = amazon._wait_for_report(sess, token, report_id)
            text = amazon._download_report(sess, token, doc_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  unavailable: {net.describe_error(exc)}")
            continue

        names = column_names(text)
        body_rows = max(0, len(text.splitlines()) - 1)
        print(f"  {len(names)} columns, {body_rows} data row(s)")
        print(f"  columns: {names}")
        for key in FIELDS:
            print(f"    {key:<14} -> {_pick(names, key) or 'NOT FOUND'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="start", help="first day to collect, YYYY-MM-DD")
    ap.add_argument("--to", dest="end",
                    help="last day to collect (default: the day before the "
                         "daily job's trailing window)")
    ap.add_argument("--probe", action="store_true",
                    help="print report column names and exit, writing nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not amazon.configured():
        print(f"Amazon not configured: {', '.join(amazon.missing())}",
              file=sys.stderr)
        return 1
    if not (args.probe or args.dry_run) and not dashboard_db.configured():
        print(f"Dashboard database not configured: "
              f"{', '.join(dashboard_db.missing())}", file=sys.stderr)
        return 1

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    token = amazon.get_access_token()
    if args.probe:
        return probe(token)

    if not args.start:
        print("--from is required (for example --from 2026-01-01)",
              file=sys.stderr)
        return 1

    start = date.fromisoformat(args.start)
    end = (date.fromisoformat(args.end) if args.end
           else orders.today_et() - timedelta(days=orders.TRAILING_DAYS + 1))
    if end < start:
        print(f"--to ({end}) is before --from ({start})", file=sys.stderr)
        return 1

    spans = windows(start, end)
    print(f"History: {start} to {end} in {len(spans)} report(s)")

    run_id = None
    if not args.dry_run:
        run_id = dashboard_db.start_run("amazon", SOURCE, start, end)

    sku_map = load_sku_map()
    total = 0
    try:
        for window_start, window_end in spans:
            rows, used = fetch_window(token, window_start, window_end)
            rows, unknown = attach_internal_codes(rows, sku_map)
            units = sum(r["units"] for r in rows)
            print(f"  {window_start}..{window_end}: {len(rows)} row(s), "
                  f"{units} units  [{used}]")
            if unknown:
                print(f"    WARN unmapped SKU(s): {', '.join(sorted(unknown))}",
                      file=sys.stderr)
            if args.dry_run:
                continue
            total += dashboard_db.upsert("sales_daily", rows)
    except Exception as exc:  # noqa: BLE001
        print(f"History FAILED: {net.describe_error(exc)}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=net.describe_error(exc))
        return 1

    if args.dry_run:
        return 0

    dashboard_db.finish_run(run_id, "ok", rows_written=total)
    print(f"Wrote {total} row(s) to sales_daily")

    # The day-by-day walk has nothing left to do below this point, so tell it
    # so rather than leaving it to spend months of runs rediscovering that.
    dashboard_db.set_setting(BACKFILL_SETTING, {
        "cursor": start.isoformat(),
        "empty_streak": 0,
        "done": True,
    })
    print(f"Day-by-day backfill marked complete from {start} onwards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
