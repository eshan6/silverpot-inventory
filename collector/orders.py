"""Units sold per SKU per day, from the SP-API Orders API.

Feeds the dashboard's Sales section, and the denominator of its Spend section:
every unit actually sold, not the units advertising takes credit for.

Uses the *Inventory and Order Tracking* role, which this app already carries -
`--diagnose` shows Orders returning 200 - so this needs no new approval.

Two things here are easy to get subtly wrong and are the reason most of this
file exists.

**Days are Eastern, not UTC.** An order placed at 22:00 on the 7th in New York
is 02:00 on the 8th in UTC. Bucketing on the UTC date would file a fifth of
each evening's sales under tomorrow, and every daily and monthly total would be
quietly off. The seller's day is `America/New_York`, so the fetch window is
built from Eastern midnight and converted, rather than fetching a UTC day and
hoping.

**Recent days move.** An order can cancel, and a Pending order has no final
price yet, so today's numbers are provisional for a while. Ingestion therefore
re-pulls a trailing window and upserts, instead of writing each day once and
believing it forever.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import net
from .amazon import _request  # same LWA token, same retry/IPv4 session
from .config import US_MARKETPLACE_ID

# Silverpot sells US-only and settles on Eastern time. Stated once, here, so no
# page has to guess which day a sale landed in.
SELLER_TZ = ZoneInfo("America/New_York")

# A cancelled order sold nothing. Everything else counts, including Pending:
# the unit is spoken for, and the trailing re-pull corrects it if it cancels.
UNCOUNTED_STATUSES = frozenset({"Canceled"})

# How far back each run re-reads. Three days covers cancellations and Pending
# orders whose price lands late, without re-reading the year every morning.
TRAILING_DAYS = 3

# Pause between order pages. getOrders is rate limited far below the item
# endpoint, and pacing is cheaper than being throttled into a failed run. A
# module constant rather than a literal so tests can set it to zero: a suite
# that sleeps is a suite people stop running.
PAGE_PAUSE_SECONDS = 1.0

# How long to wait for the order quota to refill after a 429. Measured, not
# guessed: a run on 2026-09-09 collected eight days in twenty-one seconds and
# was then refused, and the session's own retry ladder (up to eight seconds)
# was not enough to clear it. Amazon restores getOrders at about one call a
# minute, so a minute is what a refused day is worth waiting.
QUOTA_REFILL_SECONDS = 65


def today_et() -> date:
    """Today's date in the seller's timezone.

    Stated once so no caller reaches for `date.today()`, which is the runner's
    idea of today and is already tomorrow in UTC for a fifth of every Eastern
    evening.
    """
    return datetime.now(SELLER_TZ).date()


def day_window_utc(day: date) -> tuple[str, str]:
    """The UTC instants bounding one Eastern calendar day.

    Built from the zone rather than a fixed offset, so the 23-hour spring
    forward and 25-hour autumn day are correct without special-casing.
    """
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=SELLER_TZ)
    end_local = datetime.combine(day + timedelta(days=1), datetime.min.time(),
                                 tzinfo=SELLER_TZ)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start_local.astimezone(timezone.utc).strftime(fmt),
            end_local.astimezone(timezone.utc).strftime(fmt))


def purchase_day(order: dict) -> date | None:
    """The Eastern calendar date an order was placed."""
    raw = (order.get("PurchaseDate") or "").strip()
    if not raw:
        return None
    try:
        # SP-API sends RFC3339, usually with a trailing Z that fromisoformat
        # rejects before 3.11.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(SELLER_TZ).date()


def counts_as_sold(order: dict) -> bool:
    return (order.get("OrderStatus") or "").strip() not in UNCOUNTED_STATUSES


def _money(node) -> float:
    """An SP-API money object, or 0.0 when the price is not settled yet."""
    if not isinstance(node, dict):
        return 0.0
    try:
        return float(node.get("Amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_order_item(item: dict) -> dict:
    """One line of an order.

    ItemPrice is the total for the line, not the unit price, so it is taken as
    given rather than multiplied by the quantity.
    """
    try:
        units = int(item.get("QuantityOrdered") or 0)
    except (TypeError, ValueError):
        units = 0
    return {
        "sku": (item.get("SellerSKU") or "").strip(),
        "asin": (item.get("ASIN") or "").strip(),
        "title": (item.get("Title") or "").strip(),
        "units": units,
        "revenue": _money(item.get("ItemPrice")),
    }


def aggregate_daily(orders: list[dict], items_by_order: dict[str, list[dict]],
                    marketplace: str = "amazon") -> list[dict]:
    """Fold orders and their lines into one row per (day, SKU).

    Rows carry zero-unit SKUs out, not in: a line that resolved to no units or
    no SKU is dropped rather than written as a zero, because a zero in
    sales_daily reads as "we sold none that day" and this would mean "we could
    not tell".
    """
    bucket: dict[tuple[date, str], dict] = {}

    for order in orders:
        if not counts_as_sold(order):
            continue
        day = purchase_day(order)
        if day is None:
            continue
        order_id = (order.get("AmazonOrderId") or "").strip()

        for raw in items_by_order.get(order_id, []):
            item = parse_order_item(raw)
            if not item["sku"] or item["units"] <= 0:
                continue
            key = (day, item["sku"])
            row = bucket.get(key)
            if row is None:
                row = bucket[key] = {
                    "marketplace": marketplace,
                    "sale_date": day.isoformat(),
                    "sku": item["sku"],
                    "asin": item["asin"],
                    "product_name": item["title"],
                    "units": 0,
                    "orders": 0,
                    "revenue": 0.0,
                }
            row["units"] += item["units"]
            row["orders"] += 1
            row["revenue"] = round(row["revenue"] + item["revenue"], 2)
            # First non-empty wins; later lines rarely carry these.
            if not row["asin"] and item["asin"]:
                row["asin"] = item["asin"]
            if not row["product_name"] and item["title"]:
                row["product_name"] = item["title"]

    return sorted(bucket.values(), key=lambda r: (r["sale_date"], r["sku"]))


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def fetch_orders(access_token: str, day: date, sess=None) -> list[dict]:
    """Every order placed on one Eastern day, following pagination."""
    sess = sess or net.session()
    created_after, created_before = day_window_utc(day)
    params = {
        "MarketplaceIds": US_MARKETPLACE_ID,
        "CreatedAfter": created_after,
        "CreatedBefore": created_before,
        "MaxResultsPerPage": 100,
    }
    out: list[dict] = []
    next_token = None
    while True:
        query = dict(params)
        if next_token:
            # NextToken replaces the filters; sending both is an error.
            query = {"MarketplaceIds": US_MARKETPLACE_ID, "NextToken": next_token}
        resp = _request(sess, "GET", access_token, "/orders/v0/orders", params=query)
        payload = (resp.json() or {}).get("payload") or {}
        out.extend(payload.get("Orders") or [])
        next_token = payload.get("NextToken")
        if not next_token:
            return out
        if PAGE_PAUSE_SECONDS:
            time.sleep(PAGE_PAUSE_SECONDS)


def fetch_order_items(access_token: str, order_id: str, sess=None) -> list[dict]:
    """The lines of one order, following pagination."""
    sess = sess or net.session()
    out: list[dict] = []
    next_token = None
    while True:
        query = {"NextToken": next_token} if next_token else {}
        resp = _request(sess, "GET", access_token,
                        f"/orders/v0/orders/{order_id}/orderItems", params=query)
        payload = (resp.json() or {}).get("payload") or {}
        out.extend(payload.get("OrderItems") or [])
        next_token = payload.get("NextToken")
        if not next_token:
            return out


def fetch_days(access_token: str, days: list[date], sess=None) -> list[dict]:
    """Aggregated sales rows for a list of Eastern days.

    One call per day plus one per order. Silverpot's volume makes that fine;
    if it ever stops being fine the replacement is the flat-file orders report,
    which trades immediacy for a single request.
    """
    sess = sess or net.session()
    orders: list[dict] = []
    items: dict[str, list[dict]] = {}

    for day in days:
        for order in fetch_orders(access_token, day, sess=sess):
            order_id = (order.get("AmazonOrderId") or "").strip()
            if not order_id or not counts_as_sold(order):
                continue
            orders.append(order)
            items[order_id] = fetch_order_items(access_token, order_id, sess=sess)

    return aggregate_daily(orders, items)


def is_throttled(exc: Exception) -> bool:
    """Did Amazon refuse this because the quota is spent, rather than broken?

    A 429 is not an error in the usual sense - nothing is wrong, the quota is
    simply empty and will refill. Telling it apart from a real failure is what
    lets the backfill wait instead of giving up, which is the difference
    between collecting eight days a run and collecting as many as the hour
    allows.
    """
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 429


def fetch_patiently(access_token: str, days: list[date], sess=None,
                    deadline: float | None = None,
                    wait: int = QUOTA_REFILL_SECONDS,
                    sleep=time.sleep, now=time.monotonic,
                    log=print) -> tuple[list[dict], list[date]]:
    """Fetch days newest-first, waiting out throttles. Returns (rows, done).

    `done` is the days actually collected, which may be fewer than asked for.
    Newest-first so that a short result is contiguous with the history already
    held, leaving no hole in the middle for a cursor to skip past.

    Waiting is bounded by `deadline` (a `time.monotonic` value). Anything that
    is not a throttle stops the walk immediately: a quota refills, a broken
    request does not.
    """
    rows: list[dict] = []
    done: list[date] = []

    for day in sorted(days, reverse=True):
        while True:
            try:
                rows.extend(fetch_days(access_token, [day], sess=sess))
                done.append(day)
                break
            except Exception as exc:  # noqa: BLE001
                if not is_throttled(exc):
                    log(f"  Stopped at {day}: {net.describe_error(exc)}")
                    return rows, sorted(done)
                if deadline is not None and now() + wait >= deadline:
                    log(f"  Quota spent at {day}, and no time left to wait for "
                        f"it - resuming here next run")
                    return rows, sorted(done)
                log(f"  Quota spent at {day}; waiting {wait}s for it to refill")
                sleep(wait)

    return rows, sorted(done)


def trailing_days(today: date | None = None, count: int = TRAILING_DAYS) -> list[date]:
    """The window each run re-reads, oldest first, ending yesterday.

    Yesterday rather than today: an Eastern day is not over until it is over,
    and writing a partial day as if it were complete would make every
    cost-per-unit reading for the current day look artificially bad.
    """
    today = today or today_et()
    end = today - timedelta(days=1)
    return [end - timedelta(days=i) for i in range(count - 1, -1, -1)]
