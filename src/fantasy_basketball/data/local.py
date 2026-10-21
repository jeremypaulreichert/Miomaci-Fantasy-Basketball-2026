"""Strict local CSV contract v1, initially exercised only with synthetic data.

This is NOT an ESPN/sportsdataverse adapter. Participation is explicit; `active`
is deliberately absent. The caller supplies byte hashes and a complete per-player
expected-game ledger. The adapter cannot prove the real-world completeness of
that ledger. Empty coverage explicitly means no scheduled games, never missing.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
from pathlib import Path
import re

STAT_FIELDS = (
    "points", "three_point_field_goals_made", "rebounds", "assists", "steals",
    "blocks", "turnovers", "field_goals_made", "field_goals_attempted",
    "free_throws_made", "free_throws_attempted",
)
HEADERS = {
    "players": ("player_id", "positions"),
    "games": ("game_id", "tipoff_utc", "status", "eligible"),
    "coverage": ("player_id", "game_ids"),
    "boxes": ("player_id", "game_id", "participation", "minutes", "zero_minutes_confirmed", *STAT_FIELDS),
}


class InputError(ValueError):
    """Invalid evidence; no silently coerced or partially accepted input."""


def identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", value):
        raise InputError("invalid identifier")
    return value


def number(value, *, signed=False):
    # Fixed-point spelling and bounded scale make precision requirements explicit.
    pattern = r"-?[0-9]+(?:\.[0-9]{1,6})?" if signed else r"[0-9]+(?:\.[0-9]{1,6})?"
    if type(value) is not str or len(value) > 24 or not re.fullmatch(pattern, value):
        raise InputError("bounded fixed-point decimal string required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise InputError("invalid decimal") from exc
    if not result.is_finite() or result.copy_abs() > Decimal("1000000"):
        raise InputError("decimal out of bounds")
    return result


def utc(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise InputError("UTC second-resolution timestamp ending in Z required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise InputError("invalid UTC timestamp") from exc


def boolean(value):
    if value not in {"true", "false"}:
        raise InputError("literal CSV true/false required")
    return value == "true"


@dataclass(frozen=True)
class Player:
    player_id: str
    positions: tuple[str, ...]


@dataclass(frozen=True)
class Game:
    game_id: str
    tipoff_utc: str
    status: str
    eligible: bool


@dataclass(frozen=True)
class Box:
    player_id: str
    game_id: str
    participation: str
    minutes: Decimal | None
    zero_minutes_confirmed: bool
    stats: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class Dataset:
    players: tuple[Player, ...]
    games: tuple[Game, ...]
    coverage: tuple[tuple[str, tuple[str, ...]], ...]
    boxes: tuple[Box, ...]
    receipts: tuple[tuple[str, str, int], ...]
    schema: str = "normalized-local-basketball/1"


def load_inputs(paths, expected_hashes):
    """Read exactly four explicit local files; verify bytes before strict parsing.

    Reject all duplicate keys (even identical rows), extra/missing columns,
    malformed numbers and orphan records. Blank stat fields remain None so the
    scorer can report INCOMPLETE rather than manufacture zero.
    """
    if set(paths) != set(HEADERS) or set(expected_hashes) != set(HEADERS):
        raise InputError("players/games/coverage/boxes paths and hashes required")
    tables, receipts = {}, []
    for kind, header in HEADERS.items():
        expected = expected_hashes[kind]
        if type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise InputError("explicit lowercase SHA-256 required")
        path = Path(paths[kind])
        if not path.is_file() or path.stat().st_size > 64_000_000:
            raise InputError("missing or oversized local input")
        raw = path.read_bytes()
        if len(raw) > 64_000_000 or hashlib.sha256(raw).hexdigest() != expected:
            raise InputError("source byte hash mismatch")
        receipts.append((kind, expected, len(raw)))
        try:
            reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
            if tuple(next(reader)) != header:
                raise InputError("unexpected CSV header: " + kind)
            rows = list(reader)
        except (UnicodeError, csv.Error, StopIteration) as exc:
            raise InputError("malformed CSV: " + kind) from exc
        if len(rows) > 100000 or any(len(row) != len(header) for row in rows):
            raise InputError("CSV row count or width invalid")
        tables[kind] = [dict(zip(header, row)) for row in rows]

    players, games, coverage, boxes = {}, {}, {}, {}
    for row in tables["players"]:
        key = identifier(row["player_id"])
        positions = tuple(sorted(row["positions"].split("|")))
        if key in players or len(set(positions)) != len(positions) or not set(positions) <= {"G", "F", "C"}:
            raise InputError("duplicate player or invalid position set")
        players[key] = Player(key, positions)
    if not players or len(players) > 10000:
        raise InputError("bounded nonempty player pool required")
    for row in tables["games"]:
        key = identifier(row["game_id"])
        utc(row["tipoff_utc"])
        if key in games or row["status"] not in {"FINAL", "SCHEDULED", "POSTPONED", "CANCELED"}:
            raise InputError("duplicate game or invalid game status")
        games[key] = Game(key, row["tipoff_utc"], row["status"], boolean(row["eligible"]))
    if len(games) > 10000:
        raise InputError("too many games")
    for row in tables["coverage"]:
        key = identifier(row["player_id"])
        ids = tuple(sorted(row["game_ids"].split("|"))) if row["game_ids"] else ()
        if key not in players or key in coverage or len(set(ids)) != len(ids) or not set(ids) <= games.keys():
            raise InputError("duplicate/unknown coverage player or game")
        coverage[key] = ids
    if coverage.keys() != players.keys():
        raise InputError("explicit coverage required for every player, including no-games players")
    for row in tables["boxes"]:
        key = (row["player_id"], row["game_id"])
        if key in boxes:
            raise InputError("duplicate player-game fact (identical or conflicting)")
        if key[0] not in coverage or key[1] not in coverage[key[0]]:
            raise InputError("box outside expected-game ledger")
        state = row["participation"]
        if state not in {"APPEARED", "DNP", "UNKNOWN"}:
            raise InputError("explicit participation required")
        minutes = number(row["minutes"]) if row["minutes"] else None
        confirmed = boolean(row["zero_minutes_confirmed"])
        stats = tuple(number(row[f]) if row[f] else None for f in STAT_FIELDS)
        if any(s is not None and s != s.to_integral_value() for s in stats):
            raise InputError("basketball counting stats must be integers")
        if confirmed and not (state == "APPEARED" and minutes == 0):
            raise InputError("zero-minute evidence inconsistent with participation")
        if state == "DNP" and (minutes not in {None, Decimal(0)} or any(s not in {None, Decimal(0)} for s in stats)):
            raise InputError("DNP contradicts production or minutes")
        values = dict(zip(STAT_FIELDS, stats))
        for made, attempted in (("field_goals_made", "field_goals_attempted"), ("free_throws_made", "free_throws_attempted"), ("three_point_field_goals_made", "field_goals_made")):
            if values[made] is not None and values[attempted] is not None and values[made] > values[attempted]:
                raise InputError("made/attempted counts conflict")
        boxes[key] = Box(*key, state, minutes, confirmed, stats)
    return Dataset(tuple(players[k] for k in sorted(players)), tuple(games[k] for k in sorted(games)),
                   tuple(sorted(coverage.items())), tuple(boxes[k] for k in sorted(boxes)), tuple(sorted(receipts)))
