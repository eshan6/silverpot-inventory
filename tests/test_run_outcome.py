"""What makes a run fail, and what merely warns.

The stickerless pools were invisible for months while the pipeline printed a
correct warning about them on every run. The warning was never the problem -
a green run was. An Amazon SKU holding fulfillable stock that no row claims
now fails the run, so it arrives as a failure notification instead of a line
in a log nobody opens.

These drive main() end to end with --dry-run, which touches no sheet, no
website and no network. The assertion is the exit code.

    python -m unittest discover -s tests -v
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import main as run  # noqa: E402
from collector.config import SkuMap, SkuRow  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
