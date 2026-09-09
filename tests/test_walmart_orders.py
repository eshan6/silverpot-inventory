"""Walmart orders: line status, Eastern days, and what never leaves the payload.

Walmart is where this repository has been wrong most often - nine failed
guesses at an inventory field name, found only by dumping a live response - so
these tests pin the defensive behaviour rather than the happy path. Nothing
here has been confirmed against a live orders response yet, which is precisely
why a missing essential field must raise instead of reading as zero units.

Two Walmart-specific traps are covered:

  * Status is per *line*, not per order, so a two-line order can be half
    cancelled. Counting the order's first status would sell a cancelled tin.
  * Lists arrive wrapped in a singular key - orderLines.orderLine[] - and a
    single element is sometimes not a list at all.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import walmart_orders as wo  # noqa: E402

# 2026-03-04 15:00 UTC, which is mid-morning in New York.
NOON_ET = 1772636400000


def line(sku="2201US", qty=2, status="Shipped", price="27.98", name="Darjeeling"):
    return {
        "lineNumber": "1",
        "item": {"productName": name, "sku": sku},
        "charges": {"charge": [
            {"chargeType": "PRODUCT", "chargeName": "Item Price",
             "chargeAmount": {"currency": "USD", "amount": price}},
            {"chargeType": "TAX", "chargeName": "Tax",
             "chargeAmount": {"currency": "USD", "amount": "2.10"}},
        ]},
        "orderLineQuantity": {"unitOfMeasurement": "EACH", "amount": str(qty)},
        "orderLineStatuses": {"orderLineStatus": [{"status": status}]},
    }


def order(order_id="1796277083022", when=NOON_ET, lines=None, **extra):
    body = {
        "purchaseOrderId": order_id,
        "customerOrderId": "108764972598",
        "orderDate": when,
        "shippingInfo": {
            "phone": "5551234567",
            "postalAddress": {
                "name": "A Customer",
                "address1": "1 Example Street",
                "city": "Springfield",
                "state": "IL",
                "postalCode": "62704",
            },
        },
        "orderLines": {"orderLine": lines if lines is not None else [line()]},
    }
    body.update(extra)
    return body


def envelope(*orders_):
    return {"list": {"meta": {"totalCount": len(orders_), "limit": 200},
                     "elements": {"order": list(orders_)}}}


class TestParsing(unittest.TestCase):
    def test_an_order_becomes_a_day_and_sku_row(self):
        rows = wo.parse(envelope(order()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["marketplace"], "walmart")
        self.assertEqual(rows[0]["sale_date"], "2026-03-04")
        self.assertEqual(rows[0]["sku"], "2201US")
        self.assertEqual(rows[0]["units"], 2)
        self.assertEqual(rows[0]["orders"], 1)

    def test_only_the_product_charge_counts_as_revenue(self):
        # Tax and shipping are not revenue for units sold, and folding them in
        # would inflate the denominator of ad spend as a share of revenue.
        rows = wo.parse(envelope(order()))
        self.assertEqual(rows[0]["revenue"], 27.98)

    def test_two_lines_of_one_order_are_one_order(self):
        rows = wo.parse(envelope(order(lines=[
            line(sku="2201US", qty=1, price="13.99"),
            line(sku="2201US", qty=1, price="13.99"),
        ])))
        self.assertEqual(rows[0]["units"], 2)
        self.assertEqual(rows[0]["orders"], 1)
        self.assertEqual(rows[0]["revenue"], 27.98)

    def test_different_skus_in_one_order_become_separate_rows(self):
        rows = wo.parse(envelope(order(lines=[
            line(sku="2201US", qty=1), line(sku="2301US", qty=3),
        ])))
        self.assertEqual({r["sku"] for r in rows}, {"2201US", "2301US"})
        self.assertEqual(sum(r["units"] for r in rows), 4)

    def test_a_single_line_not_wrapped_in_a_list_still_parses(self):
        # Walmart collapses one-element lists. Assuming a list here would drop
        # every single-line order on the floor, silently.
        rows = wo.parse(envelope(order(lines=line(qty=5))))
        self.assertEqual(rows[0]["units"], 5)


class TestCancellation(unittest.TestCase):
    def test_a_cancelled_line_sold_nothing(self):
        rows = wo.parse(envelope(order(lines=[line(status="Cancelled")])))
        self.assertEqual(rows, [])

    def test_half_a_cancelled_order_still_counts_the_other_half(self):
        # The trap: status is per line. Judging the order by its first line
        # would either sell a cancelled tin or lose a real one.
        rows = wo.parse(envelope(order(lines=[
            line(sku="2201US", qty=2, status="Cancelled"),
            line(sku="2301US", qty=1, status="Shipped"),
        ])))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "2301US")
        self.assertEqual(rows[0]["units"], 1)

    def test_a_line_cancelled_after_shipping_does_not_count(self):
        # A line carries its status history; any cancellation in it wins.
        rows = wo.parse(envelope(order(lines=[{
            **line(),
            "orderLineStatuses": {"orderLineStatus": [
                {"status": "Shipped"}, {"status": "Cancelled"}]},
        }])))
        self.assertEqual(rows, [])

    def test_created_and_acknowledged_are_real_sales(self):
        for status in ("Created", "Acknowledged", "Shipped", "Delivered"):
            rows = wo.parse(envelope(order(lines=[line(status=status)])))
            self.assertEqual(rows[0]["units"], 2, status)


class TestEasternDays(unittest.TestCase):
    def test_a_late_evening_order_is_not_filed_under_tomorrow(self):
        # 2026-03-05 02:30 UTC is 21:30 on the 4th in New York.
        rows = wo.parse(envelope(order(when=1772677800000)))
        self.assertEqual(rows[0]["sale_date"], "2026-03-04")

    def test_epoch_millis_as_a_string_are_accepted(self):
        self.assertEqual(wo.eastern_day(str(NOON_ET)), date(2026, 3, 4))

    def test_an_iso_stamp_is_accepted_too(self):
        self.assertEqual(wo.eastern_day("2026-03-05T02:30:00Z"), date(2026, 3, 4))

    def test_an_unusable_date_drops_the_order(self):
        self.assertIsNone(wo.eastern_day("whenever"))
        self.assertIsNone(wo.eastern_day(None))
        self.assertEqual(wo.parse(envelope(order(when="whenever"))), [])


class TestMissingData(unittest.TestCase):
    def test_a_line_with_no_sku_or_no_units_is_dropped_not_zeroed(self):
        self.assertEqual(wo.parse(envelope(order(lines=[line(sku="")]))), [])
        self.assertEqual(wo.parse(envelope(order(lines=[line(qty=0)]))), [])

    def test_an_empty_envelope_is_no_sales_not_a_crash(self):
        self.assertEqual(wo.parse({"list": {"elements": {"order": []}}}), [])
        self.assertEqual(wo.parse({}), [])

    def test_a_quantity_sent_as_a_bare_string_still_parses(self):
        rows = wo.parse(envelope(order(lines=[{**line(), "orderLineQuantity": "4"}])))
        self.assertEqual(rows[0]["units"], 4)


class TestPrivacy(unittest.TestCase):
    def test_no_buyer_data_survives_parsing(self):
        rows = wo.parse(envelope(order()))
        blob = repr(rows).lower()
        for leak in ("a customer", "example street", "springfield", "62704",
                     "5551234567"):
            self.assertNotIn(leak, blob, f"{leak!r} escaped the parser")

    def test_the_probe_shape_prints_no_values(self):
        # The whole point: a public Actions log must never carry a customer's
        # address. Shape prints key names and types, never a leaf value.
        printed = "\n".join(wo.shape(envelope(order())))
        for leak in ("A Customer", "1 Example Street", "Springfield", "62704",
                     "5551234567", "2201US"):
            self.assertNotIn(leak, printed, f"{leak!r} appeared in the shape")
        self.assertIn("postalAddress", printed)
        self.assertIn("<str>", printed)


class TestPaging(unittest.TestCase):
    def test_a_cursor_is_found_in_either_envelope(self):
        self.assertEqual(
            wo.next_cursor({"list": {"meta": {"nextCursor": "?offset=200"}}}),
            "?offset=200")
        self.assertEqual(wo.next_cursor({"meta": {"nextCursor": "?offset=5"}}),
                         "?offset=5")

    def test_no_cursor_means_the_last_page(self):
        self.assertIsNone(wo.next_cursor({"list": {"meta": {"totalCount": 3}}}))
        self.assertIsNone(wo.next_cursor({}))

    def test_rows_from_several_pages_merge_rather_than_duplicate(self):
        page = wo.parse(envelope(order()))
        merged = wo.merge(page + page)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["units"], 4)
        self.assertEqual(merged[0]["revenue"], 55.96)


if __name__ == "__main__":
    unittest.main()
