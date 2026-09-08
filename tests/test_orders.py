"""Sales ingestion, against the payload shapes SP-API actually returns.

The bugs this repo has shipped were all shape bugs: a stale dict key, nine
wrong guesses at a Walmart field name. So these use real Orders payloads,
including the details that bite - Amount arrives as a *string*, ItemPrice is
the line total rather than the unit price, and PurchaseDate is UTC while the
business day is Eastern.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import orders  # noqa: E402


def order(order_id, purchase_date, status="Shipped"):
    """A getOrders entry, trimmed to the fields this code reads."""
    return {
        "AmazonOrderId": order_id,
        "PurchaseDate": purchase_date,
        "OrderStatus": status,
        "MarketplaceId": "ATVPDKIKX0DER",
        "SalesChannel": "Amazon.com",
    }


def item(sku, units, amount="27.98", asin="B0AAAAAAAA", title="Original Darjeeling"):
    """A getOrderItems entry. Amount is a string in SP-API, deliberately."""
    return {
        "ASIN": asin,
        "SellerSKU": sku,
        "OrderItemId": "12345678901234",
        "Title": title,
        "QuantityOrdered": units,
        "QuantityShipped": units,
        "ItemPrice": {"CurrencyCode": "USD", "Amount": amount},
    }


class TestEasternDays(unittest.TestCase):
    """The seller's day is Eastern. Getting this wrong misfiles every evening."""

    def test_a_late_evening_order_belongs_to_the_eastern_day(self):
        # 02:15 UTC on the 8th is 22:15 on the 7th in New York. Bucketing on
        # the UTC date would file it under the wrong day, and roughly a fifth
        # of each day's orders land in this window.
        self.assertEqual(
            orders.purchase_day(order("1", "2026-09-08T02:15:00Z")),
            date(2026, 9, 7))

    def test_a_morning_order_is_the_same_day_either_way(self):
        self.assertEqual(
            orders.purchase_day(order("1", "2026-09-08T15:00:00Z")),
            date(2026, 9, 8))

    def test_an_offset_timestamp_is_handled_as_well_as_a_z(self):
        self.assertEqual(
            orders.purchase_day(order("1", "2026-09-08T02:15:00+00:00")),
            date(2026, 9, 7))

    def test_an_unparseable_date_is_dropped_not_guessed(self):
        self.assertIsNone(orders.purchase_day(order("1", "not a date")))
        self.assertIsNone(orders.purchase_day(order("1", "")))

    def test_the_fetch_window_is_eastern_midnight_to_eastern_midnight(self):
        start, end = orders.day_window_utc(date(2026, 9, 7))
        self.assertEqual(start, "2026-09-07T04:00:00Z")   # EDT, UTC-4
        self.assertEqual(end, "2026-09-08T04:00:00Z")

    def test_the_spring_forward_day_is_23_hours(self):
        # 2026-03-08 EST->EDT. Built from the zone, so no special-casing.
        start, end = orders.day_window_utc(date(2026, 3, 8))
        self.assertEqual(start, "2026-03-08T05:00:00Z")   # still EST
        self.assertEqual(end, "2026-03-09T04:00:00Z")     # now EDT

    def test_the_autumn_day_is_25_hours(self):
        start, end = orders.day_window_utc(date(2026, 11, 1))
        self.assertEqual(start, "2026-11-01T04:00:00Z")   # still EDT
        self.assertEqual(end, "2026-11-02T05:00:00Z")     # now EST


