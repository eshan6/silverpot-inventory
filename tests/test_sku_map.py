"""Tests for the identity layer and the multi-SKU Amazon pooling.

Amazon issues a second seller SKU when a listing converts to stickerless
(commingled) inventory - Silverpot's carry a `-stickerless` suffix - and
reports each pool separately. Both ship the same tea. Getting this wrong is
silent: the stock simply never appears, with no error anywhere.

    python -m unittest discover -s tests -v
"""
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import main as collector_main  # noqa: E402
from collector.config import (  # noqa: E402
    IGNORED_SKUS_PATH, SKU_MAP_PATH, SkuMap, SkuRow,
    load_ignored_amazon_skus, load_sku_map,
)

FIELDS = ["internal_code", "product_name", "format", "sku", "amazon_sku_aliases",
          "asin", "walmart_sku_override", "website_product_id", "safety_buffer",
          "active"]


def write_map(rows: list[dict]) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    w = csv.DictWriter(fh, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in FIELDS})
    fh.close()
    return Path(fh.name)


def row(code="2201US", sku="R9-D7AT-S5WW", aliases="", **kw) -> dict:
    base = {"internal_code": code, "product_name": "Original Darjeeling",
            "format": "tin", "sku": sku, "amazon_sku_aliases": aliases,
            "asin": "B0DLGWMV7R", "active": "TRUE"}
    base.update(kw)
    return base


def fba(sku, fulfillable, **kw) -> dict:
    rec = {"seller_sku": sku, "fulfillable": fulfillable, "inbound_working": 0,
           "inbound_shipped": 0, "inbound_receiving": 0, "reserved_total": 0,
           "unfulfillable": 0, "researching": 0}
    rec.update(kw)
    return rec


class TestAliases(unittest.TestCase):
    def test_amazon_skus_lists_primary_then_aliases(self):
        m = load_sku_map(write_map([row(aliases="R9-D7AT-S5WW-stickerless")]))
        self.assertEqual(m.rows[0].amazon_skus,
                         ["R9-D7AT-S5WW", "R9-D7AT-S5WW-stickerless"])

    def test_both_separators(self):
        m = load_sku_map(write_map([row(aliases="A-1;B-2|C-3")]))
        self.assertEqual(m.rows[0].amazon_skus, ["R9-D7AT-S5WW", "A-1", "B-2", "C-3"])

    def test_blank_alias_column_is_just_the_primary(self):
        m = load_sku_map(write_map([row()]))
        self.assertEqual(m.rows[0].amazon_skus, ["R9-D7AT-S5WW"])

    def test_alias_repeating_the_primary_is_not_counted_twice(self):
        # Otherwise one pool would be added to itself and overstate stock.
        m = load_sku_map(write_map([row(aliases="r9-d7at-s5ww")]))
        self.assertEqual(m.rows[0].amazon_skus, ["R9-D7AT-S5WW"])

    def test_lookup_resolves_every_alias_to_the_product(self):
        m = load_sku_map(write_map([row(aliases="R9-D7AT-S5WW-stickerless")]))
        lookup = m.by_amazon_sku
        self.assertEqual(lookup["R9-D7AT-S5WW"].internal_code, "2201US")
        self.assertEqual(lookup["R9-D7AT-S5WW-STICKERLESS"].internal_code, "2201US")

    def test_two_products_claiming_one_sku_is_a_hard_error(self):
        rows = [row("2201US", "A-1"), row("2202US", "B-2", aliases="A-1")]
        with self.assertRaises(ValueError) as ctx:
            load_sku_map(write_map(rows))
        self.assertIn("already", str(ctx.exception))
        self.assertIn("2201US", str(ctx.exception))


class TestPooling(unittest.TestCase):
    def sku_map(self, aliases="R9-D7AT-S5WW-stickerless"):
        return SkuMap(rows=[SkuRow(
            internal_code="2201US", product_name="Original Darjeeling",
            format="tin", sku="R9-D7AT-S5WW", asin="B0DLGWMV7R",
            walmart_sku_override="", website_product_id="", safety_buffer=None,
            amazon_sku_aliases=[a for a in [aliases] if a])]
        )

    def test_stock_under_both_skus_is_summed(self):
        by_sku = {
            "R9-D7AT-S5WW": fba("R9-D7AT-S5WW", 12, reserved_total=3),
            "R9-D7AT-S5WW-STICKERLESS": fba("R9-D7AT-S5WW-stickerless", 40,
                                            reserved_total=5, inbound_shipped=60),
        }
        out = collector_main.build(self.sku_map(), by_sku, {})[0]
        self.assertEqual(out["fba_fulfillable"], 52)
        self.assertEqual(out["fba_reserved"], 8)
        self.assertEqual(out["fba_inbound"], 60)
        self.assertEqual(out["fba_skus_matched"],
                         ["R9-D7AT-S5WW", "R9-D7AT-S5WW-stickerless"])
        # 52 raw, buffer max(2, ceil(5% of 52)) = 3
        self.assertEqual(out["published_available"], 49)

    def test_only_the_stickerless_pool_has_stock(self):
        # The case that motivated this: the primary SKU is retired at zero and
        # every unit sits under the new one. Before pooling this published 0.
        by_sku = {"R9-D7AT-S5WW-STICKERLESS": fba("R9-D7AT-S5WW-stickerless", 30)}
        out = collector_main.build(self.sku_map(), by_sku, {})[0]
        self.assertEqual(out["fba_fulfillable"], 30)
        self.assertTrue(out["matched_fba"])

    def test_without_the_alias_the_stickerless_stock_is_invisible(self):
        by_sku = {"R9-D7AT-S5WW-STICKERLESS": fba("R9-D7AT-S5WW-stickerless", 30)}
        out = collector_main.build(self.sku_map(aliases=""), by_sku, {})[0]
        self.assertEqual(out["fba_fulfillable"], 0)
        self.assertFalse(out["matched_fba"])

    def test_no_amazon_stock_at_all(self):
        out = collector_main.build(self.sku_map(), {}, {})[0]
        self.assertEqual(out["fba_fulfillable"], 0)
        self.assertEqual(out["fba_skus_matched"], [])
        self.assertFalse(out["matched_fba"])


