"""setup.sql must still be the migrations.

A generated file checked into the repository is only trustworthy if something
notices when it stops matching its source. Without this test, adding a
migration and forgetting to rebuild would leave a paste-once file that
silently sets up an older schema than the code expects - and the symptom
would appear much later, as a column that does not exist.

    python -m unittest discover -s tests
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPA = ROOT / "dashboard" / "supabase"
sys.path.insert(0, str(SUPA))

import build_setup  # noqa: E402


class TestGeneratedSetup(unittest.TestCase):
    def test_the_checked_in_file_matches_the_migrations(self):
        self.assertEqual(
            build_setup.TARGET.read_text(),
            build_setup.build(),
            "dashboard/supabase/setup.sql is stale - run "
            "`python dashboard/supabase/build_setup.py`")

    def test_every_migration_is_included_in_order(self):
        text = build_setup.TARGET.read_text()
        names = [p.name for p in sorted(build_setup.MIGRATIONS.glob("*.sql"))]
        self.assertTrue(names, "no migrations found")
        positions = [text.index(n) for n in names]
        self.assertEqual(positions, sorted(positions),
                         "migrations appear out of order, so setup.sql would "
                         "reference tables before they exist")

    def test_the_bootstrap_placeholder_refuses_to_run(self):
        # The file ships with a placeholder address. If that guard were
        # dropped, pasting the file unedited would leave a live project whose
        # super admin slot is claimable by whoever signs up as the example
        # address first.
        text = build_setup.TARGET.read_text()
        self.assertIn(build_setup.PLACEHOLDER, text)
        self.assertIn("raise exception", text)

    def test_it_carries_no_psql_backslash_commands(self):
        # The Supabase SQL editor is not psql. A \set or \echo here would be
        # a syntax error at the worst possible moment - first-time setup.
        for i, line in enumerate(build_setup.TARGET.read_text().splitlines(), 1):
            self.assertFalse(line.startswith("\\"),
                             f"line {i} is a psql command: {line!r}")

    def test_rebuilding_is_a_no_op(self):
        # Belt and braces: run the generator as the developer would and check
        # nothing moves. Catches a build() that is not deterministic.
        before = build_setup.TARGET.read_text()
        subprocess.run([sys.executable, str(SUPA / "build_setup.py")],
                       check=True, capture_output=True)
        self.assertEqual(before, build_setup.TARGET.read_text())


if __name__ == "__main__":
    unittest.main()
