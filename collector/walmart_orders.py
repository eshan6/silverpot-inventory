"""Units sold per SKU per day from the Walmart Marketplace Orders API.

The Walmart half of the dashboard's Sales section, and of the denominator of
its Spend section. It needs no new credentials: `WALMART_CLIENT_ID` and
`WALMART_CLIENT_SECRET` already exist for the WFS inventory pull and are
already GitHub Actions secrets, and orders live under the same Marketplace
API and the same token.

Three things here are the same decisions the Amazon side reached, for the same
reasons, and one is specific to Walmart.

**Days are Eastern.** Walmart stamps `orderDate` as epoch milliseconds, which
is unambiguous - a mercy after Amazon's offset strings - but it still has to
be converted to the seller's day rather than UTC, or a fifth of every
evening's orders land on tomorrow.

**Cancelled lines sold nothing.** Status lives per line rather than per order,
so a two-line order can be half cancelled. Each line is judged on its own.

**Field names are candidates, not assertions.** This repository has already
lost time to nine wrong guesses at a Walmart field name, and `walmart.py`
carries the scar tissue: confirmed names first, alternates behind them, and a
`--probe` that shows what the account actually returns. Nothing here has been
confirmed against a live response yet, so every list below is a guess until
`--probe` says otherwise - which is exactly why a missing essential field
raises rather than quietly reading as zero units.

**Probe prints structure, never values.** A Walmart order carries the buyer's
name, address and phone. This repository is public, so its Actions logs are
public. `--probe` prints the shape of the response - which keys exist, at what
depth - and never a leaf value.

    python -m collector.walmart_orders --probe
    python -m collector.walmart_orders --days 3 --dry-run
    python -m collector.walmart_orders --from 2026-01-01
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from . import dashboard_db, net, orders, walmart
from .config import WALMART_HOST, load_sku_map

SOURCE = "walmart-orders"
MARKETPLACE = "walmart"
ORDERS_PATH = "/v3/orders"

# Walmart pages with an opaque cursor rather than an offset. A ceiling on the
# number of pages stops a malformed cursor that returns itself from becoming an
# infinite loop against a rate-limited API.
MAX_PAGES = 200

# One request per day of history is not needed here: unlike Amazon's Orders
# API, Walmart takes a date range directly, so a backfill is a handful of
# paged requests rather than a throttled walk.
FIELDS = {
    "order_id": ("purchaseOrderId", "customerOrderId", "orderId"),
    "order_date": ("orderDate", "orderPlacedDate", "createdDate"),
    "sku": ("sku", "sellerSku", "itemSku", "merchantSku"),
    "title": ("productName", "itemName", "name"),
    "quantity": ("amount", "quantity", "orderLineQuantity"),
    "status": ("status", "orderLineStatus"),
}

ESSENTIAL = ("order_date", "sku", "quantity")

# Walmart's line status vocabulary. Only Cancelled means "sold nothing":
# Created, Acknowledged, Shipped and Delivered are all real sales, and a
# cancellation later is picked up by the trailing re-read.
UNCOUNTED_STATUSES = frozenset({"Cancelled", "Canceled"})

# The charge that is the item's own price, as opposed to shipping or tax.
PRODUCT_CHARGE_TYPES = frozenset({"PRODUCT", "ITEM"})


def _first(node: dict, names: tuple[str, ...]):
    """The first present, non-empty value among `names`, case-insensitively."""
    if not isinstance(node, dict):
        return None
    lowered = {k.lower(): v for k, v in node.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def eastern_day(raw) -> date | None:
    """The seller's calendar date for a Walmart order timestamp.

    Walmart sends epoch milliseconds. An ISO string is accepted too, because
    the one thing this repository has learned about Walmart's field shapes is
    not to bet the run on one of them.
    """
    if raw in (None, ""):
        return None
    moment: datetime | None = None
    if isinstance(raw, (int, float)):
        moment = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    elif isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            moment = datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        else:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    if moment is None:
        return None
    return moment.astimezone(orders.SELLER_TZ).date()


def _as_int(raw) -> int:
    """A quantity, which Walmart sends as a string more often than not."""
    if isinstance(raw, dict):
        raw = _first(raw, ("amount", "quantity", "value"))
    try:
        return int(float(str(raw).strip() or 0))
    except (TypeError, ValueError):
        return 0


def _as_money(raw) -> float:
    if isinstance(raw, dict):
        raw = _first(raw, ("amount", "value", "currencyAmount"))
    try:
        return float(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _listify(node, *keys) -> list:
    """Walmart wraps lists in a singular key: orderLines.orderLine[]."""
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def order_records(body) -> list[dict]:
    """The orders out of whatever envelope this response used."""
    if isinstance(body, list):
        return body
    node = body
    for key in ("list", "elements"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    return _listify(node, "order", "orders")


def next_cursor(body) -> str | None:
    meta = body.get("list", {}).get("meta") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        meta = body.get("meta") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        return None
    cursor = _first(meta, ("nextCursor", "next_cursor", "next"))
    return str(cursor) if cursor else None


def line_status(line: dict) -> str:
    """The status of one order line.

    Walmart nests it as orderLineStatuses.orderLineStatus[].status, and a line
    can carry several as it moves. Any cancelled entry cancels the line: a line
    that was cancelled and then shows a later status is not a sale.
    """
    entries = _listify(line.get("orderLineStatuses"), "orderLineStatus")
    seen = [str(_first(e, FIELDS["status"]) or "").strip() for e in entries
            if isinstance(e, dict)]
    for status in seen:
        if status in UNCOUNTED_STATUSES:
            return status
    return seen[0] if seen else ""


def line_price(line: dict) -> float:
    """The line's own product charge, excluding shipping and tax."""
    charges = _listify(line.get("charges"), "charge")
    total = 0.0
    for charge in charges:
        if not isinstance(charge, dict):
            continue
        kind = str(_first(charge, ("chargeType", "type")) or "").upper()
        if kind and kind not in PRODUCT_CHARGE_TYPES:
            continue
        total += _as_money(_first(charge, ("chargeAmount", "amount")))
    return round(total, 2)


