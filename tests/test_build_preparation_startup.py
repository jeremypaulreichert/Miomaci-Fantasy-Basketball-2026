"""Fresh-process drift probes: no candidate code may run before byte checks."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import test_build_core as support

ROOT = Path(__file__).resolve().parents[1]


class PreparationStartupTests(support.BuildCase):
    def copy_candidate(self):
        manifest = json.loads((ROOT / "workstreams/build/AWAY_REPAIR_ENGINE_CANDIDATE_2026-10-17.json").read_text())
        paths = [ROOT / p for p in manifest["files"]]
        paths.extend(ROOT / p for p in (
            "src/fantasy_basketball/__init__.py", "workstreams/build/AWAY_REPAIR_ENGINE_CANDIDATE_2026-10-17.json",
            "workstreams/build/LIVE_TRIAL_PLAN.json", "workstreams/build/trial_preflight.py",
            "tests/test_build_trial_preflight.py", "workstreams/build/check_provider_compatibility.py"))
        for pattern in ("NATIVE_TRIAL_PREFLIGHT*.json", "PROVIDER_COMPATIBILITY*.json"):
            paths.extend((ROOT / "workstreams/build").glob(pattern))
        for source in paths:
            target = self.directory / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def run_cli(self, name):
        return subprocess.run([sys.executable, "-B", str(self.directory / "workstreams/build" / name)],
                              cwd=self.directory, text=True, capture_output=True, timeout=20)

    def changed_module(self, name):
        self.copy_candidate()
        marker = self.directory / "unexpected-import.marker"
        target = self.directory / name
        target.write_text(target.read_text(encoding="utf-8") +
                          "\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n", encoding="utf-8")
        for cli in ("trial_preflight.py", "check_provider_compatibility.py"):
            with self.subTest(cli=cli):
                result = self.run_cli(cli)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("changed", result.stderr)
                self.assertFalse(marker.exists(), "changed code executed before rejection")

    def test_changed_pinned_module_rejected_before_import(self):
        self.changed_module("workstreams/build/provider_cases.py")

    def test_changed_package_initializer_rejected_before_import(self):
        self.changed_module("src/fantasy_basketball/__init__.py")

    def test_fresh_unchanged_commands_are_read_only_and_do_not_dispatch(self):
        self.copy_candidate()
        before = {p.relative_to(self.directory): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        for cli, status in (("trial_preflight.py", "PREPARED_NOT_AUTHORIZED"),
                            ("check_provider_compatibility.py", "DOCUMENTATION_MATCH_ACCOUNT_CHECKS_PENDING")):
            result = self.run_cli(cli)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], status)
            self.assertEqual(output["paid_requests"], 0)
        after = {p.relative_to(self.directory): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
