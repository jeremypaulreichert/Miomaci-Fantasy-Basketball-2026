"""Large packets and generous configured deadlines, with no network or keys."""
from dataclasses import replace
import json
import sys
import time
from unittest.mock import patch

import test_build_core as support
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.draft.trial import TrialCase, TrialConfig
from fantasy_basketball.providers import http_worker
from fantasy_basketball.providers.envelope import freeze_envelope
from fantasy_basketball.providers.native import prepare
from fantasy_basketball.providers.qualification import candidate_profiles, synthetic_response
from fantasy_basketball.providers.transport import HTTPTransport, LiveAuthorization, bounded_process
from fantasy_basketball.providers.worker_limits import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_TIMEOUT_MS

sys.path.insert(0, str(support.Path(__file__).resolve().parents[1] / "workstreams/build"))
from native_draft_check import scale_board
from worker_input_probe import probe
from fantasy_basketball.providers.worker_limits import MAX_INPUT_BYTES


class CapacityTests(support.BuildCase):
    def payload(self, **changes):
        return {"endpoint": "https://api.openai.com/v1/responses", "credential": "FAKE_CAPACITY_KEY",
                "body": {"text": "x" * 600000}, "timeout_ms": 600000,
                "deadline_ms": time.time_ns() // 1000000 + 600000,
                "request_bytes": 2199552, "response_bytes": 262144, **changes}

    def factory(self, raw=b'{"model":"synthetic"}'):
        observed = []
        class Response:
            status = 200
            def read(self, count):
                observed.append(("read", count))
                return raw
            def getheader(self, name, default):
                return default
        class Connection:
            def __init__(self, host, **kwargs):
                observed.append(("host", host, kwargs["timeout"], kwargs["context"].check_hostname))
            def request(self, method, path, **kwargs):
                observed.append(("request", method, path, len(kwargs["body"])))
            def getresponse(self):
                return Response()
            def close(self):
                observed.append(("close",))
        return Connection, observed

    def test_large_body_and_ten_minute_configuration_reach_single_bounded_tls_call(self):
        factory, calls = self.factory()
        result = http_worker.perform(self.payload(), factory)
        self.assertEqual(result["outcome"], "RESPONSE")
        self.assertEqual(calls[0], ("host", "api.openai.com", 600.0, True))
        self.assertEqual(sum(row[0] == "request" for row in calls), 1)
        self.assertGreater(calls[1][3], 524288)
        self.assertEqual(calls[2], ("read", 262145))
        self.assertEqual(calls[-1], ("close",))

    def test_absolute_support_and_actual_profile_limits_fail_before_connection(self):
        changes = ({"request_bytes": MAX_REQUEST_BYTES + 1}, {"response_bytes": MAX_RESPONSE_BYTES + 1},
                   {"timeout_ms": MAX_TIMEOUT_MS + 1}, {"request_bytes": 599999})
        for values in changes:
            with self.subTest(values=values), patch("http.client.HTTPSConnection", side_effect=AssertionError("no network")):
                self.assertEqual(http_worker.perform(self.payload(**values))["outcome"], "INVALID_REQUEST")

    def test_actual_child_accepts_large_stdin_then_rejects_endpoint_without_network(self):
        payload = self.payload(endpoint="http://forbidden.invalid/")
        result = bounded_process([sys.executable, "-I", "-B", http_worker.__file__], canonical(payload).encode(), 5000, 1000)
        self.assertEqual(result["outcome"], "INVALID_REQUEST")

    def test_actual_child_expired_wall_deadline_does_not_use_generous_timeout(self):
        payload = self.payload(deadline_ms=time.time_ns() // 1000000 - 1)
        result = bounded_process([sys.executable, "-I", "-B", http_worker.__file__], canonical(payload).encode(), 5000, 1000)
        self.assertEqual(result["outcome"], "WORKER_TIMEOUT")

    def test_transport_keeps_config_authority_and_large_body_bound_together(self):
        profile, _ = freeze_envelope(candidate_profiles()[0], scale_board(), max_picks=39,
                                     managers=("openai", "deepseek", "mistral"))
        profile = replace(profile, timeout_ms=600000, opportunity_ms=1200000)
        board = scale_board()
        config = TrialConfig("capacity", (TrialCase(profile, board),), profile.bound_micro_usd * 2,
                             1200000, "1" * 64, mode="LIVE_DIAGNOSTIC")
        now = time.time_ns() // 1000000
        authority = LiveAuthorization(config.hash, now + 3000, "OFFLINE_FAKE_OWNER", "OFFLINE_FAKE_REVIEW")
        transport = HTTPTransport(config, authority, {"openai": "FAKE_CAPACITY_KEY"})
        request = prepare(profile, board, now_ms=now, deadline_ms=now + 600000,
                          correction={"failure": "MALFORMED_ACTION", "response_hash": "0" * 64, "previous_text": '"' * 131000})
        observed = []
        def worker(command, payload, timeout_ms, output_limit, **kwargs):
            wire = json.loads(payload)
            observed.append((len(payload), wire["request_bytes"], wire["timeout_ms"], timeout_ms))
            self.assertLessEqual(wire["deadline_ms"], authority.expires_ms)
            self.assertLessEqual(timeout_ms, 3000)
            return {"outcome": "RESPONSE", "raw": synthetic_response(profile, "p0000")}
        with patch("fantasy_basketball.providers.transport.bounded_process", side_effect=worker):
            self.assertEqual(transport.send("case:0:attempt:0", request, 600000)["outcome"], "RESPONSE")
        self.assertGreater(observed[0][0], 524288)
        self.assertEqual(observed[0][1], profile.request_bytes)
        self.assertEqual(profile.timeout_ms, 600000)  # clamping did not mutate configuration
        with self.assertRaises(ValueError):
            HTTPTransport(replace(config, cases=(TrialCase(replace(profile, request_bytes=MAX_REQUEST_BYTES + 1), board),)),
                          authority, {"openai": "FAKE_CAPACITY_KEY"})

    def test_maximum_native_body_and_escaped_credential_fit_both_envelopes(self):
        with patch('socket.socket', side_effect=AssertionError('offline only')):
            result = probe(MAX_INPUT_BYTES)
        self.assertEqual(result['request_bytes'], MAX_REQUEST_BYTES)
        self.assertLessEqual(result['serialized_bytes'], MAX_INPUT_BYTES)
        self.assertEqual(result['transport_outcome'], 'OFFLINE_ACCEPTED')
        self.assertEqual(result['worker_outcome'], 'OFFLINE_ACCEPTED')
        self.assertEqual(result['dispatches'], 1)
        self.assertEqual(result['perform_calls'], 1)

    def test_one_byte_over_native_body_cap_still_rejects_before_connection(self):
        with patch('socket.socket', side_effect=AssertionError('offline only')):
            result = probe(MAX_INPUT_BYTES, over_body=True)
        self.assertEqual(result['request_bytes'], MAX_REQUEST_BYTES+1)
        self.assertEqual(result['transport_outcome'], 'BINDING_REJECTED')
        self.assertEqual(result['worker_outcome'], 'INVALID_REQUEST')
        self.assertEqual(result['dispatches'], 0)

    def test_worker_stdin_exact_limit_and_one_byte_over_remain_bounded(self):
        with patch('socket.socket', side_effect=AssertionError('offline only')):
            initial = probe(MAX_INPUT_BYTES)
            padding = MAX_INPUT_BYTES - initial['serialized_bytes']
            exact = probe(MAX_INPUT_BYTES, padding=padding)
            over = probe(MAX_INPUT_BYTES, padding=padding+1)
        self.assertEqual(exact['serialized_bytes'], MAX_INPUT_BYTES)
        self.assertEqual(exact['worker_outcome'], 'OFFLINE_ACCEPTED')
        self.assertEqual(exact['perform_calls'], 1)
        self.assertEqual(over['serialized_bytes'], MAX_INPUT_BYTES+1)
        self.assertEqual(over['worker_outcome'], 'WORKER_ERROR')
        self.assertEqual(over['perform_calls'], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
