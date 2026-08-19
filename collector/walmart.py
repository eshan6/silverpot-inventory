"""Walmart Marketplace WFS inventory client.

Two things to know:

1. The legacy endpoint /v3/fulfillment/inventory reached end of life on
   2026-03-03 and was replaced by /v3/wfs/inventory. We call the new one and
   fall back to the legacy path only if the new one 404s.

2. Walmart's own documentation for the new endpoint is inconsistent about the
   response field names (the "new" page still shows the legacy URL in its curl
   sample). So we do not hardcode a single field name for available-to-sell.
   We search a candidate list, and `probe_wfs()` dumps the raw first record so
   you can confirm the real field names against your own account on day one.
"""
import base64
import uuid

import requests

from .config import WALMART_HOST, require_env

# Ordered by preference. First match wins.
ATS_FIELDS = [
    "availableToSellQty", "availableToSellQuantity", "availableToSell",
    "atsQty", "ats", "sellableQty", "sellableQuantity",
    "availToSellQty", "availableQuantity",
]
ONHAND_FIELDS = ["onHandQty", "onHandQuantity", "onHand", "totalQty", "totalQuantity"]
RESERVED_FIELDS = ["reservedQty", "reservedQuantity", "reserved"]
INBOUND_FIELDS = ["inboundQty", "inboundQuantity", "inbound"]
SKU_FIELDS = ["sku", "sellerSku", "itemSku", "merchantSku"]


def _headers(token: str | None = None) -> dict:
    h = {
        "WM_SVC.NAME": "Walmart Marketplace",
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "Accept": "application/json",
    }
    if token:
        h["WM_SEC.ACCESS_TOKEN"] = token
    return h


def get_access_token() -> str:
    env = require_env("WALMART_CLIENT_ID", "WALMART_CLIENT_SECRET")
    basic = base64.b64encode(
        f"{env['WALMART_CLIENT_ID']}:{env['WALMART_CLIENT_SECRET']}".encode()
    ).decode()
    headers = _headers()
    headers["Authorization"] = f"Basic {basic}"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    resp = requests.post(
        f"{WALMART_HOST}/v3/token",
        headers=headers,
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _deep_find(obj, names: list[str]):
    """Find the first matching key anywhere in a nested dict/list, case-insensitively."""
    lowered = {n.lower() for n in names}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in lowered and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = _deep_find(v, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, names)
            if found is not None:
                return found
    return None


def _extract_records(body) -> list[dict]:
    """Pull the list of per-SKU records out of whatever envelope Walmart used."""
    if isinstance(body, list):
        return body
    for key in ("elements", "payload", "inventories", "items", "inventory", "data"):
        val = body.get(key) if isinstance(body, dict) else None
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            inner = _extract_records(val)
            if inner:
                return inner
    return []


def fetch_wfs_inventory(token: str) -> tuple[list[dict], dict]:
    """Return (records, raw_first_page) for WFS inventory."""
    endpoints = [f"{WALMART_HOST}/v3/wfs/inventory", f"{WALMART_HOST}/v3/fulfillment/inventory"]
    last_error = None
    for url in endpoints:
        records: list[dict] = []
        raw_first = {}
        offset, limit = 0, 200
        try:
            for _ in range(25):
                resp = requests.get(
                    url,
                    headers=_headers(token),
                    params={"limit": limit, "offset": offset},
                    timeout=60,
                )
                if resp.status_code == 404:
                    raise FileNotFoundError(url)
                resp.raise_for_status()
                body = resp.json()
                if not raw_first:
                    raw_first = body
                page = _extract_records(body)
                records.extend(page)
                if len(page) < limit:
                    break
                offset += limit
            return records, raw_first
        except FileNotFoundError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"No working WFS inventory endpoint. Last: {last_error}")


def parse_record(rec: dict) -> dict:
    def n(v) -> int:
        try:
            return int(float(v or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "sku": str(_deep_find(rec, SKU_FIELDS) or "").strip(),
        "available_to_sell": n(_deep_find(rec, ATS_FIELDS)),
        "on_hand": n(_deep_find(rec, ONHAND_FIELDS)),
        "reserved": n(_deep_find(rec, RESERVED_FIELDS)),
        "inbound": n(_deep_find(rec, INBOUND_FIELDS)),
    }
