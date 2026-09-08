"""Amazon Ads reporting: transport and the column handling around it.

The transport is documented and followed. The *columns* are what this
repository has been bitten by - nine wrong guesses at a Walmart field name,
found only by dumping a live response - so these pin the defensive behaviour:
alternative names resolve, unknown keys survive rather than becoming zeros,
and a failed report is never mistaken for an empty one.

    python -m unittest discover -s tests -v
"""
import gzip
import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import ads  # noqa: E402

ENV = {
    "ADS_CLIENT_ID": "amzn1.application-oa2-client.test",
    "ADS_CLIENT_SECRET": "shh",
    "ADS_REFRESH_TOKEN": "Atzr|refresh",
    "ADS_PROFILE_ID": "123456789",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued responses and records what was asked for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, verb, url, **kw):
        self.calls.append({"verb": verb, "url": url, **kw})
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None, timeout=None, data=None):
        return self._next("POST", url, headers=headers, body=json, data=data)

    def get(self, url, headers=None, timeout=None):
        return self._next("GET", url, headers=headers)


class TestConfiguration(unittest.TestCase):
    def test_all_four_values_are_required(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ads.configured())
            self.assertEqual(len(ads.missing()), 4)
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertTrue(ads.configured())

    def test_the_profile_id_travels_as_the_scope_header(self):
        # No SP-API equivalent, and the step people miss. Without it every
        # call is a 401 that reads like a credential problem.
        with mock.patch.dict(os.environ, ENV, clear=True):
            h = ads._headers("token")
        self.assertEqual(h["Amazon-Advertising-API-Scope"], "123456789")
        self.assertEqual(h["Amazon-Advertising-API-ClientId"], ENV["ADS_CLIENT_ID"])
        self.assertEqual(h["Authorization"], "Bearer token")


class TestAllThreeProgramsAreWired(unittest.TestCase):
    def test_sponsored_products_brands_and_display_all_have_reports(self):
        # Only Sponsored Products runs today. The other two are wired so that
        # switching one on is a settings change, not a code change.
        programs = {s["ad_program"] for s in ads.REPORTS.values()}
        self.assertEqual(programs, {"sponsored_products", "sponsored_brands",
                                    "sponsored_display"})

    def test_search_terms_are_collected_for_sponsored_products(self):
        grains = {k: s["grain"] for k, s in ads.REPORTS.items()}
        self.assertEqual(grains["sp_search_terms"], "search_term")


class TestNormalize(unittest.TestCase):
    SPEC = ads.REPORTS["sp_campaigns"]
    TERMS = ads.REPORTS["sp_search_terms"]

    def test_a_campaign_row_becomes_the_table_shape(self):
        row = {"date": "2026-09-07", "campaignId": 987, "campaignName": "Tins",
               "impressions": 1500, "clicks": 42, "cost": 18.34,
               "purchases7d": 6, "sales7d": 83.94}
        out = ads.normalize(row, self.SPEC)
        self.assertEqual(out["ad_program"], "sponsored_products")
        self.assertEqual(out["ad_date"], "2026-09-07")
        self.assertEqual(out["campaign_id"], "987")     # string, as the PK is
        self.assertEqual(out["spend"], 18.34)
        self.assertEqual(out["attributed_units"], 6)
        self.assertEqual(out["attributed_sales"], 83.94)

    def test_spend_reads_from_cost_or_spend(self):
        # Amazon says "cost" in some reports and "spend" in others. Spend is
        # the numerator of cost-per-unit, so reading the wrong key would show
        # zero advertising cost - a very believable wrong answer.
        a = ads.normalize({"cost": 12.5}, self.SPEC)
        b = ads.normalize({"spend": 12.5}, self.SPEC)
        self.assertEqual(a["spend"], 12.5)
        self.assertEqual(b["spend"], 12.5)

    def test_sales_reads_from_the_7d_or_14d_or_plain_name(self):
        for key in ("sales7d", "sales", "attributedSales14d"):
            out = ads.normalize({key: 100.0}, self.SPEC)
            self.assertEqual(out["attributed_sales"], 100.0, key)

    def test_a_search_term_row_carries_the_term_and_match_type(self):
        row = {"date": "2026-09-07", "campaignId": 1, "searchTerm": "fennel tea",
               "matchType": "BROAD", "clicks": 3, "cost": 1.2}
        out = ads.normalize(row, self.TERMS)
        self.assertEqual(out["search_term"], "fennel tea")
        self.assertEqual(out["match_type"], "BROAD")
        self.assertNotIn("sku", out)          # not part of that table's key

    def test_unknown_columns_are_kept_not_dropped(self):
        # Program-specific metrics - Display's viewable impressions, Brands'
        # video quartiles - land in extra rather than being lost, and a
        # renamed field shows up there instead of silently becoming zero.
        out = ads.normalize({"cost": 1, "viewableImpressions": 900,
                             "videoFirstQuartileViews": 12}, self.SPEC)
        self.assertEqual(out["extra"]["viewableImpressions"], 900)
        self.assertEqual(out["extra"]["videoFirstQuartileViews"], 12)

    def test_a_missing_metric_is_zero_not_a_crash(self):
        out = ads.normalize({"date": "2026-09-07"}, self.SPEC)
        self.assertEqual(out["spend"], 0)
        self.assertEqual(out["clicks"], 0)

    def test_a_non_numeric_metric_does_not_crash_the_run(self):
        out = ads.normalize({"cost": "n/a", "clicks": None}, self.SPEC)
        self.assertEqual(out["spend"], 0)
        self.assertEqual(out["clicks"], 0)

    def test_cpc_is_not_stored(self):
        # Derived at read time. A stored ratio goes stale the moment either
        # side is corrected by a re-ingest.
        out = ads.normalize({"cost": 10, "clicks": 5}, self.SPEC)
        self.assertNotIn("cpc", out)


