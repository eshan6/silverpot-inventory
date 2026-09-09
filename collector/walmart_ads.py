"""Does anything we already hold reach Walmart Connect? A diagnostic, not a sync.

Walmart advertising is not the Walmart Marketplace API under a different path.
Walmart Connect is a separate product with its own developer portal, and its
documentation says the Ads APIs are available to Walmart Connect Partner
Network partners - agencies and tech platforms - rather than to any seller who
happens to advertise. An advertiser's own route is the Ad Center admin page,
where they authorise a partner; there is no self-serve "generate advertising
credentials" button of the kind Amazon has.

That is a claim from documentation, and this repository has been wrong from
documentation before: Walmart's inventory docs named no field at all and cost
nine wrong guesses, and its orders endpoint hides every WFS order behind a
shipNodeType the docs do not warn about. So before anyone applies for anything,
this asks the live account and reports the status codes.

It writes nothing, ingests nothing and creates nothing. It answers one
question: do the Marketplace client id and secret already in this repository
reach any Walmart Connect endpoint, and if not, how are they refused? A 401 or
403 everywhere means the documentation is right and access needs an approval.
A 200 anywhere means there is a route worth building on, and this prints which.

    python -m collector.walmart_ads --diagnose

Nothing here prints a token or a secret - only the length of the access token,
which is how the SP-API diagnostic does it, and the repository's logs are
public.
"""
import sys

from . import net, walmart
from .config import WALMART_HOST

# Walmart Connect's own documentation gives this as the production base for the
# Sponsored Search APIs. It is an api-proxy path rather than the marketplace
# host, which is itself a hint that it is a different service with a different
# front door.
WPA_HOST = "https://developer.api.walmart.com/api-proxy/service/WPA/Api/v1"

# Candidates, in the pattern walmart.py already uses for field names: the
# documented one leads and the rest are alternates, because the documentation
# has been wrong about this account before. Each is a read - nothing here can
# create a campaign or spend money.
PROBES: tuple[tuple[str, str, str], ...] = (
    ("Sponsored Search advertisers", "GET", f"{WPA_HOST}/advertiser"),
    ("Sponsored Search campaigns", "GET", f"{WPA_HOST}/campaign"),
    ("Sponsored Search snapshot report", "GET", f"{WPA_HOST}/snapshot/report"),
    ("Marketplace host: advertiser", "GET", f"{WALMART_HOST}/v3/advertising/advertiser"),
    ("Marketplace host: ads", "GET", f"{WALMART_HOST}/v3/ads"),
    # The control. This one is known to work, so a run where every line is a
    # failure - including this one - is a broken token or a broken network,
    # not a verdict about advertising access.
    ("Control: WFS inventory (known good)", "GET", f"{WALMART_HOST}/v3/wfs/inventory"),
)

CONTROL = "Control: WFS inventory (known good)"

PARTNER_HINT = (
    "Walmart Connect's Ads APIs are documented as available to Walmart Connect\n"
    "   Partner Network partners, not to advertisers directly. An advertiser\n"
    "   authorises a partner in Ad Center (Admin -> API Partner - Advertiser\n"
    "   Level -> Add Partner); they do not mint their own credentials."
)

# Confirmed against the live account on 2026-09-09. The WPA endpoints answer
# 403 with this, which is *not* "you may not advertise" - it is the gateway
# refusing the request before it ever looks at what we are allowed to do. The
# distinction matters: one is a verdict about access, the other is a verdict
# about our headers, and reading the second as the first is how you conclude
# something false from a real response.
GATEWAY_REJECTION = "missing required security headers"


def _trim(text: str, limit: int = 160) -> str:
    body = " ".join((text or "").split())
    return body[:limit] + ("..." if len(body) > limit else "")


