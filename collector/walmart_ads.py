"""Finding a route to Walmart advertising data. A diagnostic, never a sync.

The first pass at this concluded there was no route, on the strength of five
probes and a documentation page. That was too little looking. Two things
turned up on a second pass and both are free:

1. **Walmart's advertising API is documented under the seller portal**, not
   only the partner one - `us-marketplace/docs/sem-apis`. SEM is Walmart
   Performance Ads, which is the same service as the WPA endpoints below.
2. The gateway wants a consumer id and a signature. Walmart used to let a
   seller mint those - and no longer does. Confirmed on this account on
   2026-09-09: the developer portal's API Keys page offers ClientId and
   ClientSecret and nothing else, `seller.walmart.com/settings/api/
   consumer-id-and-private-keys` now redirects to a generic page, and the
   portal states that new Delegated Access keys are no longer issued at all.
   So the credential that documentation says to generate cannot be generated.
   What remains untested is whether `WM_CONSUMER.ID` simply means the
   ClientId, which several Walmart services treat it as - and that costs
   nothing to ask.

So this module sweeps every candidate route and reports what each one says,
under whichever credentials are present. It never writes, never ingests and
never creates: every probe is a GET or a report-listing read, so nothing here
can start a campaign or spend a dollar.

    python -m collector.walmart_ads --diagnose   # the focused question
    python -m collector.walmart_ads --explore    # the whole sweep, verbosely

Five auth modes appear in the output because the answer depends on which one
a given host accepts:

    oauth      the Marketplace access token this repository already holds
    oauth+cid  that token plus WM_CONSUMER.ID carrying the ClientId
    cid        WM_CONSUMER.ID alone, no token, to see which one it wants
    signed     Consumer ID + RSA signature (walmart_sign), if ever configured
    none       no credentials at all, to see how the host refuses a stranger

Nothing prints a token, a key, a signature or a consumer id. Bodies from
Walmart are printed because they are Walmart's own error text and they are
where the useful detail lives; this repository's Actions logs are public, so
nothing of ours joins them.
"""
import os
import sys

from . import net, walmart, walmart_sign
from .config import WALMART_HOST

# Walmart Connect / Walmart Performance Ads. An api-proxy host rather than the
# marketplace one, which is the first hint that it is a different front door
# with different authentication.
WPA_HOST = "https://developer.api.walmart.com/api-proxy/service/WPA/Api/v1"

OAUTH, SIGNED, NONE = "oauth", "signed", "none"
# WM_CONSUMER.ID carrying the ClientId, with and without the OAuth token.
OAUTH_CID, CID = "oauth+cid", "cid"

CONTROL = "Control: WFS inventory"
SIGNED_CONTROL = "Control: WFS inventory, signed"

