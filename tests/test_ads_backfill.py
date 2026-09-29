"""The advertising history walk asks on one run and collects on the next.

It used to call `ads.fetch_many`, which polls until Amazon answers or
POLL_LIMIT expires. Run 53 spent seventeen of its eighteen minutes in there
for twenty seconds of work, and a run that timed out threw away reports Amazon
had already finished building. The trailing window was rebuilt around not
waiting; this is the walk catching up.

What has to hold, and is easy to get wrong:

- the cursor moves only when a chunk is *whole*, or a half-collected chunk
  leaves a hole in the archive that nothing goes back for
- rows found are summed across the runs a chunk spans, or the empty-streak
  rule reads the second run's zero and calls a busy month empty
- the two pending sets never touch each other's ids

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import ads_sync, backfill  # noqa: E402

TODAY = date(2026, 9, 29)
# What the trailing window already covers, so the walk starts below it.
COVERED = date(2026, 9, 14)
KEYS = ["sp_campaigns", "sp_search_terms"]


def row(day: str) -> dict:
    return {"ad_date": day, "campaign_id": "1", "spend": 1.0}


class FakeDB:
    """app_settings as a dict, and a note of every upsert."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.writes: list[tuple[str, int]] = []

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value

    def upsert(self, table, rows):
        if rows:
            self.writes.append((table, len(rows)))
        return len(rows)

    @property
    def rows_written(self) -> int:
        return sum(n for _, n in self.writes)


class Harness:
    """ads_sync with its database and its Amazon transport replaced."""

    def __init__(self, db: FakeDB, ready=None, rows=None):
        self.db = db
        # Keys Amazon says are finished. None means "all of them".
        self.ready = ready
        self.rows = rows if rows is not None else {k: [row("2026-08-20")]
                                                   for k in KEYS}
        self.requested: list[tuple[date, date]] = []
        self.collected: list[dict] = []
        self.waited = False

    def request_all(self, token, keys, start, end, sess=None):
        self.requested.append((start, end))
        return {k: f"report-{k}" for k in keys}

    def collect(self, token, pending, sess=None, budget_seconds=0.0, **kw):
        self.collected.append(dict(pending))
        ready = set(pending) if self.ready is None else set(self.ready)
        got = {k: self.rows.get(k, []) for k in pending if k in ready}
        still = {k: v for k, v in pending.items() if k not in ready}
        return got, still, {}

    def fetch_many(self, *a, **kw):
        # The blocking path. Nothing in the walk may reach it any more.
        self.waited = True
        raise AssertionError("extend_history blocked on Amazon")

    def run(self, keys=KEYS, covered=COVERED):
        with mock.patch.multiple(
            ads_sync.dashboard_db,
            get_setting=self.db.get_setting,
            set_setting=self.db.set_setting,
            upsert=self.db.upsert,
        ), mock.patch.multiple(
            ads_sync.ads,
            request_all=self.request_all,
            collect=self.collect,
            fetch_many=self.fetch_many,
        ), mock.patch.object(ads_sync.orders, "today_et", lambda: TODAY):
            return ads_sync.extend_history("token", keys, covered)


def pending_of(db: FakeDB) -> dict:
    return db.settings.get(ads_sync.BACKFILL_PENDING_SETTING) or {}


def cursor_of(db: FakeDB):
    return backfill.State.from_settings(
        db.settings.get(ads_sync.BACKFILL_SETTING)).cursor


