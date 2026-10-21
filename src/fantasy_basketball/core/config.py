"""One immutable source for fixture request, spending and deadline limits."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError("finite bounded integer required")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", value):
        raise ValueError("invalid identifier")
    return value


@dataclass(frozen=True)
class ProviderLimits:
    manager: str
    model: str
    output_field: str
    output_tokens: int
    input_tokens: int
    reasoning_mode: str
    separate_reasoning_tokens: int
    input_usd_per_million: str
    output_usd_per_million: str
    reasoning_usd_per_million: str
    fixed_micro_usd: int
    safety_micro_usd: int
    timeout_ms: int
    response_bytes: int
    max_attempts: int

    def __post_init__(self):
        identifier(self.manager)
        identifier(self.model)
        if not self.model.startswith("fixture-"):
            raise ValueError("only fixture model identities are supported")
        if self.output_field not in {"max_output_tokens", "max_tokens", "max_completion_tokens", "maxOutputTokens"}:
            raise ValueError("unsupported fixture output field")
        for name in ("output_tokens", "input_tokens", "timeout_ms", "response_bytes", "max_attempts"):
            integer(getattr(self, name), 1)
        for name in ("separate_reasoning_tokens", "fixed_micro_usd", "safety_micro_usd"):
            integer(getattr(self, name))
        if self.reasoning_mode not in {"combined", "separate"}:
            raise ValueError("unknown or unbounded reasoning semantics")
        if self.reasoning_mode == "combined" and self.separate_reasoning_tokens:
            raise ValueError("combined output must not add reasoning twice")
        for name in ("input_usd_per_million", "output_usd_per_million", "reasoning_usd_per_million"):
            value = getattr(self, name)
            try:
                rate = Decimal(value) if type(value) is str else Decimal("NaN")
            except InvalidOperation as exc:
                raise ValueError("invalid rate") from exc
            if not rate.is_finite() or not 0 <= rate <= 1000000 or rate.as_tuple().exponent < -6:
                raise ValueError("finite rate with at most six decimals required")


@dataclass(frozen=True)
class RunConfig:
    experiment_id: str
    run_id: str
    providers: tuple[ProviderLimits, ...]
    roster_size: int
    budget_micro_usd: int
    opportunity_ms: int
    rules_digest: str
    correction_failures: tuple[str, ...] = ("MALFORMED_ACTION", "ILLEGAL_PICK")
    mode: str = "OFFLINE_FIXTURE"
    schema: str = "basketball-build/1"

    def __post_init__(self):
        identifier(self.experiment_id)
        identifier(self.run_id)
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", self.run_id)
                or self.run_id.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}):
            raise ValueError("run ID must be a safe filename stem")
        if not self.experiment_id.startswith("miomaci-basketball-"):
            raise ValueError("foreign experiment namespace")
        if self.mode != "OFFLINE_FIXTURE" or self.schema not in {"basketball-build/1", "basketball-build/2"}:
            raise ValueError("offline fixture configuration required")
        if type(self.providers) is not tuple or not self.providers or any(type(p) is not ProviderLimits for p in self.providers):
            raise ValueError("immutable provider profiles required")
        if len({p.manager for p in self.providers}) != len(self.providers):
            raise ValueError("duplicate manager")
        integer(self.roster_size, 1)
        integer(self.opportunity_ms, 1)
        integer(self.budget_micro_usd)
        if not isinstance(self.rules_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.rules_digest):
            raise ValueError("explicit rules digest required")
        if type(self.correction_failures) is not tuple or len(set(self.correction_failures)) != len(self.correction_failures):
            raise ValueError("immutable correction policy required")
        if set(self.correction_failures) - {"MALFORMED_ACTION", "ILLEGAL_PICK"}:
            raise ValueError("unreviewed correction class")
        from .budget import request_bound
        for profile in self.providers:
            integer(request_bound(profile))

    @property
    def hash(self):
        return digest(asdict(self))

    @property
    def total_picks(self):
        return len(self.providers) * self.roster_size

    def provider(self, manager):
        return next(p for p in self.providers if p.manager == manager)

    def at_pick(self, index):
        integer(index)
        if index >= self.total_picks:
            raise ValueError("draft complete")
        round_number, seat = divmod(index, len(self.providers))
        return self.providers[seat if round_number % 2 == 0 else -1 - seat]
