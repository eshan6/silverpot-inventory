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
import os
import time
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
# Confirmed by the API on 2026-10-20. Sending an unsupported value makes it
# enumerate the whole set, which is how this list was obtained rather than
# guessed:
#
#   Key in HistoryEventTypes: 0 is not supported. Needs to be one of
#   CAMPAIGN,AD_GROUP,AD,KEYWORD,PRODUCT_TARGETING,NEGATIVE_KEYWORD,
#   NEGATIVE_PRODUCT_TARGETING,THEME
#
# All eight are requested. The task names six; NEGATIVE_PRODUCT_TARGETING and
# THEME cost nothing to include and are the same kind of change, and a type
# left out is a change that silently never arrives.
RECORD_TYPES = [
    "CAMPAIGN",
    "AD_GROUP",
    "AD",
    "KEYWORD",
    "PRODUCT_TARGETING",
    "NEGATIVE_KEYWORD",
    "NEGATIVE_PRODUCT_TARGETING",
    "THEME",
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


# How `eventTypes` is encoded. Round three sent Amazon's own enumerated list
# back to it verbatim, as a JSON array of strings, and was refused:
#
#   Key in HistoryEventTypes: 0 is not supported. Needs to be one of
#   CAMPAIGN,AD_GROUP,...
#
# The `0` is the interesting part. It is reporting a *key* of 0 - the array
# index - rather than the value at that index, and an empty array was
# accepted where a populated one was not. That is what a deserialiser does
# when it wanted a map and got a list: an empty list and an empty map look
# alike, a populated one does not. So the encoding is the open question, not
# the vocabulary.
def _event_type_encodings() -> list[tuple[str, object]]:
    return [
        ("array of strings (the round-three control)", list(RECORD_TYPES)),
        ("map of name to true", {t: True for t in RECORD_TYPES}),
        ("comma-separated string", ",".join(RECORD_TYPES)),
        ("array of objects", [{"eventType": t} for t in RECORD_TYPES]),
        ("map of index to name", {str(i): t for i, t in enumerate(RECORD_TYPES)}),
        ("single string", RECORD_TYPES[0]),
    ]


def _request(token: str, start: datetime, end: datetime, sess,
             page: int = 0, page_size: int = 50, event_types=None):
    """One history request. Confirmed shape, retried through a 429.

    Rate limiting is real here: round two fired five requests back to back and
    the third came back 429, which is also why the one candidate that would
    have worked appeared not to. A probe that cannot tell "wrong request" from
    "too many requests" teaches the wrong lesson, so this backs off.
    """
    body = {
        "fromDate": _millis(start),
        "toDate": _millis(end),
        "eventTypes": list(RECORD_TYPES) if event_types is None else event_types,
        "pageSize": page_size,
        "pageOffset": page * page_size,
    }
    delay = 2.0
    for attempt in range(5):
        resp = sess.post(
            f"{ads.ADS_HOST}{HISTORY_PATH}",
            headers=_headers(token, "application/json"),
            json=body,
            timeout=net.TIMEOUT,
        )
        if resp.status_code != 429:
            return resp, body
        if attempt < 4:
            print(f"    429, waiting {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    return resp, body


def _shape(value) -> str:
    """What a value *is*, never what it says.

    The Actions logs on this repository are public, and change history
    carries campaign names, keyword text and bid amounts - Silverpot's
    advertising strategy in full. The same reasoning already applies to
    orders_report, which prints column names and nothing from any row because
    the rows carry buyer addresses. So the probe reports each field's type and
    size and redacts the content.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        digits = len(str(abs(int(value))))
        return f"{type(value).__name__}(~{digits} digits)"
    if isinstance(value, str):
        return f"str(len {len(value)})"
    if isinstance(value, list):
        inner = _shape(value[0]) if value else "empty"
        return f"list[{len(value)}] of {inner}"
    if isinstance(value, dict):
        return f"object{{{', '.join(sorted(value))}}}"
    return type(value).__name__


def _candidates(start: datetime, end: datetime, max_results: int = 100):
    """Round two: the envelope is settled, the vocabulary is not.

    Round one sent five shapes and all five came back with the same 400:

        Invalid request: Error: eventTypes, fromDate(milliseconds) and
        toDate(milliseconds) are required fields in the request body

    So the body is `eventTypes`, `fromDate` and `toDate`, the dates are epoch
    milliseconds, and none of `startDateTime`, `endDateTime` or
    `recordTypeFilter` - the names this module guessed first - exist. A 400
    about body fields also means the endpoint is live and the token reaches
    it, which is worth more than the guesses were.

    What is still unknown is the `eventTypes` vocabulary. The first candidate
    below is deliberately invalid: an enum rejection usually enumerates the
    values it would have accepted, which is cheaper than guessing six names
    one run at a time.
    """
    window = {"fromDate": _millis(start), "toDate": _millis(end)}
    return [
        (
            "deliberately invalid eventType, to make Amazon list the real ones",
            "application/json",
            {**window, "eventTypes": ["SILVERPOT_NOT_A_REAL_EVENT_TYPE"]},
        ),
        (
            "entity names, SCREAMING_SNAKE",
            "application/json",
            {**window, "eventTypes": RECORD_TYPES},
        ),
        (
            "entity names, camelCase",
            "application/json",
            {**window, "eventTypes": ["campaign", "adGroup", "ad", "keyword",
                                      "negativeKeyword", "productTargeting"]},
        ),
        (
            "lifecycle names rather than entity names",
            "application/json",
            {**window, "eventTypes": ["CREATED", "UPDATED", "DELETED"]},
        ),
        (
            "empty list, to see whether the filter may be omitted in effect",
            "application/json",
            {**window, "eventTypes": []},
        ),
        (
            "entity names plus paging, once a vocabulary is accepted",
            "application/json",
            {**window, "eventTypes": RECORD_TYPES, "maxResults": max_results},
        ),
    ]


def _headers(token: str, content_type: str) -> dict:
    head = ads._headers(token)
    head["Content-Type"] = content_type
    head["Accept"] = content_type
    return head


def probe(days: int = HISTORY_RETENTION_DAYS, sess=None) -> int:
    """Ask for a real window and report the record shape. Writes nothing.

    Rounds one and two settled the request: POST /history with `eventTypes`,
    `fromDate` and `toDate` in epoch milliseconds, answering with
    {"events": [...], "pageSize", "pageOffset", "maxPageNumber",
    "totalRecords"}. What is left is the shape of an individual event, which
    needs a window that actually contains changes - hence ninety days rather
    than seven.

    Field *names* are printed. Field *values* are not: see `_shape`.
    """
    sess = sess or net.session()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    token = ads.get_access_token(sess=sess)
    print(f"Probing {ads.ADS_HOST}{HISTORY_PATH} over the last {days} day(s).")
    print(f"Window {start:%Y-%m-%d}..{end:%Y-%m-%d}")
    print("Values are redacted: these logs are public and change history "
          "carries keyword text and bids.\n")

    # Round four: find the encoding `eventTypes` wants, one request at a
    # time with a pause between, because the limiter here is tight enough
    # that back-to-back probes answer 429 and look like refusals.
    payload = None
    for label, encoding in _event_type_encodings():
        resp, body = _request(token, start, end, sess, event_types=encoding)
        sent = body["eventTypes"]
        print(f"[{label}] HTTP {resp.status_code}  eventTypes={_shape(sent)}")
        if resp.status_code >= 400:
            print(f"    {(resp.text or '')[:260]}")
            time.sleep(3)
            continue
        try:
            candidate = resp.json()
        except ValueError:
            print("    200 but not JSON")
            time.sleep(3)
            continue
        total = candidate.get("totalRecords")
        print(f"    accepted; totalRecords={total}")
        # An empty answer is not proof the encoding worked - the empty array
        # in round two was accepted and returned nothing, which is exactly
        # what a silently-ignored filter looks like. Prefer an encoding that
        # actually brings records back, and keep looking otherwise.
        if payload is None or (total or 0) > (payload.get("totalRecords") or 0):
            payload = candidate
        if total:
            print("    records returned - this is the encoding.\n")
            break
        time.sleep(3)

    if payload is None:
        print("\nNo encoding was accepted. The bodies above are the evidence.")
        return 1
    print(f"envelope keys : {sorted(payload)}")
    for k in ("totalRecords", "pageSize", "pageOffset", "maxPageNumber"):
        if k in payload:
            print(f"  {k:<14}= {payload[k]}")

    events = _rows_of(payload)
    print(f"events on page: {len(events)}")
    if not events:
        print("\nAccepted and empty. Either nothing changed in ninety days, "
              "or the account's history is genuinely bare - both are real "
              "answers, and neither is a wrong request.")
        return 0

    keys: dict[str, set] = {}
    types: dict[str, int] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        for k, v in e.items():
            keys.setdefault(k, set()).add(_shape(v))
        label = str(e.get("eventType") or e.get("type") or "?")
        types[label] = types.get(label, 0) + 1

    print("\nfields seen across every event on this page:")
    for k in sorted(keys):
        print(f"  {k:<28} {', '.join(sorted(keys[k]))}")
    print("\nevent types on this page:")
    for k in sorted(types):
        print(f"  {k:<28} {types[k]}")
    print("\nThose names are what the ingestion maps from. Nothing is written "
          "until they are in ads_history's own FIELDS list.")
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
    # `events` first: confirmed live on 2026-10-20, in the envelope
    # {"events": [...], "pageSize": 50, "pageOffset": 0, "maxPageNumber": 0,
    #  "totalRecords": 0}. The rest are kept as fallbacks only.
    for key in ("events", "changes", "records", "history", "elements",
                "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="send the candidate requests and print what returns")
    ap.add_argument("--days", type=int, default=HISTORY_RETENTION_DAYS,
                    help="how far back the probe window reaches (default 90, Amazon.s retention window)")
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
