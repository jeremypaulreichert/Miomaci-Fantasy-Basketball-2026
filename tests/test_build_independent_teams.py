"""Separate independent roster construction, using synthetic native receipts."""
from dataclasses import replace
from unittest.mock import patch

import test_build_core as support
from test_build_research import ten_profiles, board, action, native, evidence, URL
from fantasy_basketball.core.config import digest
from fantasy_basketball.draft.board import Player
from fantasy_basketball.draft.native_plan import NativeDraftConfig
from fantasy_basketball.draft.research import ResearchConfig, ScriptedResearchTransport
from independent_teams import IndependentTeams, single_team


class IndependentTeamTests(support.BuildCase):
    def configs(self, size=2, teams=2):
        cohort = ten_profiles()[:teams]
        opening = replace(board(cohort[0].provider), roster_size=size,
                          players=tuple(Player(f"p{i:02}", f"Synthetic {i}", ("G", "F", "C"), ())
                                        for i in range(max(30, size * teams))))
        if size == 13:
            opening = replace(opening, slots=tuple(
                [("G1", ("G",)), ("G2", ("G",)), ("F1", ("F",)), ("F2", ("F",)), ("C1", ("C",))]
                + [(f"U{i}", ("G", "F", "C")) for i in range(3)]))
        plan = NativeDraftConfig("independent-template", (cohort[0],), opening, 2000000000,
                                 1200000, digest({"fixture": 1}), digest({"slots": opening.slots}))
        template = ResearchConfig("research-template", cohort[0], opening,
                                  budget_micro_usd=2000000000, draft_plan=plan)
        return tuple(single_team(template, p) for p in cohort)

    def experiment(self, configs):
        return IndependentTeams(self.directory, configs,
                                maximum_micro_usd=sum(c.budget_micro_usd for c in configs), clock=self.clock)

    def transports(self, configs, research=False):
        result = {}
        for cfg in configs:
            fixtures = {}
            for i in range(cfg.opportunities):
                prefix = f"pick:{i}:"
                offset = 0
                if research and i == 0:
                    fixtures.update({prefix + "model:0": native(cfg.profile, action("search", query="availability")),
                                     prefix + "search:0": evidence(cfg.profile.provider + " private source"),
                                     prefix + "model:1": native(cfg.profile, action("read", url=URL)),
                                     prefix + "read:0": evidence(cfg.profile.provider + " private read")})
                    offset = 2
                fixtures[prefix + f"model:{offset}"] = native(cfg.profile, action(player_id=f"p{i:02}"))
            result[cfg.profile.provider] = ScriptedResearchTransport(fixtures)
        return result

    def finish(self, experiment, transports, limit=100):
        for _ in range(limit):
            result = experiment.cycle(transports)
            if all(s["phase"] in {"COMPLETE", "PAUSED"} for s in result["states"].values()):
                return result
        self.fail("fixture did not terminate within bound")

    def test_ten_complete_thirteen_player_teams_overlap_and_restart_without_resend(self):
        configs = self.configs(13, 10)
        transports = self.transports(configs, research=True)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = self.finish(self.experiment(configs), transports)
        self.assertFalse(result["process_errors"])
        for provider, state in result["states"].items():
            self.assertEqual(state["phase"], "COMPLETE")
            self.assertEqual([r["action"]["player_id"] for r in state["results"]], [f"p{i:02}" for i in range(13)])
            self.assertEqual(state["total_counts"], dict(model=15, search=1, read=1))
            calls = transports[provider].calls
            text = str(calls)
            for other in transports:
                if other != provider:
                    self.assertNotIn(other + " private", text)
            cfg = next(c for c in configs if c.profile.provider == provider)
            self.assertEqual(state["retained_micro_usd"], 15 * cfg.profile.bound_micro_usd + 2 * cfg.tool_bound_micro_usd)
        fresh = {p: ScriptedResearchTransport({}) for p in transports}
        restored = self.finish(self.experiment(configs), fresh)
        self.assertEqual(restored, result)
        self.assertTrue(all(not t.calls for t in fresh.values()))

    def test_duplicate_is_corrected_by_same_ai_and_peer_is_unaffected(self):
        configs = self.configs()
        transports = self.transports(configs)
        cfg = configs[0]
        transport = transports[cfg.profile.provider]
        transport.results["pick:1:model:0"] = native(cfg.profile, action(player_id="p00"))
        transport.results["pick:1:model:1"] = native(cfg.profile, action(player_id="p01"))
        result = self.finish(self.experiment(configs), transports)
        self.assertEqual(result["states"][cfg.profile.provider]["corrections"], 1)
        self.assertTrue(all(s["phase"] == "COMPLETE" for s in result["states"].values()))

    def test_provider_error_pauses_one_team_but_other_completes(self):
        configs = self.configs()
        transports = self.transports(configs)
        transports[configs[0].profile.provider].results["pick:0:model:0"] = {"outcome": "HTTP_ERROR"}
        result = self.finish(self.experiment(configs), transports)
        failed = result["states"][configs[0].profile.provider]
        self.assertEqual(failed["phase"], "PAUSED")
        self.assertEqual(failed["results"], [])
        self.assertGreater(failed["retained_micro_usd"], 0)
        self.assertEqual(result["states"][configs[1].profile.provider]["phase"], "COMPLETE")

    def test_crash_after_send_is_isolated_and_restart_does_not_resend_unknown(self):
        configs = self.configs()
        experiment = self.experiment(configs)
        provider = configs[0].profile.provider
        def crash(boundary):
            if boundary == "after_send":
                raise RuntimeError("synthetic crash")
        experiment.teams[provider].fault = crash
        transports = self.transports(configs)
        result = self.finish(experiment, transports)
        self.assertEqual(result["process_errors"], {provider: "RuntimeError"})
        self.assertEqual(result["states"][configs[1].profile.provider]["phase"], "COMPLETE")
        fresh = {p: ScriptedResearchTransport({}) for p in transports}
        result = self.finish(self.experiment(configs), fresh)
        self.assertEqual(result["states"][provider]["phase"], "PAUSED")
        self.assertTrue(all(not t.calls for t in fresh.values()))

    def test_caps_pool_rules_namespace_and_transport_rejected_before_work(self):
        configs = self.configs()
        with self.assertRaises(ValueError):
            IndependentTeams(self.directory, configs, maximum_micro_usd=1, clock=self.clock)
        with self.assertRaises(ValueError):
            self.experiment((configs[0], configs[0]))
        changed = replace(configs[1].draft_plan, rules_digest=digest({"different": 1}))
        with self.assertRaises(ValueError):
            self.experiment((configs[0], replace(configs[1], draft_plan=changed)))
        with self.assertRaises(ValueError):
            self.experiment((replace(configs[0], run_id="research-wrong"),))
        experiment = self.experiment(configs)
        with self.assertRaises(ValueError):
            experiment.cycle({c.profile.provider: object() for c in configs})
        self.assertTrue(all(t.status()["phase"] == "NEW" for t in experiment.teams.values()))

    def test_damaged_team_journal_does_not_prevent_other_team_reopening(self):
        configs = self.configs()
        self.finish(self.experiment(configs), self.transports(configs))
        damaged = self.directory / (configs[0].run_id + ".sqlite3")
        damaged.write_bytes(b"deliberately damaged synthetic test journal")
        fresh = {c.profile.provider: ScriptedResearchTransport({}) for c in configs}
        result = self.finish(self.experiment(configs), fresh)
        self.assertIn(configs[0].profile.provider, result["process_errors"])
        self.assertEqual(result["states"][configs[1].profile.provider]["phase"], "COMPLETE")
        self.assertEqual(damaged.read_bytes(), b"deliberately damaged synthetic test journal")
        self.assertTrue(all(not t.calls for t in fresh.values()))


if __name__ == "__main__":
    import unittest
    unittest.main()