class TestTheFirstRunOnlyAsks(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.h = Harness(self.db)
        self.written = self.h.run()

    def test_it_writes_no_rows(self):
        self.assertEqual(self.written, 0)
        self.assertEqual(self.db.writes, [])

    def test_it_never_waits_on_amazon(self):
        self.assertFalse(self.h.waited)

    def test_it_asks_for_the_chunk_below_the_trailing_window(self):
        (start, end), = self.h.requested
        self.assertEqual(end, COVERED - timedelta(days=1))
        self.assertEqual((end - start).days + 1, ads_sync.ADS_CHUNK_DAYS)

    def test_it_writes_the_ids_down_with_the_days_they_cover(self):
        saved = pending_of(self.db)
        self.assertEqual(set(saved["reports"]), set(KEYS))
        self.assertEqual(saved["end"], (COVERED - timedelta(days=1)).isoformat())
        self.assertEqual(saved["found"], 0)

    def test_the_cursor_does_not_move_yet(self):
        # Nothing has been collected, so nothing has been covered.
        self.assertIsNone(cursor_of(self.db))


class TestTheSecondRunCollects(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        Harness(self.db).run()                      # asks
        self.h = Harness(self.db)
        self.written = self.h.run()                 # collects

    def test_it_writes_the_rows(self):
        self.assertEqual(self.written, 2)
        self.assertEqual(self.db.rows_written, 2)

    def test_it_writes_each_grain_to_its_own_table(self):
        self.assertEqual({t for t, _ in self.db.writes},
                         {"ads_daily", "ads_search_terms"})

    def test_it_does_not_ask_for_another_chunk_in_the_same_run(self):
        # One chunk per run, whichever half of it this run is doing.
        self.assertEqual(self.h.requested, [])

    def test_the_pending_set_is_cleared(self):
        self.assertEqual(pending_of(self.db).get("reports", {}), {})

    def test_the_cursor_moves_to_the_oldest_day_of_the_chunk(self):
        expected = COVERED - timedelta(days=ads_sync.ADS_CHUNK_DAYS)
        self.assertEqual(cursor_of(self.db), expected)

    def test_the_next_run_asks_for_the_chunk_below_that(self):
        third = Harness(self.db)
        third.run()
        (_, end), = third.requested
        self.assertEqual(end, cursor_of(self.db) - timedelta(days=1))


class TestAHalfReadyChunkStaysOpen(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        Harness(self.db).run()
        self.h = Harness(self.db, ready=["sp_campaigns"])
        self.written = self.h.run()

    def test_what_is_ready_is_written(self):
        self.assertEqual(self.written, 1)

    def test_the_cursor_does_not_move_on_half_a_chunk(self):
        # Moving it here would leave the search terms for those days
        # uncollected with nothing ever going back for them.
        self.assertIsNone(cursor_of(self.db))

    def test_the_rest_is_carried_under_the_same_window(self):
        saved = pending_of(self.db)
        self.assertEqual(set(saved["reports"]), {"sp_search_terms"})
        self.assertEqual(saved["end"], (COVERED - timedelta(days=1)).isoformat())

    def test_the_rows_already_found_are_remembered(self):
        self.assertEqual(pending_of(self.db)["found"], 1)

    def test_finishing_it_later_moves_the_cursor(self):
        Harness(self.db).run()
        self.assertEqual(cursor_of(self.db),
                         COVERED - timedelta(days=ads_sync.ADS_CHUNK_DAYS))


class TestTheEmptyStreakReadsTheWholeChunk(unittest.TestCase):
    """The bug this guards: a chunk collected over two runs shows the second
    run zero rows. Counting that as an empty chunk would end the walk six
    chunks early, on months that were not empty at all."""

    def test_a_chunk_whose_rows_all_came_in_run_one_is_not_empty(self):
        db = FakeDB()
        Harness(db).run()
        # Run one: campaigns arrive with rows.
        Harness(db, ready=["sp_campaigns"],
                rows={"sp_campaigns": [row("2026-08-20")]}).run()
        # Run two: the search terms arrive genuinely empty.
        Harness(db, rows={"sp_search_terms": []}).run()
        state = backfill.State.from_settings(
            db.settings[ads_sync.BACKFILL_SETTING])
        self.assertEqual(state.empty_streak, 0)

    def test_a_genuinely_empty_chunk_still_counts_as_empty(self):
        db = FakeDB()
        Harness(db).run()
        Harness(db, rows={k: [] for k in KEYS}).run()
        state = backfill.State.from_settings(
            db.settings[ads_sync.BACKFILL_SETTING])
        self.assertEqual(state.empty_streak, 1)


class TestAbandoningReportsAmazonNeverFinished(unittest.TestCase):
    def stale(self) -> dict:
        old = datetime.now(timezone.utc) - timedelta(
            hours=ads_sync.PENDING_MAX_AGE_HOURS + 1)
        return {
            "reports": {"sp_campaigns": "report-old"},
            "start": "2026-07-01", "end": "2026-07-30",
            "asked_at": old.isoformat(), "found": 0,
        }

    def test_a_stale_set_is_dropped_and_a_fresh_chunk_asked_for(self):
        db = FakeDB({ads_sync.BACKFILL_PENDING_SETTING: self.stale()})
        h = Harness(db)
        h.run()
        self.assertEqual(len(h.requested), 1)
        self.assertEqual(h.collected, [])

    def test_a_fresh_set_is_collected_rather_than_re_requested(self):
        fresh = self.stale()
        fresh["asked_at"] = datetime.now(timezone.utc).isoformat()
        db = FakeDB({ads_sync.BACKFILL_PENDING_SETTING: fresh})
        h = Harness(db)
        h.run()
        self.assertEqual(h.requested, [])
        self.assertEqual(len(h.collected), 1)


class TestAPendingRecordWithNoUsableWindow(unittest.TestCase):
    """Report ids are useless without knowing which days they cover, and a
    cursor moved on a guess leaves a hole."""

    def run_with(self, saved):
        db = FakeDB({ads_sync.BACKFILL_PENDING_SETTING: saved})
        h = Harness(db)
        written = h.run()
        return db, h, written

    def test_a_missing_window_drops_the_ids_without_moving_the_cursor(self):
        db, _, written = self.run_with({
            "reports": {"sp_campaigns": "x"},
            "asked_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(written, 0)
        self.assertIsNone(cursor_of(db))
        self.assertEqual(pending_of(db).get("reports", {}), {})

    def test_an_unparseable_window_does_the_same(self):
        db, _, _ = self.run_with({
            "reports": {"sp_campaigns": "x"},
            "start": "not-a-date", "end": "also-not",
            "asked_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertIsNone(cursor_of(db))
        self.assertEqual(pending_of(db).get("reports", {}), {})


class TestTheWalkNeverFailsTheRun(unittest.TestCase):
    def test_an_exception_is_reported_and_swallowed(self):
        db = FakeDB()
        h = Harness(db)
        h.request_all = mock.Mock(side_effect=RuntimeError("Amazon said no"))
        self.assertEqual(h.run(), 0)

    def test_the_walk_stops_when_the_horizon_is_reached(self):
        floor = TODAY - timedelta(days=ads_sync.ADS_HORIZON_DAYS)
        db = FakeDB({ads_sync.BACKFILL_SETTING: {
            "cursor": floor.isoformat(), "empty_streak": 0, "done": False}})
        h = Harness(db)
        h.run()
        self.assertEqual(h.requested, [])
        self.assertTrue(backfill.State.from_settings(
            db.settings[ads_sync.BACKFILL_SETTING]).done)


class TestAmazonNamesItsOwnRetentionWindow(unittest.TestCase):
    """Run 54 asked for 2026-07-16 and was told:

        startDate (2026-07-16) must be equal to or after report type data
        retention start date (2026-07-31)

    ADS_HORIZON_DAYS was 95 and the truth was 60. Worse than the wrong number
    was what the wrong number did: extend_history caught the error, so nothing
    failed, and the cursor never moved because nothing was collected - so the
    walk would have asked for the same impossible chunk on every run forever.
    """

    BODY = ('{"code":"400","detail":"startDate (2026-07-16) must be equal to '
            'or after report type data retention start date (2026-07-31)"}')

    def refusing(self, earliest: date):
        def request_all(token, keys, start, end, sess=None):
            if start < earliest:
                raise ads_sync.ads.RetentionLimit(earliest, self.BODY)
            return {k: f"report-{k}" for k in keys}
        return request_all

    def test_the_date_is_read_out_of_amazons_own_words(self):
        self.assertEqual(ads_sync.ads.retention_start(self.BODY),
                         date(2026, 7, 31))

    def test_the_second_date_is_taken_not_the_first(self):
        # The message names the rejected start date too. Taking that one would
        # clamp to exactly the value Amazon just refused.
        self.assertNotEqual(ads_sync.ads.retention_start(self.BODY),
                            date(2026, 7, 16))

    def test_a_body_naming_no_retention_date_reads_as_none(self):
        self.assertIsNone(ads_sync.ads.retention_start(
            '{"code":"400","detail":"something else entirely"}'))
        self.assertIsNone(ads_sync.ads.retention_start(""))

    def test_a_chunk_that_straddles_the_limit_is_clamped_and_asked_again(self):
        # Inside the chunk (2026-08-15..2026-09-13), so there is something to
        # clamp. A limit older than the chunk would need no clamping at all.
        earliest = date(2026, 9, 1)
        db = FakeDB()
        h = Harness(db)
        h.request_all = self.refusing(earliest)
        h.run()
        saved = pending_of(db)
        self.assertEqual(saved["start"], earliest.isoformat())
        self.assertEqual(saved["end"], (COVERED - timedelta(days=1)).isoformat())

    def test_collecting_the_clamped_chunk_moves_the_cursor_to_the_limit(self):
        earliest = date(2026, 9, 1)
        db = FakeDB()
        asked = Harness(db)
        asked.request_all = self.refusing(earliest)
        asked.run()
        Harness(db).run()
        self.assertEqual(cursor_of(db), earliest)

    def test_a_chunk_wholly_older_than_the_limit_ends_the_walk(self):
        # The thing that stops the forever-loop: no chunk of it is servable,
        # so the walk is over rather than retried.
        db = FakeDB()
        h = Harness(db)
        h.request_all = self.refusing(date(2026, 12, 1))
        h.run()
        self.assertTrue(backfill.State.from_settings(
            db.settings[ads_sync.BACKFILL_SETTING]).done)

    def test_and_a_finished_walk_asks_for_nothing_on_the_next_run(self):
        db = FakeDB()
        stuck = Harness(db)
        stuck.request_all = self.refusing(date(2026, 12, 1))
        stuck.run()
        after = Harness(db)
        after.run()
        self.assertEqual(after.requested, [])
        self.assertEqual(pending_of(db).get("reports", {}), {})

    def test_the_horizon_matches_what_amazon_actually_answered(self):
        # 2026-09-29 minus sixty days is 2026-07-31, the date in the refusal.
        self.assertEqual(TODAY - timedelta(days=ads_sync.ADS_HORIZON_DAYS),
                         date(2026, 7, 31))


class TestTheTwoPendingSetsAreSeparate(unittest.TestCase):
    def test_the_walk_does_not_read_or_clear_the_trailing_window(self):
        trailing = {
            "reports": {"sp_campaigns": "trailing-id"},
            "start": "2026-09-14", "end": "2026-09-28",
            "asked_at": datetime.now(timezone.utc).isoformat(),
        }
        db = FakeDB({ads_sync.PENDING_SETTING: dict(trailing)})
        h = Harness(db)
        h.run()
        self.assertEqual(db.settings[ads_sync.PENDING_SETTING], trailing)
        # And it asked using its own key, not the trailing window's ids.
        self.assertEqual(h.collected, [])
        self.assertEqual(len(h.requested), 1)

    def test_the_keys_are_not_the_same_string(self):
        self.assertNotEqual(ads_sync.PENDING_SETTING,
                            ads_sync.BACKFILL_PENDING_SETTING)


if __name__ == "__main__":
    unittest.main()
