"""Scientific observer checks using disposable journals, never live services."""
from dataclasses import asdict, replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import test_build_core as support
from test_build_research import ten_profiles, board, action, native, evidence, URL
from fantasy_basketball.core.config import digest
from fantasy_basketball.draft.board import Player
from fantasy_basketball.draft.native_plan import NativeDraftConfig
from fantasy_basketball.draft.research import ResearchConfig, ResearchOpportunity, ScriptedResearchTransport
from study_observer import baseline, read_events, measurements, strict_json


SPEC = {"schema": "basketball-packet-baseline/1", "weights": {"points": "1", "minutes": "0"},
        "required_equal": {}}


class ObserverTests(support.BuildCase):
    def cfg(self, multiple=False):
        cohort = ten_profiles()[:3]
        opening = board(cohort[0].provider)
        if multiple:
            opening = replace(opening, players=opening.players + (Player("c", "Third", ("G",), (("points", "10"),)),))
            plan = NativeDraftConfig("observer-plan", cohort, opening, 0, 1200000,
                                     digest({"source": "fixture"}), digest({"rules": "fixture"}))
            return ResearchConfig("research-observer", cohort[0], opening, draft_plan=plan,
                                  budget_micro_usd=1000000000)
        return ResearchConfig("research-observer", cohort[0], opening)

    def run_until_terminal(self, op, tx):
        for _ in range(60):
            state = op.step(tx)
            if state["phase"] in {"COMPLETE", "PAUSED"}:
                return state
        self.fail("bounded fixture did not finish")

    def observe(self, op, cfg, spec=SPEC):
        before = op.journal.path.read_bytes()
        with patch("socket.socket", side_effect=AssertionError("offline observer only")):
            events, tail = read_events(op.journal.path, cfg)
            report = measurements(cfg, events, spec)
        self.assertEqual(op.journal.path.read_bytes(), before)
        self.assertIsNotNone(tail)
        return report

    def test_baseline_excludes_unknown_but_preserves_zero_and_negative(self):
        current = board()
        self.assertEqual(baseline(current, SPEC, "b")["comparable_players"], 1)
        self.assertIsNone(baseline(current, SPEC, "b")["chosen_value"])
        current = replace(current, players=(replace(current.players[0], facts=(("points", "-2"),)),
                                            replace(current.players[1], facts=(("points", "0"),))))
        result = baseline(current, SPEC, "a")
        self.assertEqual((result["reference_id"], result["reference_minus_chosen"]), ("b", "2"))

    def test_baseline_uses_actual_legality_exact_tie_and_required_state(self):
        current = board()
        current = replace(current, players=tuple(replace(p, facts=(("points", "0.1"), ("state", "usable"))) for p in current.players))
        spec = {**SPEC, "required_equal": {"state": "usable"}, "weights": {"points": "3"}}
        self.assertEqual(baseline(current, spec)["reference_value"], "3/10")
        self.assertEqual(baseline(current, spec)["reference_id"], "a")
        owned = replace(current, ownership=(("a", "competitor"),))
        self.assertEqual(baseline(owned, spec)["reference_id"], "b")
        self.assertEqual(baseline(current, {**spec, "required_equal": {"state": "other"}})["comparable_players"], 0)
        with self.assertRaises(ValueError):
            baseline(current, {**spec, "weights": {"points": "NaN"}})

    def test_completed_research_counts_and_unadjudicated_public_answer(self):
        cfg = self.cfg()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        tx = ScriptedResearchTransport({"model:0": native(cfg.profile, action("search", query="Private query")),
            "search:0": evidence(), "model:1": native(cfg.profile, action("read", url=URL)),
            "read:0": evidence(), "model:2": native(cfg.profile, action())})
        state = self.run_until_terminal(op, tx)
        report = self.observe(op, cfg)
        row = report["opportunities"][0]
        self.assertEqual(row["status"], "settled")
        self.assertEqual(row["completed_research"], {"search": 1, "read": 1})
        self.assertEqual(row["intent_counts"], {"model": 3, "search": 1, "read": 1})
        self.assertEqual(row["unique_saved_urls"], 1)
        self.assertTrue(row["first_final_success"])
        self.assertEqual(len(row["public_answers"]), 1)
        self.assertEqual(row["public_answers"][0]["adjudication_status"], "PENDING")
        self.assertNotIn("Private query", json.dumps(report))
        self.assertEqual(report["accounting"]["retained_micro_usd"], state["retained_micro_usd"])

    def test_correction_preserves_failed_public_answer_and_denominator(self):
        cfg = self.cfg()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        self.run_until_terminal(op, ScriptedResearchTransport({
            "model:0": native(cfg.profile, action(player_id="not-legal")),
            "model:1": native(cfg.profile, action())}))
        report = self.observe(op, cfg)
        row = report["opportunities"][0]
        self.assertFalse(row["first_final_success"])
        self.assertEqual([r["settled"] for r in row["public_answers"]], [False, True])
        self.assertEqual(report["by_provider"][cfg.profile.provider]["first_final_success_over_reached"]["exact_ratio"], "0")

    def test_stopped_draft_keeps_never_reached_distinct_from_failure(self):
        cfg = self.cfg(multiple=True)
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        self.run_until_terminal(op, ScriptedResearchTransport({
            "pick:0:model:0": native(cfg.profile, action()),
            "pick:1:model:0": {"outcome": "HTTP_ERROR", "http_status": 403}}))
        report = self.observe(op, cfg)
        self.assertEqual([r["status"] for r in report["opportunities"]], ["settled", "paused", "never_reached"])
        last = report["by_provider"][cfg.draft_plan.profiles[2].provider]
        self.assertEqual(last["settled_over_reached"]["denominator"], 0)
        self.assertIsNone(last["settled_over_reached"]["exact_ratio"])
        self.assertIsNone(report["opportunities"][2]["baseline"])

    def test_unknown_dispatch_is_intent_not_proven_api_request(self):
        cfg = self.cfg()
        def fault(name):
            if name == "after_intent":
                raise RuntimeError("synthetic crash")
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock, fault=fault)
        op.step(ScriptedResearchTransport({}))
        with self.assertRaises(RuntimeError):
            op.step(ScriptedResearchTransport({}))
        report = self.observe(op, cfg)
        row = report["opportunities"][0]
        self.assertEqual(row["intent_counts"]["model"], 1)
        self.assertEqual(row["receipt_counts"]["model"], 0)
        self.assertTrue(report["pending_intent_without_result"])
        self.assertFalse(row["first_final_success"])

    def test_expiry_between_turns_does_not_invent_a_reached_failure(self):
        cfg = self.cfg(multiple=True)
        cfg = replace(cfg, draft_plan=replace(cfg.draft_plan, trial_ms=100))
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        tx = ScriptedResearchTransport({"pick:0:model:0": native(cfg.profile, action())})
        for _ in range(3):
            state = op.step(tx)
        self.assertEqual(state["phase"], "PICK_SAVED")
        self.clock.advance(100)
        op.step(tx)
        report = self.observe(op, cfg)
        self.assertEqual([r["status"] for r in report["opportunities"]], ["settled", "never_reached", "never_reached"])
        self.assertEqual(report["run_pause_events"][0]["reason"], "DEADLINE")
        self.assertIsNone(report["run_pause_events"][0]["opportunity_index"])

    def test_late_valid_final_is_preserved_but_never_counted_successful(self):
        cfg = self.cfg()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        clock = self.clock
        # Exact transport type is intentionally required. Advance the clock
        # through its result dictionary hook while retaining the real fixture.
        tx = ScriptedResearchTransport({"model:0": native(cfg.profile, action())})
        original = tx.send
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            clock.advance(cfg.profile.timeout_ms)
            return result
        tx.send = slow
        self.run_until_terminal(op, tx)
        row = self.observe(op, cfg)["opportunities"][0]
        self.assertEqual(row["status"], "paused")
        self.assertFalse(row["first_final_success"])
        self.assertEqual(row["public_answers"][0]["applied_failure"], "RESPONSE_LATE")
        self.assertFalse(row["public_answers"][0]["settled"])

    def test_saved_research_counts_even_when_expiry_blocks_next_dispatch(self):
        cfg = self.cfg()
        tx = ScriptedResearchTransport({"model:0": native(cfg.profile, action("search", query="Private")),
                                        "search:0": evidence()})
        def fault(name):
            if name == "after_result" and len(tx.calls) == 2:
                raise RuntimeError("saved search interruption")
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock, fault=fault)
        for _ in range(3):
            op.step(tx)
        with self.assertRaises(RuntimeError):
            op.step(tx)
        self.clock.advance(cfg.opportunity_ms + 1)
        restarted = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        self.run_until_terminal(restarted, ScriptedResearchTransport({}))
        report = self.observe(restarted, cfg)
        row = report["opportunities"][0]
        self.assertEqual(row["completed_research"]["search"], 1)
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["pause_history"], ["DEADLINE"])

    def test_saved_but_unapplied_final_answer_is_visible_and_not_settled(self):
        cfg = self.cfg()
        def fault(name):
            if name == "after_result":
                raise RuntimeError("synthetic crash")
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock, fault=fault)
        op.step(ScriptedResearchTransport({}))
        with self.assertRaises(RuntimeError):
            op.step(ScriptedResearchTransport({"model:0": native(cfg.profile, action())}))
        row = self.observe(op, cfg)["opportunities"][0]
        self.assertEqual(row["status"], "open")
        self.assertEqual(len(row["public_answers"]), 1)
        self.assertFalse(row["public_answers"][0]["settled"])

    def test_empty_journal_and_integrity_binding_rejections(self):
        cfg = self.cfg()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        events, tail = read_events(op.journal.path, cfg)
        self.assertEqual(events, [])
        self.assertIsNone(tail)
        self.assertEqual(measurements(cfg, events)["opportunities"][0]["status"], "never_reached")
        with self.assertRaises(ValueError):
            read_events(self.directory / "research-missing.sqlite3", cfg)
        other = replace(cfg, budget_micro_usd=cfg.budget_micro_usd + 1)
        with self.assertRaises(ValueError):
            read_events(op.journal.path, other)
        op.step(ScriptedResearchTransport({}))
        with closing(sqlite3.connect(op.journal.path)) as db:
            db.execute("DROP TRIGGER immutable_events_UPDATE")
            db.execute("UPDATE events SET hash=?", ("0" * 64,))
            db.commit()
        with self.assertRaises(ValueError):
            read_events(op.journal.path, cfg)

    def test_cli_does_not_overwrite_input_or_access_credentials(self):
        cfg = self.cfg()
        op = ResearchOpportunity(self.directory, cfg, clock=self.clock)
        self.run_until_terminal(op, ScriptedResearchTransport({"model:0": native(cfg.profile, action())}))
        package = self.directory / "observer-package.json"
        package.write_text(json.dumps({"config": asdict(cfg)}), encoding="utf-8")
        output = self.directory / "report.json"
        before = hashlib.sha256(op.journal.path.read_bytes()).hexdigest()
        script = Path(__file__).resolve().parents[1] / "workstreams/build/study_observer.py"
        command = [sys.executable, "-B", str(script), "--package", str(package), "--journal", str(op.journal.path), "--output", str(output)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse(json.loads(output.read_text())["observer_activity"]["credentials_accessed"])
        saved = output.read_bytes()
        again = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(output.read_bytes(), saved)
        self.assertEqual(hashlib.sha256(op.journal.path.read_bytes()).hexdigest(), before)
        with self.assertRaises(ValueError):
            strict_json('{"duplicate":1,"duplicate":2}')
