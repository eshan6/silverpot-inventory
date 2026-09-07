"""What makes a run fail, and what merely warns.

The stickerless pools were invisible for months while the pipeline printed a
correct warning about them on every run. The warning was never the problem -
a green run was. An Amazon SKU holding fulfillable stock that no row claims
now fails the run, so it arrives as a failure notification instead of a line
in a log nobody opens.

These drive main() end to end with --dry-run, which touches no sheet, no
website and no network. The assertion is the exit code.

TestWriteOrder drives the full path instead, with every sink faked, because
the order the sinks are written in is itself a decision worth pinning.

    python -m unittest discover -s tests -v
"""
import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collector  # noqa: E402
from collector import main as run  # noqa: E402
from collector import website as website_mod  # noqa: E402
from collector.config import SkuMap, SkuRow  # noqa: E402


@contextlib.contextmanager
def fake_sheets(**funcs):
    """Stand in for collector.sheets for the duration of one run.

    main() imports it lazily (`from . import sheets`), so a stub in sys.modules
    is picked up instead of the real one. That keeps the suite free of gspread
    and google-auth: these tests are about the order sinks are written in, not
    about Google's client library, and a test that needs a crypto stack to
    check a call order is a test that stops being run.
    """
    stub = types.ModuleType("collector.sheets")
    for name, fn in funcs.items():
        setattr(stub, name, fn)
    real = sys.modules.get("collector.sheets")
    sys.modules["collector.sheets"] = stub
    collector.sheets = stub
    try:
        yield stub
    finally:
        if real is not None:
            sys.modules["collector.sheets"] = real
            collector.sheets = real
        else:
            sys.modules.pop("collector.sheets", None)
            if hasattr(collector, "sheets"):
                del collector.sheets


def product(code, sku, asin, aliases=()):
    return SkuRow(
        internal_code=code, product_name=f"Tea {code}", format="tin", sku=sku,
        asin=asin, walmart_sku_override="", website_product_id="",
        safety_buffer=None, amazon_sku_aliases=list(aliases), active=True,
    )


MAP = SkuMap(rows=[
    product("2201US", "AA-1111-AAAA", "B0AAAAAAAA", ["AA-1111-AAAA-stickerless"]),
    product("2202US", "BB-2222-BBBB", "B0BBBBBBBB"),
])


def fba(seller_sku, fulfillable, asin=""):
    return {"seller_sku": seller_sku, "fulfillable": fulfillable, "asin": asin}


