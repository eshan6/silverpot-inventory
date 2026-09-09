"""The Walmart advertising sweep: does it read its own results honestly?

This module exists to answer a question with data instead of a documentation
quote, so the thing worth testing is its reasoning, not its HTTP. A diagnostic
that draws a confident conclusion from a run that did not work is worse than
no diagnostic - the SP-API one did exactly that once and announced the app was
dead while the endpoint that mattered was answering 200, and the first draft
of this one called a header complaint a permission denial.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import walmart_ads as wa  # noqa: E402

CONTROL = wa.CONTROL
GATEWAY = ('{"details":{"Description":"Request is missing required security '
           'headers, please read documentation for security headers"}}')


def rec(label, status=404, note="", mode=wa.OAUTH, skipped=False):
    r = {"label": label, "mode": mode, "url": "https://example.invalid",
         "status": status, "note": note}
    if skipped:
        r["skipped"] = True
        r["status"] = None
    return r


def results(control=200, *extra):
    return [rec(CONTROL, control), *extra]


class TestVerdict(unittest.TestCase):
    def test_a_dead_control_makes_the_run_inconclusive(self):
        # Everything refused *and* the known-good endpoint refused is a broken
        # token, not a finding about advertising.
        said = " ".join(wa.verdict(results(
            401, rec("WPA advertiser (oauth)", 401, GATEWAY))))
        self.assertIn("inconclusive", said.lower())
        self.assertNotIn("gateway refused", said)

    def test_a_network_failure_is_inconclusive_too(self):
        said = " ".join(wa.verdict(results(None)))
        self.assertIn("inconclusive", said.lower())

    def test_a_reachable_endpoint_is_the_headline(self):
        said = " ".join(wa.verdict(results(
            200,
            rec("Marketplace: SEM report", 200),
            rec("WPA advertiser (oauth)", 403, GATEWAY))))
        self.assertIn("there is a route", said)
        self.assertIn("Marketplace: SEM report", said)

    def test_a_reachable_endpoint_still_demands_a_shape_probe(self):
        # Nine wrong guesses at a Walmart field name is why this line exists.
        # Reachable is not the same as understood.
        said = " ".join(wa.verdict(results(200, rec("WPA campaign (signed)", 200))))
        self.assertIn("shape probe", said)

    def test_a_gateway_rejection_is_not_read_as_a_denial(self):
        # The live 403 on 2026-09-09 was "Request is missing required security
        # headers" - the gateway refusing the request's shape before anything
        # looked at what this account may do. Calling that "advertising is
        # refused" is a false conclusion drawn from a real response.
        said = " ".join(wa.verdict(results(
            200, rec("WPA advertiser (oauth)", 403, GATEWAY))))
        self.assertIn("headers, not access", said)
        self.assertNotIn("genuinely refused", said)

    def test_a_plain_403_is_still_read_as_a_denial(self):
        said = " ".join(wa.verdict(results(
            200, rec("WPA advertiser (signed)", 403, "Not authorized",
                     mode=wa.SIGNED))))
        self.assertIn("genuinely refused", said)
        self.assertIn("Partner Network", said)

    def test_a_signed_request_refused_for_its_headers_blames_our_signature(self):
        # The important case. If signing is configured and the gateway still
        # complains about headers, the bug is in this repository - a wrong
        # canonical string or key version - and telling the user to go apply
        # for access would send them off to solve the wrong problem.
        said = " ".join(wa.verdict(results(
            200, rec("WPA advertiser (signed)", 403, GATEWAY, mode=wa.SIGNED))))
        self.assertIn("signature itself is being rejected", said)
        self.assertIn("not an approval to go and ask for", said)

    def test_when_signing_is_unconfigured_it_names_the_free_self_serve_fix(self):
        said = " ".join(wa.verdict(results(
            200,
            rec("WPA advertiser (oauth)", 403, GATEWAY),
            rec("WPA advertiser (signed)", skipped=True, mode=wa.SIGNED))))
        self.assertIn("Consumer IDs & Private Keys", said)
        self.assertIn("skipped", said.lower())

    def test_404s_say_nothing_either_way(self):
        said = " ".join(wa.verdict(results(200, rec("Marketplace: ads", 404))))
        self.assertIn("not a real path", said)
        self.assertIn("says nothing", said)


class TestProbes(unittest.TestCase):
    def test_the_control_is_one_of_the_probes(self):
        self.assertIn(CONTROL, [label for label, _m, _u, _a in wa.PROBES])

    def test_every_probe_is_a_read(self):
        # A diagnostic that can create a campaign is not a diagnostic.
        for _label, method, _url, _auth in wa.PROBES:
            self.assertEqual(method, "GET")

    def test_the_free_routes_are_tried_before_the_ones_needing_a_new_key(self):
        # If something we already hold answers, nobody has to generate
        # anything. Ordering is for the reader of the log, and it is pinned so
        # a later edit does not bury the cheap answer under the expensive one.
        modes = [auth for _l, _m, _u, auth in wa.PROBES]
        self.assertLess(modes.index(wa.OAUTH), modes.index(wa.SIGNED))

    def test_all_three_auth_modes_are_exercised(self):
        self.assertEqual({auth for _l, _m, _u, auth in wa.PROBES},
                         {wa.OAUTH, wa.SIGNED, wa.NONE})

    def test_a_probe_that_throws_does_not_end_the_sweep(self):
        class FakeResp:
            status_code = 403
            text = "Not authorized"

        class FakeSession:
            def request(self, method, url, **kw):
                if "campaign" in url:
                    raise RuntimeError("no route")
                return FakeResp()

        out = wa.probe("tok", sess=FakeSession())
        self.assertEqual(len(out), len(wa.PROBES))

    def test_signed_probes_are_skipped_not_failed_when_unconfigured(self):
        class FakeResp:
            status_code = 200
            text = "{}"

        class FakeSession:
            def request(self, method, url, **kw):
                return FakeResp()

        out = wa.probe("tok", sess=FakeSession())
        signed = [r for r in out if r["mode"] == wa.SIGNED]
        self.assertTrue(signed)
        self.assertTrue(all(r.get("skipped") for r in signed),
                        "signed probes must skip, not fail, without credentials")

    def test_no_token_reaches_the_output(self):
        class FakeResp:
            status_code = 403
            text = "Not authorized"

        class FakeSession:
            def request(self, method, url, **kw):
                return FakeResp()

        blob = repr(wa.probe("s3cret-token-value", sess=FakeSession()))
        self.assertNotIn("s3cret", blob)


class TestEntryPoint(unittest.TestCase):
    def test_an_unknown_flag_does_not_silently_run_a_diagnosis(self):
        self.assertEqual(wa.main(["--sync"]), 2)

    def test_no_argument_does_not_silently_run_a_diagnosis(self):
        self.assertEqual(wa.main([]), 2)


if __name__ == "__main__":
    unittest.main()