class TestItemParsing(unittest.TestCase):
    def test_amount_arrives_as_a_string(self):
        parsed = orders.parse_order_item(item("AA-1111-AAAA", 2, amount="27.98"))
        self.assertEqual(parsed["revenue"], 27.98)
        self.assertEqual(parsed["units"], 2)

    def test_item_price_is_the_line_total_not_the_unit_price(self):
        # Two tins at 13.99 arrive as one 27.98, not as 13.99 to be multiplied.
        # Multiplying would double the revenue on every multi-unit line.
        parsed = orders.parse_order_item(item("AA-1111-AAAA", 2, amount="27.98"))
        self.assertEqual(parsed["revenue"], 27.98)

    def test_a_pending_order_without_a_price_still_counts_its_units(self):
        # Pending orders have no settled price. The unit is spoken for; the
        # trailing re-pull fills the money in later.
        raw = item("AA-1111-AAAA", 3)
        del raw["ItemPrice"]
        parsed = orders.parse_order_item(raw)
        self.assertEqual(parsed["units"], 3)
        self.assertEqual(parsed["revenue"], 0.0)

    def test_a_malformed_quantity_is_zero_rather_than_a_crash(self):
        self.assertEqual(orders.parse_order_item(item("X", "not a number"))["units"], 0)


class TestAggregation(unittest.TestCase):
    def test_units_and_revenue_sum_per_sku_per_day(self):
        os_ = [order("A", "2026-09-07T15:00:00Z"), order("B", "2026-09-07T16:00:00Z")]
        items = {"A": [item("AA-1111-AAAA", 2, "27.98")],
                 "B": [item("AA-1111-AAAA", 1, "13.99")]}
        rows = orders.aggregate_daily(os_, items)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["units"], 3)
        self.assertEqual(rows[0]["revenue"], 41.97)
        self.assertEqual(rows[0]["orders"], 2)
        self.assertEqual(rows[0]["sale_date"], "2026-09-07")

    def test_a_cancelled_order_sold_nothing(self):
        os_ = [order("A", "2026-09-07T15:00:00Z", status="Canceled")]
        items = {"A": [item("AA-1111-AAAA", 5)]}
        self.assertEqual(orders.aggregate_daily(os_, items), [])

    def test_a_pending_order_is_counted(self):
        os_ = [order("A", "2026-09-07T15:00:00Z", status="Pending")]
        items = {"A": [item("AA-1111-AAAA", 2)]}
        rows = orders.aggregate_daily(os_, items)
        self.assertEqual(rows[0]["units"], 2)

    def test_one_order_spanning_two_skus_produces_two_rows(self):
        os_ = [order("A", "2026-09-07T15:00:00Z")]
        items = {"A": [item("AA-1111-AAAA", 1, "13.99"),
                       item("BB-2222-BBBB", 2, "43.98", asin="B0BBBBBBBB",
                            title="Original Fennel")]}
        rows = orders.aggregate_daily(os_, items)
        self.assertEqual([r["sku"] for r in rows], ["AA-1111-AAAA", "BB-2222-BBBB"])
        self.assertEqual(rows[1]["product_name"], "Original Fennel")

    def test_orders_split_across_the_eastern_midnight_land_on_different_days(self):
        os_ = [order("A", "2026-09-08T02:00:00Z"),   # 22:00 ET on the 7th
               order("B", "2026-09-08T05:00:00Z")]   # 01:00 ET on the 8th
        items = {"A": [item("AA-1111-AAAA", 1)], "B": [item("AA-1111-AAAA", 1)]}
        rows = orders.aggregate_daily(os_, items)
        self.assertEqual([r["sale_date"] for r in rows],
                         ["2026-09-07", "2026-09-08"])

    def test_a_line_with_no_sku_is_dropped_not_written_as_zero(self):
        # A zero in sales_daily reads as "sold none". "Could not tell" is a
        # different statement and must not be written as the first one.
        os_ = [order("A", "2026-09-07T15:00:00Z")]
        items = {"A": [item("", 3), item("AA-1111-AAAA", 0)]}
        self.assertEqual(orders.aggregate_daily(os_, items), [])

    def test_marketplace_is_stamped_on_every_row(self):
        os_ = [order("A", "2026-09-07T15:00:00Z")]
        rows = orders.aggregate_daily(os_, {"A": [item("AA-1111-AAAA", 1)]})
        self.assertEqual(rows[0]["marketplace"], "amazon")


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued payloads and records the queries it was asked for."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.queries = []

    def request(self, method, url, headers=None, timeout=None, params=None, **kw):
        self.queries.append({"url": url, "params": params or {}})
        return FakeResponse(self._payloads.pop(0))


