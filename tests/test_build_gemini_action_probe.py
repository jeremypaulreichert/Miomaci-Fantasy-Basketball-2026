"""Offline diagnostic request construction; no runtime changes or paid calls."""
from copy import deepcopy

import test_build_core as support
from test_build_research import board, ten_profiles
from fantasy_basketball.core.config import digest, canonical
from fantasy_basketball.providers.research import request
from gemini_action_probe import schema_candidate


class GeminiActionProbeTests(support.BuildCase):
    def original(self):
        profile = next(p for p in ten_profiles() if p.provider == "gemini")
        return profile, request(profile, board("gemini"), [], {}, 10000, 1210000)

    def test_exactly_one_generation_setting_changes_and_original_is_untouched(self):
        profile, original = self.original()
        before = deepcopy(original)
        candidate = schema_candidate(original, profile.request_bytes)
        changed = deepcopy(candidate["body"])
        schema = changed["generationConfig"].pop("responseJsonSchema")
        self.assertEqual(changed, original["body"])
        self.assertEqual(original, before)
        self.assertEqual(schema["properties"]["action"]["enum"], ["search", "read", "pick"])
        self.assertEqual(schema["properties"]["query"]["type"], ["string", "null"])
        self.assertEqual(candidate["request_hash"], digest(candidate["body"]))
        self.assertNotEqual(candidate["request_hash"], original["request_hash"])
        self.assertEqual(candidate["request_bytes"], len(canonical(candidate["body"]).encode()))
        self.assertEqual(candidate["deadline_ms"], original["deadline_ms"])
        self.assertEqual(candidate["reservation_micro_usd"], original["reservation_micro_usd"])

    def test_wrong_provider_hash_existing_schema_tools_and_small_cap_reject(self):
        profile, original = self.original()
        for alteration in ("provider", "hash", "schema", "tools", "cap"):
            with self.subTest(alteration=alteration):
                value = deepcopy(original)
                if alteration == "provider": value["provider"] = "openai"
                if alteration == "hash": value["request_hash"] = "0" * 64
                if alteration == "schema": value["body"]["generationConfig"]["responseSchema"] = {}
                if alteration == "tools": value["body"]["tools"] = []
                if alteration in {"schema", "tools"}:
                    value.update(request_hash=digest(value["body"]), request_bytes=len(canonical(value["body"]).encode()))
                with self.assertRaises(ValueError):
                    schema_candidate(value, 1 if alteration == "cap" else profile.request_bytes)


if __name__ == "__main__":
    import unittest
    unittest.main()
