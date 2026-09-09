"""The Walmart Connect diagnostic: does it read status codes honestly?

This module exists to answer one question with data instead of a documentation
quote, so the thing worth testing is its reasoning, not its HTTP. A diagnostic
that reports a confident verdict off a run that did not work is worse than no
diagnostic - the SP-API one did exactly that once and announced the app was
dead while the endpoint that mattered was answering 200.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import walmart_ads as wa  # noqa: E402

CONTROL = wa.CONTROL


def results(control=200, **ads):
    out = [(label, ads.get(label, 404), "") for label, _m, _u in wa.PROBES
           if label != CONTROL]
    out.append((CONTROL, control, ""))
    return out


class TestVerdict(unittest.TestCase):
    def test_a_dead_control_makes_the_run_inconclusive(self):
        # Everything refused *and* the known-good endpoint refused is a broken
        # token, not a finding about advertising.
        said = " ".join(wa.verdict(results(control=401,
                                          **{"Sponsored Search advertisers": 401})))
        self.assertIn("inconclusive", said.lower())
        self.assertNotIn("Partner Network", said)

    def test_a_network_failure_is_inconclusive_too(self):
        said = " ".join(wa.verdict(results(control=None)))
        self.assertIn("inconclusive", said.lower())

    def test_denial_is_only_reported_when_the_control_answered(self):
        said = " ".join(wa.verdict(results(
            control=200, **{"Sponsored Search advertisers": 403,
                            "Sponsored Search campaigns": 401})))
        self.assertIn("refused", said.lower())
        self.assertIn("Sponsored Search advertisers", said)
        self.assertIn("Partner Network", said)

    def test_a_reachable_endpoint_wins_over_the_denied_ones(self):
        # One 200 among the 403s is a route, and the verdict must lead with it
        # rather than averaging the failures into "denied".
        said = " ".join(wa.verdict(results(
            control=200, **{"Sponsored Search advertisers": 200,
                            "Sponsored Search campaigns": 403})))
        self.assertIn("reach Walmart Connect", said)
        self.assertIn("Sponsored Search advertisers", said)

    def test_a_reachable_endpoint_still_demands_a_probe_first(self):
        # Nine wrong guesses at a Walmart field name is the reason this line
        # exists. Reachable is not the same as understood.
        said = " ".join(wa.verdict(results(
            control=200, **{"Sponsored Search campaigns": 200})))
        self.assertIn("shape", said.lower())

    def test_all_404_is_a_wrong_path_not_a_denial(self):
        said = " ".join(wa.verdict(results(control=200)))
        self.assertIn("wrong path", said.lower())
        self.assertIn("says nothing", said.lower())


class TestProbes(unittest.TestCase):
    def test_the_control_is_one_of_the_probes(self):
        self.assertIn(CONTROL, [label for label, _m, _u in wa.PROBES])

    def test_every_probe_is_a_read(self):
        # A diagnostic that can create a campaign is not a diagnostic.
        for _label, method, _url in wa.PROBES:
            self.assertEqual(method, "GET")

    def test_probe_reports_a_status_per_candidate_and_survives_a_throw(self):
        class Boom(Exception):
            pass

        class FakeResp:
            status_code = 403
            text = "Not authorized"

        calls = []

        class FakeSession:
            def request(self, method, url, **kw):
                calls.append(url)
                if "campaign" in url:
                    raise Boom("no route")
                return FakeResp()

        out = wa.probe("tok", sess=FakeSession())
        self.assertEqual(len(out), len(wa.PROBES))
        self.assertEqual(len(calls), len(wa.PROBES))
        codes = {label: code for label, code, _n in out}
        self.assertIsNone(codes["Sponsored Search campaigns"])
        self.assertEqual(codes["Sponsored Search advertisers"], 403)

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


if __name__ == "__main__":
    unittest.main()
