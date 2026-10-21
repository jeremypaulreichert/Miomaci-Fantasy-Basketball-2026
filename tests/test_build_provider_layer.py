"""Native contracts and changing-board checks, with zero network access."""
from dataclasses import replace
from itertools import combinations, permutations
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workstreams/build"))
from provider_cases import cases
from fantasy_basketball.draft.board import Board, Player
from fantasy_basketball.providers.native import inspect, prepare
from fantasy_basketball.providers.qualification import candidate_profiles, fixture_config, NativeFixtureTransport, synthetic_response


class BoardTests(unittest.TestCase):
    def test_late_round_owned_locked_and_positional_traps(self):
        boards = cases("openai")
        self.assertNotIn("p08", boards["opening"].legal_ids())
        self.assertNotIn("p01", boards["owned_star"].legal_ids())
        self.assertEqual(boards["last_slot"].legal_ids(), ("p05",))
        self.assertEqual(boards["flexibility"].legal_ids(), ("p04", "p06"))
        next_board = replace(boards["flexibility"], ownership=(*boards["flexibility"].ownership, ("p06", "openai")))
        self.assertEqual(next_board.legal_ids(), ("p04",))
        impossible = replace(boards["last_slot"], locked_ids=("p05", "p08"))
        self.assertEqual(impossible.legal_ids(), ())

    def test_packet_retains_every_fact_and_unknown_value_without_rank(self):
        board = cases("openai")["owned_star"]
        packet = board.packet()
        self.assertEqual(len(packet["players"]), len(board.players))
        for player, row in zip(sorted(board.players, key=lambda p: p.player_id), packet["players"]):
            self.assertEqual(row[:3], [player.player_id, player.name, list(player.positions)])
            self.assertEqual(row[5], dict(player.facts))
        self.assertIsNone(next(row for row in packet["players"] if row[0] == "p07")[5]["sample_points"])
        self.assertEqual(next(row for row in packet["players"] if row[0] == "p01")[3], "other-manager")
        packet["players"][0][5]["sample_points"] = "9999"
        self.assertEqual(board.players[0].facts, (("sample_points", "40"),))

    def test_eligibility_matches_separate_exhaustive_reference(self):
        rng = random.Random(52351)
        for case in range(80):
            players = tuple(Player(f"p{i}", f"Player {i}", tuple(p for p in "GFC" if rng.choice((True, False))) or ("G",)) for i in range(6))
            own = tuple(f"p{i}" for i in range(rng.randrange(3)))
            slots = tuple((f"slot{i}", tuple(p for p in "GFC" if rng.choice((True, False))) or ("C",)) for i in range(rng.choice((2, 3))))
            board = Board(f"case-{case}", "openai", case, players,
                          tuple((p, "openai") for p in own) + (("p5", "other"),), ("p4",),
                          3, slots, "SYNTHETIC")
            available = {p.player_id for p in players} - set(own) - {"p4", "p5"}
            positions = {p.player_id: set(p.positions) for p in players}
            expected = set()
            # Enumerate complete rosters and slot permutations. No production
            # matcher or scorer is used by this reference calculation.
            for additions in combinations(sorted(available), 3 - len(own)):
                roster = (*own, *additions)
                if any(all(positions[player].intersection(allowed)
                           for player, (_, allowed) in zip(assignment, slots))
                       for assignment in permutations(roster, len(slots))):
                    expected.update(additions)
            self.assertEqual(set(board.legal_ids()), expected, f"case {case}")

    def test_duplicate_ownership_and_invalid_rule_shapes_fail(self):
        board = cases("openai")["opening"]
        for change in ({"ownership": (("p01", "a"), ("p01", "b"))}, {"players": list(board.players)},
                       {"locked_ids": ("missing",)}, {"provisional": 1}, {"roster_size": 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(board, **change)


class NativeContractTests(unittest.TestCase):
    def test_native_output_cap_and_reservation_change_together(self):
        profiles = candidate_profiles()
        for smaller, larger in zip(profiles[::2], profiles[1::2]):
            board = cases(smaller.provider)["opening"]
            a = prepare(smaller, board, now_ms=1000, deadline_ms=10000)
            b = prepare(larger, board, now_ms=1000, deadline_ms=10000)
            field = smaller.fixture_limits().output_field
            self.assertEqual((a["body"][field], b["body"][field]), (4096, 16384))
            self.assertGreater(b["reservation_micro_usd"], a["reservation_micro_usd"])
            self.assertEqual(a["timeout_ms"], 9000)
            self.assertFalse(a["execution_authorized"])
            self.assertNotIn("tools", a["body"])

    def test_exact_provider_fields_and_explicit_reasoning(self):
        for profile in candidate_profiles()[::2]:
            body = prepare(profile, cases(profile.provider)["opening"], now_ms=0, deadline_ms=5000)["body"]
            self.assertEqual(body[profile.fixture_limits().output_field], profile.output_tokens)
            if profile.provider == "openai":
                self.assertEqual(body["reasoning"], {"effort": "medium"})
                self.assertEqual(body["text"]["format"]["type"], "json_schema")
                self.assertFalse(body["store"])
            elif profile.provider == "deepseek":
                self.assertEqual(body["thinking"], {"type": "enabled"})
                self.assertNotIn("n", body)
                self.assertEqual(body["reasoning_effort"], "high")
            else:
                self.assertEqual(body["n"], 1)
                self.assertEqual(body["service_tier"], "standard_only")
                self.assertEqual(body["reasoning_effort"], "high")

    def test_no_unproven_byte_to_token_bound_or_unbounded_profile(self):
        profile = candidate_profiles()[0]
        for change in ({"input_tokens": 10000}, {"input_bound_basis": "BYTES"}, {"max_attempts": 3},
                       {"input_usd_per_million": "NaN"}, {"timeout_ms": 300000}, {"output_tokens": True},
                       {"accepted_models": []}, {"effort": "none"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(profile, **change)

    def test_byte_and_deadline_limits_and_no_legal_roster_fail_before_request(self):
        profile = candidate_profiles()[0]
        board = cases(profile.provider)["opening"]
        with self.assertRaises(ValueError):
            prepare(replace(profile, request_bytes=10), board, now_ms=0, deadline_ms=1000)
        with self.assertRaises(ValueError):
            prepare(profile, board, now_ms=1000, deadline_ms=1000)
        board = replace(cases(profile.provider)["last_slot"], locked_ids=("p05", "p08"))
        with self.assertRaises(ValueError):
            prepare(profile, board, now_ms=0, deadline_ms=1000)

    def test_native_identity_usage_and_reasoning_are_observed(self):
        for profile in candidate_profiles()[::2]:
            raw = synthetic_response(profile, "p01")
            receipt = inspect(profile, raw, ["p01"])
            self.assertIsNone(receipt["failure"])
            self.assertEqual(receipt["observed_model"], profile.model)
            self.assertEqual(receipt["usage"]["output_tokens"], 100)
            if profile.provider == "mistral":
                self.assertIsNone(receipt["usage"]["reasoning_tokens"])
            else:
                self.assertEqual(receipt["usage"]["reasoning_tokens"], 50)
            self.assertNotIn("SYNTHETIC_TRACE_NOT_EXPORTED", json.dumps(receipt))
            self.assertLess(receipt["estimated_charge_micro_usd"], profile.bound_micro_usd)

    def test_identity_missing_usage_and_incoherent_totals_fail_closed(self):
        for profile in candidate_profiles()[::2]:
            for change, failure in (({"model": "other-model"}, "IDENTITY_MISMATCH"),
                                    ({"id": None}, "IDENTITY_MISMATCH"),
                                    ({"usage": None}, "MISSING_OR_INVALID_USAGE")):
                raw = synthetic_response(profile, "p01")
                raw.update(change)
                self.assertEqual(inspect(profile, raw, ["p01"])["failure"], failure)
            raw = synthetic_response(profile, "p01")
            raw["usage"]["total_tokens"] = 1
            self.assertEqual(inspect(profile, raw, ["p01"])["failure"], "MISSING_OR_INVALID_USAGE")

    def test_output_exhaustion_and_overrun_are_not_actions(self):
        for profile in candidate_profiles()[::2]:
            exhausted = inspect(profile, synthetic_response(profile, "p01", stop="length", output_tokens=profile.output_tokens), ["p01"])
            self.assertEqual(exhausted["failure"], "INCOMPLETE_RESPONSE")
            self.assertIsNone(exhausted["action"])
            self.assertIsNotNone(exhausted["usage"])
            excess = inspect(profile, synthetic_response(profile, "p01", output_tokens=profile.output_tokens + 1), ["p01"])
            self.assertEqual(excess["failure"], "USAGE_BOUND_EXCEEDED")

    def test_wrapper_never_repairs_an_unavailable_choice(self):
        for profile in candidate_profiles()[::2]:
            text = json.dumps({"answer": json.dumps({"player_id": "p01", "rationale": "Fixture."})})
            raw = synthetic_response(profile, "p01", text=text)
            self.assertIsNone(inspect(profile, raw, ["p01"])["failure"])
            self.assertEqual(inspect(profile, raw, ["p05"])["failure"], "ILLEGAL_PICK")

    def test_multiple_answers_tools_and_refusals_are_not_extracted(self):
        for profile in candidate_profiles()[::2]:
            raw = synthetic_response(profile, "p01")
            if profile.provider == "openai":
                raw["output"].append(raw["output"][0])
            else:
                raw["choices"].append(raw["choices"][0])
            self.assertEqual(inspect(profile, raw, ["p01"])["failure"], "AMBIGUOUS_NATIVE_ACTION")
        profile = candidate_profiles()[0]
        raw = synthetic_response(profile, "p01")
        raw["output"][0]["content"] = [{"type": "refusal", "refusal": "No"}]
        self.assertEqual(inspect(profile, raw, ["p01"])["failure"], "MODEL_REFUSAL")
        for change in ({"role": "user"}, {"status": "incomplete"}):
            raw = synthetic_response(profile, "p01")
            raw["output"][0].update(change)
            self.assertEqual(inspect(profile, raw, ["p01"])["failure"], "AMBIGUOUS_NATIVE_ACTION")
        for profile in candidate_profiles()[2::2]:
            raw = synthetic_response(profile, "p01")
            raw["choices"][0]["message"]["tool_calls"] = [{"id": "unexpected"}]
            self.assertEqual(inspect(profile, raw, ["p01"])["failure"], "AMBIGUOUS_NATIVE_ACTION")


class NativeDurableTests(support.BuildCase):
    def run_case(self, profile, case, replies, run_id):
        board = cases(profile.provider)[case]
        config = fixture_config(profile, board, run_id)
        draft = self.draft(config)
        draft.open(board.legal_ids())
        transport = NativeFixtureTransport(profile, board, config, replies, self.clock)
        return draft, transport, draft.resume(transport), config

    def test_late_round_same_ai_correction_and_native_receipts_survive_restart(self):
        for index, profile in enumerate(candidate_profiles()[::2]):
            with self.subTest(provider=profile.provider), patch("socket.socket", side_effect=AssertionError("network forbidden")):
                replies = {"pick:0:attempt:0": synthetic_response(profile, "p06"),
                           "pick:0:attempt:1": synthetic_response(profile, "p05")}
                draft, transport, result, config = self.run_case(profile, "last_slot", replies, f"native-{index}")
                self.assertEqual(result["phase"], "COMPLETE")
                self.assertEqual(result["picks"][0]["player_id"], "p05")
                self.assertEqual(len(transport.native_calls), 2)
                first, second = transport.native_calls
                self.assertEqual(first["profile_hash"], second["profile_hash"])
                self.assertEqual(first["board_hash"], second["board_hash"])
                self.assertEqual(first["body"]["model"], second["body"]["model"])
                stored = draft.journal.read()["opportunities"][0]["attempts"]
                self.assertEqual(stored[0]["response"]["native_evidence"]["failure"], "ILLEGAL_PICK")
                self.assertEqual(stored[1]["response"]["native_evidence"]["observed_model"], profile.model)
                unused = NativeFixtureTransport(profile, cases(profile.provider)["last_slot"], config, {}, self.clock)
                self.assertEqual(self.draft(config).resume(unused), result)
                self.assertEqual(unused.native_calls, [])

    def test_exhausted_correction_output_limit_and_unknown_request_pause_durably(self):
        profile = candidate_profiles()[0]
        variants = [({"pick:0:attempt:0": synthetic_response(profile, "p06"),
                      "pick:0:attempt:1": synthetic_response(profile, "p06")}, 2),
                    ({"pick:0:attempt:0": synthetic_response(profile, "p05", stop="length")}, 1),
                    ({"pick:0:attempt:0": TimeoutError("not logged")}, 1)]
        for index, (responses, attempts) in enumerate(variants):
            draft, transport, result, config = self.run_case(profile, "last_slot", responses, f"native-pause-{index}")
            self.assertEqual(result["phase"], "PAUSED")
            self.assertEqual(result["picks"], [])
            self.assertEqual(len(transport.native_calls), attempts)
            self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], profile.bound_micro_usd * attempts)
            unused = NativeFixtureTransport(profile, cases(profile.provider)["last_slot"], config, {}, self.clock)
            self.assertEqual(self.draft(config).resume(unused), result)
            self.assertEqual(unused.native_calls, [])

    def test_board_profile_or_price_change_cannot_resume_old_run(self):
        profile = candidate_profiles()[0]
        board = cases(profile.provider)["opening"]
        config = fixture_config(profile, board, "native-drift")
        self.draft(config).open(board.legal_ids())
        for changed_profile, changed_board in ((replace(profile, output_tokens=8192), board),
                                               (replace(profile, input_usd_per_million="6"), board),
                                               (profile, replace(board, locked_ids=("p01", "p08")))):
            with self.assertRaises(ValueError):
                self.draft(fixture_config(changed_profile, changed_board, "native-drift"))
            with self.assertRaises(ValueError):
                NativeFixtureTransport(changed_profile, changed_board, config, {}, self.clock)


if __name__ == "__main__":
    unittest.main()