class RunTestCase(unittest.TestCase):
    """Runs main() --dry-run against a synthetic Amazon response."""

    def run_sync(self, fba_rows, ignored=None, amazon_ok=True, sku_map=MAP):
        out, err = io.StringIO(), io.StringIO()

        def fetch(_token):
            return list(fba_rows), "test-source"

        def boom(_token):
            raise RuntimeError("403 Unauthorized")

        with mock.patch.object(run, "load_sku_map", lambda *a, **k: sku_map), \
                mock.patch.object(run, "load_ignored_amazon_skus",
                                  lambda *a, **k: dict(ignored or {})), \
                mock.patch.object(run.net, "apply_ipv4_preference", lambda: False), \
                mock.patch.object(run.amazon, "configured", lambda: True), \
                mock.patch.object(run.amazon, "get_access_token", lambda: "token"), \
                mock.patch.object(run.amazon, "fetch_fba_inventory",
                                  fetch if amazon_ok else boom), \
                mock.patch.object(run.walmart, "configured", lambda: True), \
                mock.patch.object(run.walmart, "get_access_token", lambda: "token"), \
                mock.patch.object(run.walmart, "fetch_wfs_inventory",
                                  lambda _t: ([], {})), \
                mock.patch.object(sys, "argv", ["collector.main", "--dry-run"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main()
        return code, err.getvalue()


class TestUnmappedStockFailsTheRun(RunTestCase):
    def test_an_unclaimed_sku_holding_stock_fails(self):
        code, err = self.run_sync([
            fba("AA-1111-AAAA", 10, "B0AAAAAAAA"),
            fba("ZZ-9999-ZZZZ", 47, "B0ZZZZZZZZ"),
        ])
        self.assertEqual(code, 1)
        self.assertIn("ZZ-9999-ZZZZ", err)
        self.assertIn("47", err)

    def test_the_failure_names_the_total_withheld(self):
        code, err = self.run_sync([
            fba("ZZ-9999-ZZZZ", 47, "B0ZZZZZZZZ"),
            fba("YY-8888-YYYY", 3, "B0YYYYYYYY"),
        ])
        self.assertEqual(code, 1)
        self.assertIn("2 Amazon SKU(s) hold 50 fulfillable unit(s)", err)

    def test_it_says_how_to_resolve_it(self):
        _code, err = self.run_sync([fba("ZZ-9999-ZZZZ", 5, "B0ZZZZZZZZ")])
        self.assertIn("amazon_sku_aliases", err)
        self.assertIn("ignored_amazon_skus.csv", err)

    def test_a_second_listing_of_a_mapped_product_is_identified_by_asin(self):
        # The stickerless case exactly: a new SKU carrying a known ASIN. The
        # fix is one alias, and the run should say so rather than pose a riddle.
        code, err = self.run_sync([fba("AA-1111-NEW", 12, "B0AAAAAAAA")])
        self.assertEqual(code, 1)
        self.assertIn("same ASIN as 2201US", err)
        self.assertIn("add as an alias", err)


class TestWhatDoesNotFail(RunTestCase):
    def test_an_unclaimed_sku_at_zero_does_not_fail(self):
        # Retired listings sit at zero forever. Failing on those would train
        # everyone to ignore the failure, which is how we got here.
        code, err = self.run_sync([
            fba("AA-1111-AAAA", 10, "B0AAAAAAAA"),
            fba("ZZ-9999-ZZZZ", 0, "B0ZZZZZZZZ"),
        ])
        self.assertEqual(code, 0)
        self.assertIn("1 further Amazon SKU(s) are unmapped but hold no", err)

    def test_an_ignored_sku_holding_stock_does_not_fail(self):
        # ignored_amazon_skus.csv is the escape hatch, and the reason column is
        # the price of using it. Eshan's four retired parent SKUs live here.
        code, _err = self.run_sync(
            [fba("ZZ-9999-ZZZZ", 208, "B0ZZZZZZZZ")],
            ignored={"ZZ-9999-ZZZZ": "retired parent SKU"},
        )
        self.assertEqual(code, 0)

    def test_stock_under_an_alias_is_claimed_not_unmapped(self):
        code, _err = self.run_sync([
            fba("AA-1111-AAAA", 4, "B0AAAAAAAA"),
            fba("AA-1111-AAAA-stickerless", 31, "B0AAAAAAAA"),
        ])
        self.assertEqual(code, 0)

    def test_everything_mapped_passes(self):
        code, _err = self.run_sync([
            fba("AA-1111-AAAA", 10, "B0AAAAAAAA"),
            fba("BB-2222-BBBB", 20, "B0BBBBBBBB"),
        ])
        self.assertEqual(code, 0)

    def test_a_failed_amazon_leg_does_not_masquerade_as_unmapped_stock(self):
        # With no FBA response there is nothing to be unmapped. A degraded run
        # is its own condition and must not borrow this failure's message.
        code, err = self.run_sync([], amazon_ok=False)
        self.assertEqual(code, 0)
        self.assertIn("DEGRADED RUN", err)
        self.assertNotIn("no row in sku_map.csv claims", err)


class TestExitHelper(unittest.TestCase):
    """unmapped_stock_exit alone, so the decision is pinned without a full run."""

    def test_nothing_unclaimed_is_a_clean_exit(self):
        self.assertEqual(run.unmapped_stock_exit([]), 0)

    def test_anything_unclaimed_is_a_failure(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run.unmapped_stock_exit([("ZZ-9999-ZZZZ", 47, "B0ZZZZZZZZ")])
        self.assertEqual(code, 1)
        self.assertIn("FAILED", err.getvalue())

    def test_the_message_says_the_numbers_were_still_written(self):
        # The distinction that keeps this from being mistaken for a data guard:
        # the mapped SKUs are correct and were published.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run.unmapped_stock_exit([("ZZ-9999-ZZZZ", 47, "")])
        self.assertIn("written", err.getvalue())


class TestWriteOrder(unittest.TestCase):
    """The 2026-09-03 outage: a Google Sheets 503 cost a day of storefront.

    open_sheet() raised APIError [503] before anything was written, so the feed
    and the website push never happened even though Amazon and Walmart had both
    answered correctly. The site carried the previous day's numbers for 31
    hours. The storefront reads Supabase and never the spreadsheet, so it was
    queuing behind a service it does not depend on.

    Sinks now go: feed, website, Sheets. These pin that, and pin that a Sheets
    failure costs a history row rather than the day's availability.
    """

    FBA = [fba("AA-1111-AAAA", 10, "B0AAAAAAAA"),
           fba("BB-2222-BBBB", 20, "B0BBBBBBBB")]

    def run_full(self, open_raises=None, append_raises=None):
        """Full (non-dry-run) main() with every sink faked. Returns (code, order, err)."""
        order, err = [], io.StringIO()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def open_sheet():
            if open_raises:
                raise open_raises
            return object()

        def append_snapshots(_sh, rows):
            if append_raises:
                raise append_raises
            order.append("sheets")
            return len(rows)

        def write_current(_sh, _rows):
            if "sheets" not in order:
                order.append("sheets")

        def push(results, dry_run=False):
            order.append("website")
            return {"updated": len(results), "no_row_updated": [],
                    "skipped_or_missing": 0, "unconfirmed": []}

        real_write_text = Path.write_text

        def spy_write_text(self_path, *a, **kw):
            if self_path.name == "inventory.json":
                order.append("feed")
            return real_write_text(self_path, *a, **kw)

        with fake_sheets(open_sheet=open_sheet,
                         read_prior_published=lambda _sh, **k: {},
                         append_snapshots=append_snapshots,
                         write_current=write_current), \
                mock.patch.object(run, "load_sku_map", lambda *a, **k: MAP), \
                mock.patch.object(run, "load_ignored_amazon_skus", lambda *a, **k: {}), \
                mock.patch.object(run, "PUBLIC_DIR", Path(tmp.name)), \
                mock.patch.object(run.net, "apply_ipv4_preference", lambda: False), \
                mock.patch.object(run.amazon, "configured", lambda: True), \
                mock.patch.object(run.amazon, "get_access_token", lambda: "t"), \
                mock.patch.object(run.amazon, "fetch_fba_inventory",
                                  lambda _t: (list(self.FBA), "test-source")), \
                mock.patch.object(run.walmart, "configured", lambda: True), \
                mock.patch.object(run.walmart, "get_access_token", lambda: "t"), \
                mock.patch.object(run.walmart, "fetch_wfs_inventory",
                                  lambda _t: ([], {})), \
                mock.patch.object(website_mod, "configured", lambda: True), \
                mock.patch.object(website_mod, "push", push), \
                mock.patch.object(Path, "write_text", spy_write_text), \
                mock.patch.object(sys, "argv", ["collector.main"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = run.main()
        return code, order, err.getvalue()

    def test_a_healthy_run_writes_feed_then_website_then_sheets(self):
        code, order, _err = self.run_full()
        self.assertEqual(code, 0)
        self.assertEqual(order, ["feed", "website", "sheets"])

    def test_sheets_unreachable_still_updates_the_website(self):
        # The whole point. Sheets is down; the customer-facing numbers still go
        # out, because the site does not read the spreadsheet.
        code, order, err = self.run_full(open_raises=RuntimeError("503 unavailable"))
        self.assertIn("feed", order)
        self.assertIn("website", order)
        self.assertNotIn("sheets", order)
        self.assertEqual(code, 1)
        self.assertIn("history", err.lower())

    def test_a_sheets_write_failure_comes_after_the_website_is_updated(self):
        # Failing on append rather than open: the site is already current by
        # then, so the loss is still only the history row.
        code, order, _err = self.run_full(append_raises=RuntimeError("503 unavailable"))
        self.assertEqual(order, ["feed", "website"])
        self.assertEqual(code, 1)

    def test_the_failure_says_the_site_is_current(self):
        # A red run must not read as "the storefront is wrong", or it trains
        # the wrong reflex on the one signal that reaches an inbox.
        _code, _order, err = self.run_full(open_raises=RuntimeError("503"))
        self.assertIn("website", err)
        self.assertIn("current", err)


class TestRunExit(unittest.TestCase):
    """run_exit reports both conditions; either alone fails the run."""

    def codes(self, unclaimed, sheets_error):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return run.run_exit(unclaimed, sheets_error), err.getvalue()

    def test_clean_run_is_zero(self):
        code, _err = self.codes([], None)
        self.assertEqual(code, 0)

    def test_sheets_failure_alone_fails(self):
        code, err = self.codes([], RuntimeError("503"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", err)

    def test_unmapped_stock_alone_still_fails(self):
        # The helper summarises and says "listed above"; the per-SKU lines are
        # printed by main() before it, so assert on the summary it does own.
        code, err = self.codes([("ZZ-9999-ZZZZ", 47, "")], None)
        self.assertEqual(code, 1)
        self.assertIn("47 fulfillable", err)
        self.assertNotIn("history row", err)

    def test_both_conditions_are_both_reported(self):
        # One must not mask the other - each names a different thing to fix.
        code, err = self.codes([("ZZ-9999-ZZZZ", 47, "")], RuntimeError("503"))
        self.assertEqual(code, 1)
        self.assertIn("47 fulfillable", err)
        self.assertIn("history row", err)
        self.assertEqual(err.count("FAILED:"), 2)


if __name__ == "__main__":
    unittest.main()