class TestReportLifecycle(unittest.TestCase):
    SPEC = ads.REPORTS["sp_campaigns"]

    def test_a_report_is_requested_for_the_asked_range(self):
        sess = FakeSession([FakeResponse(200, {"reportId": "r-1"})])
        with mock.patch.dict(os.environ, ENV, clear=True):
            rid = ads.request_report("tok", self.SPEC, date(2026, 9, 1),
                                     date(2026, 9, 7), sess=sess)
        self.assertEqual(rid, "r-1")
        body = sess.calls[0]["body"]
        self.assertEqual(body["startDate"], "2026-09-01")
        self.assertEqual(body["endDate"], "2026-09-07")
        self.assertEqual(body["configuration"]["adProduct"], "SPONSORED_PRODUCTS")
        self.assertEqual(body["configuration"]["timeUnit"], "DAILY")

    def test_a_401_names_the_missed_onboarding_step(self):
        # Approved but unassigned looks exactly like not approved. Saying so
        # in the error saves an afternoon.
        sess = FakeSession([FakeResponse(401, text="Unauthorized")])
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(ads.AdsDenied) as ctx:
                ads.request_report("tok", self.SPEC, date(2026, 9, 1),
                                   date(2026, 9, 7), sess=sess)
        self.assertIn("assigned", str(ctx.exception))

    def test_polling_continues_until_the_report_is_built(self):
        sess = FakeSession([
            FakeResponse(200, {"status": "PENDING"}),
            FakeResponse(200, {"status": "PROCESSING"}),
            FakeResponse(200, {"status": "COMPLETED", "url": "https://dl/x"}),
        ])
        with mock.patch.dict(os.environ, ENV, clear=True):
            url = ads.wait_for_report("tok", "r-1", sess=sess, pause=0)
        self.assertEqual(url, "https://dl/x")
        self.assertEqual(len(sess.calls), 3)

    def test_a_failed_report_raises_rather_than_returning_no_rows(self):
        # Zero rows would publish "no advertising happened", which is a claim
        # about the business, not an absence of data.
        sess = FakeSession([FakeResponse(200, {"status": "FAILURE",
                                               "failureReason": "boom"})])
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                ads.wait_for_report("tok", "r-1", sess=sess, pause=0)
        self.assertIn("boom", str(ctx.exception))

    def test_a_gzipped_report_is_decompressed(self):
        rows = [{"date": "2026-09-07", "cost": 1.5}]
        blob = gzip.compress(json.dumps(rows).encode())
        sess = FakeSession([FakeResponse(200, content=blob)])
        self.assertEqual(ads.download_report("https://dl/x", sess=sess), rows)

    def test_an_already_decompressed_report_also_works(self):
        # Some HTTP layers gunzip transparently; assuming otherwise would
        # crash on a perfectly good report.
        rows = [{"date": "2026-09-07", "cost": 1.5}]
        sess = FakeSession([FakeResponse(200, content=json.dumps(rows).encode())])
        self.assertEqual(ads.download_report("https://dl/x", sess=sess), rows)

    def test_an_empty_report_is_an_empty_list(self):
        sess = FakeSession([FakeResponse(200, content=b"")])
        self.assertEqual(ads.download_report("https://dl/x", sess=sess), [])

    def test_fetch_runs_the_whole_lifecycle(self):
        blob = gzip.compress(json.dumps(
            [{"date": "2026-09-07", "campaignId": 5, "cost": 2.0}]).encode())
        sess = FakeSession([
            FakeResponse(200, {"reportId": "r-9"}),
            FakeResponse(200, {"status": "COMPLETED", "url": "https://dl/y"}),
            FakeResponse(200, content=blob),
        ])
        with mock.patch.dict(os.environ, ENV, clear=True):
            rows = ads.fetch("tok", "sp_campaigns", date(2026, 9, 7),
                             date(2026, 9, 7), sess=sess, pause=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spend"], 2.0)
        self.assertEqual(rows[0]["ad_program"], "sponsored_products")


if __name__ == "__main__":
    unittest.main()
