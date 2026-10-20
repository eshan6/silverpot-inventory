"""Amazon's own Ads MCP server, asked to describe itself.

    python -m collector.ads_mcp --probe     # list its tools, write nothing

Why this exists. `collector/ads_history.py` has asked `POST /history` for a
filtered window eleven ways across six rounds - three field-name spellings,
six encodings of `eventTypes`, three media types - and every non-empty filter
comes back with the same refusal:

    Key in HistoryEventTypes: 0 is not supported. Needs to be one of
    CAMPAIGN,AD_GROUP,AD,KEYWORD,PRODUCT_TARGETING,NEGATIVE_KEYWORD,
    NEGATIVE_PRODUCT_TARGETING,THEME

An empty filter is accepted and returns an empty page. The `0` tracks nothing
in the request - it is identical for an array, a map and a bare string - so it
is a constant in a message template and says nothing about the shape wanted.
Guessing further is exactly what CLAUDE.md forbids.

Amazon's merged OpenAPI specification does not settle it either: it covers the
`/adsApi/v1/...` surface only and carries no `/history` path and no
`HistoryEventTypes` schema.

So this asks a different way. Amazon runs an MCP server over the same Ads
credentials, and an MCP server publishes a machine-readable schema for every
tool it exposes. If change history is among them, `tools/list` hands over the
exact argument names and the enum - the one thing six probe rounds could not
extract - from Amazon rather than from a guess.

What it found, run 104 on 2026-10-20. Both endpoints answer, with the same
Ads credentials and no extra setup:

    lite  Amazon Ads MCP Server (Tool Search)    3 tools
    full  Amazon Ads MCP Server                110 tools

The 110 cover campaign management, reporting, AMC, billing, account and user
management, Amazon Live and promotions. **None of them is change history.**
There is no `history`, `change`, `audit` or `revision` tool in the set, so the
MCP route does not expose it either and `POST /history` remains the only
surface that does.

The lite endpoint is the interesting half: it is a tool *search* server, and
`--search` asks Amazon's own index by what a tool does rather than reading
110 names and judging by eye.

Reads only. There is no ingestion here and will not be until a schema has
come back.

What is printed, and what is not: tool names, descriptions and argument
schemas are Amazon's documentation and are printed in full. Anything that
could carry Silverpot's own advertising - a tool *result* - is not called at
all. Same posture as `ads_history.probe`, for the same reason: this
repository's Actions logs are public.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import ads, net

# Both surfaces Amazon documents. `lite` is the smaller tool set and needs no
# per-account setup, so it is asked first; the full server is asked after,
# because a change-history tool is more likely to live in the larger set.
MCP_ENDPOINTS = [
    ("lite", "https://advertising-ai.amazon.com/mcp/lite"),
    ("full", "https://advertising-ai.amazon.com/mcp"),
]

# Streamable HTTP: one POST per JSON-RPC message, and the server may answer
# either as JSON or as a single-event SSE stream. Both are accepted here
# because which one arrives is the server's choice, not ours.
ACCEPT = "application/json, text/event-stream"

PROTOCOL_VERSION = "2025-06-18"

# What a change-history tool would be called. Used only to flag likely hits in
# the printed list - every tool is printed either way, so a name not in here
# is not hidden.
HISTORY_WORDS = ("history", "change", "audit", "event", "revision")


def _rpc(sess, url: str, token: str, method: str, params=None,
         session_id: str | None = None, rpc_id=1):
    """One JSON-RPC call. Returns (status, headers, parsed-or-None, raw-text)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Advertising-API-ClientId": os.environ["ADS_CLIENT_ID"].strip(),
        "Amazon-Advertising-API-Scope": os.environ["ADS_PROFILE_ID"].strip(),
        "Content-Type": "application/json",
        "Accept": ACCEPT,
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not method.startswith("notifications/"):
        body["id"] = rpc_id
    resp = sess.post(url, headers=headers, json=body, timeout=net.TIMEOUT)
    return resp.status_code, resp.headers, _parse(resp), (resp.text or "")