# Every candidate worth asking, with the credential each is asked under.
#
# The order matters to a reader, not to the code: the free routes that need no
# new credential come first, because if one of those answers then nobody has
# to generate anything. Ordered within that by how likely the documentation
# makes them.
PROBES: tuple[tuple[str, str, str, str], ...] = (
    # --- Needs nothing new: the token this repository already holds ---
    (CONTROL, "GET", f"{WALMART_HOST}/v3/wfs/inventory", OAUTH),
    ("Marketplace: report requests", "GET",
     f"{WALMART_HOST}/v3/reports/reportRequests", OAUTH),
    # A deliberately impossible report type. Walmart's validators tend to
    # answer an unknown enum by listing the ones it would have accepted, which
    # enumerates the catalogue for free and without a guess. If an advertising
    # report is in there, this whole question is answered on credentials the
    # repository already holds.
    ("Marketplace: report type list", "GET",
     f"{WALMART_HOST}/v3/reports/reportRequests"
     "?reportType=NOT_A_REAL_REPORT_TYPE&reportVersion=v1", OAUTH),
    ("Marketplace: advertising report", "GET",
     f"{WALMART_HOST}/v3/reports/reportRequests"
     "?reportType=ADVERTISING&reportVersion=v1", OAUTH),
    ("Marketplace: item performance", "GET",
     f"{WALMART_HOST}/v3/reports/reportRequests"
     "?reportType=ITEM_PERFORMANCE&reportVersion=v1", OAUTH),
    ("Marketplace: insights item perf", "GET",
     f"{WALMART_HOST}/v3/insights/items/performance", OAUTH),
    ("Marketplace: SEM campaigns", "GET", f"{WALMART_HOST}/v3/sem/campaigns", OAUTH),
    ("Marketplace: SEM report", "GET", f"{WALMART_HOST}/v3/sem/report", OAUTH),
    ("Marketplace: advertising", "GET", f"{WALMART_HOST}/v3/advertising", OAUTH),
    ("Marketplace: advertiser", "GET",
     f"{WALMART_HOST}/v3/advertising/advertiser", OAUTH),
    ("Marketplace: ads", "GET", f"{WALMART_HOST}/v3/ads", OAUTH),

    # --- The Growth scope, because the name is worth a live answer ---
    # This account's key carries fourteen scopes and "Growth" is the only one
    # whose name does not obviously exclude advertising. Walmart's docs put
    # Listing Quality and Assortment under it, but the docs have been wrong
    # about this account three times, so the account gets asked directly.
    ("Marketplace: growth", "GET", f"{WALMART_HOST}/v3/growth", OAUTH),
    ("Marketplace: growth opportunities", "GET",
     f"{WALMART_HOST}/v3/growth/opportunities", OAUTH),
    ("Marketplace: growth ads", "GET", f"{WALMART_HOST}/v3/growth/ads", OAUTH),
    ("Marketplace: growth advertising", "GET",
     f"{WALMART_HOST}/v3/growth/advertising", OAUTH),
    ("Marketplace: insights", "GET", f"{WALMART_HOST}/v3/insights", OAUTH),

    # --- The advertising gateway, under the OAuth token it already refused ---
    ("WPA advertiser (oauth)", "GET", f"{WPA_HOST}/advertiser", OAUTH),

    # --- The ClientId as WM_CONSUMER.ID: free, and needs no new credential ---
    # The developer portal issues ClientId and ClientSecret and nothing else,
    # so if the advertising gateway wants a consumer id, this is the only one
    # that exists for this account. Tried with the OAuth token and without,
    # because which of the two it wants is exactly what is unknown.
    ("WPA advertiser (oauth+cid)", "GET", f"{WPA_HOST}/advertiser", OAUTH_CID),
    ("WPA campaign (oauth+cid)", "GET", f"{WPA_HOST}/campaign", OAUTH_CID),
    ("WPA snapshot report (oauth+cid)", "GET",
     f"{WPA_HOST}/snapshot/report", OAUTH_CID),
    ("WPA advertiser (cid only)", "GET", f"{WPA_HOST}/advertiser", CID),

    # --- Is our signature itself any good? ---
    # The same endpoint as the OAuth control, signed instead. Walmart's legacy
    # scheme is accepted on the marketplace host, so this separates two
    # failures that otherwise look identical: a signature this repository
    # builds wrongly, and an advertising gateway that refuses sellers. Without
    # it, one failed signed probe against WPA means either and we cannot tell
    # which - and telling someone to go and apply for access when the bug is
    # ours is the exact mistake this module has already made twice.
    (SIGNED_CONTROL, "GET", f"{WALMART_HOST}/v3/wfs/inventory", SIGNED),

    # --- The advertising gateway, signed. The route this is really testing ---
    ("WPA advertiser (signed)", "GET", f"{WPA_HOST}/advertiser", SIGNED),
    ("WPA campaign (signed)", "GET", f"{WPA_HOST}/campaign", SIGNED),
    ("WPA snapshot report (signed)", "GET", f"{WPA_HOST}/snapshot/report", SIGNED),

    # --- Signed against the marketplace host, in case SEM lives there ---
    ("Marketplace SEM (signed)", "GET", f"{WALMART_HOST}/v3/sem/campaigns", SIGNED),

    # --- The stranger's view: how does the gateway refuse no credentials at
    #     all? If that is byte-identical to the OAuth refusal, then the OAuth
    #     token was never being read, which is itself the finding.
    ("WPA advertiser (no auth)", "GET", f"{WPA_HOST}/advertiser", NONE),
)

PARTNER_HINT = (
    "Walmart Connect's Ads APIs are documented as available to Walmart Connect\n"
    "   Partner Network partners. An advertiser can also authorise a partner in\n"
    "   Ad Center (Admin -> API Partner - Advertiser Level -> Add Partner)."
)

# Superseded on 2026-09-09 by the account itself. The developer portal's API
# Keys page offers ClientId and ClientSecret and nothing else, and says new
# Delegated Access keys for Solution Providers are no longer issued at all.
# So there is no Consumer ID and Private Key to generate: Walmart has retired
# that scheme here. The hint stands only for an account that still has it.
SELLER_KEY_HINT = (
    "If this account still offers it: Seller Center -> Settings -> API ->\n"
    "   Consumer IDs & Private Keys -> Generate Key, then set\n"
    "   WALMART_CONSUMER_ID and WALMART_PRIVATE_KEY. Silverpot's account does\n"
    "   NOT offer it - the portal issues ClientId and ClientSecret only - so\n"
    "   the oauth+cid probes above are the ones that matter here."
)

