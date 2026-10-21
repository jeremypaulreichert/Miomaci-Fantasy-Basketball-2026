"""Pinned local sportsdataverse ESPN release normalization. Never fetches URLs.

Policies here are offline candidates: regular-season NBA team-v-team games,
excluding the explicitly labelled Cup championship. Unknown participation stays
unknown. Current roster 'Active' does not establish injury/availability status.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import csv
import hashlib
import io
import json
from pathlib import Path

from .local import HEADERS, STAT_FIELDS, InputError, load_inputs, number

POSITION = {"G": "G", "PG": "G", "SG": "G", "F": "F", "SF": "F", "PF": "F", "C": "C"}


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise InputError("source timestamp must include offset")
    return result.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_sources(raw_directory, manifest):
    tables = {}
    for filename, receipt in manifest["files"].items():
        raw = (Path(raw_directory) / filename).read_bytes()
        if len(raw) != receipt["bytes"] or hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise InputError("raw source drift: " + filename)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise InputError("duplicate raw header")
        rows = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in rows):
            raise InputError("malformed source row")
        tables[filename] = rows
    required = {"player_box_2026.csv": {"game_id", "athlete_id", "team_id", "season_type", "minutes", "did_not_play", "athlete_position_abbreviation", *STAT_FIELDS},
                "rosters_2027.csv": {"athlete_id", "team_id", "team_abbreviation", "full_name", "position_abbreviation", "status_name"},
                "nba_schedule_2026.csv": {"game_id", "date", "home_id", "away_id", "home_score", "away_score", "season_type", "status_type_name", "status_type_completed", "notes_headline"},
                "nba_schedule_2027.csv": {"game_id", "date", "home_id", "away_id", "season_type", "notes_headline"}}
    for filename, fields in required.items():
        if not tables.get(filename) or not fields <= tables[filename][0].keys():
            raise InputError("missing verified source fields: " + filename)
    for filename, keys in (("player_box_2026.csv", ("game_id", "athlete_id")), ("rosters_2027.csv", ("athlete_id",)),
                           ("nba_schedule_2026.csv", ("game_id",)), ("nba_schedule_2027.csv", ("game_id",))):
        seen = set()
        for r in tables[filename]:
            key = tuple(r[k] for k in keys)
            # Known raw-file defect: unidentified Chicago rows. Retain them for
            # coverage auditing; never invent an ID or attach their stats.
            allowed_unidentified = filename == "player_box_2026.csv" and key[0] and not key[1]
            if key in seen or (any(not v for v in key) and not allowed_unidentified):
                raise InputError("duplicate/empty source identity: " + filename)
            seen.add(key)
    return tables


def eligibility(game, nba_teams):
    if game["season_type"] != "2":
        return "NON_REGULAR_SEASON"
    if game["home_id"] not in nba_teams or game["away_id"] not in nba_teams:
        return "NON_NBA_TEAM_EVENT"
    if game["notes_headline"] == "NBA Cup Championship":
        return "CUP_FINAL_EXCLUDED_CANDIDATE"
    return "ELIGIBLE"


def stats(row):
    values = []
    for field in STAT_FIELDS:
        value = None if row[field] == "" else number(row[field])
        if value is not None and value != value.to_integral_value():
            raise InputError("fractional counting stat")
        values.append(value)
    return tuple(values)


def participation(row):
    values = stats(row)
    minutes = number(row["minutes"]) if row["minutes"] else None
    activity = any(v is not None and v > 0 for v in values)
    if row["did_not_play"] == "true":
        if activity or (minutes is not None and minutes > 0):
            raise InputError("DNP contradicts source production")
        return "DNP", minutes, False
    if row["did_not_play"] != "false":
        raise InputError("unknown source DNP flag")
    if minutes is not None and minutes > 0:
        return "APPEARED", minutes, False
    if minutes == 0 and activity:
        return "APPEARED", minutes, True
    # Includes all-zero brief appearances that cannot be resolved from these
    # bytes. The reason/active fields are not trusted to silently turn this DNP.
    return "UNKNOWN", minutes, False


def week_audit(tables, start, end):
    teams = {r["team_id"] for r in tables["rosters_2027.csv"]}
    games = [g for g in tables["nba_schedule_2026.csv"] if start <= timestamp(g["date"]) < end and eligibility(g, teams) == "ELIGIBLE"]
    by_game_team = defaultdict(list)
    for row in tables["player_box_2026.csv"]:
        by_game_team[row["game_id"], row["team_id"]].append(row)
    checks, issues = [], []
    for game in games:
        if game["status_type_completed"] != "true" or game["status_type_name"] != "STATUS_FINAL":
            issues.append({"game_id": game["game_id"], "reason": game["status_type_name"]})
            continue
        for side in ("home", "away"):
            rows = by_game_team[game["game_id"], game[side + "_id"]]
            total = sum((number(r["points"]) for r in rows if r["points"]), Decimal(0))
            missing = [r["athlete_id"] for r in rows if r["did_not_play"] == "false" and not r["points"]]
            unidentified = sum(not r["athlete_id"] for r in rows)
            passed = bool(rows) and not missing and not unidentified and total == number(game[side + "_score"])
            check = {"game_id": game["game_id"], "team_id": game[side + "_id"], "box_points": str(total),
                     "schedule_points": game[side + "_score"], "rows": len(rows), "missing_points": missing, "unidentified_rows": unidentified, "passed": passed}
            checks.append(check)
            if not passed:
                issues.append({**check, "reason": "TEAM_POINTS_RECONCILIATION"})
    return {"start_utc": start, "end_utc": end, "games": len(games), "team_checks": checks,
            "issues": issues, "complete_game_spine": not issues,
            "limitation": "Team points cross-check does not prove every DNP roster row or every non-points stat"}


def normalize_week(tables, case, output):
    """Selected real player-week ledger, checked against every team schedule game.

    Case records explicit historical team association/positions; no present-day
    roster is backfilled into history. Missing selected rows stay missing.
    """
    start, end = case["rules"]["start_utc"], case["rules"]["end_utc"]
    audit = week_audit(tables, start, end)
    if not audit["complete_game_spine"]:
        raise InputError("historical game spine has unresolved outcomes or point mismatches")
    nba = {r["team_id"] for r in tables["rosters_2027.csv"]}
    games = [g for g in tables["nba_schedule_2026.csv"] if start <= timestamp(g["date"]) < end and eligibility(g, nba) == "ELIGIBLE"]
    raw_boxes = {(r["game_id"], r["athlete_id"]): r for r in tables["player_box_2026.csv"]}
    rows = {k: [] for k in HEADERS}
    rows["games"] = [[g["game_id"], timestamp(g["date"]), "FINAL", "true"] for g in sorted(games, key=lambda g: g["game_id"])]
    trace = []
    for player in case["players"]:
        pid, source_id, team_id = player["player_id"], player["athlete_id"], player["historical_team_id"]
        if pid != "espn:" + source_id:
            raise InputError("source namespace mapping mismatch")
        expected = [g for g in games if team_id in (g["home_id"], g["away_id"])]
        rows["players"].append([pid, "|".join(player["positions"])])
        rows["coverage"].append([pid, "|".join(sorted(g["game_id"] for g in expected))])
        for g in expected:
            r = raw_boxes.get((g["game_id"], source_id))
            if r is None:
                trace.append({"player_id": pid, "game_id": g["game_id"], "state": "MISSING_ROW"})
                continue
            if r["team_id"] != team_id or POSITION.get(r["athlete_position_abbreviation"]) not in player["positions"]:
                raise InputError("historical membership or position conflict")
            state, minutes, confirmed = participation(r)
            rows["boxes"].append([pid, g["game_id"], state, "" if minutes is None else str(minutes), str(confirmed).lower(), *[r[k] for k in STAT_FIELDS]])
            trace.append({"player_id": pid, "game_id": g["game_id"], "state": state, "zero_minutes_confirmed": confirmed})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    paths, hashes = {}, {}
    for kind, header in HEADERS.items():
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows[kind])
        raw = stream.getvalue().encode("utf-8")
        path = output / (kind + ".csv")
        path.write_bytes(raw)
        paths[kind], hashes[kind] = path, hashlib.sha256(raw).hexdigest()
    return load_inputs(paths, hashes), {"game_spine": audit, "participation_trace": trace, "normalized_hashes": hashes}


def player_packet(tables, manifest):
    """Fixed header table, observed prior-season averages, explicit unknown health.

    Prior statistics are descriptive observed records, not projections or picks.
    Any ambiguous/missing-stat row suppresses that player's full-season averages.
    """
    roster = tables["rosters_2027.csv"]
    nba = {r["team_id"] for r in roster}
    schedule = {g["game_id"]: g for g in tables["nba_schedule_2026.csv"]}
    collected, unresolved, source_gaps = defaultdict(list), Counter(), Counter()
    season_audit = week_audit(tables, "2025-07-01T00:00:00Z", "2026-07-01T00:00:00Z")
    bad_team_games = {(c["game_id"], c["team_id"]) for c in season_audit["team_checks"] if not c["passed"]}
    rejected = Counter()
    for row in tables["player_box_2026.csv"]:
        if not row["athlete_id"]:
            rejected["UNIDENTIFIED_PLAYER_ROW"] += 1
            continue
        game = schedule.get(row["game_id"])
        if game is None:
            raise InputError("box game absent from schedule")
        reason = eligibility(game, nba)
        if reason != "ELIGIBLE":
            rejected[reason] += 1
            continue
        if row["team_id"] not in (game["home_id"], game["away_id"]):
            raise InputError("box team outside schedule")
        if (row["game_id"], row["team_id"]) in bad_team_games:
            source_gaps[row["athlete_id"]] += 1
        state, minutes, _ = participation(row)
        if game["status_type_completed"] != "true" or state == "UNKNOWN" or (state == "APPEARED" and any(v is None for v in stats(row))):
            unresolved[row["athlete_id"]] += 1
        elif state == "APPEARED":
            collected[row["athlete_id"]].append((minutes, stats(row)))
    columns = ["player_id", "name", "positions", "team", "prior_season", "confirmed_gp", "prior_stats_state", "ambiguous_rows", "source_gap_games", "minutes_pg", *[f + "_pg" for f in STAT_FIELDS], "injury_status", "injury_as_of_utc"]
    records = []
    aggregates = []
    with localcontext() as ctx:
        ctx.prec = 60
        for r in sorted(roster, key=lambda r: r["athlete_id"]):
            aid = r["athlete_id"]
            if r["position_abbreviation"] not in POSITION:
                raise InputError("unmapped current source position")
            observed = collected[aid]
            count = len(observed)
            state = "TEAM_SOURCE_GAPS" if source_gaps[aid] else "INCOMPLETE_ROWS" if unresolved[aid] else "OBSERVED_RECORDS_ONLY" if count else "NO_OBSERVED_PRIOR_DATA"
            totals = [sum((v[i] for _, v in observed), Decimal(0)) for i in range(len(STAT_FIELDS))]
            minute_total = sum((m for m, _ in observed), Decimal(0))
            averages = [None] * 12
            if count and not unresolved[aid] and not source_gaps[aid]:
                averages = [format((v / count).quantize(Decimal("0.001")), "f") for v in (minute_total, *totals)]
            records.append(["espn:" + aid, r["full_name"], [POSITION[r["position_abbreviation"]]], r["team_abbreviation"], "2025-26", count, state, unresolved[aid], source_gaps[aid], *averages, None, None])
            aggregates.append({"player_id": "espn:" + aid, "confirmed_gp": count, "minutes_total": str(minute_total),
                               "stat_totals": dict(zip(STAT_FIELDS, map(str, totals))), "ambiguous_rows": unresolved[aid], "source_gap_games": source_gaps[aid]})
    return {"schema": "basketball-real-player-packet-candidate/1", "provisional": True, "draft_ready": False,
            "retrieved_at_utc": manifest["retrieved_at_utc"], "columns": columns, "players": records,
            "provenance": manifest["files"], "excluded_box_rows": dict(rejected), "observed_aggregates": aggregates,
            "season_game_spine_issues": season_audit["issues"],
            "display_rounding": "Per-game averages rounded to 0.001, half-even; underlying integer totals retained",
            "limitations": ["Injury status and as-of are unknown for every player; roster Active is not health",
                            "GP counts explicit confirmed source appearances; season-long roster/missing-game coverage not independently established",
                            "Ambiguous rows or a failed team/game source check suppress all per-game averages for the affected player; no-data is not zero or a rookie assertion",
                            "Current source position/team labels and proposed event exclusions require owner freeze",
                            "ID order is not ranking; Build must apply the frozen packet row-order policy"]}


def build_player_specs(packet):
    """Return constructor-shaped data for Build Player; no Build import or picks.

    Every fact is in column order, string or explicit null. The maximum is
    exactly 20 facts under the current Build contract. Row randomization and
    ownership belong to Build's separately frozen decision-packet policy.
    """
    columns = packet["columns"]
    if columns[:3] != ["player_id", "name", "positions"] or len(set(columns)) != len(columns) or len(columns) - 3 > 20:
        raise InputError("incompatible real packet columns")
    specs = []
    for row in packet["players"]:
        if len(row) != len(columns):
            raise InputError("packet row/column misalignment")
        specs.append({"player_id": row[0], "name": row[1], "positions": tuple(row[2]),
                      "facts": tuple((name, None if value is None else str(value)) for name, value in zip(columns[3:], row[3:]))})
    return specs
