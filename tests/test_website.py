"""Tests for the Supabase writer, against silverpottea.com's real schema.

`public.product_inventory` holds: id, product_id, sku, quantity, created_at,
updated_at, inventory_source. There is no `inventory_synced_at` - `updated_at`
is maintained by a database trigger. Naming a column that does not exist makes
Supabase reject the write, so the shape of the request body is the thing worth
pinning.

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import website  # noqa: E402

LIVE_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "service-key",
    "SUPABASE_TABLE": "product_inventory",
    "SUPABASE_SKU_COLUMN": "sku",
    "SUPABASE_QTY_COLUMN": "quantity",
}

RESULTS = [
    {"sku": "R9-D7AT-S5WW", "published_available": 55},
    {"sku": "06-2F24-Y8AJ", "published_available": 51},
]


class FakeResponse:
    """Mimics requests.Response closely enough to exercise the parsing."""

    def __init__(self, rows, status_code=200, text=None, headers=None):
        self._rows = rows
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        if text is not None:
            self.text = text
        else:
            self.text = "[]" if rows == [] else "[{}]"

    def json(self):
        # requests raises on an unparseable body rather than returning None.
        import json as _json
        return _json.loads(self.text) if self._rows is None else self._rows


class PushTestCase(unittest.TestCase):
    def push(self, results=RESULTS, rows=None, env=None, **kw):
        """Run push() against a fake Supabase, returning (summary, calls)."""
        calls = []

        def fake_patch(url, headers=None, params=None, json=None, timeout=None):
            calls.append({"url": url, "params": params, "body": json,
                          "headers": headers})
            return FakeResponse([{"id": 1}] if rows is None else rows)

        with mock.patch.dict(os.environ, {**LIVE_ENV, **(env or {})}, clear=True), \
                mock.patch.object(website.requests, "patch", fake_patch):
            summary = website.push(results, **kw)
        return summary, calls


class TestRequestShape(PushTestCase):
    def test_body_carries_only_the_quantity(self):
        # The regression that would have broken every write on day one: the
        # old default added `inventory_synced_at`, which this schema lacks.
        _s, calls = self.push()
        self.assertEqual(calls[0]["body"], {"quantity": 55})
        self.assertNotIn("inventory_synced_at", calls[0]["body"])

    def test_a_synced_column_is_written_only_when_configured(self):
        _s, calls = self.push(env={"SUPABASE_SYNCED_COLUMN": "synced_at"})
        self.assertIn("synced_at", calls[0]["body"])

    def test_matches_on_sku_and_skips_manual_rows(self):
        _s, calls = self.push()
        self.assertEqual(calls[0]["params"]["sku"], "eq.R9-D7AT-S5WW")
        self.assertEqual(calls[0]["params"]["inventory_source"], "not.eq.manual")

    def test_manual_rows_can_be_overwritten_when_explicitly_allowed(self):
        _s, calls = self.push(env={"RESPECT_MANUAL_OVERRIDE": "false"})
        self.assertNotIn("inventory_source", calls[0]["params"])

    def test_writes_to_the_configured_table(self):
        _s, calls = self.push()
        self.assertTrue(calls[0]["url"].endswith("/rest/v1/product_inventory"))

    def test_the_service_key_is_sent_but_never_in_the_url(self):
        _s, calls = self.push()
        self.assertEqual(calls[0]["headers"]["apikey"], "service-key")
        self.assertNotIn("service-key", calls[0]["url"])


class TestOutcomes(PushTestCase):
    def test_every_sku_is_attempted(self):
        summary, calls = self.push()
        self.assertEqual(len(calls), 2)
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(summary["no_row_updated"], [])

    def test_a_row_that_matched_nothing_is_reported_not_assumed_written(self):
        # No row updated means the SKU is absent or pinned to manual. Either
        # way it must be named rather than counted as a success.
        summary, _calls = self.push(rows=[])
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["no_row_updated"],
                         ["R9-D7AT-S5WW", "06-2F24-Y8AJ"])

    def test_a_failed_write_raises_rather_than_reporting_success(self):
        def failing_patch(url, headers=None, params=None, json=None, timeout=None):
            return FakeResponse(None, status_code=400)

        with mock.patch.dict(os.environ, LIVE_ENV, clear=True), \
                mock.patch.object(website.requests, "patch", failing_patch):
            with self.assertRaises(RuntimeError) as ctx:
                website.push(RESULTS)
        self.assertIn("R9-D7AT-S5WW", str(ctx.exception))

    def test_dry_run_writes_nothing(self):
        _s, calls = self.push(dry_run=True)
        self.assertEqual(calls, [])

    def test_rows_without_a_sku_are_skipped(self):
        summary, calls = self.push(results=[{"sku": "", "published_available": 5}])
        self.assertEqual(calls, [])
        self.assertEqual(summary["updated"], 0)


class TestConfiguration(unittest.TestCase):
    def test_not_configured_until_all_five_are_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(website.configured())
            self.assertEqual(len(website.missing()), 5)
        with mock.patch.dict(os.environ, LIVE_ENV, clear=True):
            self.assertTrue(website.configured())
            self.assertEqual(website.missing(), [])


class TestUnreadableResponses(PushTestCase):
    """The live 2026-08-26 failure: a success status with a non-JSON body.

    push() called resp.json() unconditionally and raised JSONDecodeError,
    aborting the run after the writes had already been sent. A response the
    code cannot read is not a reason to lose the work.
    """

    def push_raw(self, **response_kw):
        calls = []

        def fake_patch(url, headers=None, params=None, json=None, timeout=None):
            calls.append(params)
            return FakeResponse(None, **response_kw)

        with mock.patch.dict(os.environ, LIVE_ENV, clear=True), \
                mock.patch.object(website.requests, "patch", fake_patch):
            return website.push(RESULTS), calls

    def test_a_non_json_body_does_not_crash_the_run(self):
        summary, calls = self.push_raw(text="<html>ok</html>",
                                       headers={"Content-Type": "text/html"})
        self.assertEqual(len(calls), 2)          # both writes still went out
        self.assertEqual(summary["unconfirmed"], ["R9-D7AT-S5WW", "06-2F24-Y8AJ"])
        self.assertEqual(summary["updated"], 0)

    def test_204_no_content_is_unconfirmed_not_a_missing_row(self):
        # Supabase honouring Prefer=return=minimal. The row may well have been
        # updated; calling it "no row updated" would invent a failure.
        summary, _calls = self.push_raw(status_code=204, text="")
        self.assertEqual(summary["unconfirmed"], ["R9-D7AT-S5WW", "06-2F24-Y8AJ"])
        self.assertEqual(summary["no_row_updated"], [])

    def test_an_empty_body_is_unconfirmed(self):
        summary, _calls = self.push_raw(text="   ")
        self.assertEqual(summary["unconfirmed"], ["R9-D7AT-S5WW", "06-2F24-Y8AJ"])

    def test_a_single_object_body_counts_as_one_updated_row(self):
        summary, _calls = self.push_raw(text='{"id": 1}')
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(summary["unconfirmed"], [])

    def test_an_empty_array_still_means_nothing_matched(self):
        # The distinction that matters: [] is Supabase telling us zero rows
        # changed. That is information, and must stay a reported miss.
        summary, _calls = self.push_raw(text="[]")
        self.assertEqual(summary["no_row_updated"], ["R9-D7AT-S5WW", "06-2F24-Y8AJ"])
        self.assertEqual(summary["unconfirmed"], [])

    def test_a_4xx_still_raises_rather_than_being_called_unconfirmed(self):
        with self.assertRaises(RuntimeError):
            self.push_raw(status_code=400, text="permission denied")


if __name__ == "__main__":
    unittest.main()
