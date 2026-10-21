"""Counted-run dataset adapter: decision-ID allocation, read-only export, privacy defaults, metadata."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "workstreams/build"
sys.path[:0] = [str(ROOT / "src"), str(BUILD), str(BUILD / "decision_dataset"), str(ROOT / "tests")]

from fantasy_basketball.core.config import canonical
from fantasy_basketball.core.journal import STATE_ROOT
from test_build_frontier_launch import SEED, counted_cfg, raw_for
from prepare_counted_draft import FIRST_TIPOFF_MS, HARD_LIMIT_MS, OWNER_POLICIES


def offline_solo(provider="anthropic"):
    from prepare_frontier_launch import solo_config
    p = next(x for x in counted_cfg().draft_plan.profiles if x.provider == provider)
    live = solo_config(p, SEED, FIRST_TIPOFF_MS, HARD_LIMIT_MS, OWNER_POLICIES)
    return replace(live, mode="OFFLINE_FIXTURE", experiment_id="miomaci-basketball-research-offline",
                   schema="basketball-research-offline/1", counted_cutoff_ms=None, counted_hard_limit_ms=None,
                   sources=(), extension_sources=(), quota_sources=())


def run_offline(cfg, folder):
    from fantasy_basketball.draft.research import ScriptedResearchTransport
    from incident_acceptance_check import Clock
    from quota_native import NativeQuotaOpportunity
    from research_integration_check import action
    fixtures, prefix = {}, []
    url = "https://www.nba.com/player/fixture"
    for index, case in enumerate(cfg.draft_plan.cases):
        board = cfg.draft_plan.board_at(index, prefix)
        pick = board.legal_ids()[0]
        steps = [action("search", f"PRIVATE-QUERY-{index}"), action("read", url), action("pick", pick)]
        for n, value in enumerate(steps):
            fixtures[f"pick:{index}:model:{n}"] = dict(outcome="RESPONSE", raw=raw_for(case.profile, canonical(value)))
        fixtures[f"pick:{index}:search:0"] = dict(outcome="OK", retrieved_ms=10000, items=[
            dict(url=url, title="t", text=f"PRIVATE-PAGE-TEXT-{index}")])
        fixtures[f"pick:{index}:read:0"] = dict(outcome="OK", retrieved_ms=10000, items=[
            dict(url=url, title="t", text=f"PRIVATE-PAGE-TEXT-{index}")])
        prefix.append(dict(case_index=index, case_id=board.case_id, profile_id=case.profile.profile_id,
                           action=dict(player_id=pick)))
    clock = Clock()
    with patch("socket.socket", side_effect=AssertionError("offline only")):
        op = NativeQuotaOpportunity(folder, cfg, clock=clock)
        transport = ScriptedResearchTransport(fixtures)
        for _ in range(400):
            for r in transport.results.values():
                if r.get("outcome") == "OK":
                    r["retrieved_ms"] = clock.wall
            state = op.step(transport)
            if state["phase"] in {"COMPLETE", "PAUSED"}:
                break
    return state, folder / (cfg.run_id + ".sqlite3")


class AllocationTests(unittest.TestCase):
    def test_every_scheduled_turn_gets_a_permanent_dd1_identifier(self):
        from counted_export import allocate
        from export import identifier
        cfg = counted_cfg()
        tmp = Path(tempfile.mkdtemp(dir=STATE_ROOT, prefix="alloc-"))
        try:
            pkg = tmp / "package.json"
            pkg.write_text(json.dumps(dict(schema="basketball-counted-draft-package/1", config=asdict(cfg),
                                           unresolved=[], execution_authorized=False)))
            self.assertEqual(allocate([str(pkg)], tmp / "ids.json"), 130)
            record = json.loads((tmp / "ids.json").read_text())
            ids = [d["decision_id"] for d in record["runs"][0]["decisions"]]
            self.assertEqual(len(set(ids)), 130)
            self.assertEqual(ids[7], identifier(cfg.run_id, cfg.hash, 7))
            with self.assertRaises(FileExistsError):
                allocate([str(pkg)], tmp / "ids.json")
        finally:
            shutil.rmtree(tmp)


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(dir=STATE_ROOT, prefix="export-"))
        cls.cfg = offline_solo()
        cls.state, cls.journal = run_offline(cls.cfg, cls.tmp / "journal")
        (cls.tmp / "journal").mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def export(self, name, **kw):
        from counted_export import export_config
        return export_config(self.cfg, self.journal, self.tmp / name, **kw)

    def test_export_is_complete_private_by_default_and_hashed(self):
        self.assertEqual(self.state["phase"], "COMPLETE")
        manifest = self.export("default")
        out = self.tmp / "default"
        decisions = [json.loads(l) for l in (out / "decisions.jsonl").read_text().splitlines()]
        calls = [json.loads(l) for l in (out / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(decisions), 13)
        self.assertTrue(all(d["status"] == "SETTLED" and d["player_id"] and d["searches"] == 1 for d in decisions))
        self.assertEqual(len({d["player_id"] for d in decisions}), 13)
        self.assertEqual(len(calls), 13 * 5)
        self.assertTrue(all(d["sources_domains"] == ["www.nba.com"] for d in decisions))
        everything = "".join(p.read_text() for p in out.iterdir())
        for secret in ("PRIVATE-QUERY", "PRIVATE-PAGE-TEXT", "SYNTHETIC_TRACE_NOT_EXPORTED", "Synthetic integration choice"):
            self.assertNotIn(secret, everything)
        croissant = json.loads((out / "croissant.json").read_text())
        self.assertEqual(croissant["conformsTo"], "http://mlcommons.org/croissant/1.0")
        from counted_export import sha
        for d in croissant["distribution"]:
            self.assertEqual(d["sha256"], sha(out / d["contentUrl"]))
        self.assertEqual({r["@id"] for r in croissant["recordSet"]}, {"decisions", "calls"})
        self.assertIn("cff-version: 1.2.0", (out / "CITATION.cff").read_text())
        self.assertNotIn("canary GUID", everything)
        self.assertEqual(manifest["counts"]["decisions"], 13)
        prompts = json.loads((out / "prompts.json").read_text())
        self.assertEqual(prompts["providers"][0]["provider"], "anthropic")
        self.assertIn("You are the assigned basketball manager", prompts["providers"][0]["system_prompt"])
        self.assertIn("after the final pick", canonical(prompts["providers"][0]["manager_task"]))
        self.assertNotIn('"players"', canonical(prompts))  # player rows are never copied
        rules = json.loads((out / "rules.json").read_text())
        self.assertEqual(rules["task"]["scoring_weights"]["steals"], "4")
        self.assertIn("prompts.json", {d["contentUrl"] for d in croissant["distribution"]})
        # Every reached decision carries the naive reference pick at its exact board.
        self.assertTrue(all(d["baseline_player_id"] for d in decisions))
        self.assertEqual(decisions[0]["baseline_minus_chosen_fppg"] is not None, True)
        self.assertEqual(rules["baseline_spec"]["weights"]["steals_pg"], "4")

    def test_include_text_and_canary_are_explicit_opt_ins(self):
        self.export("text", include_text=True, canary=True)
        out = self.tmp / "text"
        decisions = [json.loads(l) for l in (out / "decisions.jsonl").read_text().splitlines()]
        self.assertTrue(all(d.get("rationale") for d in decisions))
        self.assertIn("PRIVATE-QUERY-0", (out / "calls.jsonl").read_text())
        self.assertNotIn("SYNTHETIC_TRACE_NOT_EXPORTED", (out / "calls.jsonl").read_text())  # never thinking blocks
        self.assertIn("canary GUID", (out / "DATA_CARD.md").read_text())

    def test_tampered_or_foreign_journal_is_rejected(self):
        from counted_export import export_config
        bad = self.tmp / "tampered.sqlite3"
        shutil.copy(self.journal, bad)
        from contextlib import closing
        with closing(sqlite3.connect(bad)) as db, db:
            for t in ("UPDATE", "DELETE", "INSERT"):
                db.execute(f"DROP TRIGGER IF EXISTS immutable_events_{t}")
            changed = db.execute("UPDATE events SET document = replace(document, 'PRIVATE-QUERY-0', 'EDITED-QUERY-0') "
                                 "WHERE document LIKE '%PRIVATE-QUERY-0%'").rowcount
        self.assertGreater(changed, 0)
        with self.assertRaises(ValueError):
            export_config(self.cfg, bad, self.tmp / "tampered-out")
        other = replace(self.cfg, run_id="research-incident-quota-counted-solo-other")
        with self.assertRaises(ValueError):
            export_config(other, self.journal, self.tmp / "foreign-out")


class ReviewFixTests(unittest.TestCase):
    """Data-additions review P2-1, P2-2, P2-3."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(dir=STATE_ROOT, prefix="export-fix-"))
        cls.cfg = offline_solo("mistral")
        (cls.tmp / "journal").mkdir()
        cls.state, cls.journal = run_offline(cls.cfg, cls.tmp / "journal")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_sealed_rows_come_only_from_the_exact_config_and_keep_failures(self):
        from counted_export import export_config
        from prepare_frontier_launch import sealed_config
        from prepare_counted_draft import real_board
        from sealed_calls import SealedPermit, Store, resume
        from test_build_frontier_launch import forecast_answer
        _, sha, _ = real_board("openai")
        fc = sealed_config("FORECAST", __import__("frontier_profiles").frontier_profiles(), counted_cfg().counted_task,
                           sha, FIRST_TIPOFF_MS)
        root = self.tmp / "sealed"
        board, _, _ = real_board("openai")
        pool = [p.player_id for p in board.players]
        def dispatch(profile, wire, now, credential):
            text = "not json" if profile.provider == "xai" else forecast_answer(pool)
            return {"outcome": "RESPONSE", "raw": raw_for(profile, text)}
        now = FIRST_TIPOFF_MS - 6 * 3600 * 1000
        permit = SealedPermit(fc.hash, now - 1, FIRST_TIPOFF_MS, fc.budget_micro_usd, fc.max_model_requests, "owner:t", True)
        with patch("socket.socket", side_effect=AssertionError("offline only")):
            resume(fc, permit, root, clock=lambda: now, dispatch=dispatch, credential=lambda n: "K")
        stale = replace(fc, cutoff_ms=fc.cutoff_ms - 5)                     # rebuilt config, same run ID
        Store(root, stale).write("openai:0", "result", dict(key="openai:0", outcome="RESPONSE"))
        export_config(self.cfg, self.journal, self.tmp / "with-sealed", sealed_configs=[fc], sealed_root=root,
                      allow_unsealed=True)
        rows = [json.loads(l) for l in (self.tmp / "with-sealed" / "forecasts.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 10)                                      # stale folder ignored, no duplicates
        xai = next(r for r in rows if r["provider"] == "xai")
        self.assertTrue(xai["failure"])                                      # failed forecast stays a forecast row
        self.assertFalse((self.tmp / "with-sealed" / "consistency.jsonl").exists())

    def test_in_flight_call_is_exported_as_unknown_and_turn_is_open(self):
        from contextlib import closing
        from counted_export import export_config
        cut = self.tmp / "cut" / self.journal.name
        cut.parent.mkdir()
        shutil.copy(self.journal, cut)
        with closing(sqlite3.connect(cut)) as db, db:
            for t in ("UPDATE", "DELETE", "INSERT"):
                db.execute(f"DROP TRIGGER IF EXISTS immutable_events_{t}")
            seq = db.execute("SELECT sequence FROM events WHERE document LIKE '%\"kind\":\"INTENT\"%' "
                             "AND document LIKE '%pick:3:model:0%'").fetchone()[0]
            db.execute("DELETE FROM events WHERE sequence > ?", (seq,))
        export_config(self.cfg, cut, self.tmp / "cut-out")
        decisions = [json.loads(l) for l in (self.tmp / "cut-out" / "decisions.jsonl").read_text().splitlines()]
        calls = [json.loads(l) for l in (self.tmp / "cut-out" / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(decisions[3]["status"], "OPEN_OR_PAUSED")
        self.assertTrue(any(c["key"] == "pick:3:model:0" and c["outcome"] == "UNKNOWN_DISPATCH" for c in calls))
        self.assertFalse(json.loads((self.tmp / "cut-out" / "manifest.json").read_text())["complete"])

    def test_journal_with_uncommitted_side_file_is_refused(self):
        from counted_export import export_config
        side = self.tmp / "side" / self.journal.name
        side.parent.mkdir()
        shutil.copy(self.journal, side)
        side.with_name(side.name + "-wal").write_bytes(b"x")
        with self.assertRaises(ValueError):
            export_config(self.cfg, side, self.tmp / "side-out")


if __name__ == "__main__":
    unittest.main()
