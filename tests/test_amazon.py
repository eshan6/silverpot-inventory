"""Tests for the Amazon client, against realistic payload shapes.

Every payload below is the documented shape of the real thing: the FBA
Inventory API summary envelope, and the tab-separated headers of the two FBA
inventory reports. The bugs this repo has actually shipped were a stale dict
key and nine wrong guesses at a field name, so the point of these tests is to
pin the shapes rather than to exercise the happy path.

    python -m unittest discover -s tests -v
"""
import contextlib
import gzip
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import amazon, main as collector_main  # noqa: E402
from collector.config import SkuMap, SkuRow  # noqa: E402


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------

SUMMARY = {
    "asin": "B0CJ7XQ8P1",
    "fnSku": "X002ABCDEF",
    "sellerSku": "2201US",
    "condition": "NewItem",
    "inventoryDetails": {
        "fulfillableQuantity": 41,
        "inboundWorkingQuantity": 0,
        "inboundShippedQuantity": 120,
        "inboundReceivingQuantity": 6,
        "reservedQuantity": {
            "totalReservedQuantity": 5,
            "pendingCustomerOrderQuantity": 3,
            "pendingTransshipmentQuantity": 2,
            "fcProcessingQuantity": 0,
        },
        "researchingQuantity": {"totalResearchingQuantity": 1, "researchingQuantityBreakdown": []},
        "unfulfillableQuantity": {"totalUnfulfillableQuantity": 2, "customerDamagedQuantity": 2},
    },
    "lastUpdatedTime": "2026-08-26T04:12:00Z",
    "productName": "Original Assam Black Tea, 50 count",
    "totalQuantity": 175,
}

# GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA
MYI_REPORT = (
    "sku\tfnsku\tasin\tproduct-name\tcondition\tyour-price\tmfn-listing-exists\t"
    "mfn-fulfillable-quantity\tafn-listing-exists\tafn-warehouse-quantity\t"
    "afn-fulfillable-quantity\tafn-unsellable-quantity\tafn-reserved-quantity\t"
    "afn-total-quantity\tper-unit-volume\tafn-inbound-working-quantity\t"
    "afn-inbound-shipped-quantity\tafn-inbound-receiving-quantity\t"
    "afn-researching-quantity\tafn-reserved-future-supply\tafn-future-supply-buyable\n"
    "2201US\tX002ABCDEF\tB0CJ7XQ8P1\tOriginal Assam Black Tea\tNew\t13.99\tNo\t0\tYes\t48\t"
    "41\t2\t5\t175\t0.12\t0\t120\t6\t1\t0\t0\n"
    "2301US\tX002GHIJKL\tB0CJ7XQ8P2\tMasala Chai Pouch\tNew\t21.99\tNo\t0\tYes\t9\t"
    "9\t0\t0\t9\t0.30\t0\t0\t0\t0\t0\t0\n"
)

# GET_AFN_INVENTORY_DATA. One row per warehouse condition, so 2201US appears
# twice: 41 sellable and 3 defective. Only the sellable units are fulfillable.
AFN_REPORT = (
    "seller-sku\tfulfillment-channel-sku\tasin\tcondition-type\t"
    "Warehouse-Condition-code\tQuantity Available\n"
    "2201US\tX002ABCDEF\tB0CJ7XQ8P1\tNew\tSELLABLE\t41\n"
    "2201US\tX002ABCDEF\tB0CJ7XQ8P1\tNew\tDEFECTIVE\t3\n"
    "2301US\tX002GHIJKL\tB0CJ7XQ8P2\tNew\tSELLABLE\t9\n"
)


# --------------------------------------------------------------------------
# A fake SP-API, so nothing here touches the network
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.text = json.dumps(body) if body is not None else content.decode("latin-1")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status at {self.status_code}")


DENIED = {"errors": [{"code": "Unauthorized",
                      "message": "Access to requested resource is denied.",
                      "details": ""}]}


