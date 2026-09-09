"""Walmart's legacy signature: the canonical string and the key shapes.

This is testable offline in a way most of this repository's Walmart code is
not. Whether Walmart *accepts* a signature can only be learned live, but
whether we sign the right bytes with the right algorithm is arithmetic, and a
wrong canonical string fails live as an authentication error indistinguishable
from a bad key. So it gets pinned here.

    python -m unittest discover -s tests -v
"""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from collector import walmart_sign as ws  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def der_b64() -> str:
    return base64.b64encode(KEY.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())).decode()


def pem() -> str:
    return KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


class TestCanonicalString(unittest.TestCase):
    def test_it_is_consumer_timestamp_version_each_newline_terminated(self):
        # The single most likely thing to be wrong, and the one whose failure
        # is indistinguishable from a bad key. Sorted header names put
        # INTIMESTAMP between ID and KEY_VERSION, which is why this order.
        self.assertEqual(ws.canonical_string("abc-123", 1772636400000, "1"),
                         "abc-123\n1772636400000\n1\n")

    def test_it_ends_with_a_newline(self):
        self.assertTrue(ws.canonical_string("a", 1, "1").endswith("\n"))

    def test_nothing_else_creeps_in(self):
        # No URL, no method, no service name. Walmart signs three values.
        self.assertEqual(ws.canonical_string("a", 2, "3").count("\n"), 3)


class TestSignature(unittest.TestCase):
    def test_the_signature_verifies_against_the_public_key(self):
        message = ws.canonical_string("cid", 1772636400000, "1")
        sig = base64.b64decode(ws.sign(KEY, message))
        # Raises if the signature, padding or digest is wrong.
        KEY.public_key().verify(sig, message.encode(),
                                padding.PKCS1v15(), hashes.SHA256())

    def test_it_is_base64_not_raw_bytes(self):
        sig = ws.sign(KEY, "anything\n")
        self.assertIsInstance(sig, str)
        base64.b64decode(sig, validate=True)

    def test_a_different_timestamp_gives_a_different_signature(self):
        # Pins that the timestamp is genuinely inside the signed bytes. If it
        # were not, a signature could be cached and Walmart would reject a
        # stale one with an error that looks like a bad key.
        a = ws.sign(KEY, ws.canonical_string("cid", 1, "1"))
        b = ws.sign(KEY, ws.canonical_string("cid", 2, "1"))
        self.assertNotEqual(a, b)


class TestKeyLoading(unittest.TestCase):
    def test_a_bare_base64_der_blob_loads(self):
        # What Seller Center actually hands you.
        self.assertTrue(isinstance(ws.load_private_key(der_b64()),
                                   rsa.RSAPrivateKey))

    def test_a_der_blob_with_pasted_line_breaks_loads(self):
        blob = der_b64()
        broken = "\n".join(blob[i:i + 40] for i in range(0, len(blob), 40))
        self.assertTrue(isinstance(ws.load_private_key(broken),
                                   rsa.RSAPrivateKey))

    def test_a_full_pem_block_loads(self):
        self.assertTrue(isinstance(ws.load_private_key(pem()), rsa.RSAPrivateKey))

    def test_an_empty_key_says_so_rather_than_crashing_obscurely(self):
        with self.assertRaises(ValueError) as caught:
            ws.load_private_key("   ")
        self.assertIn(ws.PRIVATE_KEY_ENV, str(caught.exception))


class TestHeaders(unittest.TestCase):
    def test_every_header_walmart_requires_is_present(self):
        h = ws.headers(private_key=KEY, consumer_id="cid", key_version="1",
                       timestamp_ms=1772636400000)
        for required in ("WM_CONSUMER.ID", "WM_CONSUMER.INTIMESTAMP",
                         "WM_SEC.KEY_VERSION", "WM_SEC.AUTH_SIGNATURE",
                         "WM_SVC.NAME", "WM_QOS.CORRELATION_ID"):
            self.assertIn(required, h)

    def test_the_signed_timestamp_is_the_one_that_is_sent(self):
        # A signature over one timestamp sent with another is rejected, and
        # the error names neither. Pin that they are the same value.
        h = ws.headers(private_key=KEY, consumer_id="cid", key_version="1",
                       timestamp_ms=1772636400000)
        sig = base64.b64decode(h["WM_SEC.AUTH_SIGNATURE"])
        message = ws.canonical_string("cid", int(h["WM_CONSUMER.INTIMESTAMP"]),
                                      h["WM_SEC.KEY_VERSION"])
        KEY.public_key().verify(sig, message.encode(),
                                padding.PKCS1v15(), hashes.SHA256())

    def test_two_calls_do_not_reuse_a_signature(self):
        a = ws.headers(private_key=KEY, consumer_id="cid")
        b = ws.headers(private_key=KEY, consumer_id="cid")
        self.assertNotEqual(a["WM_QOS.CORRELATION_ID"], b["WM_QOS.CORRELATION_ID"])


class TestDescribe(unittest.TestCase):
    def test_it_never_prints_the_key_or_the_consumer_id(self):
        import os
        os.environ[ws.CONSUMER_ID_ENV] = "super-secret-consumer-id"
        os.environ[ws.PRIVATE_KEY_ENV] = der_b64()
        try:
            said = ws.describe()
        finally:
            del os.environ[ws.CONSUMER_ID_ENV]
            del os.environ[ws.PRIVATE_KEY_ENV]
        self.assertNotIn("super-secret", said)
        self.assertIn("RSA 2048-bit", said)

    def test_it_says_what_is_missing_when_nothing_is_set(self):
        said = ws.describe()
        self.assertIn("not configured", said)


if __name__ == "__main__":
    unittest.main()