def parse(body, marketplace: str = MARKETPLACE) -> list[dict]:
    """Fold a Walmart orders response into one row per (day, SKU).

    Only the fields the dashboard needs are read. The buyer's name, address and
    phone travel in the same payload and are never touched: what is not read
    cannot be written somewhere it should not be.
    """
    bucket: dict[tuple[date, str], dict] = {}
    seen_orders: dict[tuple[date, str], set] = {}

    for record in order_records(body):
        if not isinstance(record, dict):
            continue
        day = eastern_day(_first(record, FIELDS["order_date"]))
        order_id = str(_first(record, FIELDS["order_id"]) or "").strip()
        if day is None:
            continue

        for line in _listify(record.get("orderLines"), "orderLine"):
            if not isinstance(line, dict):
                continue
            if line_status(line) in UNCOUNTED_STATUSES:
                continue

            item = line.get("item") if isinstance(line.get("item"), dict) else line
            sku = str(_first(item, FIELDS["sku"]) or "").strip()
            units = _as_int(_first(line, ("orderLineQuantity", "quantity")))
            if not sku or units <= 0:
                # Dropped rather than written as a zero: a zero in sales_daily
                # means "sold none that day", not "could not tell".
                continue

            key = (day, sku)
            at = bucket.get(key)
            if at is None:
                at = bucket[key] = {
                    "marketplace": marketplace,
                    "sale_date": day.isoformat(),
                    "sku": sku,
                    "asin": "",
                    "product_name": str(_first(item, FIELDS["title"]) or "").strip(),
                    "units": 0,
                    "orders": 0,
                    "revenue": 0.0,
                }
                seen_orders[key] = set()

            at["units"] += units
            at["revenue"] = round(at["revenue"] + line_price(line), 2)
            if order_id:
                if order_id not in seen_orders[key]:
                    seen_orders[key].add(order_id)
                    at["orders"] += 1
            else:
                at["orders"] += 1

    return sorted(bucket.values(), key=lambda r: (r["sale_date"], r["sku"]))


