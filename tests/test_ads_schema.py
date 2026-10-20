"""Every key `ads.normalize` produces must be a real column.

This exists because of a live failure. On 2026-09-29 the backfill collected
its rows and then died on the write:

    upsert into ads_search_terms failed: 400 PGRST204
    Could not find the 'campaign_name' column of 'ads_search_terms'

`normalize` put `campaign_name` on every row, and `ads_search_terms` has never
had that column. Nothing caught it: the row shape is built in Python, the
columns are declared in SQL, and until PostgREST is asked to reconcile them
nothing does. The unit tests all passed, because they checked the values in
the dict rather than whether the dict could be stored.

So the columns are read out of the migrations themselves rather than restated
here. A column added in SQL is available immediately; a key added in Python
with no column behind it fails here instead of in production twenty minutes
into a run.

    python -m unittest discover -s tests -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import ads  # noqa: E402
from collector.ads_sync import TABLE_FOR_GRAIN  # noqa: E402

SUPABASE = Path(__file__).resolve().parent.parent / "dashboard" / "supabase"
MIGRATIONS = SUPABASE / "migrations"

# Not a column the collector may send: Postgres fills it.
SERVER_MANAGED = {"updated_at"}

_CREATE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?public\.(\w+)\s*\(", re.I)
_ALTER = re.compile(r"alter\s+table\s+(?:only\s+)?public\.(\w+)\b", re.I)
_ADD_COLUMN = re.compile(
    r"add\s+column\s+(?:if\s+not\s+exists\s+)?(\w+)", re.I)
_DROP_COLUMN = re.compile(
    r"drop\s+column\s+(?:if\s+not\s+exists\s+)?(\w+)", re.I)

# Words that begin a table constraint rather than a column.
_NOT_A_COLUMN = {"primary", "unique", "foreign", "check", "constraint",
                 "exclude", "like"}


def _split_top_level(body: str) -> list[str]:
    """Split a create-table body on commas outside parentheses.

    `numeric(12, 2)` is one column, not two, which is the whole reason this
    is not a `.split(",")`.
    """
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _body_of(sql: str, open_paren: int) -> str:
    depth, i = 0, open_paren
    while i < len(sql):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[open_paren + 1:i]
        i += 1
    raise ValueError("unbalanced parentheses in a create table")


def columns_from(sql_files) -> dict[str, set[str]]:
    """Table -> column names, replaying creates and alters in file order."""
    tables: dict[str, set[str]] = {}
    for path in sql_files:
        sql = path.read_text()
        for match in _CREATE.finditer(sql):
            table = match.group(1)
            body = _body_of(sql, match.end() - 1)
            cols = set()
            for piece in _split_top_level(body):
                first = piece.split()[0].lower()
                if first in _NOT_A_COLUMN:
                    continue
                cols.add(piece.split()[0])
            tables[table] = cols
        # Alters are read per statement, because one statement can add
        # several columns and two statements can name different tables.
        for statement in sql.split(";"):
            alter = _ALTER.search(statement)
            if not alter:
                continue
            table = tables.setdefault(alter.group(1), set())
            for added in _ADD_COLUMN.finditer(statement):
                table.add(added.group(1))
            for dropped in _DROP_COLUMN.finditer(statement):
                table.discard(dropped.group(1))
    return tables


MIGRATED = columns_from(sorted(MIGRATIONS.glob("*.sql")))


def sample_row() -> dict:
    """A raw report row carrying every field name the collector reads."""
    return {
        "date": "2026-09-20",
        "campaignId": 123456789,
        "campaignName": "SP - Assam - Exact",
        "adGroupName": "Assam",
        "advertisedSku": "2201US",
        "advertisedAsin": "B0TEST0001",
        "searchTerm": "assam tea tin",
        "matchType": "EXACT",
        "impressions": 400,
        "clicks": 12,
        "cost": 4.44,
        "sales7d": 27.98,
        "purchases7d": 2,
        "somethingNew": 9,
    }


class TestTheParserItself(unittest.TestCase):
    """If the parser is wrong, the tests below pass for the wrong reason."""

    def test_it_found_the_ads_tables(self):
        self.assertIn("ads_daily", MIGRATED)
        self.assertIn("ads_search_terms", MIGRATED)

    def test_a_numeric_precision_is_not_read_as_two_columns(self):
        self.assertIn("spend", MIGRATED["ads_daily"])
        self.assertNotIn("2", MIGRATED["ads_daily"])

    def test_the_primary_key_line_is_not_read_as_a_column(self):
        self.assertNotIn("primary", MIGRATED["ads_daily"])

    def test_columns_added_by_a_later_migration_are_picked_up(self):
        # 0002 adds these to both tables. Missing them would make the checks
        # below fail rather than pass, but it would still mean the parser is
        # not replaying alters, so it is named here.
        for table in ("ads_daily", "ads_search_terms"):
            self.assertIn("ad_program", MIGRATED[table])
            self.assertIn("extra", MIGRATED[table])

    def test_campaign_name_is_on_ads_daily_and_not_on_search_terms(self):
        # The exact fact the live failure turned on.
        self.assertIn("campaign_name", MIGRATED["ads_daily"])
        self.assertNotIn("campaign_name", MIGRATED["ads_search_terms"])


class TestNormalizedRowsFitTheirTable(unittest.TestCase):
    def test_every_key_of_every_report_is_a_column(self):
        for key, spec in ads.REPORTS.items():
            table = TABLE_FOR_GRAIN[spec["grain"]]
            row = ads.normalize(sample_row(), spec)
            unknown = set(row) - MIGRATED[table]
            self.assertEqual(unknown, set(), f"{key} -> {table} sends {unknown}")

    def test_an_empty_report_row_still_fits(self):
        # Sponsored Brands and Display are empty today, and a row built from
        # a payload missing most keys must still be storable.
        for key, spec in ads.REPORTS.items():
            table = TABLE_FOR_GRAIN[spec["grain"]]
            row = ads.normalize({}, spec)
            self.assertEqual(set(row) - MIGRATED[table], set(), key)

    def test_the_primary_key_columns_are_all_supplied(self):
        # A missing key column is the other half of the same failure: the
        # write succeeds and upserts land on the wrong row, or on a row of
        # empty strings. Checked against the keys the migrations declare.
        keys = {
            "ads_daily": {"marketplace", "ad_program", "ad_date",
                          "campaign_id", "ad_group", "sku"},
            "ads_search_terms": {"marketplace", "ad_program", "ad_date",
                                 "campaign_id", "search_term", "match_type"},
            "ads_advertised_product": {"marketplace", "ad_program", "ad_date",
                                       "campaign_id", "ad_group", "asin",
                                       "sku"},
        }
        for key, spec in ads.REPORTS.items():
            row = ads.normalize(sample_row(), spec)
            missing = keys[TABLE_FOR_GRAIN[spec["grain"]]] - set(row)
            self.assertEqual(missing, set(), f"{key} omits {missing}")

    def test_the_collector_never_sets_a_server_managed_column(self):
        for key, spec in ads.REPORTS.items():
            row = ads.normalize(sample_row(), spec)
            self.assertEqual(set(row) & SERVER_MANAGED, set(), key)

    def test_the_campaign_name_still_arrives_where_there_is_a_column_for_it(self):
        # Dropping it from search terms must not drop it from ads_daily: the
        # SPA's campaign labels come from there and nowhere else.
        for key, spec in ads.REPORTS.items():
            if spec["grain"] != "campaign":
                continue
            self.assertEqual(ads.normalize(sample_row(), spec)["campaign_name"],
                             "SP - Assam - Exact", key)

    def test_a_search_term_row_keeps_its_campaign_id(self):
        # The name is reachable by joining ads_daily on the id, so the id is
        # the thing that must not go missing.
        spec = ads.REPORTS["sp_search_terms"]
        self.assertEqual(ads.normalize(sample_row(), spec)["campaign_id"],
                         "123456789")


class TestNoTwoGrainsShareATable(unittest.TestCase):
    """The guard that keeps every spend figure honest.

    `ads_summary` reads `ads_daily` and sums it grouped only by
    (marketplace, ad_program). It does not filter by grain, because until the
    advertised-product report there was only one. Put a second grain in that
    table and the same spend is counted twice - once under the campaign, once
    under the ASIN - and nothing errors, because the primary key carries
    `sku` so the rows insert cleanly beside each other.

    That is the failure this project cares about most: not a crash, but a
    number that looks right and is double.
    """

    def test_each_grain_writes_to_its_own_table(self):
        seen = {}
        for key, spec in ads.REPORTS.items():
            grain, table = spec["grain"], TABLE_FOR_GRAIN[spec["grain"]]
            if table in seen:
                self.assertEqual(
                    seen[table], grain,
                    f"{key} writes {grain} into {table}, which already holds "
                    f"{seen[table]} - two grains in one table double its sums")
            seen[table] = grain

    def test_the_advertised_product_grain_is_not_ads_daily(self):
        # Named explicitly, because this is the one that would be tempting:
        # ads_daily already has asin and sku columns sitting empty.
        self.assertEqual(TABLE_FOR_GRAIN["advertised_product"],
                         "ads_advertised_product")
        self.assertNotEqual(TABLE_FOR_GRAIN["advertised_product"],
                            TABLE_FOR_GRAIN["campaign"])

    def test_every_grain_in_use_has_a_table(self):
        for key, spec in ads.REPORTS.items():
            self.assertIn(spec["grain"], TABLE_FOR_GRAIN, key)

    def test_the_two_sp_grains_carry_the_same_attribution_window(self):
        # ads_by_asin is meant to be readable against ads_summary. If one
        # grain reported 7-day attribution and the other 14-day, the two
        # would disagree for reasons no one would think to look for.
        campaign = set(ads.REPORTS["sp_campaigns"]["columns"])
        product = set(ads.REPORTS["sp_advertised_product"]["columns"])
        for metric in ("cost", "purchases7d", "sales7d"):
            self.assertIn(metric, campaign)
            self.assertIn(metric, product)


class TestSetupSqlAgreesWithTheMigrations(unittest.TestCase):
    """`setup.sql` is what a fresh database is built from; the migrations are
    what the live one was built from. They drifting apart is how a test suite
    goes green against a schema production does not have."""

    def test_the_ads_tables_have_the_same_columns_either_way(self):
        fresh = columns_from([SUPABASE / "setup.sql"])
        for table in ("ads_daily", "ads_search_terms"):
            self.assertEqual(fresh[table], MIGRATED[table], table)


if __name__ == "__main__":
    unittest.main()