# Confirmed live on 2026-09-09: the WPA endpoints answer 403 with this to an
# OAuth token. It is *not* "you may not advertise" - it is the gateway
# refusing the request's shape before anything looks at what we may do.
GATEWAY_REJECTION = "missing required security headers"


def _trim(text: str, limit: int = 400) -> str:
    body = " ".join((text or "").split())
    return body[:limit] + ("..." if len(body) > limit else "")


def _auth_headers(mode: str, token: str | None, private_key=None) -> dict | None:
    """Headers for one probe, or None if that mode is not available."""
    if mode == NONE:
        return {"Accept": "application/json"}
    if mode == OAUTH:
        return walmart._headers(token) if token else None
    if mode == SIGNED:
        if not walmart_sign.configured():
            return None
        return walmart_sign.headers(private_key=private_key)
    if mode in (OAUTH_CID, CID):
        # WM_CONSUMER.ID is not always a separate credential. On several
        # Walmart services it is simply the ClientId from the developer
        # portal, and the portal no longer issues anything else - confirmed
        # on 2026-09-09, where the account's API Keys page offers ClientId and
        # ClientSecret and nothing more. So the "missing required security
        # headers" 403 may mean nothing more than that we never sent the
        # ClientId as a header, which costs nothing to find out.
        client_id = os.getenv("WALMART_CLIENT_ID")
        if not client_id:
            return None
        head = walmart._headers(token) if mode == OAUTH_CID else {
            "Accept": "application/json", "WM_SVC.NAME": "Walmart Marketplace"}
        if mode == OAUTH_CID and not token:
            return None
        head["WM_CONSUMER.ID"] = client_id
        return head
    raise ValueError(mode)


def probe(token: str | None, sess=None, private_key=None,
          probes=PROBES) -> list[dict]:
    """Ask every candidate. Returns one record per probe, never raising."""
    sess = sess or net.session()
    out: list[dict] = []
    for label, method, url, mode in probes:
        record = {"label": label, "mode": mode, "url": url,
                  "status": None, "note": ""}
        try:
            head = _auth_headers(mode, token, private_key)
        except Exception as exc:                            # noqa: BLE001
            record["note"] = f"could not build {mode} headers - {type(exc).__name__}"
            out.append(record)
            continue
        if head is None:
            record["note"] = f"skipped: no {mode} credentials configured"
            record["skipped"] = True
            out.append(record)
            continue
        try:
            resp = sess.request(
                method, url, headers=head,
                params={"limit": 1} if url.endswith("inventory") else None,
                timeout=net.TIMEOUT)
            record["status"] = resp.status_code
            record["note"] = "" if resp.status_code == 200 else _trim(resp.text)
        except Exception as exc:                            # noqa: BLE001
            record["note"] = net.describe_error(exc)
        out.append(record)
    return out


def is_advertising(record: dict) -> bool:
    """Is this probe actually asking about advertising?

    Derived from the URL rather than a hand-kept list, so a probe added later
    is classified by where it points instead of by whether someone remembered
    to register it.
    """
    url = (record.get("url") or "").lower()
    if url.startswith(WPA_HOST.lower()):
        return True
    return any(mark in url for mark in ("/sem", "advertis", "/ads"))