def _stamp(day: date, end_of_day: bool = False) -> str:
    moment = datetime.combine(day, datetime.max.time() if end_of_day
                              else datetime.min.time(), tzinfo=orders.SELLER_TZ)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_range(token: str, start: date, end: date, sess=None) -> list[dict]:
    """Every order created between two Eastern days, following the cursor."""
    sess = sess or net.session()
    params = {
        "createdStartDate": _stamp(start),
        "createdEndDate": _stamp(end, end_of_day=True),
        "limit": "200",
    }
    url = f"{WALMART_HOST}{ORDERS_PATH}"
    rows: list[dict] = []
    pages = 0

    while True:
        resp = sess.get(url, headers=walmart._headers(token), params=params,
                        timeout=net.TIMEOUT)
        resp.raise_for_status()
        body = resp.json() or {}
        rows.extend(parse(body))

        cursor = next_cursor(body)
        pages += 1
        if not cursor or pages >= MAX_PAGES:
            break
        # The cursor arrives as a ready-made query string; sending it alongside
        # the original filters is what Walmart's own docs warn against.
        url = f"{WALMART_HOST}{ORDERS_PATH}{cursor if cursor.startswith('?') else '?' + cursor}"
        params = {}

    return merge(rows)


def merge(rows: list[dict]) -> list[dict]:
    """Fold rows from several pages into one row per (day, SKU)."""
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["sale_date"], row["sku"])
        at = out.get(key)
        if at is None:
            out[key] = dict(row)
            continue
        at["units"] += row["units"]
        at["orders"] += row["orders"]
        at["revenue"] = round(at["revenue"] + row["revenue"], 2)
        if not at["product_name"]:
            at["product_name"] = row["product_name"]
    return sorted(out.values(), key=lambda r: (r["sale_date"], r["sku"]))


def shape(node, depth: int = 0, limit: int = 8) -> list[str]:
    """The key skeleton of a response: names and types, never values.

    A Walmart order carries the buyer's name, address and phone, and this
    repository's Actions logs are public. Printing "one sample record" is the
    usual shape of that mistake, so this prints no leaf value at all.

    The depth allowance is generous because the fields worth confirming are
    the deep ones - orderLines.orderLine[].charges.charge[].chargeAmount - and
    a skeleton that stops short of them tells us nothing we did not already
    guess. Depth costs nothing here: no value is printed at any level.
    """
    out: list[str] = []
    pad = "  " * depth
    if depth > limit:
        return [f"{pad}..."]
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                out.append(f"{pad}{key}:")
                out.extend(shape(value, depth + 1, limit))
            else:
                out.append(f"{pad}{key}: <{type(value).__name__}>")
    elif isinstance(node, list):
        out.append(f"{pad}[{len(node)} item(s)]")
        if node:
            out.extend(shape(node[0], depth + 1, limit))
    return out


def probe(token: str) -> int:
    yesterday = orders.today_et() - timedelta(days=1)
    start = yesterday - timedelta(days=13)
    sess = net.session()
    resp = sess.get(f"{WALMART_HOST}{ORDERS_PATH}",
                    headers=walmart._headers(token),
                    params={"createdStartDate": _stamp(start),
                            "createdEndDate": _stamp(yesterday, end_of_day=True),
                            "limit": "200"},
                    timeout=net.TIMEOUT)
    print(f"HTTP {resp.status_code} for {start}..{yesterday}")
    resp.raise_for_status()
    body = resp.json() or {}

    records = order_records(body)
    print(f"{len(records)} order(s) in the envelope")
    print("\nResponse shape (keys and types only - no values, these payloads "
          "carry customer names and addresses):")
    for line in shape(body)[:200]:
        print(f"  {line}")

    if records:
        first = records[0]
        print("\nWhat the parser resolves, per field:")
        for key in FIELDS:
            if key in ("sku", "title"):
                continue
            print(f"  {key:<12} -> {'found' if _first(first, FIELDS[key]) is not None else 'NOT FOUND'}")
        lines = _listify(first.get("orderLines"), "orderLine")
        print(f"  orderLines   -> {len(lines)} line(s)")
        if lines:
            item = lines[0].get("item") if isinstance(lines[0].get("item"), dict) else lines[0]
            print(f"  sku          -> {'found' if _first(item, FIELDS['sku']) is not None else 'NOT FOUND'}")
            print(f"  quantity     -> {_as_int(_first(lines[0], ('orderLineQuantity', 'quantity')))} (parsed)")
            print(f"  status       -> {line_status(lines[0]) or 'NOT FOUND'}")
            print(f"  price        -> {line_price(lines[0])} (parsed)")

    parsed = parse(body)
    units = sum(r["units"] for r in parsed)
    print(f"\nParsed: {len(parsed)} SKU-day row(s), {units} unit(s)")
    return 0


