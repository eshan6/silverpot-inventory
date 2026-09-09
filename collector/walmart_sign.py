"""Walmart's legacy signed-header authentication.

The Marketplace APIs this repository already uses take an OAuth token. Walmart
Connect's advertising gateway does not - it answers `403 Request is missing
required security headers` to a perfectly good OAuth token, because it still
speaks Walmart's older scheme: a Consumer ID plus an RSA signature, sent as
headers on every single call.

The important discovery is that a *seller* can mint that credential without
anyone's approval. Seller Center -> Settings -> API -> Consumer IDs & Private
Keys -> Generate Key. It is free and it is self-serve, which is exactly what
the partner-network reading of Walmart's documentation suggested was
impossible.

**The canonical string.** Sign this, and nothing else:

    <consumer id>\\n<timestamp in epoch millis>\\n<key version>\\n

That is the sorted-header rule the documentation states the long way round.
The three signed headers are WM_CONSUMER.ID, WM_CONSUMER.INTIMESTAMP and
WM_SEC.KEY_VERSION; sort those names alphabetically and INTIMESTAMP falls
between ID and KEY_VERSION, so concatenating each value with a trailing
newline in key order produces exactly the string above. Two independent
sources describe it both ways and they agree.

RSA PKCS#1 v1.5 over SHA-256, base64 encoded, into WM_SEC.AUTH_SIGNATURE.

Every call needs a fresh signature. The timestamp is inside the signed string,
so a signature cannot be cached and reused - Walmart rejects a stale one.

Nothing here logs a key, a signature or a consumer id. `describe()` exists so
a diagnostic can say what it is holding without printing any of it.
"""
import base64
import os
import time
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CONSUMER_ID_ENV = "WALMART_CONSUMER_ID"
PRIVATE_KEY_ENV = "WALMART_PRIVATE_KEY"
KEY_VERSION_ENV = "WALMART_KEY_VERSION"

DEFAULT_KEY_VERSION = "1"


def configured() -> bool:
    return bool(os.getenv(CONSUMER_ID_ENV) and os.getenv(PRIVATE_KEY_ENV))


def missing() -> list[str]:
    return [k for k in (CONSUMER_ID_ENV, PRIVATE_KEY_ENV) if not os.getenv(k)]


def load_private_key(raw: str):
    """Accept what Seller Center actually hands you, in any of its shapes.

    The portal gives a bare base64 blob - PKCS#8 DER with no PEM armour and
    often with the line breaks preserved from the web page. People also paste
    a full PEM block, because that is what a private key usually looks like.
    Guessing one shape and failing on the other would read as "the key is
    wrong" when the key is fine, so try each and let the last error stand.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{PRIVATE_KEY_ENV} is empty")

    if "-----BEGIN" in text:
        return serialization.load_pem_private_key(text.encode(), password=None)

    # A bare blob: strip whitespace the clipboard added, then try DER, then
    # PEM with armour we add ourselves.
    compact = "".join(text.split())
    try:
        return serialization.load_der_private_key(
            base64.b64decode(compact, validate=True), password=None)
    except Exception:
        pass

    wrapped = "\n".join(compact[i:i + 64] for i in range(0, len(compact), 64))
    pem = f"-----BEGIN PRIVATE KEY-----\n{wrapped}\n-----END PRIVATE KEY-----\n"
    return serialization.load_pem_private_key(pem.encode(), password=None)


def canonical_string(consumer_id: str, timestamp_ms: int, key_version: str) -> str:
    """The exact bytes Walmart expects to have been signed.

    Kept as its own function because it is the single thing most likely to be
    wrong, and a wrong canonical string fails as an authentication error that
    looks identical to a bad key. A test pins the shape.
    """
    return f"{consumer_id}\n{timestamp_ms}\n{key_version}\n"


def sign(private_key, message: str) -> str:
    return base64.b64encode(
        private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()


def headers(
    private_key=None,
    consumer_id: str | None = None,
    key_version: str | None = None,
    timestamp_ms: int | None = None,
    service_name: str = "Walmart Marketplace",
) -> dict:
    """Signed headers for one request. Call it again for the next one."""
    consumer_id = consumer_id or os.getenv(CONSUMER_ID_ENV, "")
    key_version = key_version or os.getenv(KEY_VERSION_ENV) or DEFAULT_KEY_VERSION
    if private_key is None:
        private_key = load_private_key(os.getenv(PRIVATE_KEY_ENV, ""))
    stamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

    return {
        "WM_CONSUMER.ID": consumer_id,
        "WM_CONSUMER.INTIMESTAMP": str(stamp),
        "WM_SEC.KEY_VERSION": key_version,
        "WM_SEC.AUTH_SIGNATURE": sign(
            private_key, canonical_string(consumer_id, stamp, key_version)),
        "WM_SVC.NAME": service_name,
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "Accept": "application/json",
    }


def describe() -> str:
    """What we are holding, in terms safe for a public Actions log."""
    if not configured():
        return f"not configured (missing {', '.join(missing())})"
    consumer = os.getenv(CONSUMER_ID_ENV, "")
    raw = os.getenv(PRIVATE_KEY_ENV, "")
    try:
        key = load_private_key(raw)
        shape = (f"RSA {key.key_size}-bit" if isinstance(key, rsa.RSAPrivateKey)
                 else type(key).__name__)
    except Exception as exc:                                # noqa: BLE001
        shape = f"UNREADABLE - {type(exc).__name__}"
    return (f"consumer id {len(consumer)} chars, private key "
            f"{len(''.join(raw.split()))} chars, {shape}")