def probe(token: str, sess=None) -> list[tuple[str, int | None, str]]:
    """Ask each candidate endpoint and return (label, status, note) per probe."""
    sess = sess or net.session()
    out: list[tuple[str, int | None, str]] = []
    for label, method, url in PROBES:
        try:
            resp = sess.request(
                method, url,
                headers=walmart._headers(token),
                params={"limit": 1} if url.endswith("inventory") else None,
                timeout=net.TIMEOUT,
            )
            note = "" if resp.status_code == 200 else _trim(resp.text)
            out.append((label, resp.status_code, note))
        except Exception as exc:                        # noqa: BLE001
            out.append((label, None, net.describe_error(exc)))
    return out


def verdict(results: list[tuple[str, int | None, str]]) -> list[str]:
    """Read the status codes. The control is judged first and on its own.

    The SP-API diagnostic once keyed its verdict off a canary it had
    misunderstood and announced the app was dead while the endpoint that
    mattered answered 200. The lesson generalises: decide what the run proves
    only after checking that the run itself worked.
    """
    by_label = {label: code for label, code, _note in results}
    control = by_label.get(CONTROL)
    ads = {label: code for label, code, _note in results if label != CONTROL}
    notes = {label: (note or "").lower() for label, _code, note in results
             if label != CONTROL}

    if control != 200:
        return [
            "VERDICT: inconclusive. The control endpoint did not answer either,",
            f"so this run proves nothing about advertising access (control: HTTP {control}).",
            "Check the credentials and the network, then run it again.",
        ]

    reachable = sorted(l for l, code in ads.items() if code == 200)
    if reachable:
        return [
            "VERDICT: the Marketplace credentials reach Walmart Connect.",
            f"   answering: {', '.join(reachable)}",
            "Probe one of those for its response shape before trusting a figure",
            "from it. No advertising number gets written off a guessed field.",
        ]

    gateway = sorted(l for l, code in ads.items()
                     if code in (401, 403) and GATEWAY_REJECTION in notes.get(l, ""))
    if gateway:
        return [
            "VERDICT: rejected at the gateway, before any question of access.",
            f"   turned away for its headers: {', '.join(gateway)}",
            "Walmart Connect does not take the Marketplace OAuth token at all. It",
            "wants the older signed scheme - a consumer id and an RSA signature -",
            "and those are issued to a partner, not generated from Seller Center.",
            "",
            "So this run does NOT prove we are denied advertising. It proves the",
            "credentials in this repository are the wrong kind of credential, which",
            "is what you would expect if the documentation below is right.",
            f"   {PARTNER_HINT}",
        ]

    denied = sorted(l for l, code in ads.items() if code in (401, 403))
    if denied:
        return [
            "VERDICT: the credentials are good and advertising is refused.",
            f"   refused with 401/403: {', '.join(denied)}",
            f"   {PARTNER_HINT}",
        ]

    return [
        "VERDICT: no advertising endpoint answered and none refused us either -",
        "every candidate path was absent (404) or errored. That is a wrong path,",
        "not a denied one, so it says nothing about whether access exists.",
        f"   {PARTNER_HINT}",
    ]


def diagnose() -> int:
    print("=" * 68)
    print("WALMART CONNECT DIAGNOSTIC")
    print("=" * 68)
    print("Reads only. Writes nothing, spends nothing, creates nothing.")

    if not walmart.configured():
        print(f"\nMissing: {', '.join(walmart.missing())}")
        return 1

    try:
        token = walmart.get_access_token()
        print("\n1. Token exchange (Marketplace) : OK")
        print(f"   access token length          : {len(token)}")
    except Exception as exc:                            # noqa: BLE001
        print(f"\n1. Token exchange (Marketplace) : FAILED - {net.describe_error(exc)}")
        return 1

    print("\n2. Candidate endpoints")
    results = probe(token)
    for label, code, note in results:
        print(f"   [{label:<36}] HTTP {code}")
        if note:
            print(f"      {note}")

    print("-" * 68)
    for line in verdict(results):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args != ["--diagnose"]:
        print(__doc__)
        return 2
    net.apply_ipv4_preference()
    return diagnose()


if __name__ == "__main__":
    raise SystemExit(main())
