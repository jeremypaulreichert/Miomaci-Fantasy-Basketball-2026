"""Offline source-shaped fixtures; real cache replay is a separate CLI check."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fantasy_basketball.data.local import InputError, STAT_FIELDS
from fantasy_basketball.data.sportsdataverse import (
    participation, eligibility, timestamp, week_audit, normalize_week,
    player_packet, build_player_specs, read_sources,
)
from fantasy_basketball.scoring import Rules, evaluate


def source_box(aid="1", points="2", minutes="1.0", dnp="false"):
    row = {k: "0" for k in STAT_FIELDS}
    row.update(game_id="g1", athlete_id=aid, team_id="1", season_type="2", minutes=minutes,
               did_not_play=dnp, athlete_position_abbreviation="G", points=points,
               active="false", reason="COACH'S DECISION")
    return row


def inputs():
    game = {"game_id": "g1", "date": "2025-10-28T00:00Z", "home_id": "1", "away_id": "2",
            "home_score": "2", "away_score": "0", "season_type": "2", "status_type_name": "STATUS_FINAL",
            "status_type_completed": "true", "notes_headline": ""}
    away = source_box("2", "0")
    away["team_id"] = "2"
    return {"player_box_2026.csv": [source_box(), away], "nba_schedule_2026.csv": [game], "nba_schedule_2027.csv": [game.copy()],
            "rosters_2027.csv": [{"athlete_id": a, "team_id": a, "team_abbreviation": "T" + a, "full_name": "Synthetic " + a,
                                   "position_abbreviation": "G", "status_name": "Active"} for a in ("1", "2")]}


def case():
    return {"players": [{"player_id": "espn:1", "athlete_id": "1", "historical_team_id": "1", "positions": ["G"]}],
            "rosters": {"test": ["espn:1"]}, "rules": {"rules_id": "source-test", "period_id": "test-week",
            "start_utc": "2025-10-27T04:00:00Z", "end_utc": "2025-11-03T05:00:00Z",
            "weights": {"points": "1"}, "slots": [{"name": "G1", "positions": ["G"]}], "roster_size": 1,
            "mode": "HISTORICAL_OFFLINE_CANDIDATE"}}


class SourceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "workstreams/data/.tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_rounding_activity_and_unknown_are_separate(self):
        self.assertEqual(participation(source_box(minutes="0.0"))[0::2], ("APPEARED", True))
        self.assertEqual(participation(source_box(points="0", minutes="0.0"))[0], "UNKNOWN")
        self.assertEqual(participation(source_box(minutes=""))[0], "UNKNOWN")
        self.assertEqual(participation(source_box(points="0", minutes="", dnp="true"))[0], "DNP")

    def test_active_and_reason_do_not_override_appearance(self):
        a = source_box()
        b = {**a, "active": "true", "reason": ""}
        self.assertEqual(participation(a), participation(b))

    def test_dnp_with_production_is_rejected(self):
        with self.assertRaises(InputError):
            participation(source_box(dnp="true"))

    def test_event_exclusion_includes_all_allstar_sides(self):
        game = inputs()["nba_schedule_2026.csv"][0]
        self.assertEqual(eligibility({**game, "home_id": "STARS"}, {"1", "2"}), "NON_NBA_TEAM_EVENT")
        self.assertEqual(eligibility({**game, "notes_headline": "NBA Cup Championship"}, {"1", "2"}), "CUP_FINAL_EXCLUDED_CANDIDATE")

    def test_utc_conversion_respects_source_offset(self):
        self.assertEqual(timestamp("2025-11-02T20:00:00.000000-0500"), "2025-11-03T01:00:00Z")
        with self.assertRaises(InputError):
            timestamp("2025-11-02T20:00:00")

    def test_score_mismatch_and_postponement_block_spine(self):
        for alteration in ({"home_score": "3"}, {"status_type_name": "STATUS_POSTPONED", "status_type_completed": "false"}):
            tables = inputs()
            tables["nba_schedule_2026.csv"][0].update(alteration)
            with self.assertRaises(InputError):
                normalize_week(tables, case(), self.path)

    def test_unidentified_rows_block_week_even_when_points_match(self):
        tables = inputs()
        tables["player_box_2026.csv"].append(source_box("", "0", ""))
        audit = week_audit(tables, case()["rules"]["start_utc"], case()["rules"]["end_utc"])
        self.assertFalse(audit["complete_game_spine"])
        packet = player_packet(tables, {"retrieved_at_utc": "test", "files": {}})
        record = dict(zip(packet["columns"], packet["players"][0]))
        self.assertEqual(record["prior_stats_state"], "TEAM_SOURCE_GAPS")
        self.assertIsNone(record["points_pg"])

    def test_missing_zero_player_row_cannot_hide_in_matching_team_points(self):
        tables = inputs()
        tables["player_box_2026.csv"].append(source_box("3", "0"))
        chosen = case()
        chosen["players"][0].update(player_id="espn:3", athlete_id="3")
        chosen["rosters"] = {"test": ["espn:3"]}
        tables["player_box_2026.csv"].pop()
        dataset, _ = normalize_week(tables, chosen, self.path)
        self.assertEqual(evaluate(dataset, chosen["rosters"], Rules.from_dict(chosen["rules"]))["state"], "INCOMPLETE")

    def test_normalized_historical_mode_is_not_synthetic_or_competition(self):
        dataset, _ = normalize_week(inputs(), case(), self.path)
        result = evaluate(dataset, case()["rosters"], Rules.from_dict(case()["rules"]))
        self.assertEqual(result["teams"]["test"]["score"], "2")
        self.assertIn("not counted competition", result["disclosure"])

    def test_packet_health_is_unknown_even_when_roster_active(self):
        packet = player_packet(inputs(), {"retrieved_at_utc": "test", "files": {}})
        for row in packet["players"]:
            record = dict(zip(packet["columns"], row))
            self.assertIsNone(record["injury_status"])
            self.assertIsNone(record["injury_as_of_utc"])
        self.assertFalse(packet["draft_ready"])

    def test_column_alignment_and_build_type_compatibility(self):
        from fantasy_basketball.draft.board import Player
        packet = player_packet(inputs(), {"retrieved_at_utc": "test", "files": {}})
        specs = build_player_specs(packet)
        players = [Player(**p) for p in specs]
        self.assertEqual(len(players[0].facts), 20)
        self.assertEqual(dict(players[0].facts)["points_pg"], "2.000")
        packet["players"][0].pop()
        with self.assertRaises(InputError):
            build_player_specs(packet)

    def test_hash_duplicate_and_unknown_schema_fail_closed(self):
        tables = inputs()

        def write():
            manifest = {"files": {}}
            for filename, rows in tables.items():
                text = io.StringIO(newline="")
                writer = csv.DictWriter(text, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
                raw = text.getvalue().encode()
                (self.path / filename).write_bytes(raw)
                manifest["files"][filename] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            return manifest

        manifest = write()
        self.assertEqual(len(read_sources(self.path, manifest)["player_box_2026.csv"]), 2)
        (self.path / "player_box_2026.csv").write_text("changed")
        with self.assertRaises(InputError):
            read_sources(self.path, manifest)
        tables["player_box_2026.csv"].append(tables["player_box_2026.csv"][0].copy())
        with self.assertRaises(InputError):
            read_sources(self.path, write())


if __name__ == "__main__":
    unittest.main()
