"""Amazon SP-API FBA inventory client.

Auth is LWA-only. Amazon removed the AWS IAM / SigV4 signing requirement in
October 2023, so there is no AWS account, no IAM user, and no request signing.
A refresh token plus a client id/secret is the whole story.

Why there are two routes in
---------------------------
The daily sync was returning 403 from `/fba/inventory/v1/summaries` while the
LWA token exchange succeeded. That combination is not a broken client and not a
bad token. It means the token is real but the application is not carrying the
role that guards this particular resource.

The role that guards the FBA Inventory API is **Amazon Fulfillment**. It is not
"Inventory and Order Tracking" - that one covers the Orders API and order
tracking reports, which is why being approved for it did nothing here. Several
sellers also had to add **Product Listing** before the 403 cleared. See
`diagnose()` below, which determines empirically which roles this token
actually carries instead of guessing.

That fix is a checkbox in Seller Central, not code. So there is a second route:

  1. FBA Inventory API   `/fba/inventory/v1/summaries`   - Amazon Fulfillment.
     One request, near real time, full reserved breakdown.
  2. Reports API         `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA`, then
     `GET_AFN_INVENTORY_DATA` - a different role set, so it can succeed while
     route 1 is denied. Slower (a report has to be generated) and coarser (the
     reserved breakdown collapses to a single total), but the number that
     matters - fulfillable units - is the same number.

Route 1 is always tried first. Route 2 only runs when route 1 returns 403, and
the route actually used is recorded in the feed so any published number can be
traced back to how it was obtained.
"""
import csv
import gzip
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from . import net
from .config import LWA_TOKEN_URL, SPAPI_HOST, US_MARKETPLACE_ID, require_env

REQUIRED = ("LWA_REFRESH_TOKEN", "LWA_CLIENT_ID", "LWA_CLIENT_SECRET")

REPORTS_PATH = "/reports/2021-06-30"

# Reports carrying FBA fulfillable quantities, richest first. The first one
# covers every quantity state the FBA Inventory API returns except the reserved
# breakdown; the second is a thinner fallback that still carries the only
# figure that is allowed to reach the storefront.
INVENTORY_REPORTS = (
    "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
    "GET_AFN_INVENTORY_DATA",
)

# Report generation is asynchronous. FBA inventory reports normally land inside
# a minute; the ceiling exists so a stuck report fails the Amazon leg loudly
# instead of hanging the whole workflow.
REPORT_POLL_SECONDS = 10
REPORT_POLL_CEILING = 420

ROLE_HINT = (
    "The FBA Inventory API is guarded by the 'Amazon Fulfillment' role. "
    "If that role is not even offered on the App registration page, the "
    "developer profile does not carry it: Developer Central > developer "
    "profile > Edit, add 'Amazon Fulfillment' (and 'Product Listing'), "
    "resubmit for evaluation, and wait for the review. Then tick it on the "
    "app, re-authorize, and mint a fresh refresh token - an existing token "
    "keeps the roles it was minted with. "
    "Run `python -m collector.main --diagnose` to confirm."
)


class SpApiDenied(RuntimeError):
    """SP-API answered 403. The token is valid; the app lacks the role."""

    def __init__(self, endpoint: str, body: str = ""):
        self.endpoint = endpoint
        self.body = body
        super().__init__(f"403 denied at {endpoint}. {body}".strip())


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


def _headers(access_token: str) -> dict:
    return {"x-amz-access-token": access_token, "Accept": "application/json"}


def _trim(text: str, limit: int = 300) -> str:
    return (text or "")[:limit].replace("\n", " ").strip()


def _request(sess, method: str, access_token: str, path: str, **kwargs):
    """One SP-API call. A 403 becomes SpApiDenied so callers can fall back."""
    resp = sess.request(
        method,
        f"{SPAPI_HOST}{path}",
        headers=_headers(access_token),
        timeout=net.TIMEOUT,
        **kwargs,
    )
    if resp.status_code == 403:
        raise SpApiDenied(path, _trim(resp.text))
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------
# Route 1: FBA Inventory API
# --------------------------------------------------------------------------

