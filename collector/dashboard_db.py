"""Writes dashboard facts into Supabase, and records that it did.

Separate from `website.py`, which patches the *storefront's* Supabase project.
This talks to the dashboard's own project, for two reasons: the storefront's
database is managed by Lovable and a migration there should not be able to
collide with these tables, and a leaked key should not reach across both.

Everything here runs as the service role, which bypasses RLS by design - the
policies in `dashboard/supabase/migrations` exist to constrain browsers, not
the ingestion job. That key lives in GitHub Actions secrets and is never sent
to anything a browser can reach.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

from . import net

REQUIRED = ("DASHBOARD_SUPABASE_URL", "DASHBOARD_SUPABASE_SERVICE_KEY")

# Supabase rejects very large payloads and PostgREST is happier in batches.
# 500 rows is roughly two weeks of Silverpot's SKU count.
BATCH = 500


def configured() -> bool:
    return all(os.getenv(k) for k in REQUIRED)


def missing() -> list[str]:
    return [k for k in REQUIRED if not os.getenv(k)]


class NotSupabase(RuntimeError):
    """The endpoint answered, but it is not a Supabase REST API."""


def _base() -> str:
    return os.environ["DASHBOARD_SUPABASE_URL"].rstrip("/")


def _headers(*, upsert: bool = False) -> dict:
    key = os.environ["DASHBOARD_SUPABASE_SERVICE_KEY"]
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if upsert:
        # Re-ingesting a trailing window is the whole point: a day that gains a
        # cancellation must be corrected, not duplicated.
        h["Prefer"] = "return=minimal,resolution=merge-duplicates"
    return h


def _check(resp, what: str) -> None:
    """Fail loudly, and name the wrong setting when the URL is not Supabase."""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    body = (resp.text or "").strip()
    if "html" in ctype or body[:1] == "<":
        # The storefront push hit exactly this: SUPABASE_URL pointed at the
        # website, a single-page app answered 200 with index.html for every
        # path, and 36 writes were swallowed while the run reported success.
        raise NotSupabase(
            f"{what}: expected a Supabase REST response, got {resp.status_code} "
            f"{ctype or 'no content-type'} starting {body[:80]!r}. "
            "DASHBOARD_SUPABASE_URL is probably pointing at a website rather "
            "than the project API URL (Project Settings > API > Project URL)."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"{what} failed: {resp.status_code} {body[:300]}")


def upsert(table: str, rows: list[dict], sess=None) -> int:
    """Insert or update rows by primary key. Returns how many were sent."""
    if not rows:
        return 0
    sess = sess or net.session()
    sent = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        resp = sess.post(f"{_base()}/rest/v1/{table}",
                         headers=_headers(upsert=True), json=chunk,
                         timeout=net.TIMEOUT)
        _check(resp, f"upsert into {table}")
        sent += len(chunk)
    return sent


def get_setting(key: str, sess=None):
    """One row out of app_settings, or None.

    Used to remember how far the history walk has got. Like the run ledger,
    this is bookkeeping: if it cannot be read, the run still has real work to
    do, so the caller gets None and starts the walk over rather than failing.
    Re-walking costs repeated upserts of rows that are already correct.
    """
    sess = sess or net.session()
    try:
        resp = sess.get(f"{_base()}/rest/v1/app_settings",
                        headers=_headers(),
                        params={"select": "value", "key": f"eq.{key}"},
                        timeout=net.TIMEOUT)
        _check(resp, f"read app_settings/{key}")
        body = resp.json() or []
        return body[0]["value"] if body else None
    except NotSupabase:
        raise
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"  NOTE could not read setting {key}: {net.describe_error(exc)}")
        return None


def set_setting(key: str, value, sess=None) -> None:
    """Record a setting. Never fails the run."""
    sess = sess or net.session()
    try:
        resp = sess.post(f"{_base()}/rest/v1/app_settings",
                         headers=_headers(upsert=True),
                         json=[{"key": key, "value": value}],
                         timeout=net.TIMEOUT)
        _check(resp, f"write app_settings/{key}")
    except Exception as exc:  # noqa: BLE001
        print(f"  NOTE could not save setting {key}: {net.describe_error(exc)}")


def start_run(marketplace: str, source: str, covers_from, covers_to,
              sess=None) -> int | None:
    """Record that ingestion began. Returns the run id, or None if unrecorded.

    Bookkeeping must never be the reason a run fails: if the ledger cannot be
    written, the facts are still worth writing.
    """
    sess = sess or net.session()
    row = {
        "marketplace": marketplace,
        "source": source,
        "status": "running",
        "covers_from": str(covers_from),
        "covers_to": str(covers_to),
    }
    try:
        headers = _headers()
        headers["Prefer"] = "return=representation"
        resp = sess.post(f"{_base()}/rest/v1/ingest_runs", headers=headers,
                         json=[row], timeout=net.TIMEOUT)
        _check(resp, "start ingest_run")
        body = resp.json()
        return body[0]["id"] if body else None
    except NotSupabase:
        raise
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"  NOTE could not open an ingest_runs row: "
              f"{net.describe_error(exc)}")
        return None


def finish_run(run_id: int | None, status: str, rows_written: int = 0,
               error: str | None = None, sess=None) -> None:
    """Close the ledger entry. A missing run id is not worth failing over."""
    if run_id is None:
        return
    sess = sess or net.session()
    patch = {
        "status": status,
        "rows_written": rows_written,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if error:
        # Truncated: this is a status line for a dashboard header, not a log.
        patch["error"] = error[:500]
    try:
        resp = sess.patch(f"{_base()}/rest/v1/ingest_runs",
                          headers=_headers(), params={"id": f"eq.{run_id}"},
                          json=patch, timeout=net.TIMEOUT)
        _check(resp, "finish ingest_run")
    except Exception as exc:  # noqa: BLE001
        print(f"  NOTE could not close ingest_run {run_id}: "
              f"{net.describe_error(exc)}")
