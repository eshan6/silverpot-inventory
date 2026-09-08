"""One-off helper to turn Amazon Ads API approval into working credentials.

The Ads API needs three things the console never hands you together: a refresh
token, and the numeric profile ID of the advertising account it may act on.
Getting them by hand means composing an authorization URL correctly, catching a
code out of a redirect, posting it to the right token endpoint within minutes
before it expires, and then calling an endpoint whose host differs by region.
Every one of those is easy to get slightly wrong, and the failure messages are
unhelpful.

So this does all of it, in two commands:

    export ADS_CLIENT_ID=amzn1.application-oa2-client.xxxx
    export ADS_CLIENT_SECRET=xxxx

    python -m collector.ads_auth url
    #  -> open the printed link, approve, and copy the ?code= from the address bar

    python -m collector.ads_auth token --code THE_CODE
    #  -> prints the refresh token and every profile, ready to paste into
    #     GitHub Secrets

The authorization code is single-use and short-lived, so run the second command
promptly. If it expires, run `url` again - nothing is lost.

This is the one place in this repository that deliberately prints a secret.
It is an interactive command you run locally, its output is the whole point,
and it never runs in CI. Do not paste its output into a chat or a ticket.
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlencode

from . import net

# Login with Amazon, same as SP-API uses. The Ads API rides on the same
# identity system, which is exactly why the two get confused: different
# console, different client, same token endpoint.
LWA_AUTH_URL = "https://www.amazon.com/ap/oa"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# North America. Europe and the Far East have their own hosts; Silverpot sells
# US-only, so this is the only one wired up, and a wrong host answers 401 in a
# way that looks like a credential problem.
ADS_HOST = "https://advertising-api.amazon.com"

# Reporting and campaign read both live under this scope. Asking for less
# would mean redoing the consent; asking for more would be claiming access the
# application form did not describe.
SCOPE = "advertising::campaign_management"

REQUIRED = ("ADS_CLIENT_ID", "ADS_CLIENT_SECRET")


def _env() -> tuple[str, str]:
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        raise SystemExit(
            f"Missing {', '.join(missing)}.\n"
            "Both come from the Login with Amazon security profile "
            "(developer.amazon.com > Login with Amazon > your profile > "
            "Show Client ID and Client Secret)."
        )
    return os.environ["ADS_CLIENT_ID"].strip(), os.environ["ADS_CLIENT_SECRET"].strip()


def authorize_url(client_id: str, redirect_uri: str) -> str:
    """The consent link to open in a browser."""
    return f"{LWA_AUTH_URL}?" + urlencode({
        "client_id": client_id,
        "scope": SCOPE,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    })


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect_uri: str, sess=None) -> dict:
    """Trade the one-time authorization code for a lasting refresh token.

    redirect_uri must match the one used to obtain the code exactly, including
    the trailing slash. Amazon compares them as strings, and a mismatch reads
    as an invalid_grant that looks like a bad code.
    """
    sess = sess or net.session()
    resp = sess.post(LWA_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=net.TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Token exchange failed: {resp.status_code} {resp.text[:300]}\n"
            "Common causes: the code was already used, more than a few minutes "
            "passed, or redirect_uri does not match the one in the "
            "authorization link character for character."
        )
    return resp.json()


def access_token(client_id: str, client_secret: str, refresh_token: str,
                 sess=None) -> str:
    """A short-lived access token from the refresh token."""
    sess = sess or net.session()
    resp = sess.post(LWA_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=net.TIMEOUT)
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_profiles(client_id: str, token: str, sess=None) -> list[dict]:
    """Every advertising account this authorization can reach.

    A profile is the Ads API's equivalent of "which advertiser am I acting
    for", and there is no SP-API counterpart, which is why it is the step
    people miss. Every later request carries it as a header.
    """
    sess = sess or net.session()
    resp = sess.get(f"{ADS_HOST}/v2/profiles", headers={
        "Amazon-Advertising-API-ClientId": client_id,
        "Authorization": f"Bearer {token}",
    }, timeout=net.TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Could not list profiles: {resp.status_code} {resp.text[:300]}\n"
            "A 401 here usually means step 3 of onboarding was skipped - API "
            "access has to be assigned to this Login with Amazon application "
            "before it can be used."
        )
    return resp.json() or []


def describe_profile(p: dict) -> str:
    info = p.get("accountInfo") or {}
    return (f"  profileId {p.get('profileId')}   "
            f"{p.get('countryCode', '?'):<3} "
            f"{info.get('type', '?'):<10} "
            f"{info.get('name') or info.get('id') or ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("step", choices=["url", "token", "profiles"])
    ap.add_argument("--code", help="the ?code= value from the redirect")
    ap.add_argument("--refresh-token",
                    help="for `profiles`, if not in ADS_REFRESH_TOKEN")
    ap.add_argument("--redirect-uri", default="https://silverpottea.com/",
                    help="must exactly match an Allowed Return URL on the "
                         "security profile (default: %(default)s)")
    args = ap.parse_args()

    client_id, client_secret = _env()

    if args.step == "url":
        print("\n1. Open this link and approve:\n")
        print(f"   {authorize_url(client_id, args.redirect_uri)}\n")
        print("2. You will land on your own site with ?code=... in the address")
        print("   bar. Copy that value - it is single-use and expires in")
        print("   minutes - then run:\n")
        print("   python -m collector.ads_auth token --code THE_CODE\n")
        return 0

    if args.step == "token":
        if not args.code:
            raise SystemExit("--code is required. Run `url` first.")
        payload = exchange_code(client_id, client_secret, args.code.strip(),
                                args.redirect_uri)
        refresh = payload.get("refresh_token")
        if not refresh:
            raise SystemExit(f"No refresh token in the response: {payload}")

        profiles = fetch_profiles(client_id, payload["access_token"])
        us = [p for p in profiles if p.get("countryCode") == "US"]

        print("\n" + "=" * 68)
        print("SECRET OUTPUT - put these in GitHub Secrets, nowhere else.")
        print("Do not paste this into a chat, an email or a ticket.")
        print("=" * 68 + "\n")
        print(f"ADS_REFRESH_TOKEN={refresh}\n")

        if not profiles:
            print("No advertising profiles came back. That usually means the")
            print("authorizing account cannot administer an ads account.")
            return 1

        print("Profiles this authorization can reach:")
        for p in profiles:
            print(describe_profile(p))
        if len(us) == 1:
            print(f"\nADS_PROFILE_ID={us[0]['profileId']}   (the US profile)")
        else:
            print("\nPick the US profile above and use its profileId as "
                  "ADS_PROFILE_ID.")
        print("\nFour secrets in total: ADS_CLIENT_ID, ADS_CLIENT_SECRET, "
              "ADS_REFRESH_TOKEN, ADS_PROFILE_ID.")
        return 0

    refresh = args.refresh_token or os.getenv("ADS_REFRESH_TOKEN")
    if not refresh:
        raise SystemExit("Pass --refresh-token or set ADS_REFRESH_TOKEN.")
    for p in fetch_profiles(client_id,
                            access_token(client_id, client_secret, refresh)):
        print(describe_profile(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
