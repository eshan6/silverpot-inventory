"""The history walk: does it actually reach the end, and does it stop there?

Two failures matter more than the rest, and both are silent.

A walk that stalls looks exactly like a walk that is working - a run every
twelve hours, a chunk fetched each time - while the archive never grows past
the first quiet month. That is why progress is a cursor over days attempted
rather than a query for the oldest row present, and why the stall is pinned
here.

A walk that never stops keeps making Amazon calls forever for data that does
not exist, on a schedule nobody watches.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import backfill  # noqa: E402

TODAY = date(2026, 9, 9)
YESTERDAY = date(2026, 9, 8)
# The oldest day the trailing window already covers, on a default 3-day run.
COVERED_FROM = date(2026, 9, 6)


class TestFirstRun(unittest.TestCase):
    def test_the_walk_starts_immediately_below_the_trailing_window(self):
        # No gap and no overlap: the day before the trailing window's oldest.
        chunk = backfill.next_chunk(backfill.State(), COVERED_FROM, TODAY)
        self.assertEqual(chunk[-1], date(2026, 9, 5))
        self.assertEqual(len(chunk), backfill.CHUNK_DAYS)
        self.assertEqual(chunk[0],
                         date(2026, 9, 5) - timedelta(days=backfill.CHUNK_DAYS - 1))

    def test_days_come_back_oldest_first_and_contiguous(self):
        chunk = backfill.next_chunk(backfill.State(), COVERED_FROM, TODAY)
        for earlier, later in zip(chunk, chunk[1:]):
            self.assertEqual(later - earlier, timedelta(days=1))


class TestItActuallyAdvances(unittest.TestCase):
    def test_each_run_reaches_a_chunk_further_back(self):
        state = backfill.State()
        first = backfill.next_chunk(state, COVERED_FROM, TODAY)
        state = backfill.advance(state, first, rows_found=40, today=TODAY)
        second = backfill.next_chunk(state, COVERED_FROM, TODAY)

        self.assertEqual(second[-1], first[0] - timedelta(days=1))
        self.assertLess(second[0], first[0])

    def test_an_empty_stretch_does_not_stall_the_walk(self):
        # The whole reason progress is a cursor. Keying off "the oldest row in
        # the table" would leave a month with no sales writing no rows, the
        # oldest row never moving, and the same month being refetched every
        # twelve hours forever - with no error, and no growth.
        state = backfill.State()
        chunk = backfill.next_chunk(state, COVERED_FROM, TODAY)
        state = backfill.advance(state, chunk, rows_found=0, today=TODAY)
        nxt = backfill.next_chunk(state, COVERED_FROM, TODAY)

        self.assertTrue(nxt)
        self.assertEqual(nxt[-1], chunk[0] - timedelta(days=1))

    def test_the_cursor_survives_a_round_trip_through_settings(self):
        state = backfill.State(cursor=date(2026, 3, 1), empty_streak=1)
        restored = backfill.State.from_settings(state.as_settings())
        self.assertEqual(restored, state)


class TestPartialProgress(unittest.TestCase):
    """A throttled chunk still counts for the days it managed."""

    def test_a_short_chunk_moves_the_cursor_only_as_far_as_it_got(self):
        state = backfill.State()
        asked = backfill.next_chunk(state, COVERED_FROM, TODAY)
        # Throttled after the newest four days of the chunk.
        completed = sorted(asked[-4:])
        state = backfill.advance(state, completed, rows_found=9, today=TODAY)
        self.assertEqual(state.cursor, completed[0])

    def test_the_days_it_missed_are_fetched_next_run_not_skipped(self):
        state = backfill.State()
        asked = backfill.next_chunk(state, COVERED_FROM, TODAY)
        completed = sorted(asked[-4:])
        state = backfill.advance(state, completed, rows_found=9, today=TODAY)

        nxt = backfill.next_chunk(state, COVERED_FROM, TODAY)
        # Picks up exactly below what was completed, so the days the throttle
        # cost are the next ones fetched rather than a permanent hole.
        self.assertEqual(nxt[-1], completed[0] - timedelta(days=1))
        self.assertIn(asked[0], nxt)


class TestItStops(unittest.TestCase):
    def test_three_empty_chunks_end_the_walk(self):
        # Three months without a single order means the walk is before the
        # business existed, not in a quiet patch.
        state = backfill.State()
        for _ in range(backfill.EMPTY_CHUNKS_TO_STOP):
            chunk = backfill.next_chunk(state, COVERED_FROM, TODAY)
            self.assertTrue(chunk)
            state = backfill.advance(state, chunk, rows_found=0, today=TODAY)

        self.assertTrue(state.done)
        self.assertEqual(backfill.next_chunk(state, COVERED_FROM, TODAY), [])

    def test_one_good_chunk_resets_the_empty_streak(self):
        # Two quiet months either side of a real one must not add up to a stop.
        state = backfill.State()
        for found in (0, 0, 12, 0, 0):
            chunk = backfill.next_chunk(state, COVERED_FROM, TODAY)
            self.assertTrue(chunk, "walk ended early")
            state = backfill.advance(state, chunk, rows_found=found, today=TODAY)
        self.assertFalse(state.done)

    def test_the_horizon_ends_the_walk_even_with_sales_every_chunk(self):
        state = backfill.State()
        for _ in range(200):                    # far more than 730/30
            chunk = backfill.next_chunk(state, COVERED_FROM, TODAY)
            if not chunk:
                break
            state = backfill.advance(state, chunk, rows_found=5, today=TODAY)
        self.assertTrue(state.done)
        self.assertGreaterEqual(state.cursor, TODAY - timedelta(days=backfill.HORIZON_DAYS))

    def test_a_finished_walk_asks_for_nothing_further(self):
        done = backfill.State(cursor=date(2024, 9, 10), done=True)
        self.assertEqual(backfill.next_chunk(done, COVERED_FROM, TODAY), [])

    def test_the_last_chunk_is_clipped_to_the_horizon(self):
        # Never requests a day older than the horizon, even when the remaining
        # gap is smaller than a chunk.
        floor = TODAY - timedelta(days=backfill.HORIZON_DAYS)
        state = backfill.State(cursor=floor + timedelta(days=5))
        chunk = backfill.next_chunk(state, COVERED_FROM, TODAY)
        self.assertEqual(chunk[0], floor)
        self.assertEqual(len(chunk), 5)


class TestDamagedState(unittest.TestCase):
    def test_a_missing_setting_starts_a_fresh_walk(self):
        self.assertEqual(backfill.State.from_settings(None), backfill.State())

    def test_nonsense_restarts_rather_than_crashing(self):
        # Re-fetching days that are already correct costs upserts. Raising here
        # would cost the day's sales, since this runs inside the ingestion job.
        for junk in ("", [], 7, {"cursor": "not-a-date"}, {"cursor": 5},
                     {"empty_streak": -3}):
            self.assertIsInstance(backfill.State.from_settings(junk),
                                  backfill.State)
        self.assertIsNone(backfill.State.from_settings({"cursor": "nope"}).cursor)
        self.assertEqual(
            backfill.State.from_settings({"empty_streak": -3}).empty_streak, 0)

    def test_a_cursor_from_the_future_still_walks_backwards(self):
        # Belt and braces against a hand-edited setting: the next chunk must
        # never be ahead of the cursor.
        state = backfill.State(cursor=TODAY + timedelta(days=10))
        chunk = backfill.next_chunk(state, COVERED_FROM, TODAY)
        self.assertTrue(all(d < state.cursor for d in chunk))


class TestAdsHorizon(unittest.TestCase):
    def test_a_shorter_horizon_stops_much_sooner(self):
        # Advertising reporting retains a fraction of what orders do, so the
        # ads walk is expected to finish in days rather than weeks.
        state = backfill.State()
        chunks = 0
        while True:
            chunk = backfill.next_chunk(state, COVERED_FROM, TODAY,
                                        chunk_days=30, horizon_days=95)
            if not chunk:
                break
            chunks += 1
            state = backfill.advance(state, chunk, rows_found=3,
                                     horizon_days=95, today=TODAY)
            self.assertLess(chunks, 10, "ads walk did not terminate")
        self.assertTrue(state.done)
        self.assertLessEqual(chunks, 4)


if __name__ == "__main__":
    unittest.main()
