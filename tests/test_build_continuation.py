"""Continuation from a stopped counted run's settled picks. Offline fixtures only."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "workstreams/build"), str(ROOT / "tests")]

from fantasy_basketball.core.config import canonical
from fantasy_basketball.core.journal import STATE_ROOT
from fantasy_basketball.draft.research import ScriptedResearchTransport, prior_results
from incident_acceptance_check import Clock
from quota_native import NativeQuotaOpportunity
from research_integration_check import action
from test_build_counted_draft import native, offline


def drive(cfg, directory, clock, picks_wanted):
    op = NativeQuotaOpportunity(directory, cfg, clock=clock)
    with patch("socket.socket", side_effect=AssertionError("offline only")):
        state = op.status()
        for _ in range(400):
            if len(state["results"]) >= picks_wanted and state["phase"] != "PICK_SAVED":
                break
            index = len(state["results"])
            profile = cfg.draft_plan.cases[index].profile
            board = cfg.draft_plan.board_at(index, state["results"])
            legal = sorted(board.legal_ids())[0]
            state = op.step(ScriptedResearchTransport({f"pick:{index}:model:0": native(profile, action("pick", legal))}))
            if state["phase"] == "PAUSED":
                raise AssertionError(state["pause"])
    return state


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="continuation-test-")
        self.dir, self.clock = Path(self.tmp.name), Clock()
        self.first = offline()
        self.done = drive(self.first, self.dir, self.clock, 3)["results"][:3]

    def tearDown(self):
        self.tmp.cleanup()

    def cont(self, results=None, **extra):
        results = self.done if results is None else results
        source = dict(run_id=self.first.run_id, config_hash=self.first.hash, journal_sha256="0" * 64,
                      settled_picks=len(results))
        return replace(self.first, run_id="research-incident-quota-counted-test-cont",
                       prior_results_json=canonical(results), prior_source_json=canonical(source), **extra)

    def test_continuation_starts_at_the_next_pick_with_prior_ownership(self):
        cfg = self.cont()
        self.assertNotEqual(cfg.hash, self.first.hash)
        state = drive(cfg, self.dir, self.clock, 4)
        self.assertEqual(state["results"][:3], self.done)
        self.assertEqual(len(state["results"]), 4)
        self.assertEqual(state["results"][3]["case_index"], 3)
        owned = cfg.draft_plan.board_at(4, state["results"]).ownership
        self.assertEqual(len({p for p, _ in owned}), 4)

    def test_prefix_is_validated(self):
        dup = [dict(r) for r in self.done]
        dup[2] = dict(dup[2], action=dict(dup[2]["action"], player_id=dup[0]["action"]["player_id"]))
        bad_order = [self.done[1], self.done[0], self.done[2]]
        wrong_profile = [dict(r) for r in self.done]
        wrong_profile[1] = dict(wrong_profile[1], profile_id="someone-else")
        unknown = [dict(r) for r in self.done]
        unknown[1] = dict(unknown[1], action=dict(unknown[1]["action"], player_id="espn:0"))
        extra_key = [dict(r) for r in self.done]
        extra_key[0] = dict(extra_key[0], note="x")
        for results in (dup, bad_order, wrong_profile, unknown, extra_key, []):
            with self.assertRaises(ValueError):
                self.cont(results)
        with self.assertRaises(ValueError):   # source count must match
            replace(self.cont(), prior_source_json=canonical(dict(run_id="x", config_hash="y", journal_sha256="z", settled_picks=2)))
        with self.assertRaises(ValueError):   # results without a source
            replace(self.first, prior_results_json=canonical(self.done))

    def test_default_config_digest_is_unchanged(self):
        self.assertNotIn("prior_results_json", self.first.__dict__.get("_hash_fields", ""))
        self.assertEqual(prior_results(self.first), [])


if __name__ == "__main__":
    unittest.main()


class ContinuationBuilderTests(unittest.TestCase):
    def test_builder_makes_a_live_counted_package_after_the_exported_picks(self):
        import json, shutil
        import prepare_frontier_launch as pfl
        from frontier_profiles import frontier_profiles
        from prepare_counted_draft import build_config, to_byte_basis
        seed = "0123456789abcdef" * 4
        byte = tuple(to_byte_basis(p, pfl.REQUEST_CAP) for p in frontier_profiles())
        trial = build_config(byte, seed=seed, live=False, run_id="research-incident-quota-counted-frontier-trial",
                             excess_reserve_calls=2)
        with tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="continuation-build-") as tmp:
            results = drive(trial, Path(tmp), Clock(), 2)["results"][:2]
        from prepare_counted_draft import OWNER_POLICIES, HARD_LIMIT_MS
        live = build_config(byte, seed=seed, live=True, run_id="research-incident-quota-counted-frontier-x",
                            policies=OWNER_POLICIES, opportunity_ms=pfl.PICK_WINDOW_MS, budget_gate="RUNTIME_CEILING_GATED",
                            budget_micro_usd=pfl.COUNTED_BUDGET_MICRO_USD, hard_limit_ms=HARD_LIMIT_MS)
        hashes = dict(draft_plan_hash=live.draft_plan.hash, counted_task_hash=live.counted_task.hash)
        export = pfl.BUILD / "TEST_CONTINUATION_EXPORT.json"
        out = pfl.BUILD / "TEST_CONTINUATION_OUT"
        try:
            export.write_text(json.dumps(dict(schema="basketball-settled-results-export/1",
                run_id="research-incident-quota-counted-frontier-20261019h", config_hash="c" * 64,
                journal_sha256="d" * 64, settled_picks=2, phase="PAUSED", pause="RESPONSE_LATE",
                pending_call=False, state_hash="e" * 64, results=results, **hashes)))
            summary = pfl.continuation(out, seed, "20261019i", export)
            self.assertEqual(summary["next_pick"], 3)
            self.assertEqual(summary["continues_from"]["settled_picks"], 2)
            package = json.loads((out / "counted/package.json").read_text())
            self.assertEqual(json.loads(package["config"]["prior_results_json"]), results)
            wrong = json.loads(export.read_text()); wrong["counted_task_hash"] = "f" * 64
            export.write_text(json.dumps(wrong))
            with self.assertRaises(ValueError):   # a different rules/pool/order is refused
                pfl.continuation(pfl.BUILD / "TEST_CONTINUATION_OUT3", seed, "20261019k", export)
            bad = json.loads(export.read_text()); bad["counted_task_hash"] = hashes["counted_task_hash"]; bad["pending_call"] = True
            export.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                pfl.continuation(pfl.BUILD / "TEST_CONTINUATION_OUT2", seed, "20261019j", export)
        finally:
            export.unlink(missing_ok=True)
            shutil.rmtree(out, ignore_errors=True)
