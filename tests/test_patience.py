"""A spent quota is not a failure, and must not end the walk.

The first real backfill run collected eight days in twenty-one seconds, was
refused with 429 QuotaExceeded, and stopped - because the loop treated any
exception as "stop here". Amazon was not broken and nothing was wrong: the
quota was empty and refills at about a call a minute. Waiting is the correct
response, and it is the difference between eight days a run and as many as
the run's budget allows.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import orders  # noqa: E402

DAYS = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]


def http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"HTTP {status}", response=resp)


class Clock:
    """A stopwatch that only moves when something sleeps."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class Amazon:
    """Refuses the first `refusals` calls for each day, then answers."""

    def __init__(self, refusals=0, status=429, broken_on=None):
        self.refusals = dict.fromkeys(DAYS, refusals)
        self.status = status
        self.broken_on = broken_on
        self.calls = []

    def __call__(self, token, days, sess=None):
        day = days[0]
        self.calls.append(day)
        if self.broken_on == day:
            raise http_error(500)
        if self.refusals.get(day, 0) > 0:
            self.refusals[day] -= 1
            raise http_error(self.status)
        return [{"sale_date": day.isoformat(), "sku": "AA", "units": 1}]


class TestThrottleDetection(unittest.TestCase):
    def test_a_429_is_a_throttle_and_a_500_is_not(self):
        self.assertTrue(orders.is_throttled(http_error(429)))
        self.assertFalse(orders.is_throttled(http_error(500)))
        self.assertFalse(orders.is_throttled(http_error(403)))
        self.assertFalse(orders.is_throttled(ValueError("boom")))


class TestWaiting(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self._real = orders.fetch_days

    def tearDown(self):
        orders.fetch_days = self._real

    def run_it(self, amazon, deadline=None):
        orders.fetch_days = amazon
        return orders.fetch_patiently(
            "tok", DAYS, deadline=deadline, wait=65,
            sleep=self.clock.sleep, now=self.clock.now, log=lambda *_: None)

    def test_a_refused_day_is_waited_for_and_then_collected(self):
        # The bug this file exists for: this used to return nothing for a day
        # Amazon was simply not ready to serve yet.
        rows, done = self.run_it(Amazon(refusals=1))
        self.assertEqual(done, sorted(DAYS))
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.clock.slept, [65, 65, 65])

    def test_it_keeps_waiting_across_repeated_refusals(self):
        rows, done = self.run_it(Amazon(refusals=3))
        self.assertEqual(done, sorted(DAYS))
        self.assertEqual(len(self.clock.slept), 9)

    def test_days_are_taken_newest_first(self):
        amazon = Amazon()
        self.run_it(amazon)
        self.assertEqual(amazon.calls, sorted(DAYS, reverse=True))

    def test_it_stops_when_there_is_no_time_left_to_wait(self):
        # Bounded: a run must end inside its budget rather than sleeping
        # through a whole job while the quota trickles back.
        rows, done = self.run_it(Amazon(refusals=99), deadline=100.0)
        self.assertEqual(done, [])
        self.assertLessEqual(self.clock.t, 100.0)

    def test_time_already_spent_counts_against_the_deadline(self):
        amazon = Amazon(refusals=1)
        orders.fetch_days = amazon
        self.clock.t = 90.0
        rows, done = orders.fetch_patiently(
            "tok", DAYS, deadline=100.0, wait=65,
            sleep=self.clock.sleep, now=self.clock.now, log=lambda *_: None)
        self.assertEqual(done, [])

    def test_a_partial_result_is_still_returned(self):
        # Two days collected before the budget ran out are two days of history
        # that never have to be fetched again.
        amazon = Amazon()
        amazon.refusals[date(2026, 8, 24)] = 99
        orders.fetch_days = amazon
        rows, done = orders.fetch_patiently(
            "tok", DAYS, deadline=70.0, wait=65,
            sleep=self.clock.sleep, now=self.clock.now, log=lambda *_: None)
        self.assertEqual(done, [date(2026, 8, 25), date(2026, 8, 26)])
        self.assertEqual(len(rows), 2)


class TestRealFailures(unittest.TestCase):
    def setUp(self):
        self._real = orders.fetch_days

    def tearDown(self):
        orders.fetch_days = self._real

    def test_a_500_stops_immediately_rather_than_being_waited_out(self):
        # A quota refills; a broken request does not. Sleeping through one
        # would burn the run's whole budget achieving nothing.
        clock = Clock()
        orders.fetch_days = Amazon(broken_on=date(2026, 8, 25))
        rows, done = orders.fetch_patiently(
            "tok", DAYS, deadline=10_000.0, wait=65,
            sleep=clock.sleep, now=clock.now, log=lambda *_: None)
        self.assertEqual(done, [date(2026, 8, 26)])
        self.assertEqual(clock.slept, [])


if __name__ == "__main__":
    unittest.main()