class TestFetching(unittest.TestCase):
    def setUp(self):
        # The live pacing between order pages exists for Amazon's rate limit,
        # not for the tests. Left in, it made this suite take two seconds.
        patch = mock.patch.object(orders, "PAGE_PAUSE_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_orders_pagination_follows_next_token(self):
        sess = FakeSession([
            {"payload": {"Orders": [order("A", "2026-09-07T15:00:00Z")],
                         "NextToken": "page2"}},
            {"payload": {"Orders": [order("B", "2026-09-07T16:00:00Z")]}},
        ])
        got = orders.fetch_orders("token", date(2026, 9, 7), sess=sess)
        self.assertEqual([o["AmazonOrderId"] for o in got], ["A", "B"])

    def test_the_second_page_sends_only_the_token(self):
        # SP-API rejects a request carrying both NextToken and the original
        # filters, which is a 400 that only shows up once you have two pages.
        sess = FakeSession([
            {"payload": {"Orders": [], "NextToken": "page2"}},
            {"payload": {"Orders": []}},
        ])
        orders.fetch_orders("token", date(2026, 9, 7), sess=sess)
        second = sess.queries[1]["params"]
        self.assertEqual(second.get("NextToken"), "page2")
        self.assertNotIn("CreatedAfter", second)

    def test_the_first_page_asks_for_an_eastern_day(self):
        sess = FakeSession([{"payload": {"Orders": []}}])
        orders.fetch_orders("token", date(2026, 9, 7), sess=sess)
        params = sess.queries[0]["params"]
        self.assertEqual(params["CreatedAfter"], "2026-09-07T04:00:00Z")
        self.assertEqual(params["CreatedBefore"], "2026-09-08T04:00:00Z")

    def test_order_items_pagination_follows_next_token(self):
        sess = FakeSession([
            {"payload": {"OrderItems": [item("AA-1111-AAAA", 1)],
                         "NextToken": "more"}},
            {"payload": {"OrderItems": [item("BB-2222-BBBB", 2)]}},
        ])
        got = orders.fetch_order_items("token", "A", sess=sess)
        self.assertEqual(len(got), 2)

    def test_fetch_days_skips_item_calls_for_cancelled_orders(self):
        # Every skipped order is a request not made against a tight rate limit.
        sess = FakeSession([
            {"payload": {"Orders": [order("A", "2026-09-07T15:00:00Z", "Canceled"),
                                    order("B", "2026-09-07T16:00:00Z")]}},
            {"payload": {"OrderItems": [item("AA-1111-AAAA", 4)]}},
        ])
        rows = orders.fetch_days("token", [date(2026, 9, 7)], sess=sess)
        self.assertEqual(len(sess.queries), 2)          # orders + one item call
        self.assertEqual(rows[0]["units"], 4)


class TestTrailingWindow(unittest.TestCase):
    def test_the_window_ends_yesterday(self):
        # Today is still in progress; writing a partial Eastern day as complete
        # would make the current day's cost-per-unit look artificially bad.
        days = orders.trailing_days(today=date(2026, 9, 8), count=3)
        self.assertEqual(days, [date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)])

    def test_the_window_is_oldest_first(self):
        days = orders.trailing_days(today=date(2026, 9, 8), count=3)
        self.assertEqual(days, sorted(days))

    def test_re_reading_a_window_is_why_cancellations_get_corrected(self):
        self.assertGreater(orders.TRAILING_DAYS, 1)


if __name__ == "__main__":
    unittest.main()