def _parse(resp):
    """JSON, or the first JSON-RPC message out of an SSE stream."""
    text = resp.text or ""
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _argument_names(schema) -> str:
    """Argument names and their enums - schema, never account data."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return ""
    required = set(schema.get("required") or [])
    parts = []
    for name in sorted(props):
        spec = props[name] if isinstance(props[name], dict) else {}
        mark = "*" if name in required else ""
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            parts.append(f"{name}{mark}={'|'.join(str(v) for v in enum)}")
        else:
            parts.append(f"{name}{mark}:{spec.get('type', '?')}")
    return ", ".join(parts)


def _list_tools(sess, label: str, url: str, token: str) -> list:
    """Handshake, then tools/list. Prints the evidence either way."""
    print(f"=== {label}: {url}")
    status, headers, parsed, raw = _rpc(sess, url, token, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "silverpot-inventory", "version": "1"},
    })
    print(f"initialize      HTTP {status}")
    if status >= 400 or parsed is None:
        print(f"    {raw[:400]}")
        return []
    if "error" in parsed:
        print(f"    error: {json.dumps(parsed['error'])[:400]}")
        return []
    server = (parsed.get("result") or {}).get("serverInfo") or {}
    if server:
        print(f"    serverInfo: {json.dumps(server)[:200]}")
    session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")

    # The initialized notification is required before any other request, and
    # a server entitled to refuse without it would look like a missing tool
    # set rather than a missing handshake.
    _rpc(sess, url, token, "notifications/initialized",
         params={}, session_id=session_id)

    status, _, parsed, raw = _rpc(sess, url, token, "tools/list",
                                  params={}, session_id=session_id, rpc_id=2)
    print(f"tools/list      HTTP {status}")
    if status >= 400 or parsed is None:
        print(f"    {raw[:400]}")
        return []
    if "error" in parsed:
        print(f"    error: {json.dumps(parsed['error'])[:400]}")
        return []
    tools = (parsed.get("result") or {}).get("tools") or []
    print(f"    {len(tools)} tool(s)\n")
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", "?"))
        flag = " <-- history?" if any(w in name.lower() for w in HISTORY_WORDS) else ""
        print(f"  {name}{flag}")
        desc = " ".join(str(tool.get("description") or "").split())
        if desc:
            print(f"      {desc[:300]}")
        args = _argument_names(tool.get("inputSchema"))
        if args:
            print(f"      args: {args[:600]}")
    print()
    return tools


# The lite endpoint is a tool-*search* server: three tools
# (search_tools, describe_tools, invoke_tool) over the same visible set. Its
# search is Amazon's own index of its own catalogue, so asking it for "change
# history" is a better answer than reading 110 names and judging by eye.
SEARCH_ENDPOINT = MCP_ENDPOINTS[0][1]
SEARCH_TOOL = "amazon_ads_mcp-search_tools"


def search(query: str, sess=None) -> int:
    """Ask Amazon's own tool index for a tool, by what it does.

    This is the only tool *call* in this module, and it is safe for the same
    reason the rest of the probe is not: its result is a catalogue of tool
    names and descriptions - Amazon's documentation - and carries none of
    Silverpot's spend, campaign names or keywords. Do not extend this to any
    tool that returns account data while these logs are public.
    """
    sess = sess or net.session()
    token = ads.get_access_token(sess=sess)
    print(f"Searching Amazon's Ads tool index for: {query!r}\n")

    status, headers, parsed, raw = _rpc(sess, SEARCH_ENDPOINT, token,
                                        "initialize", {
                                            "protocolVersion": PROTOCOL_VERSION,
                                            "capabilities": {},
                                            "clientInfo": {
                                                "name": "silverpot-inventory",
                                                "version": "1",
                                            },
                                        })
    print(f"initialize      HTTP {status}")
    if status >= 400 or parsed is None or "error" in parsed:
        print(f"    {raw[:400]}")
        return 1
    session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    _rpc(sess, SEARCH_ENDPOINT, token, "notifications/initialized",
         params={}, session_id=session_id)

    status, _, parsed, raw = _rpc(
        sess, SEARCH_ENDPOINT, token, "tools/call",
        params={"name": SEARCH_TOOL, "arguments": {"query": query}},
        session_id=session_id, rpc_id=2)
    print(f"tools/call      HTTP {status}")
    if status >= 400 or parsed is None:
        print(f"    {raw[:600]}")
        return 1
    if "error" in parsed:
        print(f"    error: {json.dumps(parsed['error'])[:600]}")
        return 1

    # The result is content blocks, and the useful one is text holding JSON.
    # Printed as it arrives rather than parsed into an assumed shape: this is
    # a probe, and the whole point is to see what Amazon actually returns.
    for block in (parsed.get("result") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            print(str(block.get("text") or "")[:6000])
    return 0


def probe(sess=None) -> int:
    sess = sess or net.session()
    token = ads.get_access_token(sess=sess)
    print("Asking Amazon's Ads MCP server to list its tools. Nothing is "
          "called, so no advertising figures reach these logs.\n")

    found = []
    for label, url in MCP_ENDPOINTS:
        try:
            found += _list_tools(sess, label, url, token)
        except Exception as exc:  # noqa: BLE001 - the error *is* the finding
            print(f"    unreachable: {net.describe_error(exc)}\n")

    if not found:
        print("No tool list came back from either endpoint. The responses "
              "above are the evidence; read them before changing anything.")
        return 1

    hits = [t.get("name") for t in found
            if isinstance(t, dict)
            and any(w in str(t.get("name", "")).lower() for w in HISTORY_WORDS)]
    if hits:
        print(f"Candidate change-history tools: {', '.join(str(h) for h in hits)}")
        print("Their `args` above are the argument names to map ingestion "
              "onto - Amazon's own schema, not a guess.")
    else:
        print("No tool here looks like change history. That is an answer "
              "too: the MCP route does not expose it, and POST /history "
              "stays the only surface that does.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="list the MCP server's tools and print nothing else")
    ap.add_argument("--search", metavar="QUERY",
                    help="ask Amazon's own tool index which tool does QUERY")
    args = ap.parse_args()

    if not ads.configured():
        print(f"Amazon Ads not configured: {', '.join(ads.missing())}",
              file=sys.stderr)
        return 1

    if net.apply_ipv4_preference():
        print("Network: forcing IPv4")

    if args.search:
        return search(args.search)

    if args.probe:
        return probe()

    print("Nothing to do but --probe or --search: this module reads a schema "
          "and has no ingestion. See the module docstring.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