def verdict(results: list[dict]) -> list[str]:
    """Read the sweep. The control is judged first and on its own.

    An earlier version of this called a gateway rejection "advertising is
    refused", which was a false conclusion drawn from a real response. The
    rule that prevents a repeat: never report on access until you have shown
    the run itself worked, and never report a header complaint as a
    permission answer.
    """
    by = {r["label"]: r for r in results}
    control = by.get(CONTROL, {}).get("status")
    signed_control = by.get(SIGNED_CONTROL, {})
    controls = {CONTROL, SIGNED_CONTROL}
    # Only advertising probes can answer the advertising question. The sweep
    # also asks endpoints that are merely nearby - the reports API, item
    # performance - and on 2026-09-09 two of those answered 200 and this
    # function announced "there is a route". They are marketplace endpoints
    # carrying no ad spend, and reporting them as a route to advertising was
    # exactly the sort of cheerful wrong answer the rest of this module is
    # built to avoid.
    ads = [r for r in results
           if r["label"] not in controls and not r.get("skipped")
           and is_advertising(r)]
    skipped = [r for r in results if r.get("skipped")]

    if control != 200:
        return [
            "VERDICT: inconclusive. The control endpoint did not answer either,",
            f"so this run proves nothing about advertising (control: HTTP {control}).",
            "Check the credentials and the network, then run it again.",
        ]

    # The signature is judged before anything it was used for. A signed probe
    # failing means nothing about advertising until we know Walmart accepts
    # our signature somewhere, and the endpoint we already reach is where to
    # find that out.
    if not signed_control.get("skipped") and signed_control.get("status") not in (
            None, 200):
        return [
            "VERDICT: our signature is not valid, so every signed result below",
            f"is meaningless (signed control: HTTP {signed_control.get('status')}).",
            "",
            "That control is an endpoint we reach fine with the OAuth token, so",
            "the endpoint is not the problem and neither is advertising access.",
            "The fault is in this repository or in the key: canonical string,",
            "key version, a clock far out of step, or a key pasted incomplete.",
            "",
            f"   {_trim(signed_control.get('note') or '', 200)}",
            "",
            "Fix that first. Nothing about Walmart advertising has been tested",
            "yet, and no approval would change any of this.",
        ]

    reachable = sorted(r["label"] for r in ads if r["status"] == 200)
    if reachable:
        return [
            "VERDICT: there is a route. These answered 200:",
            f"   {', '.join(reachable)}",
            "",
            "Next step is a shape probe, not an ingestion module. Nine wrong",
            "guesses at a Walmart field name is why nothing gets written off a",
            "field name that has not been seen in a real response.",
        ]

    lines = ["VERDICT: no candidate answered 200. What each refusal means:"]
    gateway = [r for r in ads if GATEWAY_REJECTION in (r["note"] or "").lower()]
    denied = [r for r in ads
              if r["status"] in (401, 403) and r not in gateway]
    absent = [r for r in ads if r["status"] == 404]

    if gateway:
        modes = sorted({r["mode"] for r in gateway})
        lines += [
            f"   gateway refused the request's shape ({len(gateway)}, auth: "
            f"{', '.join(modes)}) - a complaint about headers, not access",
        ]
        if SIGNED in modes:
            lines += [
                "   The signed attempt was ALSO refused for its headers, so the",
                "   signature itself is being rejected: wrong canonical string,",
                "   wrong key version, or a clock skew. That is a bug to fix here,",
                "   not an approval to go and ask for.",
            ]
        elif any(r.get("skipped") for r in skipped):
            lines += ["", f"   {SELLER_KEY_HINT}"]
    if denied:
        lines += [
            f"   genuinely refused ({len(denied)}): "
            f"{', '.join(sorted(r['label'] for r in denied))}",
            f"   {PARTNER_HINT}",
        ]
    if absent:
        lines += [f"   not a real path ({len(absent)}) - says nothing either way"]
    if skipped:
        lines += ["",
                  f"   {len(skipped)} probe(s) skipped for want of credentials:",
                  f"   {', '.join(sorted(r['label'] for r in skipped))}"]
    return lines


def report(results: list[dict], verbose: bool) -> None:
    for r in results:
        if r.get("skipped"):
            print(f"   [{r['label']:<32}] {r['mode']:<6}  -- {r['note']}")
            continue
        print(f"   [{r['label']:<32}] {r['mode']:<6}  HTTP {r['status']}")
        if r["note"]:
            print(f"      {r['note']}")
        if verbose:
            print(f"      {r['url']}")


def diagnose(verbose: bool = False) -> int:
    print("=" * 72)
    print("WALMART ADVERTISING: IS THERE A ROUTE?")
    print("=" * 72)
    print("Reads only. Writes nothing, spends nothing, creates nothing.")

    if not walmart.configured():
        print(f"\nMissing: {', '.join(walmart.missing())}")
        return 1

    token: str | None
    try:
        token = walmart.get_access_token()
        print("\n1. Marketplace OAuth token : OK "
              f"({len(token)} chars)")
    except Exception as exc:                                # noqa: BLE001
        print(f"\n1. Marketplace OAuth token : FAILED - {net.describe_error(exc)}")
        return 1

    print(f"2. Signed credentials      : {walmart_sign.describe()}")

    private_key = None
    if walmart_sign.configured():
        try:
            private_key = walmart_sign.load_private_key(
                os.getenv(walmart_sign.PRIVATE_KEY_ENV, ""))
        except Exception as exc:                            # noqa: BLE001
            print(f"   private key will not load - {type(exc).__name__}: {exc}")

    print("\n3. Candidates")
    results = probe(token, private_key=private_key)
    report(results, verbose)

    print("-" * 72)
    for line in verdict(results):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    known = {"--diagnose", "--explore"}
    if not args or not set(args) <= known:
        print(__doc__)
        return 2
    net.apply_ipv4_preference()
    return diagnose(verbose="--explore" in args)


if __name__ == "__main__":
    raise SystemExit(main())
