"""Regression for Review's recovery decision time-floor finding."""
import test_build_core as support
import time
from unittest.mock import patch
from test_build_native_trial import configuration, reply
from test_build_recovery import paused_response, decision
from fantasy_basketball.draft.trial import NativeTrial, reduce_event
from fantasy_basketball.providers.transport import HTTPTransport, LiveAuthorization


class RecoveryClockTests(support.BuildCase):
    def legacy_trial(self):
        trial = paused_response(self.directory, configuration(), self.clock, "p05")
        # Reproduce the unreleased V4 reader's exact KEEP_PAUSED behavior.
        def legacy_reduce(state, kind, data, config):
            before = state["last_ms"]
            reduce_event(state, kind, data, config)
            if kind == "RECOVER" and data["decision"]["action"] == "KEEP_PAUSED":
                state["last_ms"] = before
        trial.journal.reducer = legacy_reduce
        return trial

    def test_keep_paused_records_time_floor_and_cannot_revive_expired_correction(self):
        trial = paused_response(self.directory, configuration(), self.clock, "p06")
        original_time = self.clock.wall
        self.clock.wall = trial.journal.read()["op"]["deadline_ms"] + 1
        observed_time = self.clock.wall
        trial.recover(decision(trial, "KEEP_PAUSED", "after-expiry"))
        self.assertEqual(trial.journal.read()["last_ms"], observed_time)
        self.clock.wall = original_time
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            trial.recover(decision(trial, "AUTHORIZE_CORRECTION", "rollback-attempt"))
        self.assertEqual(trial.status()["reserved_requests"], 1)

    def test_single_legacy_keep_replays_with_safe_floor_without_rewriting_bytes(self):
        trial = self.legacy_trial()
        self.clock.advance(100)
        trial.recover(decision(trial, "KEEP_PAUSED", "legacy-keep"))
        before = trial.journal.path.read_bytes()
        reopened = NativeTrial(self.directory, trial.config, clock=self.clock)
        self.assertEqual(reopened.journal.read()["last_ms"], self.clock.wall)
        self.assertEqual(reopened.journal.path.read_bytes(), before)
        self.clock.wall -= 1
        self.assertEqual(reopened.explain_pause()["available_actions"], ["KEEP_PAUSED"])

    def test_legacy_hash_chain_depending_on_bug_fails_closed_without_migration(self):
        trial = self.legacy_trial()
        for i in range(2):
            self.clock.advance(100)
            trial.recover(decision(trial, "KEEP_PAUSED", f"legacy-keep-{i}"))
        before = trial.journal.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "stale"):
            NativeTrial(self.directory, trial.config, clock=self.clock)
        self.assertEqual(trial.journal.path.read_bytes(), before)

    def test_backward_keep_paused_cannot_lower_an_existing_time_floor(self):
        trial = paused_response(self.directory, configuration(), self.clock, "p05")
        original_time = self.clock.wall
        self.clock.advance(100)
        trial.recover(decision(trial, "KEEP_PAUSED", "later-observation"))
        self.clock.wall = original_time
        trial.recover(decision(trial, "KEEP_PAUSED", "backward-observation"))
        self.assertEqual(trial.journal.read()["last_ms"], original_time + 100)
        self.assertEqual(trial.explain_pause()["available_actions"], ["KEEP_PAUSED"])

    def test_recorded_live_authority_expiry_cannot_be_reversed_by_clock_rollback(self):
        config = configuration(mode="LIVE_DIAGNOSTIC")
        self.clock.wall = time.time_ns() // 1000000
        original_time = self.clock.wall
        permit = LiveAuthorization(config.hash, original_time + 1000, "TEST_OWNER", "TEST_REVIEW")
        trial = NativeTrial(self.directory, config, clock=self.clock)
        transport = HTTPTransport(config, permit, {"openai": "FAKE_KEY"})
        def returned(*args, **kwargs):
            trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
            return reply(config.cases[0].profile, "p06")
        with patch("fantasy_basketball.providers.transport.bounded_process", side_effect=returned):
            trial.resume(transport)
        self.clock.wall = permit.expires_ms + 1
        trial.recover(decision(trial, "KEEP_PAUSED", "expired-live-authority"))
        self.clock.wall = original_time
        with self.assertRaisesRegex(ValueError, "unavailable"):
            trial.recover(decision(trial, "AUTHORIZE_CORRECTION", "rollback-live-attempt"))
        self.assertEqual(trial.status()["reserved_requests"], 1)
