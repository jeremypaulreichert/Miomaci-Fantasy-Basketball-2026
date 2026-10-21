"""Fake HTTPS only. No API key, account, network or credential-store access."""
from copy import deepcopy
import json
import time
import unittest

import test_build_core
from fantasy_basketball.providers.research_worker import perform


KEY = "FAKE-NOT-A-REAL-TAVILY-KEY"
URL = "https://www.nba.com/player/example"


def payload():
    return {"action": {"action": "search", "query": "my research", "url": None,
                       "player_id": None, "rationale": None}, "discovered_urls": [], "max_results": 3,
            "timeout_ms": 1000, "deadline_ms": time.time_ns() // 1000000 + 10000,
            "response_bytes": 65536, "credential": KEY}


class FakeResponse:
    def __init__(self, data, status=200, encoding="identity"):
        self.data, self.status, self.encoding = data, status, encoding
        self.read_limit = None

    def read(self, limit):
        self.read_limit = limit
        return self.data[:limit]

    def getheader(self, name, default=None):
        return self.encoding if name == "Content-Encoding" else default


class FakeConnection:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ResearchWorkerTests(unittest.TestCase):
    def invoke(self, data, status=200, encoding="identity", value=None):
        response = FakeResponse(data, status, encoding)
        connection = FakeConnection(response)
        seen = []
        def factory(host, **kwargs):
            seen.append((host, kwargs))
            return connection
        result = perform(value or payload(), factory)
        return result, connection, response, seen

    def test_fixed_https_host_no_generated_answer_and_no_secret_in_receipt(self):
        raw = {"results": [{"url": URL, "title": "Source", "content": "Facts"}], "usage": {"credits": 1}}
        result, connection, response, seen = self.invoke(json.dumps(raw).encode())
        self.assertEqual(result["outcome"], "SERVICE_RESPONSE")
        self.assertEqual(result["raw"], raw)
        self.assertEqual(seen[0][0], "api.tavily.com")
        method, path, wire = connection.calls[0]
        self.assertEqual((method, path), ("POST", "/search"))
        self.assertEqual(wire["headers"]["Authorization"], "Bearer " + KEY)
        body = json.loads(wire["body"])
        self.assertFalse(body["include_answer"])
        self.assertFalse(body["auto_parameters"])
        self.assertNotIn(KEY, repr(result))
        self.assertEqual(response.read_limit, 65537)
        self.assertTrue(connection.closed)

    def test_read_never_fetches_source_host_locally(self):
        value = payload()
        value["action"].update(action="read", query=None, url=URL)
        value["discovered_urls"] = [URL]
        result, connection, _, seen = self.invoke(b'{"results":[]}', value=value)
        self.assertEqual(seen[0][0], "api.tavily.com")
        self.assertEqual(connection.calls[0][:2], ("POST", "/extract"))
        self.assertEqual(json.loads(connection.calls[0][2]["body"])["urls"], [URL])

    def test_errors_redirects_oversize_and_encoding_return_only_fixed_metadata(self):
        for data, status, encoding, expected in (
                (b"server error with private details", 429, "identity", "HTTP_ERROR"),
                (b"redirect content", 302, "identity", "HTTP_ERROR"),
                (b"x" * 65537, 200, "identity", "RESPONSE_TOO_LARGE"),
                (b"{}", 200, "gzip", "UNSUPPORTED_ENCODING"),
                (b"not JSON", 200, "identity", "MALFORMED_JSON"),
                (b'{"x":1,"x":2}', 200, "identity", "MALFORMED_JSON")):
            result, connection, _, _ = self.invoke(data, status, encoding)
            self.assertEqual(result["outcome"], expected)
            self.assertNotIn("raw", result)
            self.assertNotIn("server error", repr(result))
            self.assertEqual(len(connection.calls), 1)
            self.assertTrue(connection.closed)

    def test_plain_and_json_escaped_credential_echo_is_suppressed(self):
        for data in (json.dumps({"echo": KEY}).encode(),
                     ('{"echo":"' + ''.join('\\u%04x' % ord(c) for c in KEY) + '"}').encode()):
            result, _, _, _ = self.invoke(data)
            self.assertEqual(result["outcome"], "CREDENTIAL_ECHO")
            self.assertNotIn(KEY, repr(result))

    def test_credential_in_query_blocks_before_connection(self):
        value = payload()
        value["action"]["query"] = KEY
        result, connection, _, seen = self.invoke(b"{}", value=value)
        self.assertEqual(result["outcome"], "CREDENTIAL_IN_REQUEST")
        self.assertFalse(connection.calls)
        self.assertFalse(seen)

    def test_expired_or_unknown_payload_cannot_connect(self):
        for mutation in ("expiry", "extra", "private", "undiscovered"):
            value = payload()
            if mutation == "expiry":
                value["deadline_ms"] = 1
            elif mutation == "extra":
                value["endpoint"] = "https://evil.example/"
            else:
                value["action"].update(action="read", query=None, url="https://127.0.0.1/" if mutation == "private" else URL)
            result, connection, _, seen = self.invoke(b"{}", value=value)
            self.assertNotEqual(result["outcome"], "SERVICE_RESPONSE")
            self.assertFalse(seen)
            self.assertFalse(connection.calls)
