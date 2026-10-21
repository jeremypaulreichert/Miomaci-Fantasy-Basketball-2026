"""Integration receipt checks using settled picks, never hypothetical replacements."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

from test_build_core import BuildCase, configuration
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.providers.fixtures import FixtureTransport, response


class RosterExportTests(BuildCase):
    def complete(self, correction=False):
        second = replace(self.config.providers[0], manager="ai-b", model="fixture-b")
        config = configuration(providers=(self.config.providers[0], second), roster_size=2)
        draft = self.draft(config)
        available = ["p1", "p2", "p3", "p4"]
        choices = ["p4", "p2", "p1", "p3"]
        for index, selected in enumerate(choices):
            draft.open(available)
            replies = {f"pick:{index}:attempt:0": response(config.at_pick(index), selected)}
            if correction and index == 0:
                replies["pick:0:attempt:0"] = response(config.at_pick(0), "not-legal")
                replies["pick:0:attempt:1"] = response(config.at_pick(0), selected)
            draft.resume(FixtureTransport(replies))
            available.remove(selected)
        return draft, config

    def test_receipt_identity_ownership_and_evidence(self):
        draft, config = self.complete(correction=True)
        receipt = draft.export_roster()
        self.assertEqual(receipt["experiment_id"], config.experiment_id)
        self.assertEqual(receipt["run_id"], config.run_id)
        self.assertEqual(receipt["config_hash"], config.hash)
        self.assertEqual(receipt["draft_rules_digest"], config.rules_digest)
        self.assertEqual(receipt["schema_version"], "miomaci.basketball.settled-roster/1")
        self.assertIs(receipt["simulated"], True)
        self.assertIs(receipt["provisional"], True)
        self.assertIs(receipt["competitive_authorization"], False)
        self.assertEqual(receipt["rosters"], [
            {"manager_id": "ai-a", "model_id": "fixture-a", "player_ids": ["p4", "p3"]},
            {"manager_id": "ai-b", "model_id": "fixture-b", "player_ids": ["p2", "p1"]},
        ])
        self.assertEqual(receipt["picks"], draft.status()["picks"])
        self.assertEqual(receipt["picks"][0]["attempt"], 1)
        body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        self.assertEqual(receipt["receipt_hash"], digest(body))

    def test_export_is_read_only_and_restart_invariant(self):
        draft, config = self.complete()
        database_before = draft.journal.path.read_bytes()
        before = canonical(draft.export_roster()).encode("utf-8")
        self.clock.advance(100000)
        restarted = self.draft(config)
        unused = FixtureTransport({})
        restarted.resume(unused)
        after = canonical(restarted.export_roster()).encode("utf-8")
        self.assertEqual(before, after)
        self.assertEqual(draft.journal.path.read_bytes(), database_before)
        self.assertEqual(unused.calls, [])

    def test_mutating_returned_receipt_cannot_change_durable_roster(self):
        draft, _ = self.complete()
        receipt = draft.export_roster()
        receipt["picks"][0]["player_id"] = "invented"
        receipt["rosters"][0]["player_ids"].append("invented")
        self.assertNotIn("invented", canonical(draft.export_roster()))

    def test_empty_and_partial_drafts_are_not_exported(self):
        config = configuration(roster_size=2)
        draft = self.draft(config)
        with self.assertRaisesRegex(ValueError, "complete settled draft"):
            draft.export_roster()
        draft.open(["p1", "p2"])
        draft.resume(FixtureTransport({"pick:0:attempt:0": response(config.providers[0], "p1")}))
        with self.assertRaisesRegex(ValueError, "complete settled draft"):
            draft.export_roster()

    def test_paused_partial_draft_has_no_export_or_replacement(self):
        config = configuration(roster_size=2)
        draft = self.draft(config)
        draft.open(["p1", "p2"])
        draft.resume(FixtureTransport({"pick:0:attempt:0": response(config.providers[0], "p1")}))
        draft.open(["p2"])
        draft.resume(FixtureTransport({"pick:1:attempt:0": TimeoutError("unknown")}))
        before = draft.status()
        with self.assertRaisesRegex(ValueError, "complete settled draft"):
            self.draft(config).export_roster()
        self.assertEqual(draft.status(), before)
        self.assertEqual(len(before["picks"]), 1)

    def test_cli_export_does_not_start_a_missing_draft(self):
        root = Path(__file__).resolve().parents[1]
        run_id = "missing-" + self.directory.name
        result = subprocess.run([sys.executable, "-B", str(root / "workstreams/build/demo.py"),
                                 "export", "--run-id", run_id], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.directory.parent / "demo/success" / (run_id + ".sqlite3")).exists())
