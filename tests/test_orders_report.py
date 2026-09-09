"""The orders report: column names, day bucketing, and what is never read.

Two failure modes matter here and both are silent.

A column name that does not match reads as zero units - the report parses, the
run goes green, and the dashboard says the business sold nothing in March.
That is why a missing essential column raises instead.

The other is privacy. An all-orders report carries buyer names, emails and
addresses, and this repository's Actions logs are public. The parser must take
the six columns it needs and leave the rest untouched.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import orders_report  # noqa: E402

HEADER = [
    "amazon-order-id", "merchant-order-id", "purchase-date",
    "last-updated-date", "order-status", "fulfillment-channel", "sales-channel",
    "order-channel", "ship-service-level", "product-name", "sku", "asin",
    "item-status", "quantity", "currency", "item-price", "item-tax",
    "shipping-price", "shipping-tax", "ship-service-level-group",
    "buyer-email", "buyer-name", "buyer-phone-number",
    "ship-address-1", "ship-city", "ship-state", "ship-postal-code",
]


def tsv(rows: list[dict], header=HEADER) -> str:
    out = ["\t".join(header)]
    for row in rows:
        out.append("\t".join(str(row.get(h, "")) for h in header))
    return "\n".join(out) + "\n"


def order(order_id="111-2222222-3333333", purchase="2026-03-04T10:15:00-05:00",
          status="Shipped", sku="AA-1111-AAAA", qty=2, price="27.98", **extra):
    row = {
        "amazon-order-id": order_id,
        "purchase-date": purchase,
        "order-status": status,
        "product-name": "Silverpot Darjeeling Tea Bags, 50 Count",
        "sku": sku,
        "asin": "B0XXXXXXX1",
        "quantity": qty,
        "item-price": price,
        "buyer-email": "someone@example.com",
        "buyer-name": "A Customer",
        "ship-address-1": "1 Example Street",
        "ship-city": "Springfield",
        "ship-postal-code": "12345",
    }
    row.update(extra)
    return row


class TestParsing(unittest.TestCase):
    def test_a_line_becomes_a_day_and_sku_row(self):
        rows = orders_report.parse(tsv([order()]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sale_date"], "2026-03-04")
        self.assertEqual(rows[0]["sku"], "AA-1111-AAAA")
        self.assertEqual(rows[0]["units"], 2)
        self.assertEqual(rows[0]["revenue"], 27.98)
        self.assertEqual(rows[0]["orders"], 1)

    def test_lines_of_the_same_order_count_as_one_order(self):
        rows = orders_report.parse(tsv([
            order(qty=1, price="13.99"),
            order(qty=1, price="13.99"),          # same order id, second line
        ]))
        self.assertEqual(rows[0]["units"], 2)
        self.assertEqual(rows[0]["revenue"], 27.98)
        self.assertEqual(rows[0]["orders"], 1)

    def test_separate_orders_are_counted_separately(self):
        rows = orders_report.parse(tsv([
            order(order_id="111-0000001-0000001"),
            order(order_id="111-0000002-0000002"),
        ]))
        self.assertEqual(rows[0]["orders"], 2)

    def test_a_cancelled_order_sold_nothing(self):
        rows = orders_report.parse(tsv([order(status="Canceled")]))
        self.assertEqual(rows, [])

    def test_a_pending_order_still_counts(self):
        # Same rule as the API path: the unit is spoken for, and a re-read
        # corrects it if it later cancels.
        rows = orders_report.parse(tsv([order(status="Pending")]))
        self.assertEqual(rows[0]["units"], 2)

    def test_a_line_with_no_sku_or_no_units_is_dropped_not_zeroed(self):
        # A zero row in sales_daily reads as "sold none that day", which is a
        # different and probably false claim from "could not tell".
        self.assertEqual(orders_report.parse(tsv([order(sku="")])), [])
        self.assertEqual(orders_report.parse(tsv([order(qty=0)])), [])
        self.assertEqual(orders_report.parse(tsv([order(qty="")])), [])


class TestEasternDays(unittest.TestCase):
    def test_a_late_evening_pacific_order_is_not_filed_under_tomorrow(self):
        # 21:15 Pacific is 00:15 UTC the next day. Bucketing on the printed
        # date, or on UTC, would move a slice of every evening's sales.
        rows = orders_report.parse(tsv([order(purchase="2026-03-04T21:15:00-08:00")]))
        self.assertEqual(rows[0]["sale_date"], "2026-03-05")

    def test_a_utc_stamp_is_converted_rather_than_truncated(self):
        rows = orders_report.parse(tsv([order(purchase="2026-03-05T02:30:00Z")]))
        self.assertEqual(rows[0]["sale_date"], "2026-03-04")

    def test_an_unparseable_date_drops_the_line(self):
        self.assertEqual(orders_report.parse(tsv([order(purchase="soon")])), [])

    def test_eastern_day_handles_a_naive_stamp_as_utc(self):
        self.assertEqual(orders_report.eastern_day("2026-03-05T02:30:00"),
                         date(2026, 3, 4))


class TestColumnNames(unittest.TestCase):
    def test_alternative_names_resolve(self):
        header = [h.replace("sku", "seller-sku").replace("quantity", "quantity-purchased")
                  for h in HEADER]
        rows = orders_report.parse(tsv([{
            "amazon-order-id": "1", "purchase-date": "2026-03-04T10:00:00-05:00",
            "order-status": "Shipped", "seller-sku": "AA-1111-AAAA",
            "quantity-purchased": 3, "item-price": "41.97",
        }], header=header))
        self.assertEqual(rows[0]["units"], 3)

    def test_a_missing_essential_column_raises_rather_than_reading_as_zero(self):
        header = [h for h in HEADER if h != "quantity"]
        with self.assertRaises(RuntimeError) as ctx:
            orders_report.parse(tsv([order()], header=header))
        self.assertIn("units", str(ctx.exception))
        self.assertIn("--probe", str(ctx.exception))

    def test_an_empty_report_is_an_error_not_an_empty_month(self):
        with self.assertRaises(RuntimeError):
            orders_report.parse("")

    def test_a_report_with_only_a_header_is_a_month_with_no_sales(self):
        self.assertEqual(orders_report.parse("\t".join(HEADER) + "\n"), [])

    def test_column_names_reads_the_header_only(self):
        text = tsv([order()])
        self.assertEqual(orders_report.column_names(text), HEADER)


class TestPrivacy(unittest.TestCase):
    def test_no_buyer_data_survives_parsing(self):
        # The point is not that we avoid printing it later; it is that it is
        # never carried out of the row in the first place.
        rows = orders_report.parse(tsv([order()]))
        blob = repr(rows).lower()
        for leak in ("someone@example.com", "a customer", "example street",
                     "springfield", "12345"):
            self.assertNotIn(leak, blob, f"{leak!r} escaped the parser")

    def test_only_the_expected_keys_are_produced(self):
        rows = orders_report.parse(tsv([order()]))
        self.assertEqual(set(rows[0]), {
            "marketplace", "sale_date", "sku", "asin", "product_name",
            "units", "orders", "revenue"})


class TestWindows(unittest.TestCase):
    def test_a_year_splits_into_month_sized_reports(self):
        spans = orders_report.windows(date(2026, 1, 1), date(2026, 9, 5))
        self.assertEqual(spans[0], (date(2026, 1, 1), date(2026, 1, 30)))
        self.assertEqual(spans[-1][1], date(2026, 9, 5))
        self.assertLessEqual(len(spans), 10)

    def test_windows_are_contiguous_and_never_overlap(self):
        spans = orders_report.windows(date(2026, 1, 1), date(2026, 9, 5))
        for (_, earlier_end), (later_start, _) in zip(spans, spans[1:]):
            self.assertEqual((later_start - earlier_end).days, 1)

    def test_a_single_day_is_one_window(self):
        self.assertEqual(orders_report.windows(date(2026, 5, 1), date(2026, 5, 1)),
                         [(date(2026, 5, 1), date(2026, 5, 1))])


if __name__ == "__main__":
    unittest.main()