class FakeSession:
    """Routes SP-API paths to canned responses and records what was asked."""

    def __init__(self, *, summaries_status=200, reports_status=200,
                 report_body=MYI_REPORT, compression="GZIP"):
        self.summaries_status = summaries_status
        self.reports_status = reports_status
        self.report_body = report_body
        self.compression = compression
        self.calls: list[tuple[str, str]] = []
        self.doc_headers = None

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        self.calls.append((method, url))
        if "/fba/inventory/v1/summaries" in url:
            if self.summaries_status != 200:
                return FakeResponse(self.summaries_status, DENIED)
            return FakeResponse(200, {"payload": {"inventorySummaries": [SUMMARY]}})
        if url.endswith("/reports") and method == "POST":
            if self.reports_status != 200:
                return FakeResponse(self.reports_status, DENIED)
            return FakeResponse(202, {"reportId": "R1"})
        if url.endswith("/reports/R1"):
            return FakeResponse(200, {"processingStatus": "DONE",
                                      "reportDocumentId": "D1"})
        if url.endswith("/documents/D1"):
            return FakeResponse(200, {"reportDocumentId": "D1",
                                      "url": "https://s3.example/doc",
                                      "compressionAlgorithm": self.compression})
        raise AssertionError(f"unrouted call: {method} {url}")

    def get(self, url, headers=None, timeout=None, params=None):
        if url.startswith("https://s3.example/"):
            self.doc_headers = headers
            raw = self.report_body.encode("utf-8")
            if self.compression == "GZIP":
                raw = gzip.compress(raw)
            return FakeResponse(200, None, raw)
        return self.request("GET", url, headers, timeout, params)


class FakeSessionMixin(unittest.TestCase):
    def use(self, session):
        original = amazon.net.session
        amazon.net.session = lambda *a, **k: session
        self.addCleanup(lambda: setattr(amazon.net, "session", original))
        return session


# --------------------------------------------------------------------------
# Route 1
# --------------------------------------------------------------------------

class TestParseSummary(unittest.TestCase):
    def test_only_fulfillable_is_sellable(self):
        p = amazon.parse_summary(SUMMARY)
        self.assertEqual(p["seller_sku"], "2201US")
        self.assertEqual(p["fulfillable"], 41)
        self.assertEqual(p["inbound_shipped"], 120)
        self.assertEqual(p["inbound_receiving"], 6)
        self.assertEqual(p["reserved_total"], 5)
        self.assertEqual(p["reserved_customer_order"], 3)
        self.assertEqual(p["unfulfillable"], 2)
        self.assertEqual(p["researching"], 1)
        self.assertEqual(p["total_quantity"], 175)

    def test_missing_detail_blocks_do_not_crash(self):
        p = amazon.parse_summary({"sellerSku": "2202US"})
        self.assertEqual(p["fulfillable"], 0)
        self.assertEqual(p["reserved_total"], 0)

    def test_keys_match_what_main_reads(self):
        # main.build() reads these by name. A rename here is the stale-dict-key
        # bug that crashed the sheet write once already.
        needed = {"seller_sku", "fulfillable", "inbound_working", "inbound_shipped",
                  "inbound_receiving", "reserved_total", "unfulfillable", "researching"}
        self.assertTrue(needed <= set(amazon.parse_summary(SUMMARY)))


# --------------------------------------------------------------------------
# Route 2
# --------------------------------------------------------------------------

