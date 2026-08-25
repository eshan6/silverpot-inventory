"""Shared HTTP behaviour for both marketplace clients.

Two problems this solves:

1. **Connect timeouts.** GitHub Actions runners sometimes cannot open a TCP
   connection to api.amazon.com. A single 30-second attempt then a hard crash
   is the worst possible response to a transient network fault, so every
   request retries with exponential backoff on connection-level errors.

2. **IPv6 hangs.** A common cause of "connect timed out" on cloud runners is
   the host resolving to an IPv6 address the runner cannot actually route.
   The TCP SYN goes nowhere and the socket sits until timeout. Setting
   FORCE_IPV4=true makes every outbound connection use IPv4 only, which
   usually clears it instantly.
"""
import os
import socket

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# (connect timeout, read timeout). Connect is deliberately short: if the SYN
# isn't answered in 10s it won't be answered in 30, and we would rather spend
# that budget on another attempt than on waiting.
TIMEOUT = (10, 60)

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def apply_ipv4_preference() -> bool:
    """Force IPv4 for all sockets if FORCE_IPV4 is set. Returns whether applied."""
    if os.getenv("FORCE_IPV4", "false").lower() == "true":
        socket.getaddrinfo = _ipv4_only_getaddrinfo
        return True
    return False


def session(total_retries: int = 4) -> requests.Session:
    """A requests Session that retries connection failures and 5xx responses."""
    s = requests.Session()
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=2,
        status=3,
        backoff_factor=2,           # 0s, 2s, 4s, 8s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def describe_error(exc: Exception) -> str:
    """A short, human-readable cause rather than a wall of traceback."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return ("could not open a connection (network reachability, not credentials). "
                "Try FORCE_IPV4=true, or re-run - this is often transient.")
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "connected but the response never arrived (server slow or hung)."
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS handshake failed."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection error (DNS, routing, or refused)."
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None:
            body = (resp.text or "")[:200].replace("\n", " ")
            return f"HTTP {resp.status_code}: {body}"
    return f"{type(exc).__name__}: {exc}"


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
