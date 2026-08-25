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
import os
import uuid

from . import net
from .config import WALMART_HOST, require_env

REQUIRED = ("WALMART_CLIENT_ID", "WALMART_CLIENT_SECRET")


def configured() -> bool:
    return all(os.getenv(k) for k in REQUIRED)


def missing() -> list[str]:
    return [k for k in REQUIRED if not os.getenv(k)]

# Confirmed against a live /v3/wfs/inventory response. The real envelope is:
#
#   payload.inventory[] = {
#     itemInformation:  { sku, gtin, itemName, brand, itemID, ... },
#     inventoryData:    { availableUnits, onhandUnits, reservedUnits,
#                         inboundUnits, stockStatus, inventoryAge{...}, ... },
#     inventoryInsights:{ daysOfSupply, sellThroughRate, surplusUnits, ... }
#   }
#
# Note "onhandUnits" - lowercase h. Walmart's own docs do not name these fields,
# so the earlier guesses were all wrong. Confirmed names lead each list; the
# alternates stay as fallbacks in case Walmart renames anything.
ATS_FIELDS = [
    "availableUnits",
    "availableToSellQty", "availableToSellQuantity", "availableToSell",
    "atsQty", "ats", "sellableQty", "sellableQuantity", "availableQuantity",
]
ONHAND_FIELDS = ["onhandUnits", "onHandUnits", "onHandQty", "onHandQuantity", "onHand"]
RESERVED_FIELDS = ["reservedUnits", "reservedQty", "reservedQuantity", "reserved"]
INBOUND_FIELDS = ["inboundUnits", "inboundQty", "inboundQuantity", "inbound"]
SKU_FIELDS = ["sku", "sellerSku", "itemSku", "merchantSku"]
STATUS_FIELDS = ["stockStatus"]
DOS_FIELDS = ["daysOfSupply"]


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
    env = require_env(*REQUIRED)
    basic = base64.b64encode(
        f"{env['WALMART_CLIENT_ID']}:{env['WALMART_CLIENT_SECRET']}".encode()
    ).decode()
    headers = _headers()
    headers["Authorization"] = f"Basic {basic}"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    resp = net.session().post(
        f"{WALMART_HOST}/v3/token",
        headers=headers,
        data={"grant_type": "client_credentials"},
        timeout=net.TIMEOUT,
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
    sess = net.session()
    last_error = None
    for url in endpoints:
        records: list[dict] = []
        raw_first = {}
        offset, limit = 0, 200
        try:
            for _ in range(25):
                resp = sess.get(
                    url,
                    headers=_headers(token),
                    params={"limit": limit, "offset": offset},
                    timeout=net.TIMEOUT,
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

    # Aged stock matters: WFS charges long-term storage on units sitting past
    # 270 days, so surface it rather than burying it in the raw payload.
    age = ((rec.get("inventoryData") or {}).get("inventoryAge") or {})
    aged = n(age.get("271To365days")) + n(age.get("365PlusDays")) + \
        n(age.get("365To450days")) + n(age.get("450PlusDays"))

    return {
        "sku": str(_deep_find(rec, SKU_FIELDS) or "").strip(),
        "item_name": str(_deep_find(rec, ["itemName"]) or "").strip(),
        "available_to_sell": n(_deep_find(rec, ATS_FIELDS)),
        "on_hand": n(_deep_find(rec, ONHAND_FIELDS)),
        "reserved": n(_deep_find(rec, RESERVED_FIELDS)),
        "inbound": n(_deep_find(rec, INBOUND_FIELDS)),
        "aged_over_270d": aged,
        "stock_status": str(_deep_find(rec, STATUS_FIELDS) or ""),
        "days_of_supply": str(_deep_find(rec, DOS_FIELDS) or ""),
    }
