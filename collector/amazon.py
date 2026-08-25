"""Amazon SP-API FBA inventory client.

Auth is LWA-only. Amazon removed the AWS IAM / SigV4 signing requirement in
October 2023, so there is no AWS account, no IAM user, and no request signing.
A refresh token plus a client id/secret is the whole story.
"""
import os
import time

from . import net
from .config import LWA_TOKEN_URL, SPAPI_HOST, US_MARKETPLACE_ID, require_env

REQUIRED = ("LWA_REFRESH_TOKEN", "LWA_CLIENT_ID", "LWA_CLIENT_SECRET")


def configured() -> bool:
    return all(os.getenv(k) for k in REQUIRED)


def missing() -> list[str]:
    return [k for k in REQUIRED if not os.getenv(k)]


def get_access_token() -> str:
    env = require_env(*REQUIRED)
    s = net.session()
    resp = s.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": env["LWA_REFRESH_TOKEN"].strip(),
            "client_id": env["LWA_CLIENT_ID"].strip(),
            "client_secret": env["LWA_CLIENT_SECRET"].strip(),
        },
        timeout=net.TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_fba_summaries(access_token: str) -> list[dict]:
    """Return every FBA inventory summary for the US marketplace, with details."""
    url = f"{SPAPI_HOST}/fba/inventory/v1/summaries"
    headers = {"x-amz-access-token": access_token, "Accept": "application/json"}
    params = {
        "details": "true",
        "granularityType": "Marketplace",
        "granularityId": US_MARKETPLACE_ID,
        "marketplaceIds": US_MARKETPLACE_ID,
    }

    s = net.session()
    out: list[dict] = []
    next_token = None
    for page in range(50):  # generous ceiling; 31 SKUs will finish in 1
        if next_token:
            params = {
                "details": "true",
                "granularityType": "Marketplace",
                "granularityId": US_MARKETPLACE_ID,
                "marketplaceIds": US_MARKETPLACE_ID,
                "nextToken": next_token,
            }
        resp = s.get(url, headers=headers, params=params, timeout=net.TIMEOUT)
        if resp.status_code == 429:
            time.sleep(5)
            continue
        resp.raise_for_status()
        body = resp.json()
        payload = body.get("payload", body)
        out.extend(payload.get("inventorySummaries", []))
        next_token = (body.get("pagination") or payload.get("pagination") or {}).get("nextToken")
        if not next_token:
            break
        time.sleep(0.6)  # rate limit is 2 req/sec
    return out


def parse_summary(summary: dict) -> dict:
    """Flatten one FBA summary into named quantity states.

    `fulfillable` is the only pool that is sellable today. The rest are stored
    for forecasting and must never reach the website feed.
    """
    d = summary.get("inventoryDetails") or {}
    reserved = d.get("reservedQuantity") or {}
    researching = d.get("researchingQuantity") or {}
    unfulfillable = d.get("unfulfillableQuantity") or {}

    def n(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "seller_sku": (summary.get("sellerSku") or "").strip(),
        "asin": (summary.get("asin") or "").strip(),
        "fnsku": (summary.get("fnSku") or "").strip(),
        "fulfillable": n(d.get("fulfillableQuantity")),
        "inbound_working": n(d.get("inboundWorkingQuantity")),
        "inbound_shipped": n(d.get("inboundShippedQuantity")),
        "inbound_receiving": n(d.get("inboundReceivingQuantity")),
        "reserved_customer_order": n(reserved.get("pendingCustomerOrderQuantity")),
        "reserved_transshipment": n(reserved.get("pendingTransshipmentQuantity")),
        "reserved_fc_processing": n(reserved.get("fcProcessingQuantity")),
        "reserved_total": n(reserved.get("totalReservedQuantity")),
        "researching": n(researching.get("totalResearchingQuantity")),
        "unfulfillable": n(unfulfillable.get("totalUnfulfillableQuantity")),
        "total_quantity": n(summary.get("totalQuantity")),
    }
