"""Writes published availability into the website's own database.

The site already has an admin panel that writes a stock number per product.
Rather than bolting a second source of truth onto the frontend, this writes into
the same table that panel writes to, so the storefront's existing in-stock and
order-limit logic keeps working untouched.

Manual override is preserved. If the products table has an `inventory_source`
column, any row set to 'manual' is skipped, so pinning a SKU by hand in the
admin panel survives the next sync instead of being silently overwritten.

Everything is configured by environment variable, so nothing here needs editing
once the real table and column names are known.
"""
import os

import requests

REQUIRED = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_TABLE",
            "SUPABASE_SKU_COLUMN", "SUPABASE_QTY_COLUMN")


def configured() -> bool:
    return all(os.getenv(k) for k in REQUIRED)


def missing() -> list[str]:
    return [k for k in REQUIRED if not os.getenv(k)]


def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def push(results: list[dict], dry_run: bool = False) -> dict:
    """Update the website stock column for every SKU. Returns a summary."""
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    table = os.environ["SUPABASE_TABLE"]
    sku_col = os.environ["SUPABASE_SKU_COLUMN"]
    qty_col = os.environ["SUPABASE_QTY_COLUMN"]
    synced_col = os.getenv("SUPABASE_SYNCED_COLUMN", "inventory_synced_at")
    source_col = os.getenv("SUPABASE_SOURCE_COLUMN", "inventory_source")
    respect_manual = os.getenv("RESPECT_MANUAL_OVERRIDE", "true").lower() == "true"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    updated, skipped, unmatched = 0, 0, []
    for r in results:
        sku = r.get("sku")
        if not sku:
            continue
        body = {qty_col: r["published_available"]}
        if synced_col:
            body[synced_col] = now

        params = {sku_col: f"eq.{sku}"}
        if respect_manual and source_col:
            # Update rows whose source is not 'manual'. NULL counts as automatic.
            params[source_col] = "not.eq.manual"

        if dry_run:
            print(f"  DRY {sku} -> {qty_col}={r['published_available']}")
            continue

        resp = requests.patch(
            f"{url}/rest/v1/{table}",
            headers=_headers(key),
            params=params,
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Supabase write failed for {sku}: {resp.status_code} {resp.text[:300]}"
            )
        rows = resp.json() if resp.text else []
        if rows:
            updated += 1
        else:
            # Either the SKU isn't in the table, or the row is pinned to manual.
            unmatched.append(sku)

    if unmatched and respect_manual:
        skipped = len(unmatched)

    return {"updated": updated, "no_row_updated": unmatched, "skipped_or_missing": skipped}
