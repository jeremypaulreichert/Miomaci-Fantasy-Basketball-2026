"""Release-export fixes (October 19-20, 2026): unknown usage stays unknown, forecast attempts are kept apart,
sealed stores are verified against their seals, continuation segments are stitched without double counting,
and timing/scoring-window fields are recorded. Offline fixtures only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "workstreams/build"
sys.path[:0] = [str(ROOT / "src"), str(BUILD), str(BUILD / "decision_dataset"), str(ROOT / "tests")]

from fantasy_basketball.core.config import canonical
from fantasy_basketball.core.journal import STATE_ROOT
from fantasy_basketball.draft.research import ScriptedResearchTransport
from incident_acceptance_check import Clock
from quota_native import NativeQuotaOpportunity
from research_integration_check import action
from test_build_continuation import drive
from test_build_counted_draft import native, offline


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


class UsageTotalsTests(unittest.TestCase):
    def test_unknown_usage_is_never_zero(self):
        from counted_export import usage_totals
        known = dict(input_tokens=10, output_tokens=5, reasoning_tokens=None, estimated_charge_micro_usd=7,
                     reserved_micro_usd=100)
        unknown = dict(input_tokens=None, output_tokens=None, reasoning_tokens=None, estimated_charge_micro_usd=None,
                       reserved_micro_usd=900)
        t = usage_totals([known, unknown])
        self.assertIsNone(t["input_tokens"])
        self.assertEqual((t["input_tokens_known"], t["input_tokens_missing_calls"]), (10, 1))
        self.assertIsNone(t["estimated_charge_micro_usd"])
        self.assertEqual(t["unknown_charge_reserved_micro_usd"], 900)
        self.assertFalse(t["usage_complete"])
        self.assertIsNone(t["reasoning_tokens"])                         # unreported reasoning is unknown
        self.assertEqual(t["reasoning_tokens_missing_calls"], 2)
        full = usage_totals([known])
        self.assertEqual((full["input_tokens"], full["estimated_charge_micro_usd"]), (10, 7))
        self.assertTrue(full["usage_complete"])
        self.assertEqual(usage_totals([])["input_tokens"], 0)            # no model call: a true zero


class StitchTests(unittest.TestCase):
    """A run stopped after 3 picks (with 2 failed calls on pick 4) and a continuation from pick 4."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(dir=STATE_ROOT, prefix="export-stitch-"))
        clock = Clock()
        cls.first = offline()
        (cls.tmp / "a").mkdir()
        drive(cls.first, cls.tmp / "a", clock, 3)
        op = NativeQuotaOpportunity(cls.tmp / "a", cls.first, clock=clock)
        prof = cls.first.draft_plan.cases[3].profile
        bad = native(prof, action("pick", "espn:0"))
        with patch("socket.socket", side_effect=AssertionError("offline only")):
            for _ in range(6):
                state = op.step(ScriptedResearchTransport({"pick:3:model:0": bad, "pick:3:model:1": bad}))
                if state["phase"] == "PAUSED":
                    break
        assert state["pause"] == "ILLEGAL_PICK" and len(state["results"]) == 3, state["pause"]
        cls.first_journal = cls.tmp / "a" / (cls.first.run_id + ".sqlite3")
        from counted_export import sha
        cls.done = state["results"]
        source = dict(run_id=cls.first.run_id, config_hash=cls.first.hash, journal_sha256=sha(cls.first_journal),
                      settled_picks=3)
        cls.cont = replace(cls.first, run_id="research-incident-quota-counted-test-cont",
                           prior_results_json=canonical(cls.done), prior_source_json=canonical(source))
        (cls.tmp / "b").mkdir()
        cls.cont_results = drive(cls.cont, cls.tmp / "b", clock, 5)["results"]
        cls.cont_journal = cls.tmp / "b" / (cls.cont.run_id + ".sqlite3")
        # A third run continuing the second, for chain tests.
        source2 = dict(run_id=cls.cont.run_id, config_hash=cls.cont.hash, journal_sha256=sha(cls.cont_journal),
                       settled_picks=len(cls.cont_results))
        cls.third = replace(cls.first, run_id="research-incident-quota-counted-test-third",
                            prior_results_json=canonical(cls.cont_results), prior_source_json=canonical(source2))
        (cls.tmp / "c").mkdir()
        drive(cls.third, cls.tmp / "c", clock, len(cls.cont_results) + 1)
        cls.third_journal = cls.tmp / "c" / (cls.third.run_id + ".sqlite3")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_segments_are_stitched_once_with_superseded_calls_labelled(self):
        from counted_export import export_config, segment_record
        from export import identifier
        seg = segment_record(self.first, self.first_journal)
        self.assertEqual((seg["phase"], seg["pause"], len(seg["results"])), ("PAUSED", "ILLEGAL_PICK", 3))
        m = export_config(self.cont, self.cont_journal, self.tmp / "out", segments=[seg])
        out = self.tmp / "out"
        decisions, calls = rows(out / "decisions.jsonl"), rows(out / "calls.jsonl")
        self.assertEqual(len(decisions), 130)
        self.assertEqual([d["status"] for d in decisions[:6]], ["SETTLED"] * 5 + ["OPEN_OR_PAUSED"])
        self.assertEqual([d["segment"] for d in decisions[:5]], [1, 1, 1, 2, 2])
        self.assertEqual(decisions[0]["decision_id"], identifier(self.first.run_id, self.first.hash, 0))
        self.assertEqual(decisions[3]["decision_id"], identifier(self.cont.run_id, self.cont.hash, 3))
        self.assertEqual([d["player_id"] for d in decisions[:3]], [r["action"]["player_id"] for r in self.done])
        self.assertTrue(all(d["model_calls"] == 1 and d["decision_wall_ms"] is not None for d in decisions[:5]))
        superseded = [c for c in calls if not c["counted_in_decision"]]
        self.assertEqual(sorted(c["key"] for c in superseded), ["pick:3:model:0", "pick:3:model:1"])
        self.assertTrue(all(c["segment"] == 1 and c["run_id"] == self.first.run_id for c in superseded))
        self.assertEqual(len([c for c in calls if c["segment"] == 1]), 3 + 2)
        self.assertEqual(len([c for c in calls if c["segment"] == 2]), 2)
        self.assertEqual(m["accounting"]["superseded_attempts"]["model_calls"], 2)
        self.assertEqual(m["accounting"]["counted"]["model_calls"], sum(d["model_calls"] for d in decisions))
        self.assertEqual([g["run_id"] for g in m["segments"]], [self.first.run_id, self.cont.run_id])
        self.assertEqual((m["segments"][0]["first_pick"], m["segments"][0]["last_pick"]), (1, 3))
        self.assertEqual(m["segments"][0]["final_pause"], "ILLEGAL_PICK")
        self.assertIsNone(m["timing"]["final_pick_settled_ms"])          # not complete: no scoring window yet
        self.assertTrue(all(c["superseded_by_decision_id"] == decisions[3]["decision_id"] for c in superseded))
        attempts = rows(out / "pick_attempts.jsonl")
        self.assertEqual(len(attempts), 130)
        self.assertEqual((attempts[3]["episodes"], attempts[3]["settled_in_first_episode"], attempts[3]["model_requests"],
                          attempts[3]["superseded_requests"]), (2, False, 3, 2))
        self.assertTrue(attempts[0]["settled_in_first_episode"])
        self.assertEqual(m["segments"][1]["journal_sha256"], __import__("counted_export").sha(self.cont_journal))
        card = (out / "DATA_CARD.md").read_text()
        self.assertIn("## Run history", card)
        self.assertNotIn("before the NBA 2026-27 season", card + (out / "croissant.json").read_text())

    def test_continuation_without_its_source_or_with_altered_segments_is_refused(self):
        from counted_export import export_config, segment_record
        with self.assertRaises(ValueError):
            export_config(self.cont, self.cont_journal, self.tmp / "no-source")
        seg = segment_record(self.first, self.first_journal)
        bad = [dict(seg, journal_sha256="0" * 64),                                   # another journal
               dict(seg, results=seg["results"][:2]),                                # fewer settled picks
               dict(seg, results=[seg["results"][1], seg["results"][0], seg["results"][2]]),
               dict(seg, pending_call=True),                                         # stopped mid-call
               dict(seg, include_text=True),                                         # mixed privacy settings
               dict(seg, counted_task_hash="f" * 64)]                                # other rules or pool
        for n, record in enumerate(bad):
            with self.assertRaises(ValueError, msg=str(n)):
                export_config(self.cont, self.cont_journal, self.tmp / f"bad-{n}", segments=[record])
            shutil.rmtree(self.tmp / f"bad-{n}", ignore_errors=True)

    def test_segment_rows_journal_and_chain_are_checked(self):
        from counted_export import export_config, segment_record
        seg = segment_record(self.first, self.first_journal)
        swapped = json.loads(json.dumps(seg))
        swapped["decisions"][1]["player_id"] = swapped["decisions"][0]["player_id"]   # edited row, results intact
        foreign = json.loads(json.dumps(seg))
        foreign["calls"][0]["decision_id"] = "mbd1-" + "0" * 64
        for n, record in enumerate((swapped, foreign)):
            with self.assertRaises(ValueError, msg=str(n)):
                export_config(self.cont, self.cont_journal, self.tmp / f"rows-{n}", segments=[record])
            self.assertFalse((self.tmp / f"rows-{n}").exists())                         # nothing half-written
        with self.assertRaises(ValueError):                                              # not the segment's journal
            export_config(self.cont, self.cont_journal, self.tmp / "wrong-journal", segments=[seg],
                          source_journals=[self.cont_journal])
        m = export_config(self.cont, self.cont_journal, self.tmp / "right-journal", segments=[seg],
                          source_journals=[self.first_journal])
        self.assertTrue(m["segments"][0]["journal_reverified"])
        seg2 = segment_record(self.cont, self.cont_journal)
        with self.assertRaises(ValueError):                                              # oldest run omitted
            export_config(self.third, self.third_journal, self.tmp / "chain-short", segments=[seg2])
        m = export_config(self.third, self.third_journal, self.tmp / "chain", segments=[seg, seg2])
        decisions = rows(self.tmp / "chain" / "decisions.jsonl")
        n = len(self.cont_results)
        self.assertEqual([d["segment"] for d in decisions[:n + 1]], [1, 1, 1] + [2] * (n - 3) + [3])
        self.assertEqual([g["last_pick"] for g in m["segments"]], [3, n, n + 1])

    def test_segment_command_writes_a_record_file(self):
        import counted_export
        from dataclasses import asdict
        pkg = self.tmp / "first-package.json"
        pkg.write_text(json.dumps(dict(schema="basketball-counted-draft-package/1", config=asdict(self.first))))
        with patch.object(counted_export, "load_package", return_value=self.first):
            summary = counted_export.write_segment(pkg, self.first_journal, self.tmp / "seg.json")
            with self.assertRaises(FileExistsError):
                counted_export.write_segment(pkg, self.first_journal, self.tmp / "seg.json")
        self.assertEqual((summary["settled"], summary["pause"]), (3, "ILLEGAL_PICK"))
        record = json.loads((self.tmp / "seg.json").read_text())
        self.assertEqual(record["schema"], "miomaci-counted-segment/1")


class SealedAttemptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from prepare_frontier_launch import sealed_config
        from prepare_counted_draft import FIRST_TIPOFF_MS, real_board
        from sealed_calls import SealedPermit, resume, seal
        from frontier_profiles import frontier_profiles
        from test_build_frontier_launch import counted_cfg, forecast_answer, raw_for
        from test_build_counted_export import offline_solo, run_offline
        cls.tmp = Path(tempfile.mkdtemp(dir=STATE_ROOT, prefix="export-sealed-"))
        cls.cfg = offline_solo("mistral")
        (cls.tmp / "journal").mkdir()
        _, cls.journal = run_offline(cls.cfg, cls.tmp / "journal")
        board, packet, _ = real_board("openai")
        pool = [p.player_id for p in board.players]
        task = counted_cfg().counted_task
        profiles = frontier_profiles()
        cls.a = sealed_config("FORECAST", profiles, task, packet, FIRST_TIPOFF_MS)
        cls.b = sealed_config("FORECAST", [p for p in profiles if p.provider in {"xai", "qwen"}], task, packet,
                              FIRST_TIPOFF_MS, suffix="20261019b")
        cls.root = cls.tmp / "sealed"
        now = FIRST_TIPOFF_MS - 6 * 3600 * 1000
        for cfg, failing in ((cls.a, {"xai", "qwen"}), (cls.b, {"qwen"})):
            def dispatch(profile, wire, t, credential, failing=failing):
                text = "not json" if profile.provider in failing else forecast_answer(pool)
                return {"outcome": "RESPONSE", "raw": raw_for(profile, text)}
            permit = SealedPermit(cfg.hash, now - 1, FIRST_TIPOFF_MS, cfg.budget_micro_usd, cfg.max_model_requests,
                                  "owner:t", True)
            with patch("socket.socket", side_effect=AssertionError("offline only")):
                resume(cfg, permit, cls.root, clock=lambda: now, dispatch=dispatch, credential=lambda n: "K")
        cls.seal_a, cls.seal_b = cls.tmp / "SEAL_A.json", cls.tmp / "SEAL_B.json"
        seal(cls.a, cls.root, cls.seal_a)
        seal(cls.b, cls.root, cls.seal_b)
        # Fixed seal times (the fingerprint covers files, not the time), so the tests do not depend on the clock.
        for path, at in ((cls.seal_a, now + 1000), (cls.seal_b, now + 2000)):
            record = json.loads(path.read_text())
            record["sealed_utc_ms"] = at
            path.write_text(json.dumps(record))
        cls.tipoff = FIRST_TIPOFF_MS

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def export(self, name, configs, seals=None, **kw):
        from counted_export import export_config
        return export_config(self.cfg, self.journal, self.tmp / name, sealed_configs=configs,
                             sealed_root=self.root, seals=seals, **kw)

    def test_primary_recovery_and_all_attempts_are_separate(self):
        seals = [json.loads(self.seal_a.read_text()), json.loads(self.seal_b.read_text())]
        m = self.export("both", [self.a, self.b], seals)
        out = self.tmp / "both"
        primary, recovery = rows(out / "forecasts.jsonl"), rows(out / "forecasts_recovery.jsonl")
        every = rows(out / "forecasts_all_attempts.jsonl")
        self.assertEqual(len(primary), 10)
        self.assertEqual({r["attempt"] for r in primary}, {1})
        self.assertEqual(len({r["provider"] for r in primary}), 10)       # one row per provider, never mixed
        self.assertTrue(next(r for r in primary if r["provider"] == "xai")["failure"])
        rec = {r["provider"]: r for r in recovery}
        self.assertEqual(len(rec), 10)
        self.assertEqual((rec["xai"]["attempt"], "top_players" in rec["xai"]), (2, True))
        self.assertEqual((rec["qwen"]["attempt"], bool(rec["qwen"]["failure"])), (2, True))   # missing, not imputed
        self.assertEqual(rec["openai"]["attempt"], 1)
        self.assertEqual(len(every), 12)
        self.assertEqual(len(m["sealed_store_verification"]), 2)
        self.assertIn("forecasts_recovery.jsonl", (out / "DATA_CARD.md").read_text())
        # The pre-registered metrics read forecasts.jsonl only: attempt 1, one row per provider.
        from analysis.prereg_metrics import compute
        result = compute(out, winner_company="OpenAI")
        self.assertEqual(len(result["league_winner_forecasts"]), 10)

    def test_store_changed_after_its_seal_is_refused(self):
        from sealed_calls import Store
        seals = [json.loads(self.seal_b.read_text())]
        late = self.root / self.b.run_id / self.b.hash[:16] / "zz-late.result.json"
        late.write_text("{}")
        try:
            with self.assertRaises(ValueError) as e:
                self.export("late", [self.b], seals)
            self.assertIn("zz-late.result.json", str(e.exception))
        finally:
            late.unlink()
        tampered = dict(seals[0], fingerprint="0" * 64)
        with self.assertRaises(ValueError):
            self.export("fp", [self.b], [tampered])
        with self.assertRaises(ValueError):                               # seal of another config
            self.export("other", [self.b], [json.loads(self.seal_a.read_text())])
        both = [json.loads(self.seal_a.read_text()), json.loads(self.seal_b.read_text())]
        with self.assertRaises(ValueError):                               # re-ask given first (seal order)
            self.export("order", [self.b, self.a], both[::-1])
        with self.assertRaises(ValueError):                               # re-ask may not add seats
            self.export("order2", [self.b, self.a], allow_unsealed=True)
        with self.assertRaises(ValueError):                               # more attempts than declared
            self.export("three", [self.a, self.b, self.b], allow_unsealed=True)
        with self.assertRaises(ValueError):                               # unverified stores refused by default
            self.export("unsealed", [self.a])
        self.assertFalse((self.tmp / "unsealed").exists())
        m = self.export("preview", [self.a], allow_unsealed=True)
        self.assertIn("NOT_VERIFIED", m["sealed_store_verification"])

    def test_seal_made_after_the_first_tipoff_is_refused(self):
        from counted_export import verify_sealed_store
        seal = json.loads(self.seal_b.read_text())
        self.assertEqual(verify_sealed_store(self.b, self.root, seal, self.tipoff)["files"], 4)
        with self.assertRaises(ValueError):
            verify_sealed_store(self.b, self.root, dict(seal, sealed_utc_ms=self.tipoff), self.tipoff)

    def test_public_seal_coverage_refuses_changed_inputs(self):
        from counted_export import public_seal_coverage
        f = BUILD / "TEST_PUBLIC_SEAL_INPUT.txt"
        try:
            f.write_text("sealed bytes")
            files = [dict(path=f.relative_to(ROOT).as_posix(), sha256=hashlib.sha256(b"sealed bytes").hexdigest())]
            compact = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            record = self.tmp / "PUBLIC.json"
            record.write_text(json.dumps(dict(files=files, seal_sha256=hashlib.sha256(compact).hexdigest(),
                                              created_utc="2026-10-19 18:19 UTC")))
            cov = public_seal_coverage([f, self.seal_a], [record])
            self.assertEqual([c["path"] for c in cov["covered"]], [files[0]["path"]])
            self.assertEqual(len(cov["not_in_a_public_seal"]), 1)
            f.write_text("changed")
            with self.assertRaises(ValueError):
                public_seal_coverage([f], [record])
        finally:
            f.unlink(missing_ok=True)


