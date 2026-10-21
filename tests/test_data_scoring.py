"""Synthetic expectations are hand calculations or independent permutations.

The exhaustive oracle consumes integer fixture totals, never production scores.
No football data, network, credentials or shared config is accessed.
"""
import csv
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
import hashlib
import io
import itertools
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fantasy_basketball.data.local import HEADERS, InputError, STAT_FIELDS, load_inputs
from fantasy_basketball.scoring import Rules, evaluate, read_release, rosters_from_draft, rosters_from_receipt, save_release


def rules(**overrides):
    base = dict(rules_id="synthetic-v1", period_id="week-1", start_utc="2025-01-06T00:00:00Z",
                end_utc="2025-01-13T00:00:00Z", weights=(("points", "1"), ("turnovers", "-2")),
                slots=(("G1", ("G",)), ("F1", ("F",))), roster_size=3)
    base.update(overrides)
    return Rules(**base)


def box(player, *, game="g1", state="APPEARED", minutes="1", confirmed="false", **stats):
    return [player, game, state, minutes, confirmed, *[str(stats.get(f, 0)) for f in STAT_FIELDS]]


def tables():
    return {"players": [["p1", "G|F"], ["p2", "G"], ["p3", "F"]],
            "games": [["g1", "2025-01-06T00:00:00Z", "FINAL", "true"]],
            "coverage": [[p, "g1"] for p in ("p1", "p2", "p3")],
            "boxes": [box("p1", points=10), box("p2", points=8), box("p3", points=1)]}


def write_inputs(directory, rows):
    paths, hashes = {}, {}
    for kind, header in HEADERS.items():
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows[kind])
        raw = stream.getvalue().encode("utf-8")
        paths[kind] = Path(directory) / (kind + ".csv")
        paths[kind].write_bytes(raw)
        hashes[kind] = hashlib.sha256(raw).hexdigest()
    return paths, hashes


