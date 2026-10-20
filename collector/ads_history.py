"""Amazon Ads change history: who changed what, and when.

    python -m collector.ads_history --probe     # find the shape, write nothing

**Read the probe before trusting anything here.** This repository has lost
time to nine wrong guesses at a Walmart field name and to `cost` vs `spend` in
the Ads reports, and the rule that came out of it is in CLAUDE.md: do not
hardcode an API field name that has not been seen in a live response.

The change history endpoint is newer than the reporting ones and this session
could reach neither Amazon's docs nor the API host, so the request shape below
is a *candidate list*, not a specification. `--probe` sends each candidate and
prints what comes back, including the error bodies - Amazon's 400s have
already given up one fact this pipeline relies on (the sixty-day retention
window came out of a refusal, not a document). Once the probe has answered,
the winning candidate is the one to keep and the rest should be deleted.

Nothing here writes to the database until that has happened.

Amazon keeps roughly ninety days of change history and deletes the rest, so
the backfill is worth running once and promptly; a day missed past the window
is gone for good, unlike the reporting data, which can be re-read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from . import ads, net

HISTORY_PATH = "/history"

# How far back Amazon is said to keep change history. A ceiling, like
# ADS_HORIZON_DAYS was: if the real figure is smaller the API will say so, and
# the probe is what will show us the wording of that refusal.
HISTORY_RETENTION_DAYS = 90

# The record types the task names, in Amazon's camelCase house style. Which
# spelling the API actually wants is exactly what the probe is for - several
# are tried below rather than one being assumed.
RECORD_TYPES = [
    "CAMPAIGN",
    "AD_GROUP",
    "AD",
    "KEYWORD",
    "NEGATIVE_KEYWORD",
    "PRODUCT_TARGETING",
]

# Candidate request shapes, most-likely first. Each is (label, content_type,
# body_builder). The probe reports which, if any, Amazon accepts.
#
# They differ in the two things most likely to be wrong: the vendor media type
# (the reporting API uses application/vnd.createasyncreportrequest.v3+json, so
# history plausibly has its own) and whether the window is epoch milliseconds
# or an ISO instant.


def _millis(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _candidates(start: datetime, end: datetime, max_results: int = 100):
    iso_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_end = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        (
            "vnd.history.v1 + epoch millis",
            "application/vnd.history.v1+json",
            {
                "startDateTime": _millis(start),
                "endDateTime": _millis(end),
                "recordTypeFilter": RECORD_TYPES,
                "maxResults": max_results,
            },
        ),
        (
            "vnd.history.v1 + ISO instants",
            "application/vnd.history.v1+json",
            {
                "startDateTime": iso_start,
                "endDateTime": iso_end,
                "recordTypeFilter": RECORD_TYPES,
                "maxResults": max_results,
            },
        ),
        (
            "plain json + epoch millis",
            "application/json",
            {
                "startDateTime": _millis(start),
                "endDateTime": _millis(end),
                "recordTypeFilter": RECORD_TYPES,
                "maxResults": max_results,
            },
        ),
        (
            "plain json + startDate/endDate days",
            "application/json",
            {
                "startDate": start.date().isoformat(),
                "endDate": end.date().isoformat(),
                "recordTypeFilter": RECORD_TYPES,
                "maxResults": max_results,
            },
        ),
        (
            "plain json, no record filter",
            "application/json",
            {
                "startDateTime": _millis(start),
                "endDateTime": _millis(end),
                "maxResults": max_results,
            },
        ),
    ]


def _headers(token: str, content_type: str) -> dict:
    head = ads._headers(token)
    head["Content-Type"] = content_type
    head["Accept"] = content_type
    return head


def probe(days: int = 7, sess=None) -> int:
    """Send each candidate and print what Amazon says. Writes nothing.

    Prints bodies because they are Amazon's own words and they are where the
    answer is. Nothing of ours joins them: no token, no client id, no profile
    id - this repository's Actions logs are public.
    """
    sess = sess or net.session()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    token = ads.get_access_token(sess=sess)
    print(f"Probing {ads.ADS_HOST}{HISTORY_PATH} over the last {days} day(s).")
    print(f"Window {start:%Y-%m-%d %H:%M}Z .. {end:%Y-%m-%d %H:%M}Z\n")

    winners = []
    for label, content_type, body in _candidates(start, end):
        try:
            resp = sess.post(
                f"{ads.ADS_HOST}{HISTORY_PATH}",
                headers=_headers(token, content_type),
                json=body,
                timeout=net.TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
            print(f"[{label}] transport error: {net.describe_error(exc)}\n")
            continue

        print(f"[{label}] HTTP {resp.status_code}")
        print(f"    sent: {json.dumps(body)[:200]}")
        text = (resp.text or "").strip()
        print(f"    body: {text[:700] or '(empty)'}")

        if 200 <= resp.status_code < 300:
            winners.append(label)
            try:
                payload = resp.json()
            except ValueError:
                print("    (200 but not JSON)\n")
                continue
            print(f"    top-level keys: {sorted(payload)}")
            rows = _rows_of(payload)
            print(f"    rows: {len(rows)}")
            if rows:
                print(f"    row keys: {sorted(rows[0])}")
                print(f"    first row: {json.dumps(rows[0], indent=2)[:1200]}")
            else:
                print("    (accepted, and empty - no changes in this window)")
        print()

    if winners:
        print(f"Accepted: {', '.join(winners)}")
        print("Keep the first of these in _candidates and delete the rest.")
    else:
        print("Nothing was accepted. The bodies above are the evidence; the "
              "wording usually names the field it wanted.")
    return 0


def _rows_of(payload) -> list:
    """The change records out of whatever envelope Amazon wrapped them in.

    Candidate-list-plus-fallback, the same pattern as walmart.py: the key is
    read through a list of plausible names rather than guessed at once.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("changes", "records", "history", "elements", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="send the candidate requests and print what returns")
    ap.add_argument("--days", type=int, default=7,
                    help="how far back the probe window reaches (default 7)")
    args = ap.parse_args()

    if not ads.configured():
        print(f"Amazon Ads not configured: {', '.join(ads.missing())}",
              file=sys.stderr)
        return 1

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    if args.probe:
        return probe(days=args.days)

    print("Nothing to do yet: ingestion is not written until --probe has "
          "settled the request shape. See the module docstring.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
