"""Exact configurable linear scores and slot-assignment weekly best-ball.

All results are synthetic/offline candidates. Missing bench outcomes also block
final standings. Negative totals never relax required slot cardinality.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
import hashlib
import json
import re

from ..data.local import Dataset, InputError, STAT_FIELDS, identifier, number, utc


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def decimal_text(value):
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


@dataclass(frozen=True)
class Rules:
    rules_id: str
    period_id: str
    start_utc: str
    end_utc: str
    weights: tuple[tuple[str, str], ...]
    slots: tuple[tuple[str, tuple[str, ...]], ...]
    roster_size: int
    mode: str = "SYNTHETIC_OFFLINE"
    fill_policy: str = "ALL_SLOTS_REQUIRED"
    postponed_policy: str = "BLOCK_UNTIL_RESOLVED"
    canceled_policy: str = "EXCLUDE"
    tie_policy: str = "EXACT_SCORE_SHARED_RANK_LEXICAL_LINEUP"
    schema: str = "basketball-scoring-rules/1"

    def __post_init__(self):
        identifier(self.rules_id)
        identifier(self.period_id)
        if utc(self.start_utc) >= utc(self.end_utc):
            raise InputError("nonempty half-open UTC period required")
        if (self.mode not in {"SYNTHETIC_OFFLINE", "HISTORICAL_OFFLINE_CANDIDATE"} or self.fill_policy != "ALL_SLOTS_REQUIRED"
                or self.postponed_policy != "BLOCK_UNTIL_RESOLVED" or self.canceled_policy != "EXCLUDE"
                or self.tie_policy != "EXACT_SCORE_SHARED_RANK_LEXICAL_LINEUP" or self.schema != "basketball-scoring-rules/1"):
            raise InputError("unsupported provisional policy")
        if type(self.roster_size) is not int or not 1 <= self.roster_size <= 30:
            raise InputError("roster size must be 1..30")
        if type(self.weights) is not tuple or not self.weights:
            raise InputError("immutable explicit weights required")
        keys = []
        for pair in self.weights:
            if type(pair) is not tuple or len(pair) != 2 or pair[0] not in STAT_FIELDS:
                raise InputError("unknown weight field")
            keys.append(pair[0])
            number(pair[1], signed=True)
        if len(set(keys)) != len(keys) or not any(number(v, signed=True) for _, v in self.weights):
            raise InputError("unique weights and a nonzero coefficient required")
        if type(self.slots) is not tuple or not 1 <= len(self.slots) <= min(self.roster_size, 10):
            raise InputError("1..10 slots, no more than roster size, required")
        names = []
        for pair in self.slots:
            if type(pair) is not tuple or len(pair) != 2:
                raise InputError("immutable slot definition required")
            name, positions = pair
            identifier(name)
            names.append(name)
            if type(positions) is not tuple or not positions or len(set(positions)) != len(positions) or not set(positions) <= {"G", "F", "C"}:
                raise InputError("invalid slot eligibility")
        if len(set(names)) != len(names):
            raise InputError("duplicate slot name")

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["weights"] = tuple((k, v) for k, v in value["weights"].items())
        value["slots"] = tuple((s["name"], tuple(s["positions"])) for s in value["slots"])
        return cls(**value)

    @property
    def hash(self):
        return digest(asdict(self))


def validate_rosters(rosters, players, rules):
    if type(rosters) is not dict or not rosters or len(rosters) > 100:
        raise InputError("nonempty manager-to-player-list mapping required")
    seen = set()
    for manager, ids in rosters.items():
        identifier(manager)
        if type(ids) not in {tuple, list} or len(ids) != rules.roster_size:
            raise InputError("roster size mismatch")
        for player in ids:
            identifier(player)
            if player not in players or player in seen:
                raise InputError("unknown or multiply owned player")
            seen.add(player)


def rosters_from_draft(status, *, managers, roster_size):
    """Convert Build Draft.status() without importing Build or creating picks.

    This checks structural completeness, not journal authenticity. The caller
    retains Build's config/journal receipts separately.
    """
    if status.get("phase") != "COMPLETE" or status.get("mode") != "OFFLINE_FIXTURE" or status.get("pause") is not None:
        raise InputError("complete offline fixture draft required")
    if type(managers) not in {tuple, list} or not managers or len(set(managers)) != len(managers):
        raise InputError("explicit unique managers required")
    if type(roster_size) is not int or not 1 <= roster_size <= 30:
        raise InputError("invalid roster size")
    result = {identifier(m): [] for m in managers}
    picks = status.get("picks")
    if type(picks) is not list or len(picks) != len(managers) * roster_size or type(status.get("total_picks")) is not int or status["total_picks"] != len(picks):
        raise InputError("incomplete pick count")
    seen = set()
    for index, pick in enumerate(picks):
        if type(pick) is not dict or type(pick.get("index")) is not int or pick["index"] != index or pick.get("origin") != "SIMULATED_AI" or pick.get("manager") not in result:
            raise InputError("invalid settled pick")
        player = identifier(pick.get("player_id"))
        if player in seen:
            raise InputError("duplicate settled player")
        seen.add(player)
        result[pick["manager"]].append(player)
    if any(len(ids) != roster_size for ids in result.values()):
        raise InputError("uneven roster counts")
    return result


def rosters_from_receipt(receipt, *, experiment_id, run_id, config_hash):
    """Consume Build's settled-roster/1 with caller-pinned run/config identity.

    Verify content integrity and ownership consistency, not authenticity. Retain
    the original receipt and its hash with the calling integration's evidence.
    """
    identifier(experiment_id)
    identifier(run_id)
    if type(config_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", config_hash):
        raise InputError("pinned configuration SHA-256 required")
    required = {"schema_version", "experiment_id", "run_id", "config_hash", "draft_rules_digest",
                "execution_mode", "simulated", "provisional", "competitive_authorization", "draft_status",
                "roster_size", "total_picks", "rosters", "picks", "receipt_hash"}
    if type(receipt) is not dict or set(receipt) != required:
        raise InputError("unexpected roster receipt fields")
    if (receipt["schema_version"] != "miomaci.basketball.settled-roster/1"
            or receipt["execution_mode"] != "OFFLINE_FIXTURE" or receipt["draft_status"] != "COMPLETE"
            or receipt["simulated"] is not True or receipt["provisional"] is not True
            or receipt["competitive_authorization"] is not False):
        raise InputError("complete provisional simulated receipt required")
    if (receipt["experiment_id"], receipt["run_id"], receipt["config_hash"]) != (experiment_id, run_id, config_hash):
        raise InputError("roster receipt identity mismatch")
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    # Match Build's documented Unicode-preserving wire canonicalization exactly.
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != receipt["receipt_hash"]:
        raise InputError("roster receipt hash mismatch")
    rows = receipt["rosters"]
    if type(rows) is not list or not rows or any(type(r) is not dict or set(r) != {"manager_id", "model_id", "player_ids"} for r in rows):
        raise InputError("invalid roster receipt rows")
    managers = [r["manager_id"] for r in rows]
    result = rosters_from_draft({"phase": receipt["draft_status"], "mode": receipt["execution_mode"], "pause": None,
                                "picks": receipt["picks"], "total_picks": receipt["total_picks"]},
                               managers=managers, roster_size=receipt["roster_size"])
    for row in rows:
        identifier(row["model_id"])
        if row["player_ids"] != result[row["manager_id"]] or any(p.get("model") != row["model_id"] for p in receipt["picks"] if p["manager"] == row["manager_id"]):
            raise InputError("roster/pick ownership or model mismatch")
    return result


def best_lineup(player_ids, players, totals, slots):
    """DP on occupied slot masks; each player is consumed at most once.

    States retain maximum exact score, then the lexical player-ID tuple in
    declared slot order. No G/F/C disjointness shortcut is used.
    """
    states = {0: (Decimal(0), ("",) * len(slots))}
    for player_id in sorted(player_ids):
        for mask, (score, assignment) in tuple(states.items()):
            for i, (_, eligible) in enumerate(slots):
                if mask & (1 << i) or not set(players[player_id].positions).intersection(eligible):
                    continue
                new_mask = mask | (1 << i)
                chosen = assignment[:i] + (player_id,) + assignment[i + 1:]
                candidate = (score + totals[player_id], chosen)
                old = states.get(new_mask)
                if old is None or candidate[0] > old[0] or (candidate[0] == old[0] and chosen < old[1]):
                    states[new_mask] = candidate
    return states.get((1 << len(slots)) - 1)


def evaluate(dataset, rosters, rules):
    """Pure deterministic result. Callers must use load_inputs for Dataset.

    Uses a private 60-digit context; bounded inputs cannot lose precision and
    changes to the process's ambient Decimal precision do not affect scores.
    """
    if type(dataset) is not Dataset or type(rules) is not Rules:
        raise InputError("loaded dataset and validated Rules required")
    with localcontext() as context:
        context.prec = 60
        return _evaluate(dataset, rosters, rules)


def _evaluate(dataset, rosters, rules):
    players = {p.player_id: p for p in dataset.players}
    validate_rosters(rosters, players, rules)
    games = {g.game_id: g for g in dataset.games}
    coverage = dict(dataset.coverage)
    boxes = {(b.player_id, b.game_id): b for b in dataset.boxes}
    weights = [(STAT_FIELDS.index(k), number(v, signed=True)) for k, v in rules.weights if number(v, signed=True)]
    start, end = utc(rules.start_utc), utc(rules.end_utc)
    teams, team_totals = {}, {}
    for manager in sorted(rosters):
        outcomes, totals, issues = {}, {}, []
        for player_id in sorted(rosters[manager]):
            facts, total, complete = [], Decimal(0), True
            for game_id in coverage[player_id]:
                game = games[game_id]
                if not start <= utc(game.tipoff_utc) < end:
                    facts.append({"game_id": game_id, "state": "OUTSIDE_PERIOD"})
                    continue
                if not game.eligible or game.status == "CANCELED":
                    facts.append({"game_id": game_id, "state": "EXCLUDED"})
                    continue
                box = boxes.get((player_id, game_id))
                state, score = game.status, None
                if game.status == "FINAL":
                    if box is None:
                        state = "MISSING_ROW"
                    elif box.participation == "DNP":
                        state, score = "DNP", Decimal(0)
                    elif box.participation == "UNKNOWN" or box.minutes is None or (box.minutes == 0 and not box.zero_minutes_confirmed):
                        state = "AMBIGUOUS_APPEARANCE"
                    elif any(box.stats[i] is None for i, _ in weights):
                        state = "MISSING_STATS"
                    else:
                        score = sum((box.stats[i] * weight for i, weight in weights), Decimal(0))
                        state = "APPEARED_ZERO" if score == 0 else "APPEARED"
                facts.append({"game_id": game_id, "state": state, "score": None if score is None else decimal_text(score)})
                if score is None:
                    complete = False
                    issues.append({"player_id": player_id, "game_id": game_id, "reason": state})
                else:
                    total += score
            counted = [f for f in facts if f["state"] not in {"OUTSIDE_PERIOD", "EXCLUDED"}]
            outcomes[player_id] = {"state": "INCOMPLETE" if not complete else "COMPLETE" if counted else "NO_COUNTED_GAMES",
                                   "total": decimal_text(total) if complete else None, "games": facts}
            if complete:
                totals[player_id] = total
        lineup = None
        if not issues:
            lineup = best_lineup(rosters[manager], players, totals, rules.slots)
            if lineup is None:
                issues.append({"reason": "NO_LEGAL_FULL_LINEUP"})
        teams[manager] = {"state": "INCOMPLETE" if issues else "COMPLETE", "issues": issues, "players": outcomes,
                          "score": None if lineup is None else decimal_text(lineup[0]),
                          "lineup": None if lineup is None else [{"slot": slot[0], "player_id": pid} for slot, pid in zip(rules.slots, lineup[1])]}
        if lineup is not None:
            team_totals[manager] = lineup[0]
    complete = len(team_totals) == len(teams)
    standings = None
    if complete:
        standings = []
        previous_score, rank = None, 0
        for place, manager in enumerate(sorted(team_totals, key=lambda m: (-team_totals[m], m)), 1):
            score = team_totals[manager]
            if score != previous_score:
                rank = place
            standings.append({"manager": manager, "rank": rank, "score": decimal_text(score)})
            previous_score = score
    return {"schema": "basketball-score-result/1", "mode": rules.mode,
            "state": "COMPLETE" if complete else "INCOMPLETE", "final_release_eligible": complete,
            "disclosure": ("Retrospective optimal lineups; synthetic fixtures only; no AI season-management claim."
                           if rules.mode == "SYNTHETIC_OFFLINE" else
                           "Retrospective optimal lineups; historical offline candidate, not counted competition; no AI season-management claim."),
            "rules": asdict(rules), "rules_hash": rules.hash,
            "rosters": {m: sorted(ids) for m, ids in sorted(rosters.items())},
            "inputs": [{"kind": k, "sha256": h, "bytes": size} for k, h, size in dataset.receipts],
            "teams": teams, "standings": standings}
