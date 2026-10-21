"""Permit/launch fencing with fake workers and credentials only."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import time
from unittest.mock import patch

import test_build_core as support
from test_build_research import action, board, native, ten_profiles
from fantasy_basketball.core.config import canonical
from fantasy_basketball.draft.research import ResearchConfig, ResearchOpportunity
from fantasy_basketball.providers.research_transport import LiveResearchTransport, ResearchPermit, code_snapshot


class ResearchTransportTests(support.BuildCase):
    def live(self):
        self.clock.wall = time.time_ns() // 1000000
        profile = ten_profiles()[0]
        cfg = ResearchConfig("research-transport", profile, board(), mode="LIVE_RESEARCH_PILOT",
                             schema="basketball-research-pilot/1", experiment_id="miomaci-basketball-research-pilot",
                             sources=code_snapshot())
        permit = ResearchPermit(cfg.hash, self.clock.wall - 1000, self.clock.wall + 60000,
                                cfg.budget_micro_usd, 6, 4, 4, "owner:fixture-not-approval", "review:fixture-not-acceptance", True)
        return cfg, permit

    def test_disabled_permit_rejects_before_credentials_or_request_intent(self):
        cfg, permit = self.live()
        getter = lambda p: self.fail("credential callback must not run")
        transport = LiveResearchTransport(cfg, replace(permit, paid_execution_authorized=False), getter)
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        with patch("fantasy_basketball.providers.research_transport.bounded_process") as worker:
            with self.assertRaises(ValueError):
                op.step(transport)
            worker.assert_not_called()
        self.assertEqual(op.status()["phase"], "NEW")
        self.assertEqual(op.status()["retained_micro_usd"], 0)

    def test_exact_model_request_worker_receipt_and_saved_settlement(self):
        cfg, permit = self.live()
        names = []
        def credential(provider):
            names.append(provider)
            return "FAKE-TEST-KEY"
        transport = LiveResearchTransport(cfg, permit, credential)
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        calls = []
        def worker(command, payload, timeout, output_limit, **kwargs):
            with kwargs["launch_guard"]() as allowed:
                self.assertTrue(allowed)
                calls.append((command, payload))
            response = native(cfg.profile, action())
            response.update(http_status=200, body_sha256="0" * 64)
            return response
        with patch("fantasy_basketball.providers.research_transport.bounded_process", side_effect=worker):
            op.step(transport)
            op.step(transport)
            self.clock.advance(61000)
            saved = op.step(transport)
        self.assertEqual(saved["phase"], "COMPLETE")
        self.assertEqual(names, ["openai"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("FAKE-TEST-KEY", repr(calls[0][0]))
        self.assertIn(b"FAKE-TEST-KEY", calls[0][1])  # stdin only
        self.assertNotIn("FAKE-TEST-KEY", canonical(saved))

    def test_approved_fixture_search_read_pick_uses_exact_service_host_worker(self):
        cfg, permit = self.live()
        names, commands, replies = [], [], [action("search", query="Research"), action("read", url="https://www.nba.com/player/example"), action()]
        def getter(provider):
            names.append(provider)
            return "FAKE-TEST-KEY"
        def worker(command, payload, timeout, output_limit, **kwargs):
            with kwargs["launch_guard"]() as allowed:
                self.assertTrue(allowed)
                commands.append(command)
            if command[-1].endswith("research_worker.py"):
                import json
                data = json.loads(payload)
                key = "content" if data["action"]["action"] == "search" else "raw_content"
                return {"outcome": "SERVICE_RESPONSE", "raw": {"results": [{"url": "https://www.nba.com/player/example", key: "Source facts"}], "usage": {"credits": 1}},
                        "retrieved_ms": self.clock.now_ms(), "http_status": 200, "body_sha256": "0" * 64}
            return native(cfg.profile, replies.pop(0))
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = LiveResearchTransport(cfg, permit, getter)
        with patch("fantasy_basketball.providers.research_transport.bounded_process", side_effect=worker):
            for _ in range(20):
                state = op.step(transport)
                if state["phase"] in {"COMPLETE", "PAUSED"}:
                    break
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(names, ["openai", "tavily", "openai", "tavily", "openai"])
        self.assertEqual(state["observed_service_credits"], "2")
        self.assertEqual(sum(c[-1].endswith("research_worker.py") for c in commands), 2)

    def test_changed_budget_request_credit_or_source_binding_rejects(self):
        cfg, permit = self.live()
        for changed in (replace(permit, budget_micro_usd=1), replace(permit, max_model_requests=7),
                        replace(permit, max_service_requests=5), replace(permit, reserved_service_credits=5),
                        replace(permit, config_hash="0" * 64)):
            with self.assertRaises(ValueError):
                changed.check(cfg, self.clock.wall)
        with patch("fantasy_basketball.providers.research_transport.code_snapshot", return_value=()):
            with self.assertRaises(ValueError):
                permit.check(cfg, self.clock.wall)
            with self.assertRaises(ValueError):
                ResearchOpportunity(self.directory, cfg, clock=self.clock)

    def test_expiry_between_intent_and_launch_prevents_credentials(self):
        cfg, permit = self.live()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = LiveResearchTransport(cfg, permit, lambda p: self.fail("no credential lookup after expiry"))
        op.step(transport)
        def expire(name):
            if name == "after_intent":
                self.clock.wall = permit.expires_ms
        op.fault = expire
        with patch("fantasy_basketball.providers.research_transport.time.time_ns", return_value=permit.expires_ms * 1000000), \
                patch("fantasy_basketball.providers.research_transport.bounded_process") as worker:
            with self.assertRaises(ValueError):
                op.step(transport)
            worker.assert_not_called()
        self.assertEqual(op.status()["phase"], "INFLIGHT")
        self.assertEqual(op.step(transport)["pause"], "UNKNOWN_DISPATCH")

    def test_valid_expiry_durably_pauses_before_new_request(self):
        cfg, permit = self.live()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = LiveResearchTransport(cfg, permit, lambda p: self.fail("no credential lookup"))
        op.step(transport)
        self.clock.wall = permit.expires_ms
        state = op.step(transport)
        self.assertEqual(state["pause"], "AUTHORIZATION_EXPIRED")
        self.assertEqual(state["retained_micro_usd"], 0)

    def test_source_change_before_saved_evidence_apply_rejects_without_events(self):
        cfg, permit = self.live()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = LiveResearchTransport(cfg, permit, lambda p: "FAKE-TEST-KEY")
        with patch("fantasy_basketball.providers.research_transport.bounded_process", return_value=native(cfg.profile, action())):
            op.step(transport)
            op.step(transport)
        before = op.journal.path.read_bytes()
        with patch("fantasy_basketball.providers.research_transport.code_snapshot", return_value=()):
            with self.assertRaises(ValueError):
                op.step(transport)
        self.assertEqual(op.journal.path.read_bytes(), before)

    def test_changed_recorded_permit_cannot_settle_or_renew_saved_response(self):
        cfg, permit = self.live()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = LiveResearchTransport(cfg, permit, lambda p: "FAKE-TEST-KEY")
        with patch("fantasy_basketball.providers.research_transport.bounded_process", return_value=native(cfg.profile, action())):
            op.step(transport)
            op.step(transport)
        before = op.journal.path.read_bytes()
        for changed in (replace(permit, expires_ms=permit.expires_ms + 1000),
                        replace(permit, owner_record="owner:different"), replace(permit, paid_execution_authorized=False)):
            with self.assertRaises(ValueError):
                op.step(LiveResearchTransport(cfg, changed, lambda p: self.fail("no keys")))
            self.assertEqual(op.journal.path.read_bytes(), before)
