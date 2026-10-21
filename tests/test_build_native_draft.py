"""Offline shared native lifecycle, packet preservation and scoring seam."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workstreams/build"))
from native_draft_check import configuration, replies, inputs, scale_board
from provider_cases import cases
from fantasy_basketball.core.config import digest, canonical
from fantasy_basketball.draft.board import Board, Player, can_complete
from fantasy_basketball.draft.native_plan import NativeDraftConfig
from fantasy_basketball.draft.trial import NativeTrial, RecoveryDecision
from fantasy_basketball.providers.native import prepare, inspect
from fantasy_basketball.providers.qualification import candidate_profiles, synthetic_response
from fantasy_basketball.providers.transport import ScriptedNativeTransport
from fantasy_basketball.scoring import evaluate, rosters_from_receipt


class PacketScaleTests(unittest.TestCase):
    def test_compact_table_is_lossless_including_null_absence_unicode_and_owned(self):
        board = cases("openai")["owned_star"]
        extra = Player("unicode", 'Name \"é\"', ("F", "G"), (("strange", '\n"\\'), ("sample_points", None)))
        board = replace(board, players=(*board.players, extra))
        packet = board.compact_packet()
        restored = {row[0]: (row[1], tuple(row[2]), tuple((packet["fact_keys"][k], v) for k, v in row[3]))
                    for row in packet["players"]}
        self.assertEqual(restored, {p.player_id: (p.name, p.positions, p.facts) for p in board.players})
        self.assertEqual(packet["ownership"], [list(p) for p in board.ownership])
        self.assertEqual(packet["locked_ids"], list(board.locked_ids))
        self.assertEqual(packet["legal_ids"], list(board.legal_ids()))
        self.assertNotIn("strange", dict(restored["p01"][2]))
        self.assertIsNone(dict(restored["unicode"][2])["sample_points"])
        packet["players"][0][1] = "changed"
        self.assertNotEqual(board.compact_packet()["players"][0][1], "changed")

    def test_full_pool_caps_are_explicit_and_no_facts_are_trimmed(self):
        board = scale_board()
        for profile in candidate_profiles()[::2]:
            board = replace(board, manager=profile.provider)
            compact = replace(profile, schema="basketball-native-profile/2")
            with self.assertRaisesRegex(ValueError, "byte limit"):
                prepare(compact, board, now_ms=0, deadline_ms=120000)
            measured = prepare(replace(compact, request_bytes=2000000), board, now_ms=0, deadline_ms=120000)
            old = prepare(replace(profile, request_bytes=2000000), board, now_ms=0, deadline_ms=120000)
            self.assertLess(measured["request_bytes"], old["request_bytes"] * 0.55)
            # Measured exact boundary, not a production cap increase.
            fitted = replace(compact, request_bytes=measured["request_bytes"])
            self.assertEqual(prepare(fitted, board, now_ms=0, deadline_ms=120000)["request_bytes"], measured["request_bytes"])
            self.assertEqual(fitted.bound_micro_usd, profile.bound_micro_usd)
            with self.assertRaises(ValueError):
                prepare(replace(fitted, request_bytes=fitted.request_bytes - 1), board, now_ms=0, deadline_ms=120000)

    def test_feasibility_work_scales_by_position_sets_and_cache_is_not_serialized(self):
        board = scale_board()
        original_hash = board.hash
        with patch("fantasy_basketball.draft.board.can_complete", wraps=can_complete) as matcher:
            legal = board.legal_ids()
            self.assertEqual(len(legal), 627)
            self.assertEqual(matcher.call_count, 3)
            self.assertEqual(board.legal_ids(), legal)
            self.assertEqual(matcher.call_count, 3)
        self.assertEqual(board.hash, original_hash)
        changed = replace(board, ownership=(("p0000", "other"),), locked_ids=("p0001",))
        self.assertNotIn("p0000", changed.legal_ids())
        self.assertNotIn("p0001", changed.legal_ids())
        self.assertNotEqual(changed.hash, original_hash)

    def test_legacy_requests_are_byte_identical_to_saved_v5(self):
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "workstreams/build/NATIVE_TRIAL_PREFLIGHT_V5.json").read_text())
        selected = [(profile, board) for profile in candidate_profiles() for board in cases(profile.provider).values()]
        for saved, (profile, board) in zip(package["initial_requests"], selected):
            request = prepare(profile, board, now_ms=0, deadline_ms=min(package["bounds"]["trial_ms"], profile.opportunity_ms))
            self.assertEqual(canonical(request), canonical(saved["request"]))

    def test_compact_grammar_still_rejects_illegal_id_and_long_correction_is_bounded(self):
        board = cases("openai")["last_slot"]
        profile = replace(candidate_profiles()[0], schema="basketball-native-profile/2")
        wire = prepare(profile, board, now_ms=0, deadline_ms=120000)
        self.assertNotIn("enum", wire["body"]["text"]["format"]["schema"]["properties"]["player_id"])
        self.assertEqual(inspect(profile, synthetic_response(profile, "p01"), board.legal_ids())["failure"], "ILLEGAL_PICK")
        correction = {"failure": "ILLEGAL_PICK", "response_hash": "0" * 64, "previous_text": "x" * 24000}
        with self.assertRaisesRegex(ValueError, "byte limit"):
            prepare(profile, board, now_ms=0, deadline_ms=120000, correction=correction)


class NativeDraftTests(support.BuildCase):
    def trial(self, config=None, **kwargs):
        return NativeTrial(self.directory, config or configuration(), clock=self.clock, **kwargs)

    def test_evolving_board_same_ai_correction_receipt_and_score(self):
        config = configuration()
        trial = self.trial(config)
        transport = ScriptedNativeTransport(replies(config, correction=True))
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = trial.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(len(transport.calls), 5)
        first, correction = [r["request"] for r in transport.calls[1:3]]
        for key in ("board_hash", "profile_hash", "deadline_ms"):
            self.assertEqual(first[key], correction[key])
        messages = first["body"]["messages"]
        packet = json.loads(messages[1]["content"])["decision"]
        self.assertEqual(packet["ownership"], [["p4", "openai"]])
        self.assertNotIn("p4", packet["legal_ids"])
        self.assertEqual(config.board_at(3, result["results"]).legal_ids(), ("p3",))
        receipt = trial.export_roster()
        dataset, rules, _ = inputs()
        self.assertEqual(receipt["draft_rules_digest"], rules.hash)
        rosters = rosters_from_receipt(receipt, experiment_id=config.experiment_id, run_id=config.run_id, config_hash=config.hash)
        scores = evaluate(dataset, rosters, rules)
        self.assertEqual({m: t["score"] for m, t in scores["teams"].items()}, {"openai": "7", "deepseek": "17"})
        self.assertEqual([p["model"] for p in receipt["picks"]], ["fixture-openai", "fixture-deepseek", "fixture-deepseek", "fixture-openai"])
        before = trial.journal.path.read_bytes()
        unused = ScriptedNativeTransport({})
        self.assertEqual(self.trial(config).resume(unused), result)
        self.assertEqual(self.trial(config).export_roster(), receipt)
        self.assertFalse(unused.calls)
        self.assertEqual(before, trial.journal.path.read_bytes())

    def test_unknown_outcome_keeps_prior_pick_and_exposure_no_export_or_resend(self):
        config = configuration()
        rows = replies(config)
        rows["case:1:attempt:0"] = TimeoutError("synthetic ambiguity")
        trial = self.trial(config)
        trial.resume(ScriptedNativeTransport(rows))
        before = trial.status()
        self.assertEqual(before["phase"], "PAUSED")
        self.assertEqual(len(before["results"]), 1)
        self.assertEqual(before["accounting"]["unresolved_exposure_micro_usd"], sum(c.profile.bound_micro_usd for c in config.cases[:2]))
        with self.assertRaises(ValueError):
            trial.export_roster()
        unused = ScriptedNativeTransport(replies(config))
        self.assertEqual(self.trial(config).resume(unused), before)
        self.assertFalse(unused.calls)

    def test_saved_response_recovery_uses_same_controller_and_never_advances_it(self):
        config = configuration()
        trial = self.trial(config)
        transport = ScriptedNativeTransport(replies(config))
        send = transport.send
        def racing_send(*args):
            self.trial(config).resume(ScriptedNativeTransport({}))
            return send(*args)
        transport.send = racing_send
        trial.resume(transport)
        d = RecoveryDecision("settle", config.hash, trial.explain_pause()["state_hash"], "SETTLE_SAVED", "TEST_OWNER")
        previous = trial.status()["accounting"]
        result = trial.recover(d)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["accounting"], previous)
        self.assertEqual(len(transport.calls), 1)
        remaining = ScriptedNativeTransport(replies(config))
        self.assertEqual(self.trial(config).resume(remaining)["phase"], "COMPLETE")
        self.assertEqual(len(remaining.calls), 3)
        self.assertEqual(self.trial(config).export_roster()["total_picks"], 4)

    def test_budget_and_illegal_correction_exhaustion_remain_durable(self):
        config = configuration()
        short = replace(config, budget_micro_usd=1)
        unused = ScriptedNativeTransport({})
        self.assertEqual(self.trial(short).resume(unused)["pause"]["reason"], "BUDGET")
        self.assertFalse(unused.calls)
        config = replace(config, run_id="exhausted")
        rows = replies(config, correction=True)
        rows["case:1:attempt:1"]["raw"] = synthetic_response(config.cases[1].profile, "p4")
        trial = self.trial(config)
        result = trial.resume(ScriptedNativeTransport(rows))
        self.assertEqual(result["pause"]["reason"], "ILLEGAL_PICK")
        self.assertEqual(len(result["results"]), 1)

    def test_no_legal_completion_pauses_without_reservation(self):
        config = configuration()
        # Two managers can each initially complete G/F, but only one F exists.
        players = tuple(replace(p, positions=("G",)) if p.player_id == "p2" else p for p in config.board.players)
        config = replace(config, board=replace(config.board, players=players))
        rows = replies(config)
        rows["case:1:attempt:0"]["raw"] = synthetic_response(config.cases[1].profile, "p3")
        rows["case:2:attempt:0"]["raw"] = synthetic_response(config.cases[2].profile, "p1")
        transport = ScriptedNativeTransport(rows)
        result = self.trial(config).resume(transport)
        self.assertEqual(result["pause"]["reason"], "NO_LEGAL_PICK")
        self.assertEqual(result["reserved_requests"], 3)
        self.assertEqual(len(transport.calls), 3)

    def test_namespace_live_mode_duplicate_managers_and_restart_drift_rejected(self):
        config = configuration()
        for changes in ({"mode": "LIVE_DIAGNOSTIC"}, {"profiles": (config.profiles[0],) * 2},
                        {"rules_digest": "not-a-hash"}, {"run_id": "CON"}, {"experiment_id": "football"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(config, **changes)
        trial = self.trial(config)
        with self.assertRaises(ValueError):
            trial.export_roster()
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.trial(replace(config, rules_digest="0" * 64))

    def test_late_draft_pool_keeps_all_players_and_blocks_owned_ids(self):
        base = scale_board()
        profiles = tuple(replace(p, schema="basketball-native-profile/2") for p in candidate_profiles()[::2])
        config = NativeDraftConfig("late-scale", profiles, base, 1000000000, 10000000, "1" * 64, "2" * 64)
        prefix = [{"case_index": i, "case_id": c.board.case_id, "profile_id": c.profile.profile_id,
                   "action": {"player_id": f"p{profiles.index(c.profile) * 39 + i // 3:04d}"}}
                  for i, c in enumerate(config.cases[:-1])]
        board = config.board_at(38, prefix)
        self.assertEqual(len(board.players), 627)
        self.assertEqual(len(board.ownership), 38)
        self.assertTrue(board.legal_ids())
        self.assertFalse(set(board.legal_ids()) & {r["action"]["player_id"] for r in prefix})

    def test_crashes_during_later_pick_preserve_native_board_and_receipt(self):
        for boundary in ("before_commit:RESERVE", "after:RESERVE", "after:DISPATCH",
                         "before_commit:RESPONSE", "after:RESPONSE", "before_commit:RESOLVE", "after:RESOLVE"):
            run_id = boundary.replace(":", "-").lower()
            child = subprocess.run([sys.executable, "-B", __file__, "--crash", str(self.directory), run_id, boundary],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 23, child.stderr)
            config = configuration(run_id)
            trial = self.trial(config)
            transport = ScriptedNativeTransport(replies(config))
            result = trial.resume(transport)
            self.assertEqual(result["results"][0]["action"]["player_id"], "p4")
            self.assertFalse(any(c["key"].startswith("case:0:") for c in transport.calls))
            if boundary in {"after:DISPATCH", "before_commit:RESPONSE"}:
                self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH")
                self.assertFalse(transport.calls)
                with self.assertRaises(ValueError):
                    trial.export_roster()
            else:
                self.assertEqual(result["phase"], "COMPLETE")
                self.assertEqual(trial.export_roster()["total_picks"], 4)
                if boundary in {"after:RESPONSE", "before_commit:RESOLVE", "after:RESOLVE"}:
                    self.assertFalse(any(c["key"].startswith("case:1:") for c in transport.calls))


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--crash":
    _, _, directory, run_id, boundary = sys.argv
    count = [0]
    def kill(point):
        if point == boundary:
            count[0] += 1
            if count[0] == 2:  # kill in the second pick, after one settlement
                os._exit(23)
    config = configuration(run_id)
    NativeTrial(directory, config, clock=support.Clock(), fault=kill).resume(ScriptedNativeTransport(replies(config)))
    raise SystemExit("crash not reached")
elif __name__ == "__main__":
    unittest.main()
