"""Native trial recovery and isolated HTTP worker checks, entirely offline."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workstreams/build"))
from provider_cases import cases
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.draft.trial import NativeTrial, TrialCase, TrialConfig
from fantasy_basketball.providers.http_worker import perform
from fantasy_basketball.providers.native import prepare
from fantasy_basketball.providers.qualification import candidate_profiles, synthetic_response
from fantasy_basketball.providers.transport import HTTPTransport, LiveAuthorization, ScriptedNativeTransport, bounded_process


def configuration(run_id="native-trial", names=("last_slot",), **changes):
    profile = candidate_profiles()[0]
    selected = tuple(TrialCase(profile, cases(profile.provider)[name]) for name in names)
    total = sum(c.profile.bound_micro_usd * c.profile.max_attempts for c in selected)
    return replace(TrialConfig(run_id, selected, total, 960000, digest({"test": "native-trial"})), **changes)


def reply(profile, player="p05", **changes):
    return {"outcome": "RESPONSE", "http_status": 200, "raw": synthetic_response(profile, player, **changes)}


class NativeTrialTests(support.BuildCase):
    def trial(self, config=None, **kwargs):
        return NativeTrial(self.directory, config or configuration(), clock=self.clock, **kwargs)

    def test_exact_native_request_is_durable_before_send_and_restart_sends_nothing(self):
        config = configuration()
        trial = self.trial(config)
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile)})
        original = transport.send
        def send(key, request, timeout):
            saved = trial.journal.read()["op"]["attempts"][-1]
            self.assertEqual(saved["state"], "DISPATCHED")
            self.assertEqual(saved["native_request"], request)
            self.assertEqual(digest(request["body"]), request["request_hash"])
            return original(key, request, timeout)
        transport.send = send
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = trial.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["results"][0]["action"]["player_id"], "p05")
        unused = ScriptedNativeTransport({})
        self.assertEqual(self.trial(config).resume(unused), result)
        self.assertEqual(unused.calls, [])

    def test_one_bad_case_stops_all_future_cases_with_retained_exposure(self):
        config = configuration(names=("opening", "last_slot", "owned_star"))
        profile = config.cases[0].profile
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(profile, "p04"),
                                             "case:1:attempt:0": reply(profile, "p06"),
                                             "case:1:attempt:1": reply(profile, "p06")})
        result = self.trial(config).resume(transport)
        self.assertEqual(result["phase"], "PAUSED")
        self.assertEqual(result["pause"], {"reason": "ILLEGAL_PICK", "case_index": 1})
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["reserved_requests"], 3)
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 3 * profile.bound_micro_usd)
        first, correction = transport.calls[1:]
        self.assertEqual(first["request"]["board_hash"], correction["request"]["board_hash"])
        self.assertEqual(first["request"]["deadline_ms"], correction["request"]["deadline_ms"])
        self.assertEqual(first["request"]["profile_hash"], correction["request"]["profile_hash"])
        unused = ScriptedNativeTransport({})
        self.assertEqual(self.trial(config).resume(unused), result)
        self.assertFalse(unused.calls)

    def test_budget_protects_every_future_case_and_correction(self):
        config = configuration(names=("opening", "last_slot"))
        config = replace(config, budget_micro_usd=config.cases[0].profile.bound_micro_usd * 2)
        unused = ScriptedNativeTransport({})
        result = self.trial(config).resume(unused)
        self.assertEqual(result["pause"]["reason"], "BUDGET")
        self.assertFalse(unused.calls)
        self.assertEqual(result["accounting"]["committed_micro_usd"], 0)

    def test_original_trial_deadline_cannot_reset_between_cases(self):
        config = configuration(names=("opening", "last_slot"), trial_ms=100)
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile, "p04"),
                                             "case:1:attempt:0": reply(config.cases[0].profile)})
        original = transport.send
        def send(key, request, timeout):
            self.assertEqual(timeout, 100 if key.startswith("case:0") else 40)
            self.clock.advance(60 if key.startswith("case:0") else 40)
            return original(key, request, timeout)
        transport.send = send
        result = self.trial(config).resume(transport)
        self.assertEqual(result["pause"]["reason"], "DEADLINE")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["deadline_ms"], 10100)
        self.assertEqual(self.trial(config).resume(ScriptedNativeTransport({})), result)

    def test_dispatch_persistence_consumes_same_attempt_timeout(self):
        config = configuration()
        profile = replace(config.cases[0].profile, timeout_ms=1000)
        config = replace(config, cases=(TrialCase(profile, config.cases[0].board),))
        def fault(point):
            if point == "after:DISPATCH":
                self.clock.advance(900)
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(profile)})
        result = self.trial(config, fault=fault).resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(transport.calls[0]["timeout_ms"], 100)

    def test_no_send_when_dispatch_persistence_exhausts_timeout(self):
        config = configuration()
        def fault(point):
            if point == "after:DISPATCH":
                self.clock.advance(config.cases[0].profile.timeout_ms)
        transport = ScriptedNativeTransport({})
        result = self.trial(config, fault=fault).resume(transport)
        self.assertEqual(result["phase"], "PAUSED")
        self.assertEqual(result["pause"]["reason"], "DEADLINE")
        self.assertFalse(transport.calls)

    def test_concurrent_resume_pauses_without_duplicate_and_retains_late_usage(self):
        config = configuration()
        entered, release = threading.Event(), threading.Event()
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile)})
        original = transport.send
        def send(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test synchronization failed")
            return original(*args)
        transport.send = send
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.trial(config).resume, transport)
            self.assertTrue(entered.wait(5))
            unused = ScriptedNativeTransport({})
            try:
                paused = self.trial(config).resume(unused)
                self.assertEqual(paused["pause"]["reason"], "UNKNOWN_DISPATCH")
                self.assertFalse(unused.calls)
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(result["phase"], "PAUSED")
        self.assertEqual(result["results"], [])
        self.assertGreater(result["accounting"]["estimated_charge_micro_usd"], 0)
        self.assertEqual(len(transport.calls), 1)

    def test_output_limit_http_error_hold_and_transport_exception_never_correct(self):
        profile = configuration().cases[0].profile
        failures = (reply(profile, stop="length"), {"outcome": "HTTP_ERROR", "http_status": 429},
                    reply(profile, None), TimeoutError("SECRET_MUST_NOT_BE_SAVED"))
        for i, response in enumerate(failures):
            config = configuration(run_id=f"failure-{i}", names=("last_slot", "opening"))
            transport = ScriptedNativeTransport({"case:0:attempt:0": response})
            trial = self.trial(config)
            result = trial.resume(transport)
            self.assertEqual(result["phase"], "PAUSED")
            self.assertFalse(result["results"])
            self.assertEqual(len(transport.calls), 1)
            self.assertNotIn("SECRET_MUST_NOT_BE_SAVED", canonical(trial.journal.read()))

    def test_price_board_policy_and_mode_drift_refuse_existing_journal(self):
        config = configuration()
        self.assertEqual(config.hash, digest(asdict(config)))
        self.assertEqual(config.hash, digest(asdict(config)))  # cached value preserves the content binding
        self.trial(config)
        case = config.cases[0]
        for changed in (replace(config, mode="LIVE_DIAGNOSTIC"), replace(config, trial_ms=1),
                        replace(config, budget_micro_usd=config.budget_micro_usd + 1),
                        replace(config, cases=(replace(case, profile=replace(case.profile, output_tokens=8192)),))):
            with self.assertRaises(ValueError):
                self.trial(changed)

    def test_live_mode_cannot_use_fixture_transport_or_absent_approval(self):
        config = configuration(mode="LIVE_DIAGNOSTIC")
        with self.assertRaises(ValueError):
            self.trial(config).resume(ScriptedNativeTransport({}))
        with self.assertRaises(ValueError):
            HTTPTransport(config, None, {"openai": "fake"})
        permit = LiveAuthorization(config.hash, 1, "test-owner-record", "test-review-record")
        transport = HTTPTransport(config, permit, {"openai": "fake"})
        with self.assertRaises(ValueError):
            self.trial(config).resume(transport)
        self.assertEqual(self.trial(config).status()["phase"], "NOT_STARTED")

    def test_authority_record_is_durable_and_cannot_be_replaced(self):
        config = configuration(mode="LIVE_DIAGNOSTIC")
        permit = LiveAuthorization(config.hash, time.time_ns() // 1000000 + 10000, "TEST_ONLY_OWNER", "TEST_ONLY_REVIEW")
        transport = HTTPTransport(config, permit, {"openai": "FAKE_CREDENTIAL"})
        trial = NativeTrial(self.directory, config)
        # Exercises the live-mode control path with the network worker replaced.
        with patch("fantasy_basketball.providers.transport.bounded_process", return_value=reply(config.cases[0].profile)):
            result = trial.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["authorization"], asdict(permit))
        self.assertNotIn("FAKE_CREDENTIAL", canonical(trial.journal.read()))
        changed = HTTPTransport(config, replace(permit, owner_record="different-test-record"), {"openai": "FAKE_CREDENTIAL"})
        with self.assertRaises(ValueError):
            trial.resume(changed)

    def test_authorization_start_race_rejects_losing_record_before_dispatch(self):
        config = configuration(mode="LIVE_DIAGNOSTIC")
        now = time.time_ns() // 1000000
        winner = LiveAuthorization(config.hash, now + 10000, "TEST_WINNER", "TEST_REVIEW")
        loser = replace(winner, owner_record="TEST_LOSER")
        trial = NativeTrial(self.directory, config)
        transport = HTTPTransport(config, loser, {"openai": "FAKE_CREDENTIAL"})
        original = trial.journal.transact
        raced = False
        def competing_start(decide):
            nonlocal raced
            if not raced:
                raced = True
                # Another controller commits START after this controller's
                # initial authority read but before its first writer lock.
                original(lambda state: [("START", {"at_ms": now, "authorization": asdict(winner)})])
            return original(decide)
        with patch.object(trial.journal, "transact", side_effect=competing_start):
            with patch("fantasy_basketball.providers.transport.bounded_process", return_value=reply(config.cases[0].profile)) as worker:
                with self.assertRaisesRegex(ValueError, "recorded authorization changed"):
                    trial.resume(transport)
                worker.assert_not_called()
        state = trial.journal.read()
        self.assertEqual(state["authorization"], asdict(winner))
        self.assertIsNone(state["op"])
        self.assertFalse(state["accounts"])

    def test_missing_identity_and_usage_pause_but_overrun_keeps_larger_exposure(self):
        profile = configuration().cases[0].profile
        for i, mutate in enumerate((lambda r: r.update(model="unexpected"), lambda r: r.pop("usage"),
                                    lambda r: r["usage"].update(input_tokens=profile.input_tokens * 2,
                                                             total_tokens=profile.input_tokens * 2 + 100))):
            response = reply(profile)
            mutate(response["raw"])
            result = self.trial(configuration(run_id=f"usage-{i}")).resume(ScriptedNativeTransport({"case:0:attempt:0": response}))
            self.assertEqual(result["phase"], "PAUSED")
            self.assertFalse(result["results"])
            if i == 2:
                self.assertGreater(result["accounting"]["unresolved_exposure_micro_usd"], profile.bound_micro_usd)


class WorkerTests(support.BuildCase):
    def payload(self):
        return {"endpoint": "https://api.openai.com/v1/responses", "credential": "FAKE_CREDENTIAL",
                "body": {"model": "synthetic"}, "timeout_ms": 1000, "deadline_ms": time.time_ns() // 1000000 + 5000,
                "request_bytes": 1000, "response_bytes": 1000}

    def worker(self, status=200, body=b'{"model":"synthetic"}', encoding="identity"):
        observed = []
        class Response:
            def read(self, limit):
                observed.append(("read", limit))
                return body[:limit]
            def getheader(self, *args):
                return encoding
        response = Response()
        response.status = status
        class Connection:
            def __init__(self, host, **kwargs):
                observed.append(("host", host, kwargs["timeout"], kwargs["context"].check_hostname))
            def request(self, method, path, **kwargs):
                observed.append(("request", method, path, kwargs))
            def getresponse(self):
                return response
            def close(self):
                observed.append(("closed",))
        return perform(self.payload(), Connection), observed

    def test_tls_exact_endpoint_single_post_and_bounded_body(self):
        result, observed = self.worker()
        self.assertEqual(result["outcome"], "RESPONSE")
        self.assertEqual(observed[0], ("host", "api.openai.com", 1.0, True))
        self.assertEqual(observed[1][:3], ("request", "POST", "/v1/responses"))
        self.assertEqual(observed[2], ("read", 1001))
        self.assertEqual(observed[-1], ("closed",))
        self.assertNotIn("FAKE_CREDENTIAL", canonical(result))

    def test_redirect_errors_overflow_compression_and_duplicate_json_fail_closed(self):
        for status in (301, 307, 401, 429, 500):
            result, calls = self.worker(status=status, body=b'FAKE_CREDENTIAL server error')
            self.assertEqual(result["outcome"], "HTTP_ERROR")
            self.assertNotIn("FAKE_CREDENTIAL", canonical(result))
            self.assertEqual(sum(c[0] == "request" for c in calls), 1)
        self.assertEqual(self.worker(body=b"x" * 2000)[0]["outcome"], "RESPONSE_TOO_LARGE")
        self.assertEqual(self.worker(encoding="gzip")[0]["outcome"], "UNSUPPORTED_ENCODING")
        self.assertEqual(self.worker(body=b'{"x":1,"x":2}')[0]["outcome"], "MALFORMED_JSON")
        for body in (b'{"echo":"FAKE_CREDENTIAL"}', b'{"echo":"FAKE_\\u0043REDENTIAL"}'):
            result = self.worker(body=body)[0]
            self.assertEqual(result["outcome"], "CREDENTIAL_ECHO")
            self.assertNotIn("FAKE_CREDENTIAL", canonical(result))

    def test_foreign_endpoint_and_header_injection_rejected_without_network(self):
        for change in ({"endpoint": "https://example.com/"}, {"credential": "key\r\nInjected: yes"}):
            with patch("http.client.HTTPSConnection", side_effect=AssertionError("network forbidden")):
                result = perform({**self.payload(), **change})
            self.assertEqual(result["outcome"], "INVALID_REQUEST")

    def test_hung_child_is_killed_reaped_and_never_retried(self):
        processes = []
        popen = subprocess.Popen
        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        start = time.monotonic()
        with patch("subprocess.Popen", side_effect=capture):
            result = bounded_process([sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"], b"", 300, 1000)
        self.assertEqual(result["outcome"], "WORKER_TIMEOUT")
        self.assertLess(time.monotonic() - start, 6)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_real_isolated_worker_rejects_invalid_endpoint_offline(self):
        from fantasy_basketball.providers import http_worker
        payload = {**self.payload(), "endpoint": "http://forbidden.example/"}
        result = bounded_process([sys.executable, "-I", "-B", http_worker.__file__], canonical(payload).encode(), 5000, 1000)
        self.assertEqual(result, {"outcome": "INVALID_REQUEST"})

    def test_worker_deadline_survives_without_controlling_watchdog(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        code = ("import sys,time;sys.path.insert(0,sys.argv[1]);"
                "from fantasy_basketball.providers import http_worker;"
                "http_worker.perform=lambda payload:time.sleep(20);http_worker.main()")
        payload = {**self.payload(), "timeout_ms": 300}
        child = subprocess.run([sys.executable, "-I", "-B", "-c", code, source],
                               input=canonical(payload).encode(), capture_output=True, timeout=5)
        self.assertEqual(child.returncode, 124, child.stderr)

    def test_live_transport_passes_credentials_only_in_stdin_and_binds_approval(self):
        config = configuration(mode="LIVE_DIAGNOSTIC")
        now = time.time_ns() // 1000000
        permit = LiveAuthorization(config.hash, now + 10000, "test-owner", "test-review")
        transport = HTTPTransport(config, permit, {"openai": "FAKE_CREDENTIAL"})
        case = config.cases[0]
        request = prepare(case.profile, case.board, now_ms=now, deadline_ms=now + 5000)
        with patch("fantasy_basketball.providers.transport.bounded_process", return_value={"outcome": "WORKER_TIMEOUT"}) as worker:
            transport.send("test", request, 1000)
        command, payload, timeout, bound = worker.call_args.args
        self.assertNotIn("FAKE_CREDENTIAL", canonical(command))
        self.assertEqual(json.loads(payload)["credential"], "FAKE_CREDENTIAL")
        self.assertLessEqual(timeout, 1000)
        with self.assertRaises(ValueError):
            transport.authorize(replace(config, budget_micro_usd=config.budget_micro_usd + 1), now)


BOUNDARIES = {"before_commit:START": (0, 1), "after:START": (0, 1),
              "before_commit:OPEN": (0, 1), "after:OPEN": (0, 1),
              "before_commit:RESERVE": (0, 1), "after:RESERVE": (1, 1),
              "before_commit:DISPATCH": (1, 1), "after:DISPATCH": (1, 0),
              "before_commit:RESPONSE": (1, 0), "after:RESPONSE": (1, 0),
              "before_commit:RESOLVE": (1, 0), "after:RESOLVE": (1, 0)}


class NativeCrashTests(support.BuildCase):
    def check_boundary(self, boundary):
        child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--crash", boundary, str(self.directory)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 23, child.stderr)
        config = configuration()
        trial = NativeTrial(self.directory, config, clock=self.clock)
        reserved, calls = BOUNDARIES[boundary]
        self.assertEqual(trial.status()["reserved_requests"], reserved)
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile)})
        result = trial.resume(transport)
        self.assertEqual(len(transport.calls), calls)
        ambiguous = boundary in {"after:DISPATCH", "before_commit:RESPONSE"}
        self.assertEqual(result["phase"], "PAUSED" if ambiguous else "COMPLETE")
        if ambiguous:
            self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH")
            self.assertFalse(result["results"])
        unused = ScriptedNativeTransport({})
        self.assertEqual(NativeTrial(self.directory, config, clock=self.clock).resume(unused), result)
        self.assertFalse(unused.calls)


for boundary in BOUNDARIES:
    def check(self, boundary=boundary):
        self.check_boundary(boundary)
    setattr(NativeCrashTests, "test_" + boundary.replace(":", "_"), check)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--crash":
    boundary, directory = sys.argv[2:]
    def terminate(point):
        if point == boundary:
            os._exit(23)
    config = configuration()
    trial = NativeTrial(directory, config, clock=support.Clock(), fault=terminate)
    trial.resume(ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile)}))
    raise SystemExit("crash boundary was not reached")
elif __name__ == "__main__":
    unittest.main()
