"""Regression cases drawn from football failures; all responses are synthetic."""
from dataclasses import replace
import json
import unittest

import test_build_core as support
from fantasy_basketball.core.config import canonical
from fantasy_basketball.providers.fixtures import FixtureTransport, response


class FailureRepairTests(support.BuildCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, schema="basketball-build/2")

    def wrapped(self, player="p1"):
        return canonical({"answer": canonical({"player_id": player, "rationale": "Synthetic choice."})})

    def test_explicit_schema_and_same_ai_correction_of_missing_player(self):
        draft = self.draft()
        draft.open(["p1", "p2"])
        profile = self.config.providers[0]
        transport = FixtureTransport({
            "pick:0:attempt:0": response(profile, None, text='{"rationale":"Synthetic choice."}'),
            "pick:0:attempt:1": response(profile, "p2"),
        })
        result = draft.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["picks"][0]["player_id"], "p2")
        first, correction = [row["request"]["body"] for row in transport.calls]
        self.assertEqual(first["response_schema"]["required"], ["player_id", "rationale"])
        self.assertEqual(first["response_schema"]["properties"]["player_id"]["enum"], ["p1", "p2", None])
        self.assertEqual(first["response_schema"], correction["response_schema"])
        self.assertEqual(first["model"], correction["model"])
        self.assertEqual(correction["input"]["correction"]["failure"], "MALFORMED_ACTION")

    def test_exact_wrapper_preserves_original_and_restart_dispatches_nothing(self):
        draft = self.draft()
        draft.open(["p1"])
        raw = response(self.config.providers[0], "p1", text=self.wrapped())
        transport = FixtureTransport({"pick:0:attempt:0": raw})
        result = draft.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["picks"][0]["origin"], "SIMULATED_AI")
        self.assertEqual(draft.journal.read()["opportunities"][0]["attempts"][0]["response"], raw)
        unused = FixtureTransport({})
        self.assertEqual(self.draft().resume(unused), result)
        self.assertEqual(unused.calls, [])

    def test_wrapped_owned_player_still_requires_same_ai_correction(self):
        config = replace(self.config, roster_size=2)
        draft = self.draft(config)
        profile = config.providers[0]
        draft.open(["p1", "p2"])
        first = draft.resume(FixtureTransport({"pick:0:attempt:0": response(profile, "p1")}))
        draft.open(["p2"])
        transport = FixtureTransport({
            "pick:1:attempt:0": response(profile, "p1", text=self.wrapped("p1")),
            "pick:1:attempt:1": response(profile, "p2", text=self.wrapped("p2")),
        })
        result = draft.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(result["picks"][0], first["picks"][0])
        self.assertEqual(result["picks"][1]["player_id"], "p2")
        self.assertEqual(result["picks"][1]["attempt"], 1)
        self.assertEqual(transport.calls[1]["request"]["body"]["input"]["correction"]["failure"], "ILLEGAL_PICK")

    def test_ambiguous_nested_duplicate_and_nonstring_wrappers_pause(self):
        duplicate = '{"player_id":"p1","player_id":"p2","rationale":"x"}'
        cases = [canonical({"answer": self.wrapped()}),
                 canonical({"answer": duplicate}),
                 canonical({"answer": {}, "player_id": "p1"}),
                 canonical({"answer": {"player_id": "p1", "rationale": "x"}}),
                 canonical({"answer": json.loads(self.wrapped())["answer"], "extra": "x"}),
                 "```json\n" + self.wrapped() + "\n```"]
        for index, text in enumerate(cases):
            with self.subTest(index=index):
                config = replace(self.config, run_id=f"wrapper-{index}", correction_failures=())
                draft = self.draft(config)
                draft.open(["p1", "p2"])
                result = draft.resume(FixtureTransport({"pick:0:attempt:0": response(config.providers[0], "p1", text=text)}))
                self.assertEqual(result["pause"]["reason"], "MALFORMED_ACTION")
                self.assertEqual(result["picks"], [])
                unused = FixtureTransport({})
                self.assertEqual(self.draft(config).resume(unused), result)
                self.assertEqual(unused.calls, [])

    def test_wrapper_does_not_bypass_hold_output_limit_or_identity(self):
        profile = self.config.providers[0]
        cases = [(response(profile, None, text=self.wrapped(None)), "AI_HOLD")]
        exhausted = response(profile, "p1", text=self.wrapped())
        exhausted["stop"] = "length"
        cases.append((exhausted, "INCOMPLETE_RESPONSE"))
        wrong = response(profile, "p1", text=self.wrapped())
        wrong["model"] = "fixture-other"
        cases.append((wrong, "IDENTITY_MISMATCH"))
        for index, (raw, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                config = replace(self.config, run_id=f"guards-{index}")
                draft = self.draft(config)
                draft.open(["p1"])
                transport = FixtureTransport({"pick:0:attempt:0": raw})
                result = draft.resume(transport)
                self.assertEqual(result["pause"]["reason"], reason)
                self.assertEqual(result["picks"], [])
                self.assertEqual(len(transport.calls), 1)
                self.assertGreater(result["accounting"]["unresolved_exposure_micro_usd"], 0)

    def test_legacy_contract_is_unchanged_and_profile_switch_is_rejected(self):
        legacy = replace(self.config, schema="basketball-build/1", correction_failures=())
        draft = self.draft(legacy)
        draft.open(["p1"])
        transport = FixtureTransport({"pick:0:attempt:0": response(legacy.providers[0], "p1", text=self.wrapped())})
        result = draft.resume(transport)
        self.assertEqual(result["pause"]["reason"], "MALFORMED_ACTION")
        self.assertNotIn("response_schema", transport.calls[0]["request"]["body"])
        self.assertNotIn("instructions", transport.calls[0]["request"]["body"]["input"])
        with self.assertRaises(ValueError):
            self.draft(replace(legacy, schema="basketball-build/2"))


if __name__ == "__main__":
    unittest.main()
