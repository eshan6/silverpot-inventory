"""The MCP probe's parsing, with no credentials and no network.

Only two things in `collector/ads_mcp.py` can be wrong without Amazon
telling us: reading a JSON-RPC message out of an SSE stream, and rendering a
tool's argument schema. Both are tested here, because a probe that
misreads the answer is worse than no probe - it would report "no tools" at
the one moment the schema finally arrived.
"""
import unittest

from collector import ads_mcp


class FakeResponse:
    def __init__(self, text, content_type="application/json"):
        self.text = text
        self.headers = {"Content-Type": content_type}


class TestParse(unittest.TestCase):
    def test_plain_json(self):
        got = ads_mcp._parse(FakeResponse('{"jsonrpc":"2.0","id":1}'))
        self.assertEqual(got["id"], 1)

    def test_server_sent_event(self):
        # Streamable HTTP answers either way, and which one arrives is the
        # server's choice. An SSE frame carries the message on a data: line
        # after an event: line that must be skipped.
        body = 'event: message\ndata: {"jsonrpc":"2.0","result":{"tools":[]}}\n\n'
        got = ads_mcp._parse(FakeResponse(body, "text/event-stream"))
        self.assertEqual(got["result"]["tools"], [])

    def test_sse_with_a_comment_and_blank_lines_first(self):
        body = ': keep-alive\n\nevent: message\ndata: {"ok":true}\n\n'
        self.assertEqual(ads_mcp._parse(FakeResponse(body, "text/event-stream")),
                         {"ok": True})

    def test_html_error_page_is_not_mistaken_for_a_message(self):
        self.assertIsNone(ads_mcp._parse(FakeResponse("<html>403</html>")))

    def test_empty_body(self):
        self.assertIsNone(ads_mcp._parse(FakeResponse("")))


class TestArgumentNames(unittest.TestCase):
    def test_required_arguments_are_marked_and_enums_printed(self):
        # The enum is the whole point of the probe: it is what six rounds of
        # guessing at eventTypes could not get out of POST /history.
        got = ads_mcp._argument_names({
            "type": "object",
            "required": ["eventTypes"],
            "properties": {
                "eventTypes": {"type": "array", "enum": ["CAMPAIGN", "AD"]},
                "pageSize": {"type": "integer"},
            },
        })
        self.assertIn("eventTypes*=CAMPAIGN|AD", got)
        self.assertIn("pageSize:integer", got)

    def test_a_schema_with_no_properties_renders_empty(self):
        self.assertEqual(ads_mcp._argument_names({"type": "object"}), "")

    def test_a_missing_schema_renders_empty(self):
        self.assertEqual(ads_mcp._argument_names(None), "")


class TestHistoryFlag(unittest.TestCase):
    def test_a_change_history_tool_would_be_flagged(self):
        for name in ("getChangeHistory", "ads_history", "campaign_audit_log"):
            self.assertTrue(
                any(w in name.lower() for w in ads_mcp.HISTORY_WORDS), name)

    def test_an_ordinary_reporting_tool_is_not_flagged(self):
        for name in ("createReport", "listCampaigns"):
            self.assertFalse(
                any(w in name.lower() for w in ads_mcp.HISTORY_WORDS), name)


if __name__ == "__main__":
    unittest.main()
