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


def rec(label, status=404, note="", mode=wa.OAUTH, skipped=False, url=None):
    # Default to an advertising URL: most records in these tests stand for
    # advertising probes, and the verdict now ignores anything else.
    r = {"label": label, "mode": mode,
         "url": url or f"{wa.WPA_HOST}/advertiser",
         "status": status, "note": note}
    if skipped:
        r["skipped"] = True
        r["status"] = None
    return r


def results(control=200, *extra, signed_control=None):
    """A sweep. The signed control defaults to skipped, as it is unconfigured."""
    sc = (rec(wa.SIGNED_CONTROL, skipped=True, mode=wa.SIGNED)
          if signed_control is None
          else rec(wa.SIGNED_CONTROL, signed_control, mode=wa.SIGNED))
    return [rec(CONTROL, control), sc, *extra]


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

    def test_a_bad_signature_is_diagnosed_before_anything_it_was_used_for(self):
        # The point of the signed control. If Walmart rejects our signature on
        # an endpoint we demonstrably reach with OAuth, then every signed
        # result in the sweep is meaningless and none of them says anything
        # about advertising. Reporting them anyway would send someone off to
        # apply for access to fix a bug that lives in this repository.
        said = " ".join(wa.verdict(results(
            200,
            rec("WPA advertiser (signed)", 401, "Invalid signature",
                mode=wa.SIGNED),
            signed_control=401)))
        self.assertIn("our signature is not valid", said)
        self.assertIn("no approval would change any of this", said)
        self.assertNotIn("genuinely refused", said)

    def test_a_working_signature_lets_the_real_verdict_through(self):
        # The control must gate the reasoning, not replace it.
        said = " ".join(wa.verdict(results(
            200,
            rec("WPA advertiser (signed)", 403, "Not authorized", mode=wa.SIGNED),
            signed_control=200)))
        self.assertNotIn("our signature is not valid", said)
        self.assertIn("genuinely refused", said)

    def test_a_signed_control_that_answers_is_not_counted_as_a_route(self):
        # It is a control, not a finding. Reporting "there is a route" because
        # WFS inventory answered would be triumphantly meaningless.
        said = " ".join(wa.verdict(results(200, signed_control=200)))
        self.assertNotIn("there is a route", said)

    def test_404s_say_nothing_either_way(self):
        said = " ".join(wa.verdict(results(200, rec("Marketplace: ads", 404))))
        self.assertIn("not a real path", said)
        self.assertIn("says nothing", said)


class TestWhatCountsAsAdvertising(unittest.TestCase):
    """The sweep asks nearby endpoints too. Only ad ones answer the question."""

    def test_a_marketplace_report_endpoint_is_not_advertising(self):
        # This is the live case that broke it. On 2026-09-09 the reports API
        # and item performance both answered 200, and the verdict announced
        # "there is a route" - to endpoints carrying no ad spend at all.
        for url in (
            "https://marketplace.walmartapis.com/v3/reports/reportRequests",
            "https://marketplace.walmartapis.com/v3/reports/reportRequests"
            "?reportType=ITEM_PERFORMANCE&reportVersion=v1",
            "https://marketplace.walmartapis.com/v3/wfs/inventory",
        ):
            self.assertFalse(wa.is_advertising({"url": url}), url)

    def test_the_advertising_gateway_and_the_ad_paths_are(self):
        for url in (f"{wa.WPA_HOST}/advertiser",
                    f"{wa.WPA_HOST}/snapshot/report",
                    "https://marketplace.walmartapis.com/v3/sem/campaigns",
                    "https://marketplace.walmartapis.com/v3/advertising",
                    "https://marketplace.walmartapis.com/v3/ads"):
            self.assertTrue(wa.is_advertising({"url": url}), url)

    def test_a_reports_200_is_not_reported_as_an_advertising_route(self):
        said = " ".join(wa.verdict([
            rec(CONTROL, 200, url="https://marketplace.walmartapis.com/v3/wfs/inventory"),
            rec(wa.SIGNED_CONTROL, skipped=True, mode=wa.SIGNED),
            rec("Marketplace: report requests", 200,
                url="https://marketplace.walmartapis.com/v3/reports/reportRequests"),
            rec("WPA advertiser (oauth)", 403, GATEWAY),
        ]))
        self.assertNotIn("there is a route", said)
        self.assertIn("headers, not access", said)


class TestBodiesInTheLog(unittest.TestCase):
    """A catalogue endpoint's body is the answer; everything else's is risk."""

    def test_the_reports_body_is_kept(self):
        self.assertTrue(wa.shows_body_when_ok(
            "https://marketplace.walmartapis.com/v3/reports/reportRequests"))

    def test_no_other_endpoint_leaks_a_body_into_a_public_log(self):
        # This repository's Actions logs are public and Walmart's order data
        # carries buyer names and addresses. Only /v3/reports metadata is
        # printed on success; anything else must stay a bare status code.
        for url in ("https://marketplace.walmartapis.com/v3/wfs/inventory",
                    "https://marketplace.walmartapis.com/v3/orders",
                    f"{wa.WPA_HOST}/advertiser"):
            self.assertFalse(wa.shows_body_when_ok(url), url)


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

    def test_every_auth_mode_is_exercised(self):
        # oauth+cid and cid exist because Walmart's developer portal issues
        # this account a ClientId and ClientSecret and nothing else - there is
        # no separate consumer id to hold. If the advertising gateway wants a
        # WM_CONSUMER.ID, the ClientId is the only candidate that exists.
        self.assertEqual({auth for _l, _m, _u, auth in wa.PROBES},
                         {wa.OAUTH, wa.OAUTH_CID, wa.CID, wa.SIGNED, wa.NONE})

    def test_the_client_id_modes_need_no_credential_we_do_not_have(self):
        # The whole point of these two: they must never skip on this account,
        # because WALMART_CLIENT_ID is already a secret here.
        import os
        os.environ["WALMART_CLIENT_ID"] = "some-client-id"
        try:
            with_token = wa._auth_headers(wa.OAUTH_CID, "tok")
            without = wa._auth_headers(wa.CID, None)
        finally:
            del os.environ["WALMART_CLIENT_ID"]
        self.assertEqual(with_token["WM_CONSUMER.ID"], "some-client-id")
        self.assertEqual(without["WM_CONSUMER.ID"], "some-client-id")
        # cid-only must genuinely carry no token, or it tests nothing new.
        self.assertNotIn("WM_SEC.ACCESS_TOKEN", without)

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
