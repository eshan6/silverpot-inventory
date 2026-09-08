"""Amazon Ads API: campaign and search-term reporting.

Feeds the dashboard's Ads section and the numerator of its Spend section.

Not SP-API. Different host, different credentials, a required profile header
with no Selling Partner equivalent, and asynchronous reports: request, poll,
download a gzipped file. The polling shape mirrors `amazon.fetch_fba_from_report`
closely enough to be familiar.

**On field names.** The transport below - auth, request, poll, download,
decompress - is well documented and this code follows it. The *columns* inside
a finished report are the part this repository has been burned by before: nine
wrong guesses at a Walmart field name, found only by dumping a live response.
So column handling here is deliberately defensive. `REPORTS` is data rather
than logic, every metric is read through a candidate list, and anything
unmatched is preserved rather than dropped:

    python -m collector.ads_sync --probe

prints a real report's raw first rows. Confirm the names against that before
trusting any number, and lead with confirmed names in the candidate lists.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
from datetime import date

from . import net

ADS_HOST = "https://advertising-api.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

REQUIRED = ("ADS_CLIENT_ID", "ADS_CLIENT_SECRET", "ADS_REFRESH_TOKEN",
            "ADS_PROFILE_ID")

# How long to wait for a report. Amazon builds these asynchronously and a
# search-term report over a wide range is not instant.
POLL_SECONDS = 10
POLL_LIMIT = 60          # ten minutes


def configured() -> bool:
    return all(os.getenv(k) for k in REQUIRED)


def missing() -> list[str]:
    return [k for k in REQUIRED if not os.getenv(k)]


class AdsDenied(RuntimeError):
    """Authorized, but not for this. Usually onboarding step 3 was skipped."""


def get_access_token(sess=None) -> str:
    sess = sess or net.session()
    resp = sess.post(LWA_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": os.environ["ADS_REFRESH_TOKEN"].strip(),
        "client_id": os.environ["ADS_CLIENT_ID"].strip(),
        "client_secret": os.environ["ADS_CLIENT_SECRET"].strip(),
    }, timeout=net.TIMEOUT)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _headers(token: str) -> dict:
    return {
        "Amazon-Advertising-API-ClientId": os.environ["ADS_CLIENT_ID"].strip(),
        "Amazon-Advertising-API-Scope": os.environ["ADS_PROFILE_ID"].strip(),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
    }


# --------------------------------------------------------------------------
# What we ask for
# --------------------------------------------------------------------------
#
# One entry per (ad program, grain). Data, not logic, so correcting a name
# after the first probe is an edit here rather than a change to the fetching.
#
# All three programs are listed although Silverpot runs only Sponsored
# Products today. An inactive program returns an empty report, which costs one
# request and keeps the day Brands or Display is switched on from needing code.

REPORTS: dict[str, dict] = {
    "sp_campaigns": {
        "ad_program": "sponsored_products",
        "grain": "campaign",
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spCampaigns",
        "groupBy": ["campaign"],
        "columns": ["date", "campaignId", "campaignName", "impressions",
                    "clicks", "cost", "purchases7d", "sales7d"],
    },
    "sp_search_terms": {
        "ad_program": "sponsored_products",
        "grain": "search_term",
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spSearchTerm",
        "groupBy": ["searchTerm"],
        "columns": ["date", "campaignId", "campaignName", "searchTerm",
                    "matchType", "impressions", "clicks", "cost",
                    "purchases7d", "sales7d"],
    },
    "sb_campaigns": {
        "ad_program": "sponsored_brands",
        "grain": "campaign",
        "adProduct": "SPONSORED_BRANDS",
        "reportTypeId": "sbCampaigns",
        "groupBy": ["campaign"],
        "columns": ["date", "campaignId", "campaignName", "impressions",
                    "clicks", "cost", "purchases", "sales"],
    },
    "sd_campaigns": {
        "ad_program": "sponsored_display",
        "grain": "campaign",
        "adProduct": "SPONSORED_DISPLAY",
        "reportTypeId": "sdCampaigns",
        "groupBy": ["campaign"],
        "columns": ["date", "campaignId", "campaignName", "impressions",
                    "clicks", "cost", "purchases", "sales"],
    },
}

# Candidate names per metric, confirmed-first. Amazon uses different words for
# the same idea across programs and report versions - cost vs spend, sales7d vs
# sales vs attributedSales - so each is read through a list rather than a
# guess. Unrecognised keys are kept in `extra` rather than silently lost.
FIELDS: dict[str, tuple[str, ...]] = {
    "ad_date": ("date", "reportDate", "day"),
    "campaign_id": ("campaignId", "campaign_id"),
    "campaign_name": ("campaignName", "campaign_name", "campaign"),
    "ad_group": ("adGroupName", "adGroupId", "adGroup"),
    "sku": ("advertisedSku", "sku", "seller_sku"),
    "asin": ("advertisedAsin", "asin", "promotedAsin"),
    "search_term": ("searchTerm", "query", "customerSearchTerm"),
    "match_type": ("matchType", "keywordType", "targetingType"),
    "impressions": ("impressions", "impressionCount"),
    "clicks": ("clicks", "clickCount"),
    "spend": ("cost", "spend", "costInLocalCurrency"),
    "attributed_sales": ("sales7d", "sales", "attributedSales7d",
                         "attributedSales14d", "sales14d"),
    "attributed_units": ("purchases7d", "purchases", "unitsSoldClicks7d",
                         "attributedUnitsOrdered7d", "unitsSoldClicks14d"),
}

_KNOWN = {name for names in FIELDS.values() for name in names}


def _pick(row: dict, names: tuple[str, ...]):
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _num(v, cast=float):
    if v is None or v == "":
        return cast(0)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return cast(0)


def normalize(row: dict, spec: dict, marketplace: str = "amazon") -> dict:
    """One raw report row into the shape ads_daily / ads_search_terms expect.

    Anything Amazon sent that we did not ask about is kept under `extra`, so a
    metric that only one program reports - viewable impressions, video
    quartiles - is preserved without a schema change, and so a renamed field
    shows up in the data rather than vanishing into a zero.
    """
    out = {
        "marketplace": marketplace,
        "ad_program": spec["ad_program"],
        "ad_date": _pick(row, FIELDS["ad_date"]),
        "campaign_id": str(_pick(row, FIELDS["campaign_id"]) or ""),
        "campaign_name": _pick(row, FIELDS["campaign_name"]) or "",
        "impressions": _num(_pick(row, FIELDS["impressions"]), int),
        "clicks": _num(_pick(row, FIELDS["clicks"]), int),
        "spend": round(_num(_pick(row, FIELDS["spend"])), 2),
        "attributed_sales": round(_num(_pick(row, FIELDS["attributed_sales"])), 2),
        "attributed_units": _num(_pick(row, FIELDS["attributed_units"]), int),
    }

    if spec["grain"] == "search_term":
        out["search_term"] = _pick(row, FIELDS["search_term"]) or ""
        out["match_type"] = _pick(row, FIELDS["match_type"]) or ""
    else:
        out["ad_group"] = str(_pick(row, FIELDS["ad_group"]) or "")
        out["sku"] = str(_pick(row, FIELDS["sku"]) or "")
        out["asin"] = _pick(row, FIELDS["asin"])

    extra = {k: v for k, v in row.items() if k not in _KNOWN}
    out["extra"] = extra
    return out


# CPC is not stored. It is spend over clicks, and a stored ratio goes stale the
# moment either side is corrected by a re-ingest.


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def request_report(token: str, spec: dict, start: date, end: date,
                   sess=None) -> str:
    sess = sess or net.session()
    body = {
        "name": f"{spec['reportTypeId']} {start} to {end}",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "configuration": {
            "adProduct": spec["adProduct"],
            "groupBy": spec["groupBy"],
            "columns": spec["columns"],
            "reportTypeId": spec["reportTypeId"],
            "timeUnit": "DAILY",
            "format": "GZIP_JSON",
        },
    }
    resp = sess.post(f"{ADS_HOST}/reporting/reports", headers=_headers(token),
                     json=body, timeout=net.TIMEOUT)
    if resp.status_code in (401, 403):
        raise AdsDenied(
            f"{spec['reportTypeId']}: {resp.status_code} {resp.text[:200]}. "
            "If onboarding step 3 was skipped, API access is approved but not "
            "assigned to this Login with Amazon application yet."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Report request failed: {resp.status_code} "
                           f"{resp.text[:300]}")
    return (resp.json() or {}).get("reportId") or ""


def wait_for_report(token: str, report_id: str, sess=None,
                    pause: float = POLL_SECONDS) -> str:
    """Poll until the report is built. Returns its download URL."""
    sess = sess or net.session()
    for _ in range(POLL_LIMIT):
        resp = sess.get(f"{ADS_HOST}/reporting/reports/{report_id}",
                        headers=_headers(token), timeout=net.TIMEOUT)
        resp.raise_for_status()
        body = resp.json() or {}
        status = (body.get("status") or "").upper()
        if status in ("COMPLETED", "SUCCESS"):
            url = body.get("url") or body.get("location")
            if not url:
                raise RuntimeError(f"Report {report_id} completed with no URL")
            return url
        if status in ("FAILURE", "FAILED", "CANCELLED"):
            # A failed report is not an empty one. Returning zero rows here
            # would publish "no advertising happened", which is a claim.
            raise RuntimeError(f"Report {report_id} failed: "
                               f"{body.get('failureReason') or body}")
        time.sleep(pause)
    raise RuntimeError(f"Report {report_id} did not finish in "
                       f"{POLL_LIMIT * pause:.0f}s")


def download_report(url: str, sess=None) -> list[dict]:
    """Fetch and decode a finished report.

    Sent gzipped, and some responses arrive already decompressed by the HTTP
    layer, so both are handled rather than assumed.
    """
    sess = sess or net.session()
    resp = sess.get(url, timeout=net.TIMEOUT)
    resp.raise_for_status()
    raw = resp.content
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else parsed.get("rows", [])


def fetch(token: str, key: str, start: date, end: date, sess=None,
          pause: float = POLL_SECONDS) -> list[dict]:
    """Request, wait for and normalize one report."""
    spec = REPORTS[key]
    report_id = request_report(token, spec, start, end, sess=sess)
    url = wait_for_report(token, report_id, sess=sess, pause=pause)
    return [normalize(r, spec) for r in download_report(url, sess=sess)]
