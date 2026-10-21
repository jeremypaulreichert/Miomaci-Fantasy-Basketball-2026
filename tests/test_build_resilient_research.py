"""Opt-in bounded model recovery with immutable evidence; offline only."""
from dataclasses import replace

import test_build_core as support
from test_build_research import ten_profiles, board, action, native
from fantasy_basketball.draft.research import ScriptedResearchTransport
from fantasy_basketball.core.config import digest
from resilient_research import RecoveryConfig, RecoveryOpportunity


class RecoveryTests(support.BuildCase):
    def configuration(self, provider=None, **changes):
        profile = next((p for p in ten_profiles() if p.provider == provider), ten_profiles()[0])
        return replace(RecoveryConfig("research-recovery-test", profile, board(profile.provider),
                                     max_model_calls=7), **changes)

    def run_to_pause(self, cfg, transport):
        op = RecoveryOpportunity(self.directory, cfg, clock=self.clock)
        for _ in range(3):
            state = op.step(transport)
        return op, state

    def test_confirmed_http_error_one_delayed_retry_and_complete_with_retained_cost(self):
        cfg = self.configuration()
        transport = ScriptedResearchTransport({"model:0": {"outcome": "HTTP_ERROR", "http_status": 503},
                                               "model:1": native(cfg.profile, action())})
        op, state = self.run_to_pause(cfg, transport)
        self.assertEqual(state["phase"], "PAUSED")
        self.assertEqual(op.step(transport), state)
        self.assertEqual(len(transport.calls), 1)
        self.clock.advance(cfg.retry_delay_ms)
        self.assertEqual(op.step(transport)["phase"], "READY_MODEL")
        self.assertEqual(len(transport.calls), 1)
        # Cold restart after the recorded recovery boundary, before dispatch.
        op = RecoveryOpportunity(self.directory, cfg, clock=self.clock)
        op.step(transport)
        state = op.step(transport)
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["total_counts"]["model"], 2)
        self.assertEqual(state["retained_micro_usd"], 2 * cfg.profile.bound_micro_usd)
        self.assertEqual(len(state["retry_history"]), 1)
        self.assertEqual(op.report()["settled_without_recorded_pause"], 0)
        before = op.journal.path.read_bytes()
        fresh = ScriptedResearchTransport({})
        self.assertEqual(RecoveryOpportunity(self.directory, cfg, clock=self.clock).step(fresh), state)
        self.assertFalse(fresh.calls)
        self.assertEqual(op.journal.path.read_bytes(), before)

    def test_second_confirmed_error_stays_paused_without_third_call(self):
        cfg = self.configuration()
        transport = ScriptedResearchTransport({f"model:{i}": {"outcome": "HTTP_ERROR", "http_status": 429} for i in range(2)})
        op, _ = self.run_to_pause(cfg, transport)
        self.clock.advance(cfg.retry_delay_ms)
        for _ in range(5): op.step(transport)
        state = op.status()
        self.assertEqual(state["phase"], "PAUSED")
        self.assertEqual(state["retry_used"], 1)
        self.assertEqual(len(state["failure_history"]), 2)
        self.assertEqual(len(transport.calls), 2)

    def test_authentication_network_timeouts_and_unrecognized_results_are_not_retried(self):
        for index, failure in enumerate(({"outcome": "HTTP_ERROR", "http_status": 401},
                                         {"outcome": "HTTP_ERROR", "http_status": 403},
                                         {"outcome": "NETWORK_ERROR"}, {"outcome": "WORKER_TIMEOUT"},
                                         None)):
            cfg = self.configuration(run_id=f"research-recovery-negative-{index}")
            transport = ScriptedResearchTransport({"model:0": failure})
            op, state = self.run_to_pause(cfg, transport)
            self.clock.advance(cfg.retry_delay_ms)
            self.assertEqual(op.step(transport), state)
            self.assertEqual(len(transport.calls), 1)
            self.assertIsNone(state["retry_candidate"])

    def test_real_shaped_gemini_failure_is_eligible_but_usage_is_not_invented(self):
        cfg = self.configuration("gemini")
        failed = {"outcome": "RESPONSE", "http_status": 200, "raw": {
            "modelVersion": cfg.profile.model, "responseId": "synthetic-failed-id",
            "usageMetadata": {"promptTokenCount": 73805, "totalTokenCount": 73805},
            "candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL", "content": {"role": "model", "parts": []}}]}}
        transport = ScriptedResearchTransport({"model:0": failed, "model:1": native(cfg.profile, action())})
        op, state = self.run_to_pause(cfg, transport)
        self.assertEqual(state["pause"], "MISSING_OR_INVALID_USAGE")
        self.assertIsNone(state["attempts"][0]["native_receipt"]["usage"])
        self.assertEqual(state["estimated_model_micro_usd"], 0)
        self.clock.advance(cfg.retry_delay_ms)
        for _ in range(3): state = op.step(transport)
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["failure_history"][0]["provider_finish_reasons"], ["MALFORMED_FUNCTION_CALL"])
        self.assertEqual(state["retained_micro_usd"], 2 * cfg.profile.bound_micro_usd)

    def test_unknown_dispatch_after_retry_is_not_retried_again(self):
        cfg = self.configuration()
        transport = ScriptedResearchTransport({"model:0": {"outcome": "HTTP_ERROR", "http_status": 503},
                                               "model:1": native(cfg.profile, action())})
        op, _ = self.run_to_pause(cfg, transport)
        self.clock.advance(cfg.retry_delay_ms)
        op.step(transport)
        def crash(boundary):
            if boundary == "after_send": raise RuntimeError("synthetic loss")
        op.fault = crash
        with self.assertRaises(RuntimeError): op.step(transport)
        cold = RecoveryOpportunity(self.directory, cfg, clock=self.clock)
        fresh = ScriptedResearchTransport({})
        state = cold.step(fresh)
        self.assertEqual(state["pause"], "UNKNOWN_DISPATCH")
        self.assertIsNone(state["retry_candidate"])
        self.clock.advance(cfg.retry_delay_ms)
        cold.step(fresh)
        self.assertFalse(fresh.calls)

    def test_expiry_blocks_recovery_and_recovery_event_rejects_stale_state(self):
        cfg = self.configuration()
        op, state = self.run_to_pause(cfg, ScriptedResearchTransport({"model:0": {"outcome": "HTTP_ERROR", "http_status": 500}}))
        self.clock.advance(cfg.retry_delay_ms)
        with self.assertRaises(ValueError):
            op.journal.transact(lambda s: [("RETRY_MODEL", {"at_ms": self.clock.now_ms(), "expected_state_hash": "0" * 64})])
        self.clock.wall = state["deadline_ms"]
        transport = ScriptedResearchTransport({})
        self.assertEqual(op.step(transport)["phase"], "PAUSED")
        self.assertFalse(transport.calls)

    def test_first_unknown_dispatch_is_never_retried_despite_unused_allowance(self):
        cfg = self.configuration()
        def crash(boundary):
            if boundary == "after_intent": raise RuntimeError("synthetic interruption")
        op = RecoveryOpportunity(self.directory, cfg, clock=self.clock, fault=crash)
        transport = ScriptedResearchTransport({})
        op.step(transport)
        with self.assertRaises(RuntimeError): op.step(transport)
        cold = RecoveryOpportunity(self.directory, cfg, clock=self.clock)
        state = cold.step(transport)
        self.assertEqual(state["pause"], "UNKNOWN_DISPATCH")
        self.assertEqual(state["retry_used"], 0)
        self.assertIsNone(state["retry_candidate"])
        self.clock.advance(cfg.retry_delay_ms)
        cold.step(transport)
        self.assertFalse(transport.calls)

    def test_caps_namespace_live_transport_and_policy_changes_reject(self):
        cfg = self.configuration()
        with self.assertRaises(ValueError): replace(cfg, max_model_calls=6)
        with self.assertRaises(ValueError): replace(cfg, max_model_retries=2)
        with self.assertRaises(ValueError): replace(cfg, run_id="research-wrong")
        with self.assertRaises(ValueError): replace(cfg, budget_micro_usd=1)
        op = RecoveryOpportunity(self.directory, cfg, clock=self.clock)
        with self.assertRaises(ValueError): op.step(object())
        with self.assertRaises(ValueError):
            RecoveryOpportunity(self.directory, replace(cfg, retry_delay_ms=10000), clock=self.clock)
        self.assertNotEqual(cfg.hash, replace(cfg, retry_delay_ms=10000).hash)

    def test_confirmed_error_with_no_time_for_backoff_stays_paused(self):
        cfg = self.configuration()
        op = RecoveryOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport({"model:0": {"outcome": "HTTP_ERROR", "http_status": 503}})
        started = op.step(transport)
        self.clock.wall = started["deadline_ms"] - 100
        op.step(transport)
        state = op.step(transport)
        self.assertEqual(state["phase"], "PAUSED")
        self.assertIsNone(state["retry_candidate"])
        self.assertEqual(state["retry_used"], 0)
        self.assertEqual(len(transport.calls), 1)

    def test_gemini_reported_input_over_bound_is_not_hidden_by_missing_output_usage(self):
        cfg = self.configuration("gemini")
        failed = {"outcome": "RESPONSE", "http_status": 200, "raw": {
            "modelVersion": cfg.profile.model, "responseId": "synthetic-invalid-usage",
            "usageMetadata": {"promptTokenCount": cfg.profile.input_tokens + 1,
                              "totalTokenCount": cfg.profile.input_tokens + 1},
            "candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL", "content": {"role": "model", "parts": []}}]}}
        transport = ScriptedResearchTransport({"model:0": failed})
        op, state = self.run_to_pause(cfg, transport)
        self.assertEqual(state["phase"], "PAUSED")
        self.assertIsNone(state["retry_candidate"])
        self.clock.advance(cfg.retry_delay_ms)
        self.assertEqual(op.step(transport), state)
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
