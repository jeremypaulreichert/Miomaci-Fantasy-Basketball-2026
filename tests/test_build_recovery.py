"""Evidence-bound pause adjudication, entirely offline."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_build_core as support
from test_build_native_trial import configuration, reply
from fantasy_basketball.draft.trial import NativeTrial, RecoveryDecision, recovery_report
from fantasy_basketball.providers.transport import ScriptedNativeTransport, HTTPTransport, LiveAuthorization


def paused_response(directory, config, clock, player="p05", **changes):
    trial = NativeTrial(directory, config, clock=clock)
    model = changes.pop("model", None)
    response = reply(config.cases[0].profile, player, **changes)
    if model:
        response["raw"]["model"] = model
    transport = ScriptedNativeTransport({"case:0:attempt:0": response})
    original = transport.send
    def send(*args):
        NativeTrial(directory, config, clock=clock).resume(ScriptedNativeTransport({}))
        return original(*args)
    transport.send = send
    trial.resume(transport)
    return trial


def decision(trial, action="SETTLE_SAVED", identity="decision-1"):
    return RecoveryDecision(identity, trial.config.hash, trial.explain_pause()["state_hash"], action, "TEST_OWNER")


class RecoveryTests(support.BuildCase):
    def test_saved_valid_answer_settles_offline_without_transport_or_new_reservation(self):
        trial = paused_response(self.directory, configuration(), self.clock)
        before = trial.status()
        report = trial.explain_pause()
        self.assertEqual(report["available_actions"], ["KEEP_PAUSED", "SETTLE_SAVED"])
        self.assertTrue(report["saved_response"])
        d = decision(trial)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch(
                "fantasy_basketball.providers.transport.subprocess.Popen", side_effect=AssertionError("worker forbidden")):
            result = trial.recover(d)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["results"][0]["action"]["player_id"], "p05")
        self.assertEqual(result["accounting"], before["accounting"])
        self.assertEqual(result["reserved_requests"], 1)
        unused = ScriptedNativeTransport({})
        self.assertEqual(NativeTrial(self.directory, trial.config, clock=self.clock).resume(unused), result)
        self.assertFalse(unused.calls)

    def test_unknown_without_saved_response_stays_paused_even_with_owner_reference(self):
        config = configuration()
        def stop(point):
            if point == "after:DISPATCH":
                NativeTrial(self.directory, config, clock=self.clock).resume(ScriptedNativeTransport({}))
        trial = NativeTrial(self.directory, config, clock=self.clock, fault=stop)
        trial.resume(ScriptedNativeTransport({}))
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])
        original = trial.journal.path.read_bytes()
        for action in ("SETTLE_SAVED", "AUTHORIZE_CORRECTION"):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                trial.recover(decision(trial, action))
        self.assertEqual(trial.journal.path.read_bytes(), original)

    def test_correction_is_explicit_same_ai_and_preserves_original_bounds(self):
        config = configuration()
        trial = paused_response(self.directory, config, self.clock, "p06")
        old = trial.journal.read()
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED", "AUTHORIZE_CORRECTION"])
        result = trial.recover(decision(trial, "AUTHORIZE_CORRECTION"))
        self.assertEqual(result["reserved_requests"], 1)  # authorization itself sends/reserves nothing
        self.assertEqual(result["results"], [])
        transport = ScriptedNativeTransport({"case:0:attempt:1": reply(config.cases[0].profile)})
        result = trial.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual([r["key"] for r in transport.calls], ["case:0:attempt:1"])
        request = transport.calls[0]["request"]
        previous = old["op"]["attempts"][0]["native_request"]
        for field in ("profile_hash", "board_hash", "deadline_ms"):
            self.assertEqual(request[field], previous[field])
        self.assertEqual(result["reserved_requests"], 2)

    def test_saved_settlement_survives_deadline_but_correction_does_not(self):
        for index, player in enumerate(("p05", "p06")):
            config = configuration(run_id=f"time-{index}")
            trial = paused_response(self.directory, config, self.clock, player)
            self.clock.advance(config.trial_ms + 1)
            if index == 0:
                self.assertEqual(trial.recover(decision(trial))["phase"], "COMPLETE")
            else:
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    trial.recover(decision(trial, "AUTHORIZE_CORRECTION"))

    def test_stale_or_cross_config_decision_cannot_apply(self):
        trial = paused_response(self.directory, configuration(), self.clock)
        stale = decision(trial)
        trial.recover(decision(trial, "KEEP_PAUSED", "kept"))
        with self.assertRaisesRegex(ValueError, "stale"):
            trial.recover(stale)
        with self.assertRaisesRegex(ValueError, "matching"):
            trial.recover(replace(decision(trial), config_hash="0" * 64))

    def test_identical_decision_is_idempotent_and_conflicting_id_is_rejected(self):
        trial = paused_response(self.directory, configuration(), self.clock)
        d = decision(trial)
        result = trial.recover(d)
        saved = trial.journal.path.read_bytes()
        self.assertEqual(trial.recover(d), result)
        self.assertEqual(trial.journal.path.read_bytes(), saved)
        self.assertEqual(len(trial.journal.read()["recoveries"]), 1)
        with self.assertRaisesRegex(ValueError, "reused"):
            trial.recover(replace(d, owner_record="DIFFERENT_OWNER"))

    def test_competing_controllers_settle_only_once(self):
        trial = paused_response(self.directory, configuration(), self.clock)
        d = decision(trial)
        def run(item):
            other = NativeTrial(self.directory, trial.config, clock=self.clock)
            try:
                other.recover(item)
                return "applied"
            except ValueError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [d, replace(d, decision_id="competitor")]))
        self.assertEqual(sorted(results), ["applied", "stale"])
        self.assertEqual(len(trial.journal.read()["recoveries"]), 1)
        self.assertEqual(len(trial.status()["results"]), 1)

    def test_keep_paused_preserves_every_pick_and_spending_entry(self):
        trial = paused_response(self.directory, configuration(), self.clock)
        before = trial.status()
        self.assertEqual(trial.recover(decision(trial, "KEEP_PAUSED")), before)
        self.assertEqual(trial.explain_pause()["decision_count"], 1)

    def test_nonrecoverable_failures_exhaustion_and_clock_rollback_remain_paused(self):
        for i, changes in enumerate(({"stop": "incomplete"}, {"model": "wrong-model"})):
            config = configuration(run_id=f"failure-{i}")
            # Mutate native envelope using the known native response structure.
            trial = paused_response(self.directory, config, self.clock, **changes)
            self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])
        config = configuration(run_id="exhausted")
        config = replace(config, cases=(replace(config.cases[0], profile=replace(config.cases[0].profile, max_attempts=1)),))
        trial = paused_response(self.directory, config, self.clock, "p06")
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])
        trial = paused_response(self.directory, configuration(run_id="rollback"), self.clock)
        self.clock.wall -= 1
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])

    def test_authorization_expiry_restricts_new_correction_not_local_settlement(self):
        for i, player in enumerate(("p05", "p06")):
            config = configuration(run_id=f"live-{i}", mode="LIVE_DIAGNOSTIC")
            self.clock.wall = time.time_ns() // 1000000
            permit = LiveAuthorization(config.hash, self.clock.wall + 1000, "TEST_OWNER", "TEST_REVIEW")
            trial = NativeTrial(self.directory, config, clock=self.clock)
            transport = HTTPTransport(config, permit, {"openai": "FAKE_KEY"})
            def returned(*args, **kwargs):
                trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
                return reply(config.cases[0].profile, player)
            with patch("fantasy_basketball.providers.transport.bounded_process", side_effect=returned):
                trial.resume(transport)
            self.clock.wall = permit.expires_ms
            if i == 0:
                self.assertEqual(trial.recover(decision(trial))["phase"], "COMPLETE")
            else:
                self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])

    def test_recovery_reports_budget_block_and_rejects_unknown_action_fields(self):
        trial = paused_response(self.directory, configuration(), self.clock, "p06")
        state = trial.journal.read()
        next(iter(state["accounts"].values()))["estimate"] = trial.config.budget_micro_usd + 1
        self.assertEqual(recovery_report(state, trial.config, self.clock.now_ms())["available_actions"], ["KEEP_PAUSED"])
        with self.assertRaises(ValueError):
            replace(decision(trial), action="RETRY_UNKNOWN")
        with self.assertRaises(ValueError):
            replace(decision(trial), owner_record="")

    def test_recovery_does_not_restart_an_already_running_controller(self):
        for i, player in enumerate(("p05", "p06")):
            config = configuration(run_id=f"running-{i}", names=("last_slot", "opening"))
            trial = NativeTrial(self.directory, config, clock=self.clock)
            transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile, player)})
            send = transport.send
            def paused_send(*args):
                trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
                return send(*args)
            transport.send = paused_send
            def after(point):
                if point == "after:RESPONSE":
                    trial.recover(decision(trial, "SETTLE_SAVED" if i == 0 else "AUTHORIZE_CORRECTION"))
            trial.fault = after
            result = trial.resume(transport)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(result["reserved_requests"], 1)
            self.assertEqual(result["phase"], "RUNNING")
            self.assertEqual(len(result["results"]), 1 if i == 0 else 0)

    def test_deadline_is_rechecked_when_applying_a_prepared_correction_decision(self):
        trial = paused_response(self.directory, configuration(), self.clock, "p06")
        d = decision(trial, "AUTHORIZE_CORRECTION")
        self.clock.advance(trial.config.trial_ms + 1)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            trial.recover(d)
        self.assertEqual(trial.explain_pause()["decision_count"], 0)

    def test_late_response_invalidates_an_older_decision_snapshot(self):
        config = configuration()
        def stop(point):
            if point == "after:DISPATCH":
                NativeTrial(self.directory, config, clock=self.clock).resume(ScriptedNativeTransport({}))
        trial = NativeTrial(self.directory, config, clock=self.clock, fault=stop)
        trial.resume(ScriptedNativeTransport({}))
        d = decision(trial, "KEEP_PAUSED")
        trial.journal.transact(lambda state: [("RESPONSE", {"at_ms": self.clock.now_ms(), "elapsed_ms": 0,
                                                          "response": reply(config.cases[0].profile)})])
        with self.assertRaisesRegex(ValueError, "stale"):
            trial.recover(d)
        self.assertIn("SETTLE_SAVED", trial.explain_pause()["available_actions"])

    def test_settled_earlier_cases_survive_recovery_and_exhausted_correction_pauses(self):
        config = configuration(names=("opening", "last_slot"))
        trial = NativeTrial(self.directory, config, clock=self.clock)
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile, "p04"),
                                             "case:1:attempt:0": reply(config.cases[1].profile, "p06")})
        send = transport.send
        def pause_second(key, *args):
            if key.startswith("case:1"):
                trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
            return send(key, *args)
        transport.send = pause_second
        before = trial.resume(transport)
        trial.recover(decision(trial, "AUTHORIZE_CORRECTION"))
        bad = ScriptedNativeTransport({"case:1:attempt:1": reply(config.cases[1].profile, "p06")})
        after = trial.resume(bad)
        self.assertEqual(after["results"], before["results"])
        self.assertEqual(after["pause"]["reason"], "ILLEGAL_PICK")
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])

    def test_explain_cli_is_read_only_and_recover_cli_never_dispatches(self):
        from native_trial import configuration as cli_configuration, FixedClock, STATE_ROOT
        root = Path(__file__).resolve().parents[1]
        run_id = self.directory.name
        config = cli_configuration(run_id, "unknown")
        directory = STATE_ROOT / "native-demo" / "unknown"
        trial = NativeTrial(directory, config, clock=FixedClock())
        # Remove only this test's uniquely named journal after all handles close.
        self.addCleanup(trial.journal.path.unlink)
        transport = ScriptedNativeTransport({"case:0:attempt:0": TimeoutError("synthetic")})
        trial.resume(transport)
        saved = trial.journal.path.read_bytes()
        def cli(command, extra=()):
            result = subprocess.run([sys.executable, "-B", str(root / "workstreams/build/native_trial.py"), command,
                                     "--run-id", run_id, "--scenario", "unknown", *extra],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        report = cli("explain")
        self.assertEqual(trial.journal.path.read_bytes(), saved)
        self.assertIn("uncertain", report["recovery"]["explanation"])
        output = cli("recover", ("--action", "KEEP_PAUSED", "--decision-id", "cli-decision",
                                  "--owner-record", "TEST_OWNER", "--expected-state-hash", report["recovery"]["state_hash"]))
        self.assertEqual(output["fixture_dispatches_this_command"], 0)
        self.assertEqual(output["status"]["phase"], "PAUSED")
        self.assertEqual(output["recovery"]["decision_count"], 1)


class RecoveryCrashTests(support.BuildCase):
    def test_real_crashes_before_and_after_recovery_commit(self):
        for action, player in (("SETTLE_SAVED", "p05"), ("AUTHORIZE_CORRECTION", "p06")):
            for boundary in ("before_commit:RECOVER", "after:RECOVER"):
                run_id = action.lower() + boundary.split(":")[0]
                config = configuration(run_id=run_id)
                trial = paused_response(self.directory, config, self.clock, player)
                d = decision(trial, action)
                child = subprocess.run([sys.executable, "-B", __file__, "--crash", str(self.directory), run_id, action, boundary],
                                       capture_output=True, text=True, timeout=20)
                self.assertEqual(child.returncode, 23, child.stderr)
                reopened = NativeTrial(self.directory, config, clock=self.clock)
                self.assertEqual(reopened.status()["phase"], "PAUSED" if boundary.startswith("before") else "COMPLETE" if action == "SETTLE_SAVED" else "RUNNING")
                reopened.recover(d)  # apply after rollback, or idempotent after commit
                self.assertEqual(len(reopened.journal.read()["recoveries"]), 1)
                unused = ScriptedNativeTransport({} if action == "SETTLE_SAVED" else {"case:0:attempt:1": reply(config.cases[0].profile)})
                self.assertEqual(reopened.resume(unused)["phase"], "COMPLETE")
                self.assertEqual(len(unused.calls), 0 if action == "SETTLE_SAVED" else 1)


if __name__ == "__main__" and len(sys.argv) == 6 and sys.argv[1] == "--crash":
    _, _, directory, run_id, action, boundary, *_ = sys.argv
    def kill(point):
        if point == boundary:
            os._exit(23)
    trial = NativeTrial(directory, configuration(run_id=run_id), clock=support.Clock(), fault=kill)
    trial.recover(decision(trial, action))
    raise SystemExit("crash not reached")
elif __name__ == "__main__":
    unittest.main()