class SegmentTreeTests(unittest.TestCase):
    def test_tree_builder_refuses_unsafe_names_and_outputs(self):
        import build_segment_tree as bst
        tmp = Path(tempfile.mkdtemp(prefix="tree-test-"))
        try:
            for name in ("../escape.py", "/abs/escape.py", "elsewhere/escape.py", "src/../../escape.py"):
                pkg = tmp / "pkg.json"
                pkg.write_text(json.dumps(dict(config=dict(run_id="x", sources=[[name, "0" * 64]]))))
                with self.assertRaises(SystemExit, msg=name):
                    bst.build(pkg, tmp / ("out-" + str(abs(hash(name)))))
            pkg.write_text(json.dumps(dict(config=dict(run_id="x", sources=[["src/fantasy_basketball/__init__.py",
                hashlib.sha256((ROOT / "src/fantasy_basketball/__init__.py").read_bytes()).hexdigest()]]))))
            with self.assertRaises(SystemExit):                            # output inside the project
                bst.build(pkg, BUILD / "decision_dataset" / "tree-out")
            self.assertFalse((BUILD / "decision_dataset" / "tree-out").exists())
            record = bst.build(pkg, tmp / "ok")
            self.assertEqual(record["restored"], 0)
            self.assertTrue((tmp / "ok" / "src/fantasy_basketball/__init__.py").is_file())
        finally:
            shutil.rmtree(tmp)


