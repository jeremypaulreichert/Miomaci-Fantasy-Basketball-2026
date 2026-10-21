"""Offline proposal binding and fail-closed preflight checks; no live execution."""
from fractions import Fraction
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workstreams/build"))
import trial_preflight as preflight
from fantasy_basketball.core.config import canonical, digest


class TrialPreflightTests(support.BuildCase):
    def copy_inputs(self):
        candidate = json.loads((preflight.ROOT / preflight.CANDIDATE).read_text())
        paths = [*candidate["files"], preflight.CANDIDATE, preflight.PLAN, preflight.PACKAGE_INITIALIZER,
                 "workstreams/build/trial_preflight.py", "tests/test_build_trial_preflight.py"]
        for name in paths:
            target = self.directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(preflight.ROOT / name, target)
        return self.directory

    def test_package_is_deterministic_bound_and_not_authority(self):
        result = preflight.prepare_package()
        self.assertEqual(canonical(result), canonical(preflight.prepare_package()))
        self.assertEqual(result["config_hash"], digest(result["configuration"]))
        self.assertEqual(result["package_hash"], digest({k: v for k, v in result.items() if k != "package_hash"}))
        self.assertIs(result["execution_authorized"], False)
        self.assertIs(result["models_qualified"], False)
        self.assertIsNone(result["owner_authorization"])
        self.assertIsNone(result["bound_review_record"])
        self.assertIsNone(result["provider_compatibility_record"])
        # Independent rational arithmetic for every case; no production scorer
        # or budget helper supplies this expectation.
        maximum = 0
        for case in result["configuration"]["cases"]:
            p = case["profile"]
            amount = Fraction(p["input_usd_per_million"]) * p["input_tokens"] + Fraction(p["output_usd_per_million"]) * p["output_tokens"]
            bound = -(-amount.numerator // amount.denominator) + p["safety_micro_usd"]
            maximum += bound * p["max_attempts"]
        self.assertEqual(maximum, 92654832)
        self.assertEqual(result["bounds"], {"original_requests": 24, "max_requests": 48,
                                          "max_exposure_micro_usd": maximum, "budget_micro_usd": 93000000,
                                          "trial_ms": 5760000})

    def test_candidate_bytes_or_source_drift_is_rejected(self):
        root = self.copy_inputs()
        source = root / "src/fantasy_basketball/draft/trial.py"
        original = source.read_bytes()
        source.write_bytes(original + b"\n# changed\n")
        with self.assertRaisesRegex(ValueError, "candidate file changed"):
            preflight.prepare_package(root=root)
        source.write_bytes(original)
        manifest = root / preflight.CANDIDATE
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "review candidate changed"):
            preflight.prepare_package(root=root)

    def test_saved_proposal_drift_including_false_like_authority_is_rejected(self):
        root = self.copy_inputs()
        path = root / preflight.PLAN
        original = json.loads(path.read_text())
        for name, value in (("paid_authorization", 0), ("max_exposure_micro_usd", 1), ("plan_hash", "0" * 64)):
            changed = {**original, name: value}
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "saved proposal differs"):
                preflight.prepare_package(root=root)

    def test_run_identity_is_bound_and_invalid_path_names_are_rejected(self):
        first = preflight.prepare_package("candidate-a")
        second = preflight.prepare_package("candidate-b")
        self.assertNotEqual(first["config_hash"], second["config_hash"])
        self.assertNotEqual(first["package_hash"], second["package_hash"])
        self.assertEqual(first["bounds"], second["bounds"])
        for name in ("../escape", "CON", ""):
            with self.assertRaises(ValueError):
                preflight.prepare_package(name)

    def test_preparation_never_creates_journal_transport_or_network(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch(
                "fantasy_basketball.draft.trial.Journal", side_effect=AssertionError("journal forbidden")), patch(
                "fantasy_basketball.providers.transport.HTTPTransport.__init__", side_effect=AssertionError("transport forbidden")), patch(
                "fantasy_basketball.providers.transport.bounded_process", side_effect=AssertionError("worker forbidden")):
            result = preflight.prepare_package()
        self.assertEqual(result["external_requests"], 0)
        self.assertEqual(result["paid_requests"], 0)
        self.assertIsNone(result["owner_authorization"])

    def test_request_previews_preserve_case_order_hashes_and_limits(self):
        result = preflight.prepare_package()
        self.assertEqual(len(result["initial_requests"]), 24)
        for index, (row, case) in enumerate(zip(result["initial_requests"], result["configuration"]["cases"])):
            request, profile = row["request"], case["profile"]
            self.assertEqual(row["case_index"], index)
            self.assertEqual(row["case_id"], case["board"]["case_id"])
            self.assertEqual(request["request_hash"], digest(request["body"]))
            self.assertEqual(request["profile_hash"], digest(profile))
            self.assertEqual(request["endpoint"], result["endpoints"][profile["provider"]])
            field = "max_output_tokens" if profile["provider"] == "openai" else "max_tokens"
            self.assertEqual(request["body"][field], profile["output_tokens"])
            self.assertLessEqual(request["request_bytes"], profile["request_bytes"])
            self.assertIs(request["execution_authorized"], False)
        self.assertIs(result["correction_policy"]["organizer_selection"], False)
        self.assertIs(result["correction_policy"]["transport_retry"], False)
