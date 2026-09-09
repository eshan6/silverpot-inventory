"""Walk history backwards, a chunk at a time, until there is none left.

The dashboard should show as much history as Amazon still holds, without
anyone running anything by hand. The obvious reading of that - "fetch the
longest possible range every run" - does not survive contact with the rate
limits: `getOrders` allows roughly one call a minute and this pipeline makes
one call per day of history, so two years would be a twelve-hour run that
fails long before it finishes.

It is also unnecessary. A day already written to `sales_daily` does not change
once its orders have settled, so re-reading 2025 every morning buys nothing.
History has to be collected once; only the last few days need re-reading.

So each run does two things: it re-reads the trailing window, as before, and
then extends the archive backwards for as long as its time budget and
Amazon's throttle allow, a chunk at a time, writing each chunk before
starting the next. It fills unattended and then stops forever - after which
runs return here immediately and cost nothing.

**Progress is a cursor, not a query.** The obvious alternative - look up the
oldest row present and go back from there - stalls forever on a genuinely
empty stretch: a month with no sales writes no rows, so the oldest row never
moves and the same month is fetched every run until the end of time. The
cursor records what has been *attempted*, which is the thing that actually
advances.

**Stopping is empirical.** `HORIZON_DAYS` is a ceiling, not a claim about how
much history Amazon keeps. What normally stops the walk first is
`EMPTY_CHUNKS_TO_STOP` consecutive chunks with no rows at all, which means the
walk has gone back past the first sale. Being wrong about the retention window
therefore costs nothing in either direction: too generous and the empty-streak
rule stops it, too mean and the ceiling does.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Roughly how far back the Orders API still answers. A ceiling, and deliberately
# generous: the empty-streak rule below is what stops the walk in practice.
HORIZON_DAYS = 730

# One chunk per run. `getOrders` allows a burst of roughly twenty calls and
# then about one a minute, and this pipeline spends one call per day of
# history, so a chunk is sized to sit inside that burst rather than to fill a
# job. Asking for more does not fetch more: it gets throttled partway.
CHUNK_DAYS = 15

# How long a run may spend extending the archive. Actions minutes are free on
# a public repository, and this budget only applies while there is history left
# to collect - once the walk finishes it returns immediately and runs are back
# to about a minute. Generous, therefore: filling two years sooner is worth
# more than a short job.
BUDGET_SECONDS = 25 * 60

# Six empty chunks in a row - about three months without a single order -
# means the walk is before the business existed rather than in a quiet patch.
EMPTY_CHUNKS_TO_STOP = 6


@dataclass(frozen=True)
class State:
    """Where the walk has got to. Persisted in app_settings between runs."""
    cursor: date | None = None      # oldest day attempted so far
    empty_streak: int = 0
    done: bool = False

    def as_settings(self) -> dict:
        return {
            "cursor": self.cursor.isoformat() if self.cursor else None,
            "empty_streak": self.empty_streak,
            "done": self.done,
        }

    @classmethod
    def from_settings(cls, raw) -> "State":
        """Rebuild from stored JSON, tolerating anything unexpected.

        A malformed value restarts the walk rather than crashing the run. The
        cost of restarting is re-fetching days that are already correct in the
        database, which upserts absorb; the cost of crashing is the day's
        sales.
        """
        if not isinstance(raw, dict):
            return cls()
        cursor = raw.get("cursor")
        parsed: date | None = None
        if isinstance(cursor, str):
            try:
                parsed = date.fromisoformat(cursor)
            except ValueError:
                parsed = None
        streak = raw.get("empty_streak")
        return cls(
            cursor=parsed,
            empty_streak=streak if isinstance(streak, int) and streak >= 0 else 0,
            done=bool(raw.get("done")),
        )


def next_chunk(state: State, start_from: date, today: date,
               chunk_days: int = CHUNK_DAYS,
               horizon_days: int = HORIZON_DAYS) -> list[date]:
    """The days to fetch next, oldest first. Empty when there is nothing left.

    `start_from` is the oldest day the trailing window already covers, used
    only on the first run: the walk begins immediately below it so no day is
    fetched twice and none is skipped.
    """
    if state.done:
        return []

    floor = today - timedelta(days=horizon_days)
    cursor = state.cursor or start_from
    end = cursor - timedelta(days=1)
    if end < floor:
        return []

    start = max(floor, end - timedelta(days=chunk_days - 1))
    span = (end - start).days
    return [start + timedelta(days=i) for i in range(span + 1)]


def advance(state: State, chunk: list[date], rows_found: int,
            horizon_days: int = HORIZON_DAYS,
            today: date | None = None) -> State:
    """The state after fetching `chunk` and finding `rows_found` rows.

    `chunk` is what was actually *completed*, which may be shorter than what
    was asked for: throttling ends a chunk early, and the days already
    collected are still progress. The caller fetches newest-first inside the
    chunk so that a short one is contiguous with the history above it, leaving
    no hole for the cursor to skip over.
    """
    if not chunk:
        return State(cursor=state.cursor, empty_streak=state.empty_streak,
                     done=True)

    streak = state.empty_streak + 1 if rows_found == 0 else 0
    oldest = chunk[0]
    today = today or date.today()
    reached_floor = oldest <= today - timedelta(days=horizon_days)

    return State(
        cursor=oldest,
        empty_streak=streak,
        done=streak >= EMPTY_CHUNKS_TO_STOP or reached_floor,
    )


def describe(state: State, chunk: list[date]) -> str:
    """One line for the run log, so progress is visible without a database."""
    if state.done and not chunk:
        return (f"Backfill: complete, archive reaches back to "
                f"{state.cursor or 'the trailing window'}")
    if not chunk:
        return "Backfill: nothing further to fetch"
    return (f"Backfill: also fetching {chunk[0]} to {chunk[-1]} "
            f"({len(chunk)} day(s))")
