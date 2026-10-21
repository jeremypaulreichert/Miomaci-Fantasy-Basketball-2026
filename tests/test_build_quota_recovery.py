"""Offline quota proposal; fresh synthetic journals, no accepted runtime edits."""
from dataclasses import fields, replace
from unittest.mock import patch

import test_build_core as support
import test_build_incident_recovery as incidents
from test_build_research import action, native, evidence, URL
from fantasy_basketball.draft.research import ScriptedResearchTransport
from quota_recovery import QuotaConfig, QuotaOpportunity


class QuotaTests(support.BuildCase):
    def quota_config(self, **changes):
        old = incidents.IncidentTests.incident_config(self, "mistral")
        values = {f.name: getattr(old, f.name) for f in fields(old)}
        values.update(run_id="research-incident-quota-fixture", **changes)
        return QuotaConfig(**values)

    def fixtures(self, cfg, *, repeat=False, illegal_after=False):
        p = cfg.profile
        return {"pick:0:model:0": native(p, action("search", query="one")),
                "pick:0:search:0": evidence(),
                "pick:0:model:1": native(p, action("search", query="two")),
                "pick:0:search:1": evidence(),
                "pick:0:model:2": native(p, action("search", query="rejected third")),
                "pick:0:model:3": native(p, action("search", query="four") if repeat else action("read", url=URL)),
                "pick:0:read:0": evidence(),
                "pick:0:model:4": native(p, action(player_id="illegal") if illegal_after else action())}

    def run_to_stop(self, op, transport):
        for _ in range(80):
            state = op.step(transport)
            if state["phase"] == "COMPLETE":
                return state
            if state["phase"] == "PAUSED" and not any(state[k] for k in
                    ("quota_candidate", "retry_candidate", "incident_candidate")):
                return state
        self.fail("did not terminate")

    def test_same_ai_recovers_without_third_search_and_cold_restart_is_idle(self):
        cfg = self.quota_config()
        op = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport(self.fixtures(cfg))
        with patch("socket.socket", side_effect=AssertionError("offline")):
            state = self.run_to_stop(op, transport)
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["total_counts"], dict(model=5, search=2, read=1))
        self.assertEqual(state["corrections"], 1)
        self.assertEqual(len(state["quota_recoveries"]), 1)
        self.assertEqual(state["retained_micro_usd"], 5*cfg.profile.bound_micro_usd+3*cfg.tool_bound_micro_usd)
        self.assertEqual(state["attempts"][4]["native_receipt"]["action"]["query"], "rejected third")
        feedback = next(h for h in state["history"] if h["kind"] == "quota_correction")
        self.assertEqual(feedback["remaining_allowance"]["searches"], 0)
        before = op.journal.path.read_bytes()
        fresh = ScriptedResearchTransport({})
        self.assertEqual(QuotaOpportunity(self.directory, cfg, clock=self.clock).step(fresh), state)
        self.assertFalse(fresh.calls)
        self.assertEqual(before, op.journal.path.read_bytes())

    def test_repeated_quota_action_exhausts_shared_pool(self):
        cfg = self.quota_config()
        state = self.run_to_stop(QuotaOpportunity(self.directory, cfg, clock=self.clock),
                                 ScriptedResearchTransport(self.fixtures(cfg, repeat=True)))
        self.assertEqual((state["phase"], state["pause"]), ("PAUSED", "RESEARCH_LIMIT"))
        self.assertEqual(state["counts"], dict(model=4, search=2, read=0))
        self.assertFalse(state["quota_candidate"])

    def test_quota_correction_leaves_no_second_illegal_pick_correction(self):
        cfg = self.quota_config()
        fixtures = self.fixtures(cfg, illegal_after=True)
        # Stop-path audit (October 19): after the shared correction pool is used, a second illegal pick
        # gets the one format correction; a third illegal pick still stops.
        fixtures["pick:0:model:5"] = native(cfg.profile, action(player_id="illegal"))
        state = self.run_to_stop(QuotaOpportunity(self.directory, cfg, clock=self.clock),
                                 ScriptedResearchTransport(fixtures))
        self.assertEqual(state["pause"], "ILLEGAL_PICK")
        self.assertEqual(state["corrections"], 1)
        self.assertEqual(state["format_corrections_used"], 1)
        self.assertEqual(state["results"], [])

    def test_restart_at_quota_pause_preserves_feedback_and_expiry_blocks(self):
        cfg = self.quota_config()
        op = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport(self.fixtures(cfg))
        for _ in range(30):
            state = op.step(transport)
            if state["quota_candidate"]:
                break
        self.assertTrue(state["quota_candidate"])
        fresh = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        self.clock.advance(cfg.opportunity_ms)
        self.assertEqual(fresh.step(ScriptedResearchTransport({})), state)

    def test_cold_restart_can_recover_once_and_preserves_original_failure(self):
        cfg = self.quota_config()
        op = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport(self.fixtures(cfg))
        for _ in range(30):
            state = op.step(transport)
            if state["quota_candidate"]:
                break
        original = state["attempts"][-1]
        fresh = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        state = self.run_to_stop(fresh, transport)
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["attempts"][4], original)
        self.assertEqual(len(state["quota_recoveries"]), 1)
        self.assertEqual(sum(h["kind"] == "quota_correction" for h in state["history"]), 1)

    def test_stale_budget_and_history_guards_refuse_recovery(self):
        from copy import deepcopy
        from fantasy_basketball.core.config import digest
        from quota_recovery import reduce
        cfg = self.quota_config()
        op = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport(self.fixtures(cfg))
        for _ in range(30):
            state = op.step(transport)
            if state["quota_candidate"]:
                break
        for reason in ("stale", "budget", "history", "clock"):
            current = deepcopy(state)
            bound = cfg
            if reason == "budget":
                current["retained_micro_usd"] = cfg.budget_micro_usd
            if reason == "history":
                current["history"].append({"kind": "fixture", "text": "x" * cfg.history_bytes})
            at = self.clock.now_ms() - (1 if reason == "clock" else 0)
            expected = "0"*64 if reason == "stale" else digest(current)
            before = deepcopy(current)
            with self.assertRaises(ValueError):
                reduce(current, "RECOVER_QUOTA", dict(at_ms=at, expected_state_hash=expected), bound)
            self.assertEqual(before, current)

    def test_disabled_correction_stays_paused(self):
        cfg = self.quota_config(max_pick_corrections=0)
        state = self.run_to_stop(QuotaOpportunity(self.directory, cfg, clock=self.clock),
                                 ScriptedResearchTransport(self.fixtures(cfg)))
        self.assertEqual(state["pause"], "RESEARCH_LIMIT")
        self.assertFalse(state["quota_candidate"])

    def test_live_configuration_and_transport_rejected(self):
        cfg = self.quota_config()
        with self.assertRaises(ValueError):
            replace(cfg, mode="LIVE_RESEARCH_PILOT")
        op = QuotaOpportunity(self.directory, cfg, clock=self.clock)
        with self.assertRaises(ValueError):
            op.step(object())

    def test_qp1_saved_quota_result_retains_unknown_and_quota_failures_once(self):
        from fantasy_basketball.core.config import digest
        for late in (False,True):
            cfg=replace(self.quota_config(),run_id='research-incident-quota-saved-'+str(late).lower())
            op=QuotaOpportunity(self.directory,cfg,clock=self.clock)
            transport=ScriptedResearchTransport(self.fixtures(cfg))
            def observe(boundary):
                if boundary=='after_send' and op.status()['pending']['key']=='pick:0:model:2':
                    if late:self.clock.advance(cfg.profile.timeout_ms)
                    observer=QuotaOpportunity(self.directory,cfg,clock=self.clock)
                    self.assertEqual(observer.step(ScriptedResearchTransport({}))['pause'],'UNKNOWN_DISPATCH')
            op.fault=observe
            for _ in range(30):
                state=op.step(transport)
                if state['phase']=='PAUSED' and state['result'] is not None:break
            self.assertEqual(state['pause'],'UNKNOWN_DISPATCH')
            fresh=QuotaOpportunity(self.directory,cfg,clock=self.clock)
            state=fresh.recover_saved(digest(state))
            if late:
                self.assertEqual(state['pause'],'RESPONSE_LATE')
                self.assertIsNone(state['quota_candidate'])
            else:
                self.assertEqual([h['pause'] for h in state['failure_history']],['UNKNOWN_DISPATCH','RESEARCH_LIMIT'])
                quota=state['failure_history'][-1]
                self.assertEqual(quota['saved_result_hash'],digest(state['attempts'][-1]['saved']))
                self.assertEqual(quota['attempt_key'],'pick:0:model:2')
                state=self.run_to_stop(fresh,transport)
                self.assertEqual(state['phase'],'COMPLETE')
                self.assertEqual(len(state['quota_recoveries']),1)
                self.assertEqual([h['pause'] for h in state['failure_history']],['UNKNOWN_DISPATCH','RESEARCH_LIMIT'])
            before=fresh.journal.path.read_bytes();empty=ScriptedResearchTransport({})
            self.assertEqual(QuotaOpportunity(self.directory,cfg,clock=self.clock).step(empty),state)
            self.assertFalse(empty.calls);self.assertEqual(fresh.journal.path.read_bytes(),before)