class TimingTests(unittest.TestCase):
    def test_window_starts_at_the_later_of_final_pick_and_first_tipoff(self):
        from counted_export import timing
        class C:  # minimal stand-in: timing reads only these attributes
            counted_cutoff_ms, counted_hard_limit_ms = 1000, 5000
            class draft_plan:
                cases = [0, 1]
        early = timing(C, dict(phase="COMPLETE"), [dict(status="SETTLED", settled_at_ms=t) for t in (500, 800)])
        self.assertEqual((early["scoring_window_start_ms"], early["finished_before_first_tipoff"]), (1000, True))
        self.assertIsNone(early["disclosure"])
        late = timing(C, dict(phase="COMPLETE"), [dict(status="SETTLED", settled_at_ms=t) for t in (900, 1500)])
        self.assertEqual((late["scoring_window_start_ms"], late["picks_settled_after_first_tipoff"]), (1500, 1))
        self.assertIn("already played", late["disclosure"])
        solo = timing(C, dict(phase="COMPLETE"), [dict(status="SETTLED", settled_at_ms=t) for t in (500, 800)],
                      dict(final_pick_settled_ms=1700, run_id="shared"))
        self.assertEqual(solo["scoring_window_start_ms"], 1700)              # common window = shared final pick
        later = timing(C, dict(phase="COMPLETE"), [dict(status="SETTLED", settled_at_ms=t) for t in (900, 1900)],
                       dict(final_pick_settled_ms=1700))
        self.assertEqual(later["scoring_window_start_ms"], 1900)             # or the solo run's own later final pick
        with self.assertRaises(ValueError):
            timing(C, dict(phase="COMPLETE"), [], dict(final_pick_settled_ms=None))
        partial = timing(C, dict(phase="PAUSED"), [dict(status="SETTLED", settled_at_ms=900)])
        self.assertIsNone(partial["scoring_window_start_ms"])


if __name__ == "__main__":
    unittest.main()
