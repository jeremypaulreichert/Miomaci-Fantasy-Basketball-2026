"""Offline adversarial checks at the native worker launch boundary."""
from contextlib import closing, contextmanager
import json
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

import test_build_core as support
from test_build_native_trial import configuration, reply
from fantasy_basketball.draft.trial import NativeTrial
from fantasy_basketball.providers.transport import HTTPTransport, LiveAuthorization, bounded_process


class FakeProcess:
    def __init__(self, response, on_input=lambda: None):
        self.response, self.on_input = response, on_input
        self.returncode = 0

    def communicate(self, data=None, timeout=None):
        if data is not None:
            self.on_input()
        return json.dumps(self.response).encode(), None

    def poll(self):
        return self.returncode


class LaunchGuardTests(support.BuildCase):
    def live(self):
        config = configuration(mode="LIVE_DIAGNOSTIC")
        self.clock.wall = time.time_ns() // 1000000
        permit = LiveAuthorization(config.hash, self.clock.wall + 60000, "TEST_OWNER", "TEST_REVIEW")
        trial = NativeTrial(self.directory, config, clock=self.clock)
        transport = HTTPTransport(config, permit, {"openai": "FAKE_CREDENTIAL"})
        return config, trial, transport

    def test_pause_after_final_read_but_before_worker_start_suppresses_launch(self):
        config, trial, transport = self.live()
        def intercepted(*args, **kwargs):
            trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
            return bounded_process(*args, **kwargs)
        with patch("fantasy_basketball.providers.transport.bounded_process", side_effect=intercepted), patch(
                "fantasy_basketball.providers.transport.subprocess.Popen",
                return_value=FakeProcess(reply(config.cases[0].profile))) as popen:
            result = trial.resume(transport)
        self.assertEqual(popen.call_count, 0)
        self.assertEqual(result["phase"], "PAUSED")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["attempts"][0]["state"], "DISPATCHED")
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], config.cases[0].profile.bound_micro_usd)
        self.assertEqual(NativeTrial(self.directory, config, clock=self.clock).resume(transport), result)

    def test_launch_excludes_pause_writer_but_response_wait_does_not(self):
        config, trial, transport = self.live()
        observed = []
        def popen(*args, **kwargs):
            with closing(sqlite3.connect(trial.journal.path, timeout=0)) as other:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")
            # Ordinary status reads still work during the short startup lock.
            self.assertEqual(trial.status()["phase"], "RUNNING")
            observed.append("started")
            def on_input():
                trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
                observed.append("paused")
            return FakeProcess(reply(config.cases[0].profile), on_input)
        with patch("fantasy_basketball.providers.transport.subprocess.Popen", side_effect=popen) as launch:
            result = trial.resume(transport)
        self.assertEqual(observed, ["started", "paused"])
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result["phase"], "PAUSED")
        self.assertFalse(result["results"])
        self.assertGreater(result["accounting"]["estimated_charge_micro_usd"], 0)
        self.assertEqual(result["accounting"]["invoice_confirmed_micro_usd"], 0)

    def test_guard_wait_cannot_extend_monotonic_worker_budget(self):
        @contextmanager
        def delayed_guard():
            yield True
        with patch("fantasy_basketball.providers.transport.time.monotonic", side_effect=[0, 2]), patch(
                "fantasy_basketball.providers.transport.subprocess.Popen") as launch:
            outcome = bounded_process(["unused"], b"", 1000, 1000, launch_guard=delayed_guard)
        self.assertEqual(outcome, {"outcome": "WORKER_TIMEOUT"})
        launch.assert_not_called()

    def test_expired_authority_during_guard_wait_cannot_start_worker(self):
        config, trial, transport = self.live()
        original = trial.journal.guard
        @contextmanager
        def delayed_guard():
            with original() as state:
                with patch("fantasy_basketball.providers.transport.time.time_ns",
                           return_value=transport.authorization.expires_ms * 1000000):
                    yield state
        with patch.object(trial.journal, "guard", delayed_guard), patch(
                "fantasy_basketball.providers.transport.subprocess.Popen") as launch:
            result = trial.resume(transport)
        launch.assert_not_called()
        self.assertEqual(result["phase"], "PAUSED")
        self.assertFalse(result["results"])

    def test_child_crash_releases_launch_lock_and_restart_never_resends(self):
        config, trial, transport = self.live()
        class Stopped(BaseException):
            pass
        def stop(point):
            if point == "after:DISPATCH":
                raise Stopped()
        trial.fault = stop
        with self.assertRaises(Stopped):
            trial.resume(transport)
        script = """
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
import test_build_core
from test_build_native_trial import configuration
from fantasy_basketball.draft.trial import NativeTrial
t = NativeTrial(sys.argv[1], configuration(mode='LIVE_DIAGNOSTIC'))
with t.journal.guard():
    print('LOCKED', flush=True)
    sys.stdin.buffer.read(1)
"""
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", script, str(self.directory)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            # The child's only output is one small readiness line. A bounded
            # reader keeps a failed startup from hanging the test runner.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                ready = pool.submit(child.stdout.readline)
                try:
                    self.assertEqual(ready.result(timeout=10), b"LOCKED\r\n" if sys.platform == "win32" else b"LOCKED\n")
                    with closing(sqlite3.connect(trial.journal.path, timeout=0)) as other:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            other.execute("BEGIN IMMEDIATE")
                    self.assertEqual(trial.status()["phase"], "RUNNING")
                finally:
                    child.kill()
                    child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        with patch("fantasy_basketball.providers.transport.subprocess.Popen") as launch:
            result = NativeTrial(self.directory, config, clock=self.clock).resume(transport)
        launch.assert_not_called()
        self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH")
        self.assertEqual(result["reserved_requests"], 1)

    def test_failed_process_creation_releases_guard_and_pauses_safely(self):
        config, trial, transport = self.live()
        with patch("fantasy_basketball.providers.transport.subprocess.Popen", side_effect=OSError("PRIVATE ERROR")):
            result = trial.resume(transport)
        self.assertEqual(result["pause"]["reason"], "TRANSPORT_ERROR")
        self.assertNotIn("PRIVATE ERROR", json.dumps(result))
        with closing(sqlite3.connect(trial.journal.path, timeout=0)) as other:
            other.execute("BEGIN IMMEDIATE")
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], config.cases[0].profile.bound_micro_usd)

    def test_guard_exit_failure_reaps_started_child_without_sending_payload(self):
        @contextmanager
        def broken_exit():
            yield True
            raise RuntimeError("gate release failure")
        from unittest.mock import Mock
        process = Mock()
        process.poll.return_value = None
        with patch("fantasy_basketball.providers.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "gate release failure"):
                bounded_process(["unused"], b"SECRET", 1000, 1000, launch_guard=broken_exit)
        process.kill.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=5)

