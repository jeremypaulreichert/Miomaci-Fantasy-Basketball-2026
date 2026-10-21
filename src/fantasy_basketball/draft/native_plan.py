"""Offline snake-draft configuration for the existing native lifecycle.

No transport, event loop or alternative recovery implementation lives here.
The initial pool, rules, profiles and snake order are frozen into one digest.
"""
from dataclasses import asdict, dataclass, replace
from collections import OrderedDict
from functools import cached_property
from threading import Lock
import re

from ..core.config import digest, identifier, integer
from ..providers.native import NativeProfile
from .board import Board


@dataclass(frozen=True)
class NativeDraftConfig:
    run_id: str
    profiles: tuple[NativeProfile, ...]
    board: Board
    budget_micro_usd: int
    trial_ms: int
    source_plan_hash: str
    rules_digest: str
    mode: str = "OFFLINE_FIXTURE"
    experiment_id: str = "miomaci-basketball-native-draft-offline"
    schema: str = "basketball-native-draft/1"

    def __post_init__(self):
        identifier(self.run_id)
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", self.run_id)
                or self.run_id.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}):
            raise ValueError("safe run ID required")
        offline=(self.mode,self.schema,self.experiment_id)==(
            "OFFLINE_FIXTURE","basketball-native-draft/1","miomaci-basketball-native-draft-offline")
        diagnostic=(self.mode,self.schema,self.experiment_id)==(
            "LIVE_DIAGNOSTIC","basketball-native-draft/2","miomaci-basketball-native-draft-qualification")
        if not (offline or diagnostic):
            raise ValueError("native draft requires an explicit offline or qualification namespace")
        if (type(self.profiles) is not tuple or not 1 <= len(self.profiles) <= 10
                or any(type(p) is not NativeProfile for p in self.profiles)
                or len({p.provider for p in self.profiles}) != len(self.profiles)
                or len({p.profile_id for p in self.profiles}) != len(self.profiles)):
            raise ValueError("unique immutable native managers required")
        if (type(self.board) is not Board or self.board.ownership or self.board.pick_index != 0
                or self.board.manager != self.profiles[0].provider):
            raise ValueError("empty initial board for first manager required")
        if {p.provider for p in self.profiles} >= {"meta", "meta_direct"}:
            raise ValueError("one Meta seat; hosted and direct routes cannot share a cohort")
        if (len(self.board.players) - len(self.board.locked_ids) < self.total_picks
                or not self.board.legal_ids()):
            raise ValueError("initial pool cannot supply draft")
        integer(self.budget_micro_usd)
        integer(self.trial_ms, 1)
        for value in (self.source_plan_hash, self.rules_digest):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("source plan and rules digests required")

    @cached_property
    def hash(self):
        return digest(asdict(self))

    @property
    def total_picks(self):
        return len(self.profiles) * self.board.roster_size

    @property
    def max_requests(self):
        return self.board.roster_size * sum(p.max_attempts for p in self.profiles)

    @cached_property
    def cases(self):
        # Schedule entries reuse the immutable pool. Runtime boards are derived
        # from settled results, never from these empty ownership templates.
        from .trial import TrialCase
        rows = []
        for index in range(self.total_picks):
            round_number, seat = divmod(index, len(self.profiles))
            profile = self.profiles[seat if round_number % 2 == 0 else -1 - seat]
            rows.append(TrialCase(profile, replace(self.board, manager=profile.provider,
                                                  case_id=f"pick-{index}", pick_index=index)))
        return tuple(rows)

    @cached_property
    def _board_cache(self):
        # Only immutable derived boards; never journal state or event validation.
        return OrderedDict(), Lock()

    def board_at(self, index, results):
        integer(index)
        if index >= self.total_picks or len(results) < index:
            raise ValueError("settled draft prefix required")
        ownership = []
        for position, result in enumerate(results[:index]):
            case = self.cases[position]
            if (result["case_index"] != position or result["case_id"] != case.board.case_id
                    or result["profile_id"] != case.profile.profile_id):
                raise ValueError("draft prefix order or identity differs")
            ownership.append((result["action"]["player_id"], case.profile.provider))
        key = (index, tuple(ownership))
        # Validate the complete prefix above even on a cache hit. A different
        # selection history must not reuse another board's ownership/legality.
        cache, lock = self._board_cache
        with lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
            board = replace(self.cases[index].board, ownership=key[1])
            cache[key] = board
            if len(cache) > 256:
                cache.popitem(last=False)
            return board