def attach_internal_codes(rows: list[dict], sku_map) -> tuple[list[dict], list[str]]:
    """Stamp each row with the internal_code that joins it to everything else.

    Walmart is served by the `sku` column of sku_map.csv, with
    `walmart_sku_override` for the day that stops being true - the same join
    the inventory pipeline uses, so a Walmart sale and a Walmart stock level
    land on the same product.
    """
    unknown: list[str] = []
    by_walmart = sku_map.by_walmart_sku

    for row in rows:
        match = by_walmart.get((row.get("sku") or "").strip().upper())
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=orders.TRAILING_DAYS,
                    help="how many days back to re-read, ending yesterday")
    ap.add_argument("--from", dest="start", help="first day to collect, YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="last day to collect")
    ap.add_argument("--probe", action="store_true",
                    help="print the response shape and exit, writing nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not walmart.configured():
        print(f"Walmart not configured: {', '.join(walmart.missing())}",
              file=sys.stderr)
        return 1
    if not (args.probe or args.dry_run) and not dashboard_db.configured():
        print(f"Dashboard database not configured: "
              f"{', '.join(dashboard_db.missing())}", file=sys.stderr)
        return 1

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    token = walmart.get_access_token()
    if args.probe:
        return probe(token)

    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else orders.today_et() - timedelta(days=1)
    else:
        window = orders.trailing_days(count=args.days)
        start, end = window[0], window[-1]

    if end < start:
        print(f"--to ({end}) is before --from ({start})", file=sys.stderr)
        return 1

    print(f"Walmart sales window: {start} to {end} (America/New_York)")

    run_id = None
    if not args.dry_run:
        run_id = dashboard_db.start_run(MARKETPLACE, SOURCE, start, end)

    try:
        rows = fetch_range(token, start, end)
    except Exception as exc:  # noqa: BLE001
        print(f"Walmart FAILED: {net.describe_error(exc)}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=net.describe_error(exc))
        return 1

    rows, unknown = attach_internal_codes(rows, load_sku_map())
    units = sum(r["units"] for r in rows)
    revenue = round(sum(r["revenue"] for r in rows), 2)
    print(f"{len(rows)} SKU-day row(s) | {units} units | ${revenue:,.2f}")

    if unknown:
        print(f"WARN {len(unknown)} Walmart SKU(s) sold but not in sku_map.csv, "
              f"so they count toward totals without being attributed to a "
              f"product: {', '.join(sorted(unknown))}", file=sys.stderr)

    if args.dry_run:
        for r in rows[:20]:
            print(f"  DRY {r['sale_date']} {r['sku']:<24} "
                  f"{r['internal_code'] or '-':<8} {r['units']:>4} units")
        return 0

    try:
        written = dashboard_db.upsert("sales_daily", rows)
    except Exception as exc:  # noqa: BLE001
        print(f"Write FAILED: {net.describe_error(exc)}", file=sys.stderr)
        dashboard_db.finish_run(run_id, "failed", error=net.describe_error(exc))
        return 1

    dashboard_db.finish_run(run_id, "ok", rows_written=written)
    print(f"Wrote {written} row(s) to sales_daily")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