def fetch_fba_summaries(access_token: str) -> list[dict]:
    """Return every FBA inventory summary for the US marketplace, with details."""
    path = "/fba/inventory/v1/summaries"
    base = {
        "details": "true",
        "granularityType": "Marketplace",
        "granularityId": US_MARKETPLACE_ID,
        "marketplaceIds": US_MARKETPLACE_ID,
    }

    s = net.session()
    out: list[dict] = []
    next_token = None
    for _page in range(50):  # generous ceiling; 36 SKUs will finish in 1
        params = dict(base)
        if next_token:
            params["nextToken"] = next_token
        resp = _request(s, "GET", access_token, path, params=params)
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

    return {
        "seller_sku": (summary.get("sellerSku") or "").strip(),
        "asin": (summary.get("asin") or "").strip(),
        "fnsku": (summary.get("fnSku") or "").strip(),
        "fulfillable": _n(d.get("fulfillableQuantity")),
        "inbound_working": _n(d.get("inboundWorkingQuantity")),
        "inbound_shipped": _n(d.get("inboundShippedQuantity")),
        "inbound_receiving": _n(d.get("inboundReceivingQuantity")),
        "reserved_customer_order": _n(reserved.get("pendingCustomerOrderQuantity")),
        "reserved_transshipment": _n(reserved.get("pendingTransshipmentQuantity")),
        "reserved_fc_processing": _n(reserved.get("fcProcessingQuantity")),
        "reserved_total": _n(reserved.get("totalReservedQuantity")),
        "researching": _n(researching.get("totalResearchingQuantity")),
        "unfulfillable": _n(unfulfillable.get("totalUnfulfillableQuantity")),
        "total_quantity": _n(summary.get("totalQuantity")),
    }


def _n(v) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Route 2: Reports API
# --------------------------------------------------------------------------
# Column names below are the documented headers of the two reports. Confirmed
# names lead each list and alternates follow, same pattern as walmart.py, so a
# renamed column degrades to a wrong-but-visible zero rather than a KeyError.

SKU_COLS = ("sku", "seller-sku", "seller_sku", "merchant-sku")
ASIN_COLS = ("asin",)
FNSKU_COLS = ("fnsku", "fulfillment-channel-sku")
FULFILLABLE_COLS = ("afn-fulfillable-quantity", "quantity available", "quantity-available")
WAREHOUSE_COLS = ("afn-warehouse-quantity",)
UNSELLABLE_COLS = ("afn-unsellable-quantity",)
RESERVED_COLS = ("afn-reserved-quantity",)
RESEARCHING_COLS = ("afn-researching-quantity",)
TOTAL_COLS = ("afn-total-quantity",)
INBOUND_WORKING_COLS = ("afn-inbound-working-quantity",)
INBOUND_SHIPPED_COLS = ("afn-inbound-shipped-quantity",)
INBOUND_RECEIVING_COLS = ("afn-inbound-receiving-quantity",)

# GET_AFN_INVENTORY_DATA emits one row per warehouse condition. Only sellable
# units may be counted as fulfillable; everything else is damaged or defective
# stock that Amazon will not ship.
WAREHOUSE_CONDITION_COLS = ("warehouse-condition-code",)
SELLABLE_CONDITIONS = {"SELLABLE", "NEW", ""}


def _pick(row: dict, names: tuple[str, ...]) -> str:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return str(row[n])
    return ""


def _create_report(sess, access_token: str, report_type: str) -> str:
    resp = _request(
        sess, "POST", access_token, f"{REPORTS_PATH}/reports",
        json={"reportType": report_type, "marketplaceIds": [US_MARKETPLACE_ID]},
    )
    report_id = resp.json().get("reportId")
    if not report_id:
        raise RuntimeError(f"createReport returned no reportId for {report_type}")
    return report_id


def _wait_for_report(sess, access_token: str, report_id: str) -> str:
    waited = 0
    while waited <= REPORT_POLL_CEILING:
        body = _request(sess, "GET", access_token,
                        f"{REPORTS_PATH}/reports/{report_id}").json()
        state = body.get("processingStatus")
        if state == "DONE":
            doc_id = body.get("reportDocumentId")
            if not doc_id:
                raise RuntimeError(f"report {report_id} is DONE but carries no document id")
            return doc_id
        if state in ("CANCELLED", "FATAL"):
            # CANCELLED means Amazon had nothing to report. Treat it as a hard
            # failure rather than as zero stock: publishing zeroes because a
            # report was cancelled is exactly the plausible-but-wrong write
            # this pipeline is built to avoid.
            raise RuntimeError(f"report {report_id} finished as {state}")
        time.sleep(REPORT_POLL_SECONDS)
        waited += REPORT_POLL_SECONDS
    raise TimeoutError(
        f"report {report_id} was still {state} after {REPORT_POLL_CEILING}s")