class TestParseReport(unittest.TestCase):
    def test_myi_report(self):
        rows = {r["seller_sku"]: r for r in amazon.parse_report(MYI_REPORT)}
        self.assertEqual(set(rows), {"2201US", "2301US"})
        a = rows["2201US"]
        self.assertEqual(a["fulfillable"], 41)
        self.assertEqual(a["unfulfillable"], 2)
        self.assertEqual(a["reserved_total"], 5)
        self.assertEqual(a["inbound_shipped"], 120)
        self.assertEqual(a["inbound_receiving"], 6)
        self.assertEqual(a["researching"], 1)
        self.assertEqual(a["total_quantity"], 175)
        self.assertEqual(a["asin"], "B0CJ7XQ8P1")
        self.assertEqual(a["fnsku"], "X002ABCDEF")

    def test_myi_matches_the_api_for_the_same_sku(self):
        # The whole point of the fallback: the publishable number must agree.
        api = amazon.parse_summary(SUMMARY)
        report = {r["seller_sku"]: r for r in amazon.parse_report(MYI_REPORT)}["2201US"]
        self.assertEqual(api["fulfillable"], report["fulfillable"])
        self.assertEqual(api["total_quantity"], report["total_quantity"])

    def test_afn_report_excludes_defective_units(self):
        rows = {r["seller_sku"]: r for r in amazon.parse_report(AFN_REPORT)}
        self.assertEqual(rows["2201US"]["fulfillable"], 41)   # not 44
        self.assertEqual(rows["2201US"]["unfulfillable"], 3)
        self.assertEqual(rows["2301US"]["fulfillable"], 9)

    def test_report_shape_matches_api_shape(self):
        api_keys = set(amazon.parse_summary(SUMMARY))
        report_keys = set(amazon.parse_report(MYI_REPORT)[0])
        self.assertEqual(api_keys, report_keys)

    def test_blank_and_ragged_rows_are_skipped(self):
        text = MYI_REPORT + "\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\n"
        self.assertEqual(len(amazon.parse_report(text)), 2)

    def test_empty_report_is_empty_not_zeroes(self):
        header = MYI_REPORT.split("\n")[0] + "\n"
        self.assertEqual(amazon.parse_report(header), [])


class TestDecodeReport(unittest.TestCase):
    def test_gzip_by_magic_bytes(self):
        self.assertEqual(amazon._decode_report(gzip.compress(b"hi"), "GZIP"), "hi")

    def test_already_decompressed_in_transit(self):
        # requests gunzips Content-Encoding: gzip itself; the metadata still
        # says GZIP. Trusting the metadata here would raise.
        self.assertEqual(amazon._decode_report(b"hi", "GZIP"), "hi")

    def test_cp1252_flat_file(self):
        self.assertIn("Darjeeling", amazon._decode_report(
            "Darjeeling café".encode("cp1252"), None))


# --------------------------------------------------------------------------
# Failover
# --------------------------------------------------------------------------

class TestFetchInventory(FakeSessionMixin):
    def test_uses_the_api_when_it_is_allowed(self):
        s = self.use(FakeSession())
        rows, source = amazon.fetch_fba_inventory("tok")
        self.assertEqual(source, "fba-inventory-api")
        self.assertEqual(rows[0]["fulfillable"], 41)
        self.assertNotIn("POST", [m for m, _u in s.calls])

    def test_403_falls_back_to_reports(self):
        self.use(FakeSession(summaries_status=403))
        rows, source = amazon.fetch_fba_inventory("tok")
        self.assertEqual(source, "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA")
        self.assertEqual({r["seller_sku"] for r in rows}, {"2201US", "2301US"})

    def test_both_routes_denied_raises_with_the_role_hint(self):
        self.use(FakeSession(summaries_status=403, reports_status=403))
        with self.assertRaises(amazon.SpApiDenied) as ctx:
            amazon.fetch_fba_inventory("tok")
        self.assertIn("Amazon Fulfillment", str(ctx.exception))

    def test_presigned_document_download_carries_no_token(self):
        s = self.use(FakeSession(summaries_status=403))
        amazon.fetch_fba_inventory("tok")
        self.assertIsNone(s.doc_headers)

    def test_uncompressed_report_document(self):
        self.use(FakeSession(summaries_status=403, compression=None))
        rows, _ = amazon.fetch_fba_inventory("tok")
        self.assertEqual(len(rows), 2)

    def test_non_403_errors_do_not_trigger_the_fallback(self):
        class Broken(FakeSession):
            def request(self, method, url, **kw):
                if "summaries" in url:
                    raise ConnectionError("no route to host")
                return super().request(method, url, **kw)

        self.use(Broken())
        with self.assertRaises(ConnectionError):
            amazon.fetch_fba_inventory("tok")


# --------------------------------------------------------------------------
# The availability contract, fed from the fallback route
# --------------------------------------------------------------------------

