"""Manager-visible pilot rules and capability gate, entirely offline."""
from dataclasses import asdict, replace
import json
from pathlib import Path

import test_build_core as support
from test_build_research import ten_profiles, board, action, native, evidence, URL
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.draft.native_plan import NativeDraftConfig
from fantasy_basketball.draft.research import ResearchConfig, ResearchOpportunity, ScriptedResearchTransport
from fantasy_basketball.providers.research import request, _cached_request
from fantasy_basketball.providers.research_task import pilot_task, ResearchTask
from research_entry import decode_config


def user_text(profile, body):
    if profile.provider in {"openai", "xai"}:
        return body["input"][1]["content"]
    if profile.provider == "gemini":
        return body["contents"][0]["parts"][0]["text"]
    return body["messages"][-1]["content"]


class ResearchTaskTests(support.BuildCase):
    def task_config(self, required=True):
        profile = ten_profiles()[0]
        opening = board(profile.provider)
        task = replace(pilot_task(opening.slots, 1), require_research=required)
        plan = NativeDraftConfig("context-test", (profile,), opening, 100000000,
                                 1200000, digest({"fixture": True}), task.hash)
        return ResearchConfig("research-context-test", profile, opening, draft_plan=plan, task=task)

    def finish(self, cfg, fixtures):
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport(fixtures)
        for _ in range(30):
            state = op.step(transport)
            if state["phase"] in {"COMPLETE", "PAUSED"}:
                return op, transport, state
        self.fail("bounded fixture did not settle")

    def test_all_ten_native_envelopes_contain_identical_formula_and_objective(self):
        expected = None
        for profile in ten_profiles():
            opening = board(profile.provider)
            task = pilot_task(opening.slots, 1)
            wire = request(profile, opening, [], dict(searches=2, reads=2, model_calls_including_this=6),
                           10000, 1210000, task)
            payload = json.loads(user_text(profile, wire["body"]))
            packet = payload["manager_task"]
            if expected is None:
                expected = packet
            self.assertEqual(packet, expected)
            self.assertNotIn("You may pick without research", canonical(wire["body"]))
            self.assertEqual(wire["request_hash"], digest(wire["body"]))
            self.assertLessEqual(wire["request_bytes"], profile.request_bytes)
        historical = json.loads((Path(__file__).resolve().parents[1] /
                                 "evidence/data/historical_2025-10-27/case.json").read_text())
        self.assertEqual(expected["scoring_weights"], historical["rules"]["weights"])

    def test_task_changes_hash_request_cache_and_decode_roundtrip(self):
        cfg = self.task_config()
        decoded = decode_config(json.loads(canonical(asdict(cfg))))
        self.assertEqual(decoded, cfg)
        self.assertEqual(decoded.hash, cfg.hash)
        changed_task = replace(cfg.task, require_research=False)
        changed = replace(cfg, task=changed_task, draft_plan=replace(cfg.draft_plan, rules_digest=changed_task.hash))
        self.assertNotEqual(cfg.hash, changed.hash)
        wires = [request(c.profile, c.board, [], {}, 10000, 1210000, c.task) for c in (cfg, changed)]
        self.assertNotEqual(wires[0]["request_hash"], wires[1]["request_hash"])
        wires[0]["body"].clear()
        self.assertTrue(request(cfg.profile, cfg.board, [], {}, 10000, 1210000, cfg.task)["body"])

    def test_legacy_no_task_binding_and_request_remain_unchanged(self):
        cfg = self.task_config()
        legacy = replace(cfg, task=None)
        old_dict = asdict(legacy)
        # Fields added later are omitted from digests at their defaults; the
        # original field set must still produce the original binding.
        for name in ("task", "counted_task", "counted_cutoff_ms", "excess_reserve_calls", "budget_gate",
                     "counted_hard_limit_ms", "prior_results_json", "prior_source_json"):
            old_dict.pop(name)
        self.assertEqual(legacy.hash, digest(old_dict))
        self.assertEqual(decode_config(json.loads(canonical(old_dict))).hash, legacy.hash)
        wire = request(cfg.profile, cfg.board, [], {}, 10000, 1210000)
        self.assertNotIn("manager_task", user_text(cfg.profile, wire["body"]))

    def test_pick_without_search_read_pauses_without_using_illegal_pick_correction(self):
        cfg = self.task_config()
        _, transport, state = self.finish(cfg, {"pick:0:model:0": native(cfg.profile, action())})
        self.assertEqual(state["pause"], "PILOT_DEMONSTRATION_INCOMPLETE")
        self.assertEqual(state["results"], [])
        self.assertEqual(state["corrections"], 0)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(state["retained_micro_usd"], cfg.profile.bound_micro_usd)

    def test_successful_search_alone_does_not_satisfy_read_requirement(self):
        cfg = self.task_config()
        _, _, state = self.finish(cfg, {"pick:0:model:0": native(cfg.profile, action("search", query="availability")),
                                        "pick:0:search:0": evidence(),
                                        "pick:0:model:1": native(cfg.profile, action())})
        self.assertEqual(state["pause"], "PILOT_DEMONSTRATION_INCOMPLETE")

    def test_rc1_illegal_early_pick_pauses_all_ten_formats_without_correction_or_resend(self):
        original = self.task_config()
        for profile in ten_profiles():
            with self.subTest(provider=profile.provider):
                opening = board(profile.provider)
                plan = replace(original.draft_plan, profiles=(profile,), board=opening)
                cfg = replace(original, run_id="research-rc1-" + profile.provider.replace("_", "-"),
                              profile=profile, board=opening, draft_plan=plan)
                op, transport, state = self.finish(cfg, {
                    "pick:0:model:0": native(profile, action(player_id="illegal-player"))})
                self.assertEqual(state["pause"], "PILOT_DEMONSTRATION_INCOMPLETE")
                self.assertEqual(state["attempts"][-1]["native_receipt"]["failure"], "ILLEGAL_PICK")
                self.assertEqual(state["corrections"], 0)
                self.assertEqual(state["results"], [])
                self.assertEqual(state["retained_micro_usd"], profile.bound_micro_usd)
                before = op.journal.path.read_bytes()
                cold = ResearchOpportunity(self.directory, cfg, clock=self.clock)
                fresh = ScriptedResearchTransport({})
                self.assertEqual(cold.step(fresh), state)
                self.assertFalse(fresh.calls)
                self.assertEqual(op.journal.path.read_bytes(), before)
                self.assertEqual(len(transport.calls), 1)

    def test_rc1_illegal_pick_after_search_only_pauses_without_correction(self):
        cfg = self.task_config()
        _, transport, state = self.finish(cfg, {
            "pick:0:model:0": native(cfg.profile, action("search", query="availability")),
            "pick:0:search:0": evidence(),
            "pick:0:model:1": native(cfg.profile, action(player_id="illegal-player"))})
        self.assertEqual(state["pause"], "PILOT_DEMONSTRATION_INCOMPLETE")
        self.assertEqual(state["corrections"], 0)
        self.assertEqual(state["results"], [])
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(state["retained_micro_usd"], 2 * cfg.profile.bound_micro_usd + cfg.tool_bound_micro_usd)

    def test_rc1_post_demonstration_illegal_pick_still_gets_same_ai_correction(self):
        cfg = self.task_config()
        _, transport, state = self.finish(cfg, {
            "pick:0:model:0": native(cfg.profile, action("search", query="availability")),
            "pick:0:search:0": evidence(), "pick:0:model:1": native(cfg.profile, action("read", url=URL)),
            "pick:0:read:0": evidence(),
            "pick:0:model:2": native(cfg.profile, action(player_id="illegal-player")),
            "pick:0:model:3": native(cfg.profile, action())})
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["corrections"], 1)
        self.assertEqual(len(transport.calls), 6)

    def test_rc1_optional_research_illegal_pick_still_gets_correction(self):
        cfg = self.task_config(required=False)
        _, _, state = self.finish(cfg, {
            "pick:0:model:0": native(cfg.profile, action(player_id="illegal-player")),
            "pick:0:model:1": native(cfg.profile, action())})
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["corrections"], 1)

    def test_demonstrated_pick_restart_preserves_rules_and_never_resends(self):
        cfg = self.task_config()
        op, _, state = self.finish(cfg, {
            "pick:0:model:0": native(cfg.profile, action("search", query="availability")),
            "pick:0:search:0": evidence(), "pick:0:model:1": native(cfg.profile, action("read", url=URL)),
            "pick:0:read:0": evidence(), "pick:0:model:2": native(cfg.profile, action())})
        self.assertEqual(state["phase"], "COMPLETE")
        before = op.journal.path.read_bytes()
        _cached_request.cache_clear()
        restarted = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        transport = ScriptedResearchTransport({})
        self.assertEqual(restarted.step(transport), state)
        self.assertFalse(transport.calls)
        self.assertEqual(op.journal.path.read_bytes(), before)
        task = replace(cfg.task, require_research=False)
        changed = replace(cfg, task=task, draft_plan=replace(cfg.draft_plan, rules_digest=task.hash))
        with self.assertRaises(ValueError):
            ResearchOpportunity(self.directory, changed, clock=self.clock)

    def test_optional_research_task_allows_direct_pick(self):
        cfg = self.task_config(required=False)
        _, _, state = self.finish(cfg, {"pick:0:model:0": native(cfg.profile, action())})
        self.assertEqual(state["phase"], "COMPLETE")

    def test_mismatched_task_and_incomplete_weights_are_rejected(self):
        cfg = self.task_config()
        with self.assertRaises(ValueError):
            replace(cfg, task=replace(cfg.task, roster_size=2))
        with self.assertRaises(ValueError):
            replace(cfg.task, scoring=cfg.task.scoring[:-1])
        with self.assertRaises(ValueError):
            replace(cfg, draft_plan=replace(cfg.draft_plan, rules_digest=digest({"wrong": 1})))


if __name__ == "__main__":
    import unittest
    unittest.main()
