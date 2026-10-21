"""Regressions for independent findings B1-B3; all responses are synthetic."""
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

import test_build_core as support
from test_build_native_trial import configuration, reply
from fantasy_basketball.draft.trial import NativeTrial
from fantasy_basketball.draft.opportunity import Draft
from fantasy_basketball.providers.fixtures import FixtureTransport, response
from fantasy_basketball.providers.transport import ScriptedNativeTransport, LiveAuthorization, HTTPTransport


class StopAfterResponse(BaseException):
    pass


def stop_after_response(point):
    if point == "after:RESPONSE":
        raise StopAfterResponse()


class ReviewRepairTests(support.BuildCase):
    def saved_native(self, config, player="p05", transport=None):
        trial = NativeTrial(self.directory, config, clock=self.clock, fault=stop_after_response)
        transport = transport or ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile, player)})
        with self.assertRaises(StopAfterResponse):
            trial.resume(transport)
        return trial

    def test_b1_pause_after_intent_prevents_avoidable_send(self):
        config = configuration()
        def fault(point):
            if point == "after:DISPATCH":
                other = NativeTrial(self.directory, config, clock=self.clock)
                paused = other.resume(ScriptedNativeTransport({}))
                self.assertEqual(paused["pause"]["reason"], "UNKNOWN_DISPATCH")
        trial = NativeTrial(self.directory, config, clock=self.clock, fault=fault)
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile)})
        result = trial.resume(transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(result["phase"], "PAUSED")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], config.cases[0].profile.bound_micro_usd)
        self.assertEqual(trial.journal.read()["op"]["attempts"][0]["state"], "DISPATCHED")
        self.assertEqual(NativeTrial(self.directory, config, clock=self.clock).resume(transport), result)
        self.assertEqual(transport.calls, [])

    def test_b2_saved_valid_answer_resolves_after_case_or_global_deadline(self):
        for index, after_global in enumerate((False, True)):
            self.clock = support.Clock()
            config = configuration(run_id=f"delayed-valid-{index}", trial_ms=500000)
            trial = self.saved_native(config)
            state = trial.journal.read()
            self.clock.wall = (state["deadline_ms"] if after_global else state["op"]["deadline_ms"]) + 1
            unused = ScriptedNativeTransport({})
            result = NativeTrial(self.directory, config, clock=self.clock).resume(unused)
            self.assertEqual(result["phase"], "COMPLETE")
            self.assertEqual(result["results"][0]["action"]["player_id"], "p05")
            self.assertFalse(unused.calls)

    def test_b2_local_settlement_does_not_extend_deadline_for_next_case(self):
        config = configuration(names=("last_slot", "opening"), trial_ms=500000)
        trial = self.saved_native(config)
        self.clock.wall = trial.journal.read()["deadline_ms"] + 1
        unused = ScriptedNativeTransport({})
        result = NativeTrial(self.directory, config, clock=self.clock).resume(unused)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["pause"], {"reason": "DEADLINE", "case_index": 1})
        self.assertEqual(result["reserved_requests"], 1)
        self.assertFalse(unused.calls)

    def test_b2_invalid_saved_answer_gets_no_correction_after_deadline(self):
        config = configuration(trial_ms=500000)
        trial = self.saved_native(config, "p06")
        self.clock.wall = trial.journal.read()["op"]["deadline_ms"] + 1
        unused = ScriptedNativeTransport({})
        result = NativeTrial(self.directory, config, clock=self.clock).resume(unused)
        self.assertEqual(result["pause"]["reason"], "DEADLINE")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["reserved_requests"], 1)
        self.assertFalse(unused.calls)

    def test_b2_late_receipt_is_not_redeemed_by_replay(self):
        config = configuration()
        transport = ScriptedNativeTransport({"case:0:attempt:0": reply(config.cases[0].profile)})
        original = transport.send
        def send(*args):
            # Wall time alone reaches the request ceiling; monotonic evidence
            # cannot override a recorded wall-time lateness.
            self.clock.wall += config.cases[0].profile.timeout_ms
            return original(*args)
        transport.send = send
        trial = self.saved_native(config, transport=transport)
        result = NativeTrial(self.directory, config, clock=self.clock).resume(ScriptedNativeTransport({}))
        self.assertEqual(result["pause"]["reason"], "DEADLINE")
        self.assertFalse(result["results"])
        self.assertIsNotNone(trial.journal.read()["op"]["attempts"][0]["receipt"])

    def test_b2_replay_preserves_clock_rollback_and_existing_pause(self):
        for index, prior_pause in enumerate((False, True)):
            self.clock = support.Clock()
            config = configuration(run_id=f"replay-guard-{index}")
            trial = self.saved_native(config)
            if prior_pause:
                trial.journal.transact(lambda state: [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})])
                self.clock.wall += config.trial_ms + 1
            else:
                self.clock.wall -= 1
            unused = ScriptedNativeTransport({})
            result = NativeTrial(self.directory, config, clock=self.clock).resume(unused)
            self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH" if prior_pause else "CLOCK_ROLLBACK")
            self.assertFalse(result["results"])
            self.assertFalse(unused.calls)

    def test_b2_expired_authority_allows_only_saved_local_resolution(self):
        for index, player in enumerate(("p05", "p06")):
            config = configuration(run_id=f"expired-replay-{index}", mode="LIVE_DIAGNOSTIC")
            self.clock = support.Clock()
            self.clock.wall = time.time_ns() // 1000000
            permit = LiveAuthorization(config.hash, self.clock.wall + 10000, "TEST_OWNER", "TEST_REVIEW")
            transport = HTTPTransport(config, permit, {"openai": "FAKE_CREDENTIAL"})
            trial = NativeTrial(self.directory, config, clock=self.clock, fault=stop_after_response)
            with patch("fantasy_basketball.providers.transport.bounded_process", return_value=reply(config.cases[0].profile, player)) as worker:
                with self.assertRaises(StopAfterResponse):
                    trial.resume(transport)
                self.assertEqual(worker.call_count, 1)
                self.clock.wall = permit.expires_ms + 1
                reopened = NativeTrial(self.directory, config, clock=self.clock)
                result = reopened.resume(transport)
                self.assertEqual(worker.call_count, 1)
            self.assertEqual(result["reserved_requests"], 1)
            if index == 0:
                self.assertEqual(result["phase"], "COMPLETE")
                self.assertEqual(len(result["results"]), 1)
            else:
                self.assertEqual(result["pause"]["reason"], "AUTHORIZATION_EXPIRED")
                self.assertFalse(result["results"])

    def fixture_configuration(self, run_id):
        profile = replace(self.config.providers[0], fixed_micro_usd=0, safety_micro_usd=0)
        return replace(self.config, run_id=run_id, providers=(profile,))

    def test_b3_late_usage_is_accounted_once_without_clearing_pause(self):
        for inp in (50, 9000):
            config = self.fixture_configuration(f"late-usage-{inp}")
            profile = config.providers[0]
            draft = self.draft(config)
            draft.open(["p1"])
            transport = FixtureTransport({"pick:0:attempt:0": response(profile, "p1", input_tokens=inp, output_tokens=20)})
            original = transport.send
            def send(*args):
                paused = self.draft(config).resume(FixtureTransport({}))
                self.assertEqual(paused["pause"]["reason"], "UNKNOWN_DISPATCH")
                return original(*args)
            transport.send = send
            result = draft.resume(transport)
            expected = inp + 20 * 2  # independent reference, no production charge call
            self.assertEqual(result["accounting"]["estimated_charge_micro_usd"], expected)
            self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], max(4296, expected))
            self.assertEqual(result["accounting"]["committed_micro_usd"], max(4296, expected))
            self.assertEqual(result["accounting"]["invoice_confirmed_micro_usd"], 0)
            self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH")
            self.assertFalse(result["picks"])
            self.assertEqual(len(transport.calls), 1)
            unused = FixtureTransport({})
            for _ in range(2):
                self.assertEqual(self.draft(config).resume(unused), result)
            self.assertFalse(unused.calls)

    def test_b3_accounting_survives_crash_before_resolution_without_double_count(self):
        config = self.fixture_configuration("usage-before-resolve")
        draft = self.draft(config, fault=stop_after_response)
        draft.open(["p1"])
        transport = FixtureTransport({"pick:0:attempt:0": response(config.providers[0], "p1", input_tokens=50, output_tokens=20)})
        with self.assertRaises(StopAfterResponse):
            draft.resume(transport)
        self.assertEqual(draft.status()["accounting"]["estimated_charge_micro_usd"], 90)
        unused = FixtureTransport({})
        result = self.draft(config).resume(unused)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["accounting"]["estimated_charge_micro_usd"], 90)
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4296)
        self.assertFalse(unused.calls)

    def test_b3_unverified_late_usage_cannot_create_an_estimate(self):
        for index, mutation in enumerate(({"model": "fixture-wrong"}, {"usage": None})):
            config = self.fixture_configuration(f"unverified-usage-{index}")
            draft = self.draft(config)
            draft.open(["p1"])
            raw = response(config.providers[0], "p1", input_tokens=9000)
            raw.update(mutation)
            transport = FixtureTransport({"pick:0:attempt:0": raw})
            original = transport.send
            def send(*args):
                self.draft(config).resume(FixtureTransport({}))
                return original(*args)
            transport.send = send
            result = draft.resume(transport)
            self.assertEqual(result["accounting"]["estimated_charge_micro_usd"], 0)
            self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4296)
            self.assertEqual(result["phase"], "PAUSED")


if __name__ == "__main__":
    unittest.main()
