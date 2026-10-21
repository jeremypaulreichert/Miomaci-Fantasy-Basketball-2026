"""Immutable manager-visible study rules, separate from untrusted player facts."""
from dataclasses import asdict, dataclass
from fractions import Fraction

from ..core.config import digest


STATS = frozenset(("points", "three_point_field_goals_made", "rebounds", "assists",
                   "steals", "blocks", "turnovers", "field_goals_made", "field_goals_attempted",
                   "free_throws_made", "free_throws_attempted"))


@dataclass(frozen=True)
class ResearchTask:
    scoring: tuple[tuple[str, str], ...]
    roster_size: int
    slots: tuple[tuple[str, tuple[str, ...]], ...]
    require_research: bool = False

    def __post_init__(self):
        if (type(self.scoring) is not tuple or len(self.scoring) != len(STATS)
                or any(type(row) is not tuple or len(row) != 2 for row in self.scoring)
                or {row[0] for row in self.scoring} != STATS
                or type(self.require_research) is not bool
                or type(self.roster_size) is not int or not 1 <= self.roster_size <= 30
                or type(self.slots) is not tuple or not 1 <= len(self.slots) <= 10):
            raise ValueError("complete immutable research task required")
        for _, weight in self.scoring:
            if type(weight) is not str or len(weight) > 24 or abs(Fraction(weight)) > 1000:
                raise ValueError("finite bounded exact scoring weights required")
        if len({name for name, _ in self.slots}) != len(self.slots):
            raise ValueError("unique lineup slots required")
        for name, positions in self.slots:
            if (type(name) is not str or not name or len(name) > 20 or type(positions) is not tuple
                    or not positions or any(p not in {"G", "F", "C"} for p in positions)):
                raise ValueError("immutable G/F/C lineup slots required")
        if len(self.slots) > self.roster_size:
            raise ValueError("lineup exceeds roster")

    @property
    def hash(self):
        return digest(asdict(self))

    def packet(self):
        return {"schema": "basketball-manager-task/1", "task_hash": self.hash,
                "scope": "uncounted real-player pilot; not a counted season rule freeze",
                "objective": "Build a frozen roster to maximize cumulative weekly fantasy points. "
                "Each week an automatic retrospective optimizer selects the best legal lineup from your roster. "
                "Bench players provide alternatives; there is no in-season AI lineup management or transaction task. "
                "Choose for this fantasy formula, not merely NBA points or a player's ID.",
                "scoring_weights": dict(self.scoring), "roster_size": self.roster_size,
                "lineup_slots": [[name, list(positions)] for name, positions in self.slots],
                "calculation": "Sum every listed stat times its weight for each eligible game, including overlapping "
                "contributions. Sum game scores into player-week totals before optimizing the lineup. "
                "Do not clamp negative scores to zero. Distinct players fill each required slot.",
                "data_limits": "Historical averages are observations, not forecasts. Injuries are unknown; "
                "missing or absent facts are not zero. G/F/C eligibility is the frozen packet eligibility. "
                "This pilot tests research and legal selection, not measured season performance. "
                "Calendar, counted-event and season tie policies remain outside this uncounted pilot.",
                "research_policy": ("Pilot capability demonstration: before each pick, independently choose a search "
                                    "and read a discovered page. Both must return accepted evidence. "
                                    "Choose your own queries, sources and final player."
                                    if self.require_research else
                                    "Independent research is available; you may choose before using all allowances.")}


def pilot_task(slots, roster_size=13):
    weights = dict(points="1", three_point_field_goals_made="1", rebounds="1", assists="2",
                   steals="4", blocks="4", turnovers="-2", field_goals_made="2", field_goals_attempted="-1",
                   free_throws_made="1", free_throws_attempted="-1")
    return ResearchTask(tuple(sorted(weights.items())), roster_size, slots, True)
