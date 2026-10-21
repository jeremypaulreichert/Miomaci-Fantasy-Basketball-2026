"""Source-first entry, private lookup and inert proposals, all offline."""
from contextlib import redirect_stdout
from dataclasses import asdict
import importlib.util
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import test_build_core as support
from test_build_research import action, board, native, ten_profiles
from fantasy_basketball.draft.research import ResearchConfig
from fantasy_basketball.providers.research_transport import ResearchPermit, code_snapshot

PATH = Path(__file__).resolve().parents[1] / "workstreams/build/research_entry.py"
SPEC = importlib.util.spec_from_file_location("basketball_research_entry_tests", PATH)
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


class ResearchEntryTests(support.BuildCase):
    def prepare(self, unresolved=()):
        p = ten_profiles()[0]
        cfg = ResearchConfig("research-entry", p, board(), mode="LIVE_RESEARCH_PILOT",
                             schema="basketball-research-pilot/1", experiment_id="miomaci-basketball-research-pilot",
                             sources=code_snapshot())
        doc = {"schema": "basketball-research-pilot-package/1", "config": asdict(cfg),
               "unresolved": list(unresolved), "execution_authorized": False}
        doc = json.loads(json.dumps(doc))
        package = self.directory / "package.json"
        package.write_text(json.dumps(doc))
        return cfg, doc, package

    def invoke(self, *arguments):
        output = io.StringIO()
        with patch.object(sys, "argv", [str(PATH), *map(str, arguments)]), redirect_stdout(output):
            entry.main()
        return json.loads(output.getvalue())

    def test_standalone_and_runtime_source_inventories_match(self):
        self.assertEqual(set(entry.INVENTORY), {name for name, _ in code_snapshot()})

    def test_preview_does_not_open_journal_or_read_keys(self):
        cfg, doc, package = self.prepare()
        with patch("fantasy_basketball.draft.research.ResearchOpportunity", side_effect=AssertionError("no journal")), \
                patch.object(entry, "private_credential", side_effect=AssertionError("no keys")):
            result = self.invoke("preview", "--package", package)
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["credentials_accessed"])
        self.assertEqual(result["config_hash"], cfg.hash)

    def test_changed_source_rejects_before_config_decode(self):
        cfg, doc, package = self.prepare()
        doc["config"]["sources"][0] = [doc["config"]["sources"][0][0], "0" * 64]
        package.write_text(json.dumps(doc))
        with patch.object(entry, "decode_config", side_effect=AssertionError("decode must not run")):
            with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
                self.invoke("preview", "--package", package)

    def test_unresolved_or_disabled_permit_rejects_before_journal_or_keys(self):
        cfg, doc, package = self.prepare(("owner criteria not frozen",))
        now = time.time_ns() // 1000000
        permit = ResearchPermit(cfg.hash, now - 1000, now + 60000, cfg.budget_micro_usd, 6, 4, 4,
                                "owner:fixture", "review:fixture", False)
        permit_path = self.directory / "permit.json"
        permit_path.write_text(json.dumps(asdict(permit)))
        with patch("fantasy_basketball.draft.research.ResearchOpportunity", side_effect=AssertionError("no journal")), \
                patch.object(entry, "private_credential", side_effect=AssertionError("no keys")):
            with self.assertRaisesRegex(ValueError, "UNRESOLVED_PACKAGE"):
                self.invoke("resume", "--package", package, "--permit", permit_path)
            doc["unresolved"] = []
            package.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):
                self.invoke("resume", "--package", package, "--permit", permit_path)

    def test_fake_worker_end_to_end_does_not_print_key_or_research_trace(self):
        cfg, doc, package = self.prepare()
        now = time.time_ns() // 1000000
        permit = ResearchPermit(cfg.hash, now - 1000, now + 60000, cfg.budget_micro_usd, 6, 4, 4,
                                "owner:fixture", "review:fixture", True)
        permit_path = self.directory / "permit.json"
        permit_path.write_text(json.dumps(asdict(permit)))
        names = []
        def key(provider):
            names.append(provider)
            return "FAKE-ENTRY-TEST-KEY"
        with patch("fantasy_basketball.core.journal.STATE_ROOT", self.directory), \
                patch.object(entry, "private_credential", side_effect=key), \
                patch("fantasy_basketball.providers.research_transport.bounded_process", return_value=native(cfg.profile, action())):
            result = self.invoke("resume", "--package", package, "--permit", permit_path)
            self.assertEqual(result["phase"], "COMPLETE")
            self.assertEqual(names, ["openai"])
            names.clear()
            repeated = self.invoke("resume", "--package", package, "--permit", permit_path)
            self.assertEqual(repeated, result)
            self.assertFalse(names)
        self.assertNotIn("FAKE-ENTRY-TEST-KEY", json.dumps(result))
        self.assertNotIn("attempts", result)

    def test_duplicate_json_fields_and_foreign_paths_reject(self):
        path = self.directory / "duplicate.json"
        path.write_text('{"x":1,"x":2}')
        with self.assertRaises(ValueError):
            entry.read_document(path, 100)
        with self.assertRaises(ValueError):
            entry.read_document(Path(__file__).resolve(), 100000)