class TestPublishMath(unittest.TestCase):
    def sku_map(self):
        row = SkuRow(internal_code="2201US", product_name="Original Assam",
                     format="tin", sku="2201US", asin="B0CJ7XQ8P1",
                     walmart_sku_override="", website_product_id="",
                     safety_buffer=None)
        return SkuMap(rows=[row])

    def test_report_route_publishes_the_same_number_as_the_api(self):
        api = {"2201US": amazon.parse_summary(SUMMARY)}
        report = {r["seller_sku"]: r for r in amazon.parse_report(MYI_REPORT)}
        wfs = {"2201US": {"available_to_sell": 10}}

        via_api = collector_main.build(self.sku_map(), api, wfs)[0]
        via_report = collector_main.build(self.sku_map(), report, wfs)[0]
        self.assertEqual(via_api["published_available"], via_report["published_available"])
        # No buffer by default: the site shows exactly Amazon + Walmart.
        self.assertEqual(via_api["published_available"], 41 + 10)

    def test_reserved_and_inbound_never_reach_published(self):
        report = {r["seller_sku"]: r for r in amazon.parse_report(MYI_REPORT)}
        out = collector_main.build(self.sku_map(), report, {})[0]
        self.assertEqual(out["raw_available"], 41)
        self.assertEqual(out["fba_inbound"], 126)
        self.assertEqual(out["published_available"], 41)


# --------------------------------------------------------------------------
# diagnose()
# --------------------------------------------------------------------------

class DiagnoseSession:
    """Answers each canary with a status code keyed off its path."""

    def __init__(self, codes: dict[str, int]):
        self.codes = codes

    def get(self, url, headers=None, timeout=None, params=None):
        for fragment, code in self.codes.items():
            if fragment in url:
                return FakeResponse(code, None if code == 200 else DENIED)
        raise AssertionError(f"unrouted canary: {url}")


class TestDiagnose(FakeSessionMixin):
    def run_with(self, codes) -> str:
        self.use(DiagnoseSession(codes))
        original = amazon.get_access_token
        amazon.get_access_token = lambda: "x" * 375
        self.addCleanup(lambda: setattr(amazon, "get_access_token", original))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            amazon.diagnose()
        return buf.getvalue()

    # The real 2026-08-26 run: FBA Inventory answered 200 while Sellers and
    # Catalog Items were denied. The verdict keyed off Sellers first and
    # announced the app was not live, which was flatly wrong.
    def test_fba_200_wins_over_a_denied_sellers_canary(self):
        out = self.run_with({"/sellers/": 403, "/fba/inventory/": 200,
                             "/fba/outbound/": 200, "/orders/": 200,
                             "/catalog/": 403, "/reports": 200})
        self.assertIn("FBA Inventory works", out)
        self.assertNotIn("not live", out)
        self.assertIn("Amazon Fulfillment", out)

    def test_sellers_is_not_labelled_role_free(self):
        # It needs Selling Partner Insights. Calling it role-free is what made
        # its 403 look like evidence about the whole app.
        out = self.run_with({"/sellers/": 403, "/fba/inventory/": 200,
                             "/fba/outbound/": 200, "/orders/": 200,
                             "/catalog/": 403, "/reports": 200})
        self.assertNotIn("no role required", out)
        self.assertIn("Selling Partner Insights", out)

    def test_fba_denied_names_the_missing_role(self):
        out = self.run_with({"/sellers/": 403, "/fba/inventory/": 403,
                             "/fba/outbound/": 403, "/orders/": 200,
                             "/catalog/": 403, "/reports": 200})
        self.assertIn("does not carry the role", out)
        self.assertIn("Inventory and Order Tracking", out)

    def test_everything_denied_says_the_app_is_not_authorized(self):
        out = self.run_with({"/sellers/": 403, "/fba/inventory/": 403,
                             "/fba/outbound/": 403, "/orders/": 403,
                             "/catalog/": 403, "/reports": 403})
        self.assertIn("every endpoint is denied", out)
        self.assertIn("authorized for API calls", out)
        self.assertNotIn("FBA Inventory works", out)


if __name__ == "__main__":
    unittest.main()