def _download_report(sess, access_token: str, document_id: str) -> str:
    meta = _request(sess, "GET", access_token,
                    f"{REPORTS_PATH}/documents/{document_id}").json()
    url = meta.get("url")
    if not url:
        raise RuntimeError(f"report document {document_id} carries no download url")

    # The document URL is pre-signed. Sending the SP-API token to S3 is both
    # unnecessary and a credential leak, so this request carries no headers.
    raw = sess.get(url, timeout=net.TIMEOUT)
    raw.raise_for_status()
    return _decode_report(raw.content, meta.get("compressionAlgorithm"))


def _decode_report(content: bytes, compression: str | None) -> str:
    # requests transparently gunzips a Content-Encoding: gzip body, so trust
    # the magic bytes over the metadata rather than double-decompressing.
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    elif (compression or "").upper() == "GZIP":
        try:
            content = gzip.decompress(content)
        except OSError:
            pass  # already decompressed in transit
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")


def parse_report(text: str) -> list[dict]:
    """Turn an FBA inventory report into the same shape as parse_summary().

    Both reports can emit several rows for one SKU (one per condition, or per
    warehouse condition code), so rows are summed per seller SKU.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    merged: dict[str, dict] = {}

    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip()
               for k, v in raw.items() if k}
        sku = _pick(row, SKU_COLS).strip()
        if not sku:
            continue

        rec = merged.setdefault(sku, {
            "seller_sku": sku,
            "asin": _pick(row, ASIN_COLS),
            "fnsku": _pick(row, FNSKU_COLS),
            "fulfillable": 0,
            "inbound_working": 0,
            "inbound_shipped": 0,
            "inbound_receiving": 0,
            # The reports do not break reserved down by cause. These stay zero
            # on this route; reserved_total still carries the real figure.
            "reserved_customer_order": 0,
            "reserved_transshipment": 0,
            "reserved_fc_processing": 0,
            "reserved_total": 0,
            "researching": 0,
            "unfulfillable": 0,
            "total_quantity": 0,
        })

        condition = _pick(row, WAREHOUSE_CONDITION_COLS).upper()
        available = _n(_pick(row, FULFILLABLE_COLS))
        if condition and condition not in SELLABLE_CONDITIONS:
            # Damaged, defective, expired: real units, never sellable.
            rec["unfulfillable"] += available
        else:
            rec["fulfillable"] += available

        rec["inbound_working"] += _n(_pick(row, INBOUND_WORKING_COLS))
        rec["inbound_shipped"] += _n(_pick(row, INBOUND_SHIPPED_COLS))
        rec["inbound_receiving"] += _n(_pick(row, INBOUND_RECEIVING_COLS))
        rec["reserved_total"] += _n(_pick(row, RESERVED_COLS))
        rec["researching"] += _n(_pick(row, RESEARCHING_COLS))
        rec["unfulfillable"] += _n(_pick(row, UNSELLABLE_COLS))
        total = _pick(row, TOTAL_COLS) or _pick(row, WAREHOUSE_COLS)
        rec["total_quantity"] += _n(total)

    return list(merged.values())


def fetch_fba_from_report(access_token: str) -> tuple[list[dict], str]:
    """Try each inventory report in turn. Returns (rows, report type used)."""
    sess = net.session()
    denials: list[str] = []
    for report_type in INVENTORY_REPORTS:
        try:
            report_id = _create_report(sess, access_token, report_type)
            print(f"Amazon: requested {report_type} (report {report_id})")
            doc_id = _wait_for_report(sess, access_token, report_id)
            rows = parse_report(_download_report(sess, access_token, doc_id))
        except SpApiDenied as denied:
            denials.append(f"{report_type}: {denied.body or '403'}")
            continue
        if rows:
            return rows, report_type
        denials.append(f"{report_type}: report was empty")
    raise SpApiDenied(
        f"{REPORTS_PATH}/reports",
        "no FBA inventory report was usable - " + "; ".join(denials),
    )


# --------------------------------------------------------------------------
# What main.py calls
# --------------------------------------------------------------------------

def fetch_fba_inventory(access_token: str) -> tuple[list[dict], str]:
    """Parsed FBA inventory plus the name of the route that produced it.

    Raises on total failure. It never returns an empty result quietly: an
    empty Amazon leg published as zeroes would take the storefront to
    out-of-stock across the catalogue.
    """
    try:
        summaries = fetch_fba_summaries(access_token)
        return [parse_summary(s) for s in summaries], "fba-inventory-api"
    except SpApiDenied as denied:
        print(f"Amazon: {denied.endpoint} denied (403). {ROLE_HINT}", file=sys.stderr)
        print("Amazon: falling back to the Reports API.", file=sys.stderr)

    try:
        return fetch_fba_from_report(access_token)
    except SpApiDenied as denied:
        raise SpApiDenied(
            denied.endpoint,
            f"{denied.body} | Both the FBA Inventory API and the Reports API "
            f"are denied to this app. {ROLE_HINT}",
        ) from denied


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
# A 403 says "denied" without saying denied *at which layer*. Probing one
# harmless endpoint per role turns that into a definite answer: whichever
# canaries pass are the roles the token carries, and whichever fail name the
# checkbox that is missing in Develop Apps.

def _canary_probes() -> list[tuple[str, str, str, dict]]:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        # There is no genuinely role-free SP-API endpoint to use as a baseline.
        # Sellers looks like one and is not: it needs Selling Partner Insights,
        # and a 403 here says nothing about whether the app works.
        ("Selling Partner Insights", "Sellers",
         "/sellers/v1/marketplaceParticipations", {}),
        ("Amazon Fulfillment", "FBA Inventory (the one that is failing)",
         "/fba/inventory/v1/summaries",
         {"details": "true", "granularityType": "Marketplace",
          "granularityId": US_MARKETPLACE_ID, "marketplaceIds": US_MARKETPLACE_ID}),
        ("Amazon Fulfillment", "Fulfillment Outbound",
         "/fba/outbound/2020-07-01/fulfillmentOrders", {}),
        ("Inventory and Order Tracking", "Orders",
         "/orders/v0/orders",
         {"MarketplaceIds": US_MARKETPLACE_ID, "CreatedAfter": since}),
        ("Product Listing", "Catalog Items",
         "/catalog/2022-04-01/items",
         {"keywords": "tea", "marketplaceIds": US_MARKETPLACE_ID}),
        ("Reports API", "Reports (the fallback route)",
         f"{REPORTS_PATH}/reports",
         {"reportTypes": INVENTORY_REPORTS[0], "marketplaceIds": US_MARKETPLACE_ID}),
    ]


def diagnose() -> None:
    print("=" * 68)
    print("SP-API DIAGNOSTIC")
    print("=" * 68)

    try:
        token = get_access_token()
        print("1. Token exchange (LWA)  : OK")
        print(f"   access token length   : {len(token)}")
    except Exception as exc:
        print(f"1. Token exchange (LWA)  : FAILED - {net.describe_error(exc)}")
        print("\n   The client id, secret or refresh token is wrong or malformed.")
        return

    sess = net.session()
    print("\n2. Role canaries")
    results: list[tuple[str, str, int | None]] = []
    for role, label, path, params in _canary_probes():
        try:
            r = sess.get(f"{SPAPI_HOST}{path}", headers=_headers(token),
                         params=params, timeout=net.TIMEOUT)
            code: int | None = r.status_code
            note = "" if r.status_code == 200 else f"   {_trim(r.text, 200)}"
        except Exception as exc:
            code, note = None, f"   {net.describe_error(exc)}"
        results.append((role, label, code))
        print(f"   [{role:<28}] {label:<38} HTTP {code}")
        if note:
            print(note)

    by_label = {label: code for _role, label, code in results}
    fba = by_label.get("FBA Inventory (the one that is failing)")
    reports = by_label.get("Reports (the fallback route)")
    granted = sorted({role for role, _label, code in results if code == 200})
    denied = sorted({role for role, _label, code in results if code == 403}
                    - set(granted))

    print("-" * 68)
    # The target endpoint is checked first and on its own. Every other canary
    # is context: a 403 somewhere else while FBA Inventory answers is a role
    # this app happens not to carry, not a problem with the sync.
    if fba == 200:
        print("VERDICT: FBA Inventory works. Run the normal sync.")
        print(f"   roles confirmed present : {', '.join(granted) or 'none'}")
        if denied:
            print(f"   roles denied (harmless) : {', '.join(denied)}")
    elif all(code == 403 for _r, _l, code in results):
        print("VERDICT: every endpoint is denied, so the app is probably not")
        print("authorized for API calls against this seller account at all.")
        print("Re-authorize the app and mint a fresh refresh token; if that")
        print("fails, open a Developer Central support case with this output.")
    elif fba == 403:
        print("VERDICT: the token is valid but this app does not carry the role")
        print("that guards the FBA Inventory API.")
        print(f"   roles confirmed present : {', '.join(granted) or 'none'}")
        print(f"   {ROLE_HINT}")
        if reports == 200:
            print("\n   Good news: the Reports API answers, so the fallback route")
            print("   should work today. Run the normal sync - it will use it")
            print("   automatically and label the feed accordingly.")
        else:
            print("\n   The Reports fallback is denied too, so there is no route to")
            print("   Amazon stock until the role is added.")
    else:
        print("VERDICT: inconclusive - see the status codes above.")
    print("=" * 68)
