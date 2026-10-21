"""Direct documentation-check API must work without preloading the engine."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class CompatibilityAPITests(unittest.TestCase):
    def test_fresh_direct_call_requires_no_prior_preflight_or_engine_import(self):
        root = Path(__file__).resolve().parents[1]
        script = """
import json, sys
def guard(event, args):
    if event in ('socket.__new__', 'sqlite3.connect', 'subprocess.Popen'):
        raise AssertionError('execution forbidden')
sys.addaudithook(guard)
sys.path.insert(0, sys.argv[1])
from check_provider_compatibility import check, EVIDENCE, PACKAGE_PATH
result = check(json.loads(PACKAGE_PATH.read_text()), json.loads(EVIDENCE.read_text(encoding='utf-8')))
print(json.dumps({'status': result['status'], 'ready': result['ready_for_paid_execution'],
                  'engine_loaded': any(n == 'fantasy_basketball' or n.startswith('fantasy_basketball.') for n in sys.modules)}))
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(root / "workstreams/build")],
                                cwd=root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "DOCUMENTATION_MATCH_ACCOUNT_CHECKS_PENDING",
                                                    "ready": False, "engine_loaded": False})
