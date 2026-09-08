"""The dashboard writer and the SKU attribution that feeds it.

Two failures this repo has already shipped are pinned here so they cannot
recur against the dashboard's own database: a Supabase URL pointing at a
website, which answered 200 with HTML and swallowed every write while the run
reported success; and a re-ingest that duplicates rather than corrects.

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import dashboard_db, dashboard_sync  # noqa: E402
from collector.config import SkuMap, SkuRow  # noqa: E402

ENV = {
    "DASHBOARD_SUPABASE_URL": "https://example.supabase.co",
    "DASHBOARD_SUPABASE_SERVICE_KEY": "service-key",
}


class FakeResponse:
    def __init__(self, status_code=201, text="", headers=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"Content-Type": "application/json"}
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None):
        self.calls = []
        self._response = response or FakeResponse()

    def post(self, url, headers=None, json=None, timeout=None, params=None):
        self.calls.append({"verb": "POST", "url": url, "headers": headers,
                           "body": json, "params": params})
        return self._response

    def patch(self, url, headers=None, json=None, timeout=None, params=None):
        self.calls.append({"verb": "PATCH", "url": url, "headers": headers,
                           "body": json, "params": params})
        return self._response


def rows(n):
    return [{"marketplace": "amazon", "sale_date": "2026-09-07",
             "sku": f"SKU-{i}", "units": 1} for i in range(n)]


class TestUpsert(unittest.TestCase):
    def test_re_ingesting_corrects_rather_than_duplicates(self):
        # The trailing window re-reads days that may have gained a
        # cancellation. Without merge-duplicates that is a duplicate-key error
        # at best and double-counted units at worst.
        sess = FakeSession()
        with mock.patch.dict(os.environ, ENV, clear=True):
            dashboard_db.upsert("sales_daily", rows(1), sess=sess)
        self.assertIn("resolution=merge-duplicates", sess.calls[0]["headers"]["Prefer"])

    def test_large_windows_are_batched(self):
        sess = FakeSession()
        with mock.patch.dict(os.environ, ENV, clear=True):
            sent = dashboard_db.upsert("sales_daily", rows(1200), sess=sess)
        self.assertEqual(sent, 1200)
        self.assertEqual(len(sess.calls), 3)          # 500 + 500 + 200

    def test_nothing_to_write_makes_no_request(self):
        sess = FakeSession()
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(dashboard_db.upsert("sales_daily", [], sess=sess), 0)
        self.assertEqual(sess.calls, [])

    def test_the_service_key_travels_in_the_header_never_the_url(self):
        sess = FakeSession()
        with mock.patch.dict(os.environ, ENV, clear=True):
            dashboard_db.upsert("sales_daily", rows(1), sess=sess)
        self.assertEqual(sess.calls[0]["headers"]["apikey"], "service-key")
        self.assertNotIn("service-key", sess.calls[0]["url"])


class TestWrongEndpoint(unittest.TestCase):
    """The 2026-08-26 storefront failure, pinned against the new database."""

    HTML = '<!DOCTYPE html>\n<html lang="en">\n  <head>\n'

    def test_an_html_page_raises_rather_than_counting_as_written(self):
        sess = FakeSession(FakeResponse(
            200, self.HTML, {"Content-Type": "text/html; charset=utf-8"}))
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(dashboard_db.NotSupabase) as ctx:
                dashboard_db.upsert("sales_daily", rows(1), sess=sess)
        self.assertIn("DASHBOARD_SUPABASE_URL", str(ctx.exception))

    def test_html_is_caught_without_a_content_type_too(self):
        sess = FakeSession(FakeResponse(200, self.HTML, {}))
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(dashboard_db.NotSupabase):
                dashboard_db.upsert("sales_daily", rows(1), sess=sess)

    def test_a_4xx_is_a_hard_failure(self):
        sess = FakeSession(FakeResponse(400, "duplicate key"))
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(RuntimeError):
                dashboard_db.upsert("sales_daily", rows(1), sess=sess)


class TestRunLedger(unittest.TestCase):
    def test_a_run_is_opened_with_the_window_it_covers(self):
        sess = FakeSession(FakeResponse(201, payload=[{"id": 7}]))
        with mock.patch.dict(os.environ, ENV, clear=True):
            run_id = dashboard_db.start_run("amazon", "sp-api-orders",
                                            "2026-09-05", "2026-09-07", sess=sess)
        self.assertEqual(run_id, 7)
        body = sess.calls[0]["body"][0]
        self.assertEqual(body["covers_from"], "2026-09-05")
        self.assertEqual(body["status"], "running")

    def test_bookkeeping_failure_never_sinks_the_run(self):
        # If the ledger cannot be written the facts are still worth writing.
        class Broken(FakeSession):
            def post(self, *a, **kw):
                raise ConnectionError("nope")

        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertIsNone(dashboard_db.start_run(
                "amazon", "sp-api-orders", "2026-09-05", "2026-09-07",
                sess=Broken()))

    def test_finishing_a_run_that_never_opened_is_a_no_op(self):
        sess = FakeSession()
        with mock.patch.dict(os.environ, ENV, clear=True):
            dashboard_db.finish_run(None, "ok", 5, sess=sess)
        self.assertEqual(sess.calls, [])

    def test_a_failure_is_recorded_with_its_reason(self):
        sess = FakeSession(FakeResponse(204))
        with mock.patch.dict(os.environ, ENV, clear=True):
            dashboard_db.finish_run(7, "failed", error="503 unavailable", sess=sess)
        self.assertEqual(sess.calls[0]["body"]["status"], "failed")
        self.assertIn("503", sess.calls[0]["body"]["error"])


def product(code, sku, aliases=()):
    return SkuRow(internal_code=code, product_name=f"Tea {code}", format="tin",
                  sku=sku, asin="", walmart_sku_override="",
                  website_product_id="", safety_buffer=None,
                  amazon_sku_aliases=list(aliases), active=True)


class TestAttribution(unittest.TestCase):
    MAP = SkuMap(rows=[product("2201US", "AA-1111-AAAA",
                               ["AA-1111-AAAA-stickerless"])])

    def test_a_sold_sku_gets_the_join_key(self):
        rows_, unknown = dashboard_sync.attach_internal_codes(
            [{"sku": "AA-1111-AAAA", "units": 2}], self.MAP)
        self.assertEqual(rows_[0]["internal_code"], "2201US")
        self.assertEqual(unknown, [])

    def test_a_stickerless_twin_resolves_to_the_same_product(self):
        # Same pooling rule the inventory feed uses. Without it, half a tea's
        # sales would land under a SKU nothing recognises.
        rows_, unknown = dashboard_sync.attach_internal_codes(
            [{"sku": "AA-1111-AAAA-stickerless", "units": 3}], self.MAP)
        self.assertEqual(rows_[0]["internal_code"], "2201US")
        self.assertEqual(unknown, [])

    def test_matching_is_case_insensitive(self):
        rows_, _ = dashboard_sync.attach_internal_codes(
            [{"sku": "aa-1111-aaaa", "units": 1}], self.MAP)
        self.assertEqual(rows_[0]["internal_code"], "2201US")

    def test_an_unknown_sku_keeps_its_units_and_is_named(self):
        # Dropping it would understate units sold, and units sold is the
        # denominator of cost-per-unit: understating it makes ad spend look
        # better than it is, which is the wrong direction to be wrong in.
        rows_, unknown = dashboard_sync.attach_internal_codes(
            [{"sku": "ZZ-9999-ZZZZ", "units": 4}], self.MAP)
        self.assertEqual(len(rows_), 1)
        self.assertEqual(rows_[0]["units"], 4)
        self.assertIsNone(rows_[0]["internal_code"])
        self.assertEqual(unknown, ["ZZ-9999-ZZZZ"])

    def test_a_product_name_from_the_map_fills_a_blank(self):
        rows_, _ = dashboard_sync.attach_internal_codes(
            [{"sku": "AA-1111-AAAA", "units": 1, "product_name": ""}], self.MAP)
        self.assertEqual(rows_[0]["product_name"], "Tea 2201US")

    def test_a_title_from_amazon_is_not_overwritten(self):
        rows_, _ = dashboard_sync.attach_internal_codes(
            [{"sku": "AA-1111-AAAA", "units": 1,
              "product_name": "Original Darjeeling 50ct"}], self.MAP)
        self.assertEqual(rows_[0]["product_name"], "Original Darjeeling 50ct")


class TestConfiguration(unittest.TestCase):
    def test_not_configured_until_both_are_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(dashboard_db.configured())
            self.assertEqual(len(dashboard_db.missing()), 2)
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertTrue(dashboard_db.configured())
            self.assertEqual(dashboard_db.missing(), [])

    def test_the_dashboard_uses_its_own_project_not_the_storefront_s(self):
        # A Lovable migration on the storefront must not be able to collide
        # with these tables, and one leaked key must not reach both.
        self.assertNotIn("SUPABASE_URL", dashboard_db.REQUIRED)
        self.assertIn("DASHBOARD_SUPABASE_URL", dashboard_db.REQUIRED)


if __name__ == "__main__":
    unittest.main()