class ScoringTest(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "workstreams/data/.tmp"
        temporary_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="scoring-", dir=temporary_root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rosters = {"ai-a": ["p1", "p2", "p3"]}

    def load(self, rows=None):
        return load_inputs(*write_inputs(self.root, tables() if rows is None else rows))

    def score(self, rows=None, config=None, rosters=None):
        return evaluate(self.load(rows), self.rosters if rosters is None else rosters, rules() if config is None else config)

    def test_multi_position_assignment_avoids_greedy_loss(self):
        result = self.score()
        self.assertEqual(result["teams"]["ai-a"]["score"], "18")
        self.assertEqual(result["teams"]["ai-a"]["lineup"], [{"slot": "G1", "player_id": "p2"}, {"slot": "F1", "player_id": "p1"}])

    def test_all_eleven_fields_hand_calculated(self):
        rows = tables()
        rows["boxes"][0] = box("p1", points=20, three_point_field_goals_made=2, rebounds=7,
                               assists=4, steals=1, blocks=2, turnovers=3, field_goals_made=7,
                               field_goals_attempted=15, free_throws_made=4, free_throws_attempted=5)
        weights = tuple(zip(STAT_FIELDS, ("1", "1", "1", "2", "4", "4", "-2", "2", "-1", "1", "-1")))
        result = self.score(rows, rules(weights=weights))
        # 20+2+7+8+4+8-6+14-15+4-5 = 41, calculated without the scorer.
        self.assertEqual(result["teams"]["ai-a"]["players"]["p1"]["total"], "41")

    def test_each_weight_field_individually(self):
        for field in STAT_FIELDS:
            with self.subTest(field=field):
                rows = tables()
                stats = {f: 2 for f in STAT_FIELDS}
                rows["boxes"][0] = box("p1", **stats)
                result = self.score(rows, rules(weights=((field, "0.125"),)))
                self.assertEqual(result["teams"]["ai-a"]["players"]["p1"]["total"], "0.25")

    def test_negative_totals_still_fill_every_slot(self):
        rows = tables()
        rows["boxes"] = [box("p1", turnovers=1), box("p2", turnovers=2), box("p3", turnovers=3)]
        result = self.score(rows)
        self.assertEqual(result["teams"]["ai-a"]["score"], "-6")
        self.assertEqual(len(result["teams"]["ai-a"]["lineup"]), 2)

    def test_tie_assignment_is_lexical_and_input_order_independent(self):
        rows = tables()
        rows["players"] = [[p, "F|G"] for p in ("p3", "p1", "p2")]
        rows["boxes"] = [box(p, points=5) for p in ("p3", "p2", "p1")]
        result = self.score(rows)
        self.assertEqual([x["player_id"] for x in result["teams"]["ai-a"]["lineup"]], ["p1", "p2"])
        self.assertEqual(result, evaluate(self.load(rows), {"ai-a": ["p3", "p2", "p1"]}, rules()))

    def test_missing_bench_row_blocks_standings_and_release(self):
        rows = tables()
        rows["boxes"].pop()  # Low-scoring bench player cannot silently disappear.
        dataset = self.load(rows)
        result = evaluate(dataset, self.rosters, rules())
        self.assertFalse(result["final_release_eligible"])
        self.assertIsNone(result["standings"])
        self.assertEqual(result["teams"]["ai-a"]["players"]["p3"]["games"][0]["state"], "MISSING_ROW")
        with self.assertRaises(InputError):
            save_release(self.root / "releases", dataset, self.rosters, rules(), reason="must block")
        self.assertFalse((self.root / "releases").exists())

    def test_dnp_zero_and_no_games_remain_distinct(self):
        rows = tables()
        rows["coverage"][2][1] = ""
        rows["boxes"] = [box("p1", state="DNP", minutes="", **{f: "" for f in STAT_FIELDS}), box("p2")]
        players = self.score(rows)["teams"]["ai-a"]["players"]
        self.assertEqual(players["p1"]["games"][0]["state"], "DNP")
        self.assertEqual(players["p2"]["games"][0]["state"], "APPEARED_ZERO")
        self.assertEqual(players["p3"]["state"], "NO_COUNTED_GAMES")
        self.assertEqual([players[p]["total"] for p in sorted(players)], ["0", "0", "0"])

    def test_missing_stats_block_only_when_weighted(self):
        rows = tables()
        rows["boxes"][0] = box("p1", points="", turnovers=1)
        result = self.score(rows)
        self.assertEqual(result["teams"]["ai-a"]["players"]["p1"]["games"][0]["state"], "MISSING_STATS")
        result = self.score(rows, rules(weights=(("turnovers", "-2"), ("points", "0"))))
        self.assertEqual(result["state"], "COMPLETE")

    def test_rounded_zero_minutes_require_explicit_confirmation(self):
        rows = tables()
        rows["boxes"][0] = box("p1", minutes="0", points=2)
        self.assertEqual(self.score(rows)["state"], "INCOMPLETE")
        rows["boxes"][0][4] = "true"
        self.assertEqual(self.score(rows)["teams"]["ai-a"]["players"]["p1"]["total"], "2")

    def test_unknown_participation_or_missing_minutes_block(self):
        for values in ({"state": "UNKNOWN"}, {"minutes": ""}):
            rows = tables()
            rows["boxes"][0] = box("p1", **values)
            self.assertEqual(self.score(rows)["state"], "INCOMPLETE")

    def test_postponed_and_scheduled_games_block_even_with_boxes(self):
        for status in ("POSTPONED", "SCHEDULED"):
            rows = tables()
            rows["games"][0][2] = status
            result = self.score(rows)
            self.assertEqual(result["state"], "INCOMPLETE")
            self.assertEqual(result["teams"]["ai-a"]["issues"][0]["reason"], status)

    def test_excluded_canceled_and_period_end_need_no_box(self):
        for change in (lambda r: r["games"][0].__setitem__(2, "CANCELED"),
                       lambda r: r["games"][0].__setitem__(3, "false"),
                       lambda r: r["games"][0].__setitem__(1, "2025-01-13T00:00:00Z")):
            rows = tables()
            change(rows)
            rows["boxes"] = []
            self.assertEqual(self.score(rows)["teams"]["ai-a"]["score"], "0")

    def test_multiple_games_sum_exactly_and_start_is_included(self):
        rows = tables()
        rows["games"].append(["g2", "2025-01-12T23:59:59Z", "FINAL", "true"])
        rows["coverage"][0][1] = "g1|g2"
        rows["boxes"].append(box("p1", game="g2", points=1))
        result = self.score(rows, rules(weights=(("points", "0.1"),)))
        self.assertEqual(result["teams"]["ai-a"]["players"]["p1"]["total"], "1.1")
        self.assertEqual(result["teams"]["ai-a"]["score"], "1.9")

    def test_ambient_decimal_precision_does_not_change_result(self):
        dataset = self.load()
        config = rules(weights=(("points", "0.123456"),))
        expected = evaluate(dataset, self.rosters, config)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(evaluate(dataset, self.rosters, config), expected)

    def test_unfillable_slots_block(self):
        result = self.score(config=rules(slots=(("C1", ("C",)),)))
        self.assertEqual(result["teams"]["ai-a"]["issues"], [{"reason": "NO_LEGAL_FULL_LINEUP"}])
        self.assertFalse(result["final_release_eligible"])

    def test_standings_exact_ties_share_rank_and_rounding_is_not_used(self):
        rows = {"players": [[f"p{i}", "G"] for i in range(4)],
                "games": tables()["games"], "coverage": [[f"p{i}", "g1"] for i in range(4)],
                "boxes": [box(f"p{i}", points=v) for i, v in enumerate((2, 2, 1, 0))]}
        result = self.score(rows, rules(roster_size=1, slots=(("G1", ("G",)),), weights=(("points", "0.001"),)),
                            {f"ai-{i}": [f"p{i}"] for i in range(4)})
        self.assertEqual([r["rank"] for r in result["standings"]], [1, 1, 3, 4])
        self.assertEqual([r["score"] for r in result["standings"]], ["0.002", "0.002", "0.001", "0"])

    def test_invalid_or_duplicate_rosters_rejected(self):
        for roster in ({"a": ["p1", "p1", "p3"]}, {"a": ["p1"]}, {"a": ["p1", "p2", "unknown"]},
                       {"a": ["p1", "p2", "p3"], "b": ["p1", "p2", "p3"]}):
            with self.assertRaises(InputError):
                self.score(rosters=roster)

    def test_duplicate_and_conflicting_facts_both_rejected(self):
        for last in (box("p1", points=10), box("p1", points=99)):
            rows = tables()
            rows["boxes"].append(last)
            with self.assertRaisesRegex(InputError, "duplicate player-game"):
                self.load(rows)

    def test_duplicate_entities_and_coverage_rejected(self):
        for kind in ("players", "games", "coverage"):
            rows = tables()
            rows[kind].append(rows[kind][0][:])
            with self.assertRaises(InputError):
                self.load(rows)

    def test_missing_coverage_and_orphan_facts_rejected(self):
        for variant in ("missing", "unknown_game", "orphan"):
            rows = tables()
            if variant == "missing":
                rows["coverage"].pop()
            elif variant == "unknown_game":
                rows["coverage"][0][1] = "unknown"
            else:
                rows["boxes"].append(box("unknown"))
            with self.assertRaises(InputError):
                self.load(rows)

    def test_numeric_garbage_and_fractional_counts_rejected(self):
        for value in ("NaN", "Infinity", "bad", "1e2", "-1", "0.5", "1000001", " 1"):
            rows = tables()
            rows["boxes"][0] = box("p1", points=value)
            with self.subTest(value=value), self.assertRaises(InputError):
                self.load(rows)

    def test_dnp_production_and_impossible_count_conflicts_rejected(self):
        for row in (box("p1", state="DNP", points=1), box("p1", state="DNP", minutes="1"),
                    box("p1", field_goals_made=2, field_goals_attempted=1),
                    box("p1", confirmed="true")):
            rows = tables()
            rows["boxes"][0] = row
            with self.assertRaises(InputError):
                self.load(rows)

    def test_pinned_hashes_and_exact_headers_and_width(self):
        paths, hashes = write_inputs(self.root, tables())
        paths["boxes"].write_bytes(paths["boxes"].read_bytes() + b"\n")
        with self.assertRaisesRegex(InputError, "hash mismatch"):
            load_inputs(paths, hashes)
        for extra in (b"active\n", b"p1,g1\n"):
            paths, hashes = write_inputs(self.root, tables())
            raw = extra if extra == b"active\n" else paths["boxes"].read_bytes() + extra
            paths["boxes"].write_bytes(raw)
            hashes["boxes"] = hashlib.sha256(raw).hexdigest()
            with self.assertRaises(InputError):
                load_inputs(paths, hashes)

    def test_policy_and_coefficient_validation(self):
        for override in ({"fill_policy": "EMPTY_ALLOWED"}, {"mode": "LIVE"}, {"roster_size": True},
                         {"weights": (("points", 1.0),)}, {"weights": (("unknown", "1"),)},
                         {"weights": (("points", "0"),)}, {"weights": (("points", "NaN"),)},
                         {"start_utc": "2025-01-13T00:00:00Z"}, {"slots": (("G", ("PG",)),)}):
            with self.assertRaises(InputError):
                rules(**override)

    def test_permutation_oracle_on_120_small_random_fixtures(self):
        rng = random.Random(7015)
        slots = (("G1", ("G",)), ("F1", ("F",)), ("UTIL", ("G", "F", "C")))
        for trial in range(120):
            ids = [f"p{i}" for i in range(5)]
            eligibility = {p: tuple(sorted(rng.sample(["G", "F", "C"], rng.randint(1, 3)))) for p in ids}
            totals = {p: rng.randint(-5, 7) for p in ids}
            # Independent enumeration on authored integer totals; no scorer call.
            legal = [(sum(totals[p] for p in assignment), assignment)
                     for assignment in itertools.permutations(ids, 3)
                     if all(set(eligibility[p]) & set(slot[1]) for p, slot in zip(assignment, slots))]
            expected = sorted(legal, key=lambda x: (-x[0], x[1]))[0] if legal else None
            rows = {"players": [[p, "|".join(eligibility[p])] for p in ids], "games": tables()["games"],
                    "coverage": [[p, "g1"] for p in ids],
                    "boxes": [box(p, points=max(0, totals[p]), turnovers=max(0, -totals[p])) for p in ids]}
            result = self.score(rows, rules(roster_size=5, slots=slots, weights=(("points", "1"), ("turnovers", "-1"))), {"ai-a": ids})
            with self.subTest(trial=trial):
                if expected is None:
                    self.assertFalse(result["final_release_eligible"])
                else:
                    self.assertEqual(Decimal(result["teams"]["ai-a"]["score"]), expected[0])
                    self.assertEqual(tuple(x["player_id"] for x in result["teams"]["ai-a"]["lineup"]), expected[1])

    def test_correction_versions_preserve_original_and_recompute(self):
        target = self.root / "releases"
        first = save_release(target, self.load(), self.rosters, rules(), reason="synthetic initial")
        original = (target / "release-000001.json").read_bytes()
        rows = tables()
        rows["boxes"][2] = box("p3", points=30)
        second = save_release(target, self.load(rows), self.rosters, rules(), previous_version=1, reason="synthetic stat correction")
        self.assertEqual(second["payload"]["previous_sha256"], first["sha256"])
        self.assertEqual(second["payload"]["result"]["teams"]["ai-a"]["score"], "40")
        self.assertEqual((target / "release-000001.json").read_bytes(), original)
        self.assertEqual(read_release(target, 2), json.loads(json.dumps(second)))
        with self.assertRaises(FileExistsError):
            save_release(target, self.load(), self.rosters, rules(), reason="overwrite refused")
        with self.assertRaises(FileExistsError):
            save_release(target, self.load(), self.rosters, rules(), previous_version=1, reason="branch refused")

    def test_correction_context_reason_and_chain_validation(self):
        target = self.root / "releases"
        dataset = self.load()
        save_release(target, dataset, self.rosters, rules(), reason="synthetic initial")
        with self.assertRaises(InputError):
            save_release(target, dataset, self.rosters, rules(weights=(("points", "2"),)), previous_version=1, reason="changed rules")
        with self.assertRaises(InputError):
            save_release(target, dataset, self.rosters, rules(), previous_version=1, reason=" ")
        path = target / "release-000001.json"
        value = json.loads(path.read_text())
        value["payload"]["reason"] = "tampered"
        path.write_text(json.dumps(value))
        with self.assertRaises(InputError):
            read_release(target, 1)

    def test_interrupted_write_before_publication_leaves_no_release(self):
        target = self.root / "releases"
        with patch("fantasy_basketball.scoring.release._publish", side_effect=OSError("simulated pre-publication failure")):
            with self.assertRaises(OSError):
                save_release(target, self.load(), self.rosters, rules(), reason="synthetic")
        self.assertEqual(list(target.iterdir()), [])

    def test_failure_after_publication_preserves_complete_release(self):
        from fantasy_basketball.scoring.release import _publish
        target = self.root / "releases"

        def fail_after(source, destination):
            _publish(source, destination)
            raise OSError("simulated lost acknowledgement after atomic publication")

        with patch("fantasy_basketball.scoring.release._publish", side_effect=fail_after):
            with self.assertRaises(OSError):
                save_release(target, self.load(), self.rosters, rules(), reason="synthetic")
        self.assertEqual(read_release(target, 1)["payload"]["result"]["teams"]["ai-a"]["score"], "18")
        self.assertEqual(len(list(target.iterdir())), 1)

    def test_concurrent_release_writers_cannot_overwrite(self):
        dataset = self.load()
        target = self.root / "releases"

        def attempt(index):
            try:
                return save_release(target, dataset, self.rosters, rules(), reason=f"synthetic writer {index}")["sha256"]
            except FileExistsError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            hashes = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(h is not None for h in hashes), 1)
        self.assertIn(read_release(target, 1)["sha256"], hashes)
        self.assertEqual(len(list(target.iterdir())), 1)

    def test_complete_build_status_contract_and_invalid_states(self):
        status = {"phase": "COMPLETE", "mode": "OFFLINE_FIXTURE", "pause": None, "total_picks": 4,
                  "picks": [{"index": i, "player_id": p, "manager": m, "origin": "SIMULATED_AI"}
                            for i, (p, m) in enumerate((("p4", "ai-a"), ("p2", "ai-b"), ("p1", "ai-b"), ("p3", "ai-a")))]}
        self.assertEqual(rosters_from_draft(status, managers=("ai-a", "ai-b"), roster_size=2), {"ai-a": ["p4", "p3"], "ai-b": ["p2", "p1"]})
        for field, value in (("phase", "PAUSED"), ("mode", "LIVE"), ("pause", {"reason": "unknown"}), ("total_picks", 5)):
            with self.assertRaises(InputError):
                rosters_from_draft({**status, field: value}, managers=("ai-a", "ai-b"), roster_size=2)
        status["picks"][3]["player_id"] = "p1"
        with self.assertRaises(InputError):
            rosters_from_draft(status, managers=("ai-a", "ai-b"), roster_size=2)

    def test_offline_slice_works_with_network_disabled(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.score()["state"], "COMPLETE")

    def test_hashed_build_receipt_requires_pinned_identity_and_exact_flags(self):
        receipt = {"schema_version": "miomaci.basketball.settled-roster/1", "experiment_id": "miomaci-basketball-v1",
                   "run_id": "synthetic-test", "config_hash": "a" * 64, "draft_rules_digest": "b" * 64,
                   "execution_mode": "OFFLINE_FIXTURE", "simulated": True, "provisional": True,
                   "competitive_authorization": False, "draft_status": "COMPLETE", "roster_size": 1, "total_picks": 1,
                   "rosters": [{"manager_id": "ai-a", "model_id": "fixture-a", "player_ids": ["p1"]}],
                   "picks": [{"index": 0, "manager": "ai-a", "model": "fixture-a", "player_id": "p1",
                              "origin": "SIMULATED_AI", "rationale": "Synthetic résumé"}]}

        def seal(body):
            return {**body, "receipt_hash": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}

        identity = dict(experiment_id="miomaci-basketball-v1", run_id="synthetic-test", config_hash="a" * 64)
        self.assertEqual(rosters_from_receipt(seal(receipt), **identity), {"ai-a": ["p1"]})
        with self.assertRaises(InputError):
            rosters_from_receipt(seal(receipt), **{**identity, "config_hash": "c" * 64})
        with self.assertRaises(InputError):
            rosters_from_receipt({**seal(receipt), "total_picks": 2}, **identity)
        for field, value in (("simulated", 1), ("competitive_authorization", 0), ("draft_status", "PAUSED")):
            with self.assertRaises(InputError):
                rosters_from_receipt(seal({**receipt, field: value}), **identity)
        broken = json.loads(json.dumps(receipt))
        broken["rosters"][0]["player_ids"] = ["p2"]
        with self.assertRaises(InputError):
            rosters_from_receipt(seal(broken), **identity)


if __name__ == "__main__":
    unittest.main()
