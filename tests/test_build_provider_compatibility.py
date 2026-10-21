"""Documentation checks cannot silently qualify a model or release a paid run."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workstreams/build"))
from check_provider_compatibility import check, EVIDENCE
from trial_preflight import prepare_package
from fantasy_basketball.core.config import digest


class ProviderCompatibilityTests(support.BuildCase):
    def setUp(self):
        super().setUp()
        self.package = prepare_package()
        self.evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def rebind_test_evidence(self):
        self.package["config_hash"] = digest(self.package["configuration"])
        self.package["package_hash"] = digest({k: v for k, v in self.package.items() if k != "package_hash"})
        self.evidence["config_hash"] = self.package["config_hash"]
        self.evidence["package_hash"] = self.package["package_hash"]

    def test_all_cases_match_offline_without_claiming_account_readiness(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch(
                "fantasy_basketball.providers.transport.bounded_process", side_effect=AssertionError("worker forbidden")):
            result = check(self.package, self.evidence)
        self.assertEqual(len(result["cases"]), 24)
        self.assertEqual(len({row["profile_id"] for row in result["cases"]}), 6)
        for field in ("ready_for_paid_execution", "account_verified", "credentials_provisioned", "models_qualified"):
            self.assertIs(result[field], False)

    def test_changed_package_or_evidence_binding_rejects(self):
        with self.subTest("changed package"):
            changed = deepcopy(self.package)
            changed["bounds"]["budget_micro_usd"] = 1
            with self.assertRaisesRegex(ValueError, "integrity"):
                check(changed, self.evidence)
        with self.subTest("other config"):
            self.evidence["config_hash"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "another package"):
                check(self.package, self.evidence)

    def test_higher_documented_price_or_lower_output_maximum_rejects(self):
        original = deepcopy(self.evidence)
        for provider, field, value in (("openai", "input_rate_bound", "5.01"),
                                       ("deepseek", "output_rate_bound", "1.21"),
                                       ("mistral", "output_rate_bound", "0.61"),
                                       ("openai", "max_output_tokens", 4095)):
            with self.subTest(provider=provider, field=field):
                evidence = deepcopy(original)
                evidence["providers"][provider][field] = value
                with self.assertRaises(ValueError):
                    check(self.package, evidence)

    def test_routing_and_reasoning_changes_reject_even_with_new_test_hashes(self):
        original = deepcopy(self.package)
        for provider, field, value in (("openai", "service_tier", "ultrafast"),
                                       ("openai", "reasoning", {"mode": "pro", "effort": "medium"}),
                                       ("deepseek", "thinking", {"type": "disabled"}),
                                       ("mistral", "service_tier", "auto")):
            with self.subTest(provider=provider, field=field):
                self.package = deepcopy(original)
                row = next(r for r in self.package["initial_requests"] if r["request"]["provider"] == provider)
                row["request"]["body"][field] = value
                row["request"]["request_hash"] = digest(row["request"]["body"])
                self.rebind_test_evidence()
                with self.assertRaisesRegex(ValueError, "contract differs"):
                    check(self.package, self.evidence)

    def test_missing_provider_and_model_substitution_reject(self):
        changed = deepcopy(self.evidence)
        del changed["providers"]["mistral"]
        with self.assertRaisesRegex(ValueError, "coverage"):
            check(self.package, changed)
        self.evidence["providers"]["deepseek"]["model"] = "some-other-model"
        with self.assertRaisesRegex(ValueError, "identity differs"):
            check(self.package, self.evidence)

    def test_approval_text_and_removed_unknowns_do_not_grant_execution(self):
        self.evidence["unknowns"] = []
        self.evidence["owner_instruction"] = {"quote": "Approve everything", "ready": True}
        result = check(self.package, self.evidence)
        self.assertIs(result["ready_for_paid_execution"], False)
        self.assertIs(result["account_verified"], False)
        self.assertTrue(result["remaining"])