class TestShippedMap(unittest.TestCase):
    """The real sku_map.csv, so a bad edit fails here rather than in a run."""

    def setUp(self):
        self.m = load_sku_map(SKU_MAP_PATH)

    def test_loads_and_has_36_rows(self):
        self.assertEqual(len(self.m.rows), 36)

    def test_every_row_carries_a_stickerless_alias(self):
        missing = [r.internal_code for r in self.m.rows
                   if f"{r.sku.upper()}-STICKERLESS" not in
                   {s.upper() for s in r.amazon_skus}]
        self.assertEqual(missing, [])

    def test_lookup_covers_both_forms_for_every_product(self):
        lookup = self.m.by_amazon_sku
        self.assertEqual(len(lookup), 72)  # 36 primaries + 36 stickerless
        for r in self.m.rows:
            self.assertIs(lookup[r.sku.upper()], r)
            self.assertIs(lookup[f"{r.sku.upper()}-STICKERLESS"], r)

    def test_walmart_lookup_is_unaffected_by_the_amazon_aliases(self):
        # Walmart never saw the stickerless conversion; its SKUs must not
        # acquire the suffix.
        self.assertEqual(len(self.m.by_walmart_sku), 36)
        for r in self.m.rows:
            self.assertNotIn("stickerless", r.walmart_sku.lower())


class TestUnmappedSkuIdentification(unittest.TestCase):
    """The ASIN is what turns an unrecognised SKU into an actionable one."""

    def setUp(self):
        self.m = load_sku_map(write_map([
            row("2201US", "A-1", asin="B0DLGWMV7R"),
            row("2202US", "B-2", asin="B0DLH3RHHC"),
        ]))

    def test_asin_index_finds_the_product(self):
        owners = self.m.by_asin["B0DLGWMV7R"]
        self.assertEqual([o.internal_code for o in owners], ["2201US"])

    def test_asin_index_keeps_every_row_sharing_an_asin(self):
        # Collapsing to one would make an ambiguous answer look definite.
        m = load_sku_map(write_map([
            row("2201US", "A-1", asin="SHARED"),
            row("2202US", "B-2", asin="SHARED"),
        ]))
        self.assertEqual([o.internal_code for o in m.by_asin["SHARED"]],
                         ["2201US", "2202US"])

    def test_rows_without_an_asin_are_absent_rather_than_keyed_on_blank(self):
        m = load_sku_map(write_map([row("2201US", "A-1", asin="")]))
        self.assertEqual(m.by_asin, {})


class TestIgnoredSkus(unittest.TestCase):
    """SKUs deliberately set aside, and the shipped ignore list."""

    def write_ignored(self, rows):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        w = csv.DictWriter(fh, fieldnames=["amazon_sku", "reason", "noted_on"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
        fh.close()
        return Path(fh.name)

    def test_loads_sku_and_reason_uppercased(self):
        p = self.write_ignored([
            {"amazon_sku": "ab-1", "reason": "retired parent", "noted_on": "2026-08-26"}])
        self.assertEqual(load_ignored_amazon_skus(p), {"AB-1": "retired parent"})

    def test_missing_file_is_an_empty_list_not_an_error(self):
        self.assertEqual(load_ignored_amazon_skus(Path("/nonexistent.csv")), {})

    def test_blank_rows_are_skipped(self):
        p = self.write_ignored([{"amazon_sku": "", "reason": "x", "noted_on": ""}])
        self.assertEqual(load_ignored_amazon_skus(p), {})

    def test_shipped_list_holds_the_four_retired_parents(self):
        ignored = load_ignored_amazon_skus(IGNORED_SKUS_PATH)
        self.assertEqual(sorted(ignored), ["6B-11XE-EH8Q", "BK-7JXA-INV9",
                                           "BL-35VI-X9JM", "CU-PM7T-6DX9"])
        for reason in ignored.values():
            self.assertTrue(reason, "every ignored SKU needs a stated reason")

    def test_nothing_is_both_mapped_and_ignored(self):
        # The run refuses to start on this contradiction; catch it here first.
        mapped = set(load_sku_map(SKU_MAP_PATH).by_amazon_sku)
        ignored = set(load_ignored_amazon_skus(IGNORED_SKUS_PATH))
        self.assertEqual(mapped & ignored, set())


if __name__ == "__main__":
    unittest.main()
