"""Durable research opportunity/pilot, isolated from historical draft runs.

This functioning slice reuses the event journal and native receipt validators.
Live pilots require a separately bound permit and source snapshot. There is no
competitive mode, automatic retry, forfeit or arbitrary unpause. Local recovery
can only revalidate an already saved response to an unknown dispatch.
Single opportunities and evolving snake drafts use the same research reducer.

The LIVE_COUNTED_DRAFT namespace (added October 19) reuses this reducer for the
counted draft with its own permit type, source inventory, manager task, fixed
information cutoff and append-only authorization renewal. Profiles with the
REQUEST_BYTES_VERIFIED basis reserve a request-specific amount while every
outcome without validated usage retains the documented-ceiling bound.
"""
from copy import deepcopy
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import cached_property
import re

from ..core.config import canonical, digest, integer
from ..core.journal import Journal
from ..providers.native import NativeProfile
from ..providers.research import inspect_action, request, tool_result
from ..providers.research_service import credit_usage, normalize_tool
from ..providers.research_task import ResearchTask
from .board import Board
from .native_plan import NativeDraftConfig


LIVE_MODES = ("LIVE_RESEARCH_PILOT", "LIVE_COUNTED_DRAFT")
COUNTED_NAMESPACE = ("LIVE_COUNTED_DRAFT", "miomaci-basketball-counted-draft", "basketball-counted-draft/1")
# Allowance for provider-added template/schema tokens beyond the serialized body.
BYTE_BOUND_OVERHEAD_TOKENS = 16384


def byte_basis(profile):
    return profile.input_bound_basis == "REQUEST_BYTES_VERIFIED"


def input_token_bound(profile, request_bytes):
    """Reservation input size for one serialized request; never above the ceiling."""
    integer(request_bytes, 1)
    return min(profile.input_tokens, request_bytes + BYTE_BOUND_OVERHEAD_TOKENS)


def call_bound(profile, request_bytes):
    """Request-specific reservation; documented-ceiling profiles are unchanged."""
    if not byte_basis(profile):
        return profile.bound_micro_usd
    from dataclasses import replace as _replace
    from ..core.budget import request_bound
    return request_bound(_replace(profile.fixture_limits(), input_tokens=input_token_bound(profile, request_bytes)))


def ceiling_excess(profile, bound):
    """Additional exposure retained when an outcome lacks validated usage."""
    return max(0, profile.bound_micro_usd - bound) if byte_basis(profile) else 0


@dataclass(frozen=True)
class ResearchConfig:
    run_id: str
    profile: NativeProfile
    board: Board
    max_searches: int = 2
    max_reads: int = 2
    max_model_calls: int = 6
    max_pick_corrections: int = 1
    text_bytes: int = 4096
    max_results: int = 3
    tool_timeout_ms: int = 20000
    opportunity_ms: int = 1200000
    tool_bound_micro_usd: int = 10000
    budget_micro_usd: int = 100000000
    history_bytes: int = 131072
    mode: str = "OFFLINE_FIXTURE"
    experiment_id: str = "miomaci-basketball-research-offline"
    schema: str = "basketball-research-offline/1"
    draft_plan: NativeDraftConfig | None = None
    service_response_bytes: int = 262144
    sources: tuple[tuple[str, str], ...] = ()
    pick_limit: int | None = None
    task: ResearchTask | None = None
    counted_task: object | None = None
    counted_cutoff_ms: int | None = None
    excess_reserve_calls: int = 0
    budget_gate: str = "WORST_CASE"
    # Owner decision G (October 19): the draft may continue past the first tipoff
    # (counted_cutoff_ms, kept as the recorded tipoff) up to this later hard limit.
    # None keeps the original behavior: the tipoff is the hard limit.
    counted_hard_limit_ms: int | None = None
    # Continuation (owner decision October 19): a new counted run may begin after the settled picks
    # of a stopped counted run. prior_results_json is the canonical JSON list of those settled
    # results, copied verbatim from the stopped journal; prior_source_json records where they came
    # from (run ID, config hash, journal SHA-256). Empty keeps every earlier behavior and digest.
    prior_results_json: str = ""
    prior_source_json: str = ""

    def __post_init__(self):
        if (not isinstance(self.run_id, str) or not re.fullmatch(r"research-[a-z0-9-]{1,60}", self.run_id)
                or type(self.profile) is not NativeProfile or type(self.board) is not Board
                or self.profile.provider != self.board.manager or not self.board.legal_ids()):
            raise ValueError("research binding")
        offline = (self.mode, self.experiment_id, self.schema) == (
            "OFFLINE_FIXTURE", "miomaci-basketball-research-offline", "basketball-research-offline/1")
        pilot = (self.mode, self.experiment_id, self.schema) == (
            "LIVE_RESEARCH_PILOT", "miomaci-basketball-research-pilot", "basketball-research-pilot/1")
        counted = (self.mode, self.experiment_id, self.schema) == COUNTED_NAMESPACE
        if not (offline or pilot or counted):
            raise ValueError("explicit offline, research pilot or counted namespace required")
        if self.counted_task is not None or counted:
            from ..providers.counted_task import CountedTask
            if (type(self.counted_task) is not CountedTask or self.task is not None or pilot
                    or self.draft_plan is None or self.pick_limit is not None
                    or self.counted_task.rules.roster_size != self.board.roster_size
                    or self.counted_task.rules.slots != self.board.slots
                    or self.draft_plan.rules_digest != self.counted_task.hash
                    or not self.run_id.startswith("research-incident-quota-counted-")):
                raise ValueError("counted task must bind the complete draft, board and plan rules")
            if self.counted_task.player_order and set(self.counted_task.player_order) != {
                    p.player_id for p in self.board.players}:
                raise ValueError("frozen counted player order must cover the pool")
        if counted:
            from ..providers.research_transport import counted_snapshot
            integer(self.counted_cutoff_ms, 1)
            if self.counted_hard_limit_ms is not None:
                integer(self.counted_hard_limit_ms, self.counted_cutoff_ms)
            if type(self.sources) is not tuple or self.sources != counted_snapshot():
                raise ValueError("reviewed counted source binding required")
        elif self.counted_cutoff_ms is not None or self.counted_hard_limit_ms is not None:
            raise ValueError("counted cutoff belongs only to the counted namespace")
        if type(self.excess_reserve_calls) is not int or not 0 <= self.excess_reserve_calls <= 1000:
            raise ValueError("bounded excess reserve required")
        if self.prior_results_json or self.prior_source_json:
            if self.counted_task is None or not (self.prior_results_json and self.prior_source_json):
                raise ValueError("continuation is counted-only and needs both prior results and source")
            source = json.loads(self.prior_source_json)
            if (type(source) is not dict or set(source) != {"run_id", "config_hash", "journal_sha256", "settled_picks"}
                    or canonical(source) != self.prior_source_json):
                raise ValueError("continuation source shape")
            if source["settled_picks"] != len(prior_results(self)):
                raise ValueError("continuation source count")
        if self.task is not None:
            if (type(self.task) is not ResearchTask or self.task.roster_size != self.board.roster_size
                    or self.task.slots != self.board.slots or self.draft_plan is None
                    or self.draft_plan.rules_digest != self.task.hash):
                raise ValueError("manager task must bind exact board and plan rules")
        if pilot:
            from ..providers.research_transport import code_snapshot
            if type(self.sources) is not tuple or self.sources != code_snapshot() or self.opportunities > 30:
                raise ValueError("reviewed pilot source/size binding required")
        if self.draft_plan is not None:
            if (type(self.draft_plan) is not NativeDraftConfig or self.draft_plan.mode != "OFFLINE_FIXTURE"
                    or self.draft_plan.board != self.board or self.draft_plan.profiles[0] != self.profile):
                raise ValueError("frozen offline draft plan required")
        if self.pick_limit is not None:
            integer(self.pick_limit, 1)
            if self.draft_plan is None or self.pick_limit > self.draft_plan.total_picks:
                raise ValueError("pilot prefix must fit the frozen draft")
        for key in ("max_searches", "max_reads", "max_pick_corrections", "tool_bound_micro_usd"):
            integer(getattr(self, key))
        for key in ("max_model_calls", "text_bytes", "max_results", "tool_timeout_ms", "opportunity_ms",
                    "budget_micro_usd", "history_bytes", "service_response_bytes"):
            integer(getattr(self, key), 1)
        if (self.max_searches > 10 or self.max_reads > 10 or self.max_model_calls > 24
                or self.max_results > 5 or self.text_bytes > 16384 or self.history_bytes > 262144
                or self.max_pick_corrections > 1 or self.tool_timeout_ms > self.opportunity_ms
                or self.service_response_bytes > 1048576):
            raise ValueError("research policy bound")
        if self.max_model_calls < self.max_searches + self.max_reads + 1 + self.max_pick_corrections:
            raise ValueError("protect final choice and correction")
        if self.budget_gate == "WORST_CASE":
            if self.budget_micro_usd < self.worst_case_micro_usd:
                raise ValueError("budget does not cover frozen research policy")
        elif self.budget_gate == "RUNTIME_CEILING_GATED" and self.counted_task is not None:
            # Money stays bounded by the runtime gate (retained + full ceiling of the
            # next call <= budget); a smaller owner budget can only pause the draft.
            profiles = [c.profile for c in self.draft_plan.cases]
            if self.budget_micro_usd < self.max_model_calls * max(p.bound_micro_usd for p in profiles) + (
                    self.max_searches + self.max_reads) * self.tool_bound_micro_usd:
                raise ValueError("budget below one complete opportunity ceiling")
        else:
            raise ValueError("unsupported budget gate")

    @property
    def opportunities(self):
        return self.pick_limit or (self.draft_plan.total_picks if self.draft_plan else 1)

    @property
    def worst_case_micro_usd(self):
        cases = self.draft_plan.cases[:self.opportunities] if self.draft_plan is not None else ()
        profiles = [case.profile for case in cases] or [self.profile]
        # Largest admissible request for each profile is its enforced byte cap.
        total = sum(self.max_model_calls * call_bound(p, p.request_bytes)
                    + (self.max_searches + self.max_reads) * self.tool_bound_micro_usd for p in profiles)
        excess = max(ceiling_excess(p, call_bound(p, p.request_bytes)) for p in profiles)
        # One ceiling-sized headroom for the in-flight call plus the declared
        # number of tolerated outcomes without validated usage.
        return total + (self.excess_reserve_calls + 1) * excess

    @cached_property
    def hash(self):
        value = asdict(self)
        if self.task is None:
            value.pop("task")  # Preserve original no-task journal/config bindings.
        for name, default in (("counted_task", None), ("counted_cutoff_ms", None), ("excess_reserve_calls", 0),
                              ("budget_gate", "WORST_CASE"), ("counted_hard_limit_ms", None),
                              ("prior_results_json", ""), ("prior_source_json", "")):
            if value[name] == default:
                value.pop(name)  # Preserve all earlier configuration digests.
        return digest(value)


def initial():
    return {"phase": "NEW", "last_ms": None, "deadline_ms": None, "counts": {"model": 0, "search": 0, "read": 0},
            "history": [], "urls": [], "pending": None, "tool_action": None, "result": None,
            "choice": None, "pause": None, "corrections": 0, "retained_micro_usd": 0,
            "estimated_model_micro_usd": 0, "invoice_confirmed_micro_usd": 0, "attempts": [],
            "results": [], "trial_deadline_ms": None, "total_counts": {"model": 0, "search": 0, "read": 0},
            "observed_service_credits": "0", "service_usage_missing": 0, "authorization": None, "recoveries": []}


def prior_results(config):
    """Settled picks a continuation run starts from, validated against the frozen draft plan."""
    if not config.prior_results_json:
        return []
    results = json.loads(config.prior_results_json)
    if (type(results) is not list or not results or len(results) >= config.opportunities
            or canonical(results) != config.prior_results_json):
        raise ValueError("prior results must be a canonical non-empty prefix")
    keys = {"case_index", "case_id", "profile_id", "action", "request_hash", "response_hash", "settled_ms"}
    for position, result in enumerate(results):
        if (not isinstance(result, dict) or set(result) != keys or result["case_index"] != position
                or not isinstance(result["action"], dict) or set(result["action"]) != {"player_id", "rationale"}
                or not isinstance(result["action"]["player_id"], str)):
            raise ValueError("prior result shape")
        # Each prior pick must have been legal on the board that existed when it was made: identity
        # and order (board_at), unknown/locked players and roster-slot feasibility (legal_ids).
        if result["action"]["player_id"] not in config.draft_plan.board_at(position, results[:position]).legal_ids():
            raise ValueError("prior pick was not legal at its position")
    board = config.draft_plan.board_at(len(results), results)
    if len({pid for pid, _ in board.ownership}) != len(results):
        raise ValueError("prior picks repeat a player")
    return deepcopy(results)


def counted_limit(config):
    """Latest instant any counted call, permit or renewal may reach (None outside counted runs)."""
    if config.counted_cutoff_ms is None:
        return None
    return config.counted_hard_limit_ms if config.counted_hard_limit_ms is not None else config.counted_cutoff_ms


def _counted_state(state):
    # Renewal history exists only in counted journals; earlier states are unchanged.
    state.setdefault("authorization_history", [])
    return state


def active_case(state, config):
    if config.draft_plan is None:
        return config.profile, config.board
    index = len(state["results"])
    if index >= config.opportunities:
        raise ValueError("draft already complete")
    return config.draft_plan.cases[index].profile, config.draft_plan.board_at(index, state["results"])


def make_intent(state, config, now):
    profile, board = active_case(state, config)
    prefix = "pick:" + str(len(state["results"])) + ":" if config.draft_plan is not None else ""
    if state["phase"] == "READY_MODEL":
        if state["counts"]["model"] >= config.max_model_calls:
            raise ValueError("MODEL_LIMIT")
        allowance = {"searches": config.max_searches - state["counts"]["search"],
                     "reads": config.max_reads - state["counts"]["read"],
                     "model_calls_including_this": config.max_model_calls - state["counts"]["model"]}
        if config.counted_task is not None:
            from ..providers.counted_task import counted_request
            wire = counted_request(profile, board, state["history"], allowance, now, state["deadline_ms"],
                                   config.counted_task)
        else:
            wire = request(profile, board, state["history"], allowance, now, state["deadline_ms"], config.task)
        intent = {"kind": "model", "request": wire, "bound": call_bound(profile, wire["request_bytes"]),
                  "timeout_ms": min(profile.timeout_ms, state["deadline_ms"] - now),
                  "key": prefix + "model:" + str(state["counts"]["model"])}
        if byte_basis(profile):
            intent["input_token_bound"] = input_token_bound(profile, wire["request_bytes"])
        return intent
    if state["phase"] == "READY_TOOL":
        action = state["tool_action"]
        kind = action["action"]
        if state["counts"][kind] >= getattr(config, "max_" + ("searches" if kind == "search" else "reads")):
            raise ValueError("RESEARCH_LIMIT")
        return {"kind": kind, "request": deepcopy(action), "bound": config.tool_bound_micro_usd,
                "timeout_ms": min(config.tool_timeout_ms, state["deadline_ms"] - now),
                "key": prefix + kind + ":" + str(state["counts"][kind])}
    raise ValueError("NOT_READY")


def reduce_event(state, kind, data, config):
    at = integer(data["at_ms"])
    if kind == "START":
        fields = {"at_ms", "authorization"} if config.mode in LIVE_MODES else {"at_ms"}
        if state["phase"] != "NEW" or set(data) != fields:
            raise ValueError("invalid research start")
        if config.mode in LIVE_MODES:
            from ..providers.research_transport import permit_type
            permit_type(config)(**data["authorization"]).check(config, at)
            state["authorization"] = deepcopy(data["authorization"])
        if config.prior_results_json:
            state["results"] = prior_results(config)
        trial_deadline = at + (config.draft_plan.trial_ms if config.draft_plan else config.opportunity_ms)
        if config.counted_cutoff_ms is not None:
            # The fixed limit is part of the configuration digest. Whether picks
            # settled before the recorded first tipoff is reported, not enforced,
            # when the owner set a later hard limit.
            limit = counted_limit(config)
            if at >= limit:
                raise ValueError("counted cutoff passed")
            trial_deadline = min(trial_deadline, limit)
            _counted_state(state)
        state.update(phase="READY_MODEL", deadline_ms=min(at + config.opportunity_ms, trial_deadline),
                     trial_deadline_ms=trial_deadline, last_ms=at)
        return
    if state["phase"] == "NEW":
        raise ValueError("start required")
    if kind == "RECOVER_SAVED":
        if (set(data) != {"at_ms", "expected_state_hash"} or digest(state) != data["expected_state_hash"]
                or state["phase"] != "PAUSED" or state["pause"] != "UNKNOWN_DISPATCH"
                or state["result"] is None or at < state["last_ms"]):
            raise ValueError("saved research recovery unavailable or stale")
        state["recoveries"].append({"at_ms": at, "key": state["pending"]["key"],
                                    "saved_result_hash": digest(state["result"])})
        state.update(phase="RECEIVED", pause=None)
        reduce_event(state, "APPLY", {"at_ms": at}, config)
        if state["phase"] in {"READY_MODEL", "READY_TOOL", "PICK_SAVED"}:
            if at >= state["deadline_ms"]:
                state.update(phase="PAUSED", pause="DEADLINE")
            elif state["authorization"] is not None and at >= state["authorization"]["expires_ms"]:
                if config.counted_task is not None:
                    _counted_state(state)["resume_phase"] = state["phase"]
                state.update(phase="PAUSED", pause="AUTHORIZATION_EXPIRED")
        return
    if kind == "RENEW_AUTHORIZATION":
        # Append-only owner renewal for counted runs: same configuration, budget,
        # request caps and cutoff; later expiry; settled prefix, counters and
        # retained exposure untouched. It never resets an opportunity deadline.
        # Two renewable states: an AUTHORIZATION_EXPIRED pause (restores exactly the
        # phase recorded at that pause), or a pause still holding a bounded retry or
        # recovery candidate whose authority has expired (pause and candidate kept).
        from ..providers.research_transport import CountedPermit
        previous = state["authorization"]
        candidate = any(state.get(k) for k in ("retry_candidate", "incident_candidate", "quota_candidate"))
        expired_pause = state["pause"] == "AUTHORIZATION_EXPIRED" and "resume_phase" in state
        held = (state["pause"] != "AUTHORIZATION_EXPIRED" and candidate
                and previous is not None and at >= previous["expires_ms"])
        if (config.mode != "LIVE_COUNTED_DRAFT" or set(data) != {"at_ms", "expected_state_hash", "authorization"}
                or digest(state) != data["expected_state_hash"] or state["phase"] != "PAUSED"
                or not (expired_pause or held) or state["pending"] is not None
                or at < state["last_ms"]):
            raise ValueError("renewal unavailable or stale")
        renewed = CountedPermit(**data["authorization"])
        renewed.check(config, at)
        if renewed.expires_ms <= previous["expires_ms"] or renewed.not_before_ms < previous["not_before_ms"]:
            raise ValueError("renewal must extend the recorded authority")
        _counted_state(state)["authorization_history"].append({**deepcopy(previous), "replaced_at_ms": at})
        state["authorization"] = deepcopy(data["authorization"])
        if expired_pause:
            state.update(phase=state.pop("resume_phase"), pause=None)
        state["last_ms"] = at
        return
    if kind == "NEXT_PICK":
        if (set(data) != {"at_ms"} or state["phase"] != "PICK_SAVED" or config.draft_plan is None
                or at < state["last_ms"] or at >= state["trial_deadline_ms"]):
            raise ValueError("invalid next pick")
        state.update(phase="READY_MODEL", counts={"model": 0, "search": 0, "read": 0},
                     history=[], urls=[], tool_action=None, choice=None, corrections=0,
                     deadline_ms=min(at + config.opportunity_ms, state["trial_deadline_ms"]), last_ms=at)
        return
    if kind == "PAUSE":
        if set(data) != {"at_ms", "reason"} or state["phase"] in {"COMPLETE", "PAUSED"}:
            raise ValueError("invalid pause")
        if data["reason"] not in {"UNKNOWN_DISPATCH", "DEADLINE", "CLOCK_ROLLBACK", "REQUEST_BOUND",
                                  "AUTHORIZATION_EXPIRED", "BUDGET"}:
            raise ValueError("unknown pause")
        pending = state["pending"]
        if data["reason"] == "UNKNOWN_DISPATCH" and pending and pending["kind"] == "model":
            # The outcome may never be observed: keep the documented ceiling.
            profile, _ = active_case(state, config)
            state["retained_micro_usd"] += ceiling_excess(profile, pending["bound"])
            if "input_token_bound" in pending:
                # Byte-basis only: a later saved-result recovery must not add it again.
                pending["ceiling_retained"] = True
        if data["reason"] == "AUTHORIZATION_EXPIRED" and config.counted_task is not None:
            _counted_state(state)["resume_phase"] = state["phase"]
        state.update(phase="PAUSED", pause=data["reason"], last_ms=max(at, state["last_ms"]))
        return
    if kind == "INTENT":
        if at < state["last_ms"] or at >= state["deadline_ms"]:
            raise ValueError("intent timing")
        expected = make_intent(state, config, at)
        if set(data) != {"at_ms", "intent"} or data["intent"] != expected:
            raise ValueError("intent mismatch")
        needed = expected["bound"]
        if expected["kind"] == "model":
            profile, _ = active_case(state, config)
            needed += ceiling_excess(profile, expected["bound"])
        if state["retained_micro_usd"] + needed > config.budget_micro_usd:
            raise ValueError("budget exceeded")
        state["counts"][expected["kind"]] += 1
        state["total_counts"][expected["kind"]] += 1
        state["retained_micro_usd"] += expected["bound"]
        state.update(phase="INFLIGHT", pending={**expected, "at_ms": at}, result=None, last_ms=at)
        return
    if kind == "RESULT":
        pending = state["pending"]
        if (set(data) != {"at_ms", "key", "elapsed_ms", "result"} or not pending or state["result"] is not None
                or state["phase"] not in {"INFLIGHT", "PAUSED"} or data["key"] != pending["key"]):
            raise ValueError("unbound result")
        integer(data["elapsed_ms"])
        profile, _ = active_case(state, config)
        cap = profile.response_bytes * 6 + 2048 if pending["kind"] == "model" else config.service_response_bytes * 6 + 2048
        if len(canonical(data["result"]).encode()) > cap:
            raise ValueError("result byte limit")
        state.update(result=deepcopy(data), last_ms=max(at, state["last_ms"]))
        if state["phase"] != "PAUSED":
            state["phase"] = "RECEIVED"
        return
    if kind != "APPLY" or set(data) != {"at_ms"} or state["phase"] != "RECEIVED":
        raise ValueError("invalid research transition")
    # A receipt saved in time can settle after restart/expiry. New work cannot.
    pending, saved = state["pending"], state["result"]
    profile, board = active_case(state, config)
    outcome = saved["result"]
    timed_out = (saved["at_ms"] < pending["at_ms"] or saved["at_ms"] >= state["deadline_ms"]
                 or saved["at_ms"] - pending["at_ms"] >= pending["timeout_ms"]
                 or saved["elapsed_ms"] >= pending["timeout_ms"])
    state["attempts"].append({"intent": pending, "saved": deepcopy(saved)})
    service_overrun = False
    if pending["kind"] != "model" and isinstance(outcome, dict) and outcome.get("outcome") == "SERVICE_RESPONSE":
        from decimal import Decimal, ROUND_CEILING
        used = credit_usage(outcome.get("raw"))
        state["attempts"][-1]["observed_service_credits"] = None if used is None else str(used)
        if used is None:
            state["service_usage_missing"] += 1
        else:
            state["observed_service_credits"] = str(Decimal(state["observed_service_credits"]) + used)
            upper_charge = int((used * config.tool_bound_micro_usd).to_integral_value(rounding=ROUND_CEILING))
            state["retained_micro_usd"] += max(0, upper_charge - pending["bound"])
            service_overrun = used > 1
    failure = None
    if pending["kind"] == "model":
        receipt = None
        if (isinstance(outcome, dict) and {"outcome", "raw"} <= set(outcome)
                and not set(outcome) - {"outcome", "raw", "http_status", "body_sha256"}
                and outcome["outcome"] == "RESPONSE"
                and ("http_status" not in outcome or outcome["http_status"] == 200)):
            receipt = inspect_action(profile, outcome["raw"], board.legal_ids(), state["urls"])
            estimate = receipt.get("estimated_charge_micro_usd")
            if estimate is not None:
                state["estimated_model_micro_usd"] += estimate
                held = pending["bound"] + (ceiling_excess(profile, pending["bound"])
                                           if pending.get("ceiling_retained") else 0)
                state["retained_micro_usd"] += max(0, estimate - held)
                usage = receipt.get("usage") or {}
                if "input_token_bound" in pending and usage.get("input_tokens", 0) > pending["input_token_bound"]:
                    # Request-specific reservation disproved. The observed estimate is
                    # already retained above, and every later dispatch is still gated on
                    # retained + full documented ceiling <= budget, so money stays bounded.
                    # Owner decision (October 19): record it as a warning and continue;
                    # the receipt keeps its own disposition.
                    state.setdefault("request_bound_warnings", []).append({
                        "key": pending["key"], "at_ms": at, "input_tokens": usage.get("input_tokens"),
                        "input_token_bound": pending["input_token_bound"],
                        "estimated_charge_micro_usd": estimate, "reserved_micro_usd": pending["bound"]})
            state["attempts"][-1]["native_receipt"] = receipt
        if ("input_token_bound" in pending and not pending.get("ceiling_retained")
                and (receipt is None or receipt.get("estimated_charge_micro_usd") is None)):
            state["retained_micro_usd"] += ceiling_excess(profile, pending["bound"])
        if timed_out:
            failure = "RESPONSE_LATE"
        elif receipt is None:
            failure = "MODEL_TRANSPORT_FAILED"
        elif (config.task is not None and config.task.require_research
              and (receipt["failure"] == "ILLEGAL_PICK"
                   or (receipt["failure"] is None and receipt["action"]["action"] == "pick"))
              and not {"search", "read"} <= {
                  e["action"]["action"] for e in state["history"] if e["kind"] == "external_evidence"}):
            # ILLEGAL_PICK is already a structurally valid, identity/usage-valid
            # pick attempt. The pilot capability gate precedes its correction.
            # Native/timing failures retain their original disposition above.
            failure = "PILOT_DEMONSTRATION_INCOMPLETE"
        elif receipt["failure"]:
            failure = receipt["failure"]
            if failure == "ILLEGAL_PICK" and state["corrections"] < config.max_pick_corrections:
                state["corrections"] += 1
                state["history"].append({"kind": "pick_correction", "failure": failure,
                                          "response_hash": receipt["response_hash"],
                                          "previous_text": receipt["answer_text"].encode()[:2048].decode("utf-8", "ignore")})
                failure = None
                state["phase"] = "READY_MODEL"
        else:
            action = receipt["action"]
            if action["action"] == "pick":
                state.update(phase="COMPLETE", choice={"player_id": action["player_id"], "rationale": action["rationale"]})
                if config.draft_plan is not None:
                    index = len(state["results"])
                    state["results"].append({"case_index": index, "case_id": board.case_id,
                                             "profile_id": profile.profile_id, "action": deepcopy(state["choice"]),
                                             "request_hash": pending["request"]["request_hash"],
                                             "response_hash": receipt["response_hash"], "settled_ms": at})
                    if len(state["results"]) < config.opportunities:
                        state["phase"] = "PICK_SAVED"
            elif state["counts"][action["action"]] >= getattr(config, "max_" + ("searches" if action["action"] == "search" else "reads")):
                failure = "RESEARCH_LIMIT"
            else:
                state.update(phase="READY_TOOL", tool_action=action)
    elif timed_out:
        failure = "RESPONSE_LATE"
    else:
        try:
            service = None
            if isinstance(outcome, dict) and outcome.get("outcome") == "SERVICE_RESPONSE":
                if (not {"outcome", "raw", "retrieved_ms"} <= set(outcome)
                        or set(outcome) - {"outcome", "raw", "retrieved_ms", "http_status", "body_sha256"}
                        or ("http_status" in outcome and outcome["http_status"] != 200)):
                    raise ValueError("service response shape")
                normalized = normalize_tool(pending["request"], outcome["raw"],
                                            retrieved_ms=outcome["retrieved_ms"], text_bytes=config.text_bytes,
                                            max_results=config.max_results, response_bytes=config.service_response_bytes)
                evidence, service = normalized["evidence"], normalized["receipt"]
            else:
                if config.mode != "OFFLINE_FIXTURE":
                    raise ValueError("live research requires native service receipt")
                evidence = tool_result(pending["request"], outcome, text_bytes=config.text_bytes, max_results=config.max_results)
            if not pending["at_ms"] <= evidence["retrieved_ms"] <= saved["at_ms"]:
                raise ValueError("retrieval time mismatch")
        except (ValueError, TypeError, UnicodeError):
            failure = "TOOL_RESULT_REJECTED"
        else:
            entry = {"kind": "external_evidence", "action": pending["request"], "result": evidence}
            if service is not None:
                state["attempts"][-1]["service_receipt"] = service
                entry["content_bounds"] = service["content_bounds"]
            state["history"].append(entry)
            state["urls"] = sorted(set(state["urls"]) | {i["url"] for i in evidence["items"]})
            state["phase"] = "READY_MODEL"
    if len(canonical(state["history"]).encode()) > config.history_bytes:
        failure = "HISTORY_BOUND"
    if service_overrun:
        failure = "SERVICE_USAGE_BOUND_EXCEEDED"
    if state["retained_micro_usd"] > config.budget_micro_usd:
        failure = "USAGE_BOUND_EXCEEDED"
    if failure:
        state.update(phase="PAUSED", pause=failure)
    state.update(pending=None, result=None, last_ms=max(at, state["last_ms"]))


class ResearchOpportunity:
    """Advance one saved boundary at a time, with mode-matched transport."""
    def __init__(self, directory, config, *, clock, fault=None):
        if config.mode in LIVE_MODES:
            from ..providers.research_transport import expected_sources
            if config.sources != expected_sources(config):
                raise ValueError("research source snapshot changed")
        self.config, self.clock, self.fault = config, clock, fault
        self.journal = Journal(directory, config, reduce_event, initial, schema=config.schema)

    def status(self):
        return self.journal.read()

    def recover_saved(self, expected_state_hash):
        if self.config.mode in LIVE_MODES:
            from ..providers.research_transport import expected_sources
            if self.config.sources != expected_sources(self.config):
                raise ValueError("research source snapshot changed")
        if not isinstance(expected_state_hash, str) or not re.fullmatch("[0-9a-f]{64}", expected_state_hash):
            raise ValueError("expected saved state hash required")
        return self.journal.transact(lambda s: [("RECOVER_SAVED", {"at_ms": self.clock.now_ms(),
                                                                "expected_state_hash": expected_state_hash})])

    def _fault(self, name):
        if self.fault:
            self.fault(name)

    def step(self, transport):
        from ..providers.research_transport import LiveResearchTransport
        live = type(transport) is LiveResearchTransport
        if ((live and (self.config.mode not in LIVE_MODES or transport.config != self.config))
                or (not live and (type(transport) is not ScriptedResearchTransport or self.config.mode != "OFFLINE_FIXTURE"))):
            raise ValueError("research transport/configuration mode differs")
        if live:
            from ..providers.research_transport import expected_sources
            if self.config.sources != expected_sources(self.config):
                raise ValueError("research source snapshot changed")
        now = self.clock.now_ms()
        claimed = []
        def decide(state):
            if live and state["authorization"] is not None and state["authorization"] != asdict(transport.permit):
                raise ValueError("recorded research authorization differs")
            if state["phase"] in {"COMPLETE", "PAUSED"}:
                return []
            if state["phase"] == "NEW":
                if live:
                    transport.authorize(now)
                return [("START", {"at_ms": now, "authorization": asdict(transport.permit)} if live else {"at_ms": now})]
            if state["phase"] == "PICK_SAVED":
                reason = "CLOCK_ROLLBACK" if now < state["last_ms"] else "DEADLINE" if now >= state["trial_deadline_ms"] else None
                return [("PAUSE", {"at_ms": now, "reason": reason})] if reason else [("NEXT_PICK", {"at_ms": now})]
            if state["phase"] == "RECEIVED":
                if now < state["last_ms"]:
                    return [("PAUSE", {"at_ms": now, "reason": "CLOCK_ROLLBACK"})]
                return [("APPLY", {"at_ms": now})]
            if state["phase"] == "INFLIGHT":
                return [("PAUSE", {"at_ms": now, "reason": "UNKNOWN_DISPATCH"})]
            reason = "CLOCK_ROLLBACK" if now < state["last_ms"] else "DEADLINE" if now >= state["deadline_ms"] else None
            if reason:
                return [("PAUSE", {"at_ms": now, "reason": reason})]
            if live:
                if now >= transport.permit.expires_ms:
                    # Validate every other binding before recording this reason;
                    # a changed/disabled permit is a rejection, not an amendment.
                    transport.authorize(transport.permit.expires_ms - 1)
                    return [("PAUSE", {"at_ms": now, "reason": "AUTHORIZATION_EXPIRED"})]
                transport.authorize(now)
            try:
                intent = make_intent(state, self.config, now)
            except ValueError:
                return [("PAUSE", {"at_ms": now, "reason": "REQUEST_BOUND"})]
            if live and self.config.counted_task is not None and now + intent["timeout_ms"] > transport.permit.expires_ms:
                # Counted runs never start a call the permit would truncate: pause at a
                # renewable boundary instead of turning expiry into a transport failure.
                return [("PAUSE", {"at_ms": now, "reason": "AUTHORIZATION_EXPIRED"})]
            needed = intent["bound"]
            if intent["kind"] == "model":
                needed += ceiling_excess(active_case(state, self.config)[0], intent["bound"])
            if state["retained_micro_usd"] + needed > self.config.budget_micro_usd:
                return [("PAUSE", {"at_ms": now, "reason": "BUDGET"})]
            claimed.append(intent)
            return [("INTENT", {"at_ms": now, "intent": intent})]
        state = self.journal.transact(decide)
        if not claimed:
            return state
        intent = claimed[0]
        self._fault("after_intent")
        started = self.clock.monotonic_ms()
        suppressed, reason = False, None
        @contextmanager
        def launch_guard():
            nonlocal suppressed, reason
            with self.journal.guard() as saved:
                current = self.clock.now_ms()
                suppressed = saved["phase"] != "INFLIGHT" or saved["pending"]["key"] != intent["key"]
                if not suppressed:
                    reason = ("CLOCK_ROLLBACK" if current < saved["last_ms"] else
                              "DEADLINE" if current >= saved["deadline_ms"] else None)
                yield not suppressed and reason is None
        # Gate only credential lookup/worker creation, never the network wait.
        profile, _ = active_case(state, self.config)
        result = transport.send(intent["key"], intent["kind"], deepcopy(intent["request"]), intent["timeout_ms"],
                                launch_guard=launch_guard, provider=profile.provider,
                                discovered_urls=tuple(state["urls"]))
        if suppressed:
            return self.status()
        if reason:
            self.journal.transact(lambda s: [] if s["phase"] == "PAUSED" else
                                  [("PAUSE", {"at_ms": self.clock.now_ms(), "reason": reason})])
            return self.status()
        self._fault("after_send")
        at = self.clock.now_ms()
        elapsed = self.clock.monotonic_ms() - started
        self.journal.transact(lambda s: [("RESULT", {"at_ms": at, "key": intent["key"],
                                                    "elapsed_ms": elapsed, "result": result})])
        self._fault("after_result")
        return self.status()


class ScriptedResearchTransport:
    """Fixture evidence only. Explicitly not accepted by live draft entry."""
    def __init__(self, results):
        self.results, self.calls = deepcopy(results), []

    def send(self, key, kind, wire, timeout_ms, *, launch_guard=None, **unused):
        from contextlib import nullcontext
        with launch_guard() if launch_guard else nullcontext(True) as allowed:
            if not allowed:
                return {"outcome": "DISPATCH_SUPPRESSED"}
            self.calls.append({"key": key, "kind": kind, "request": deepcopy(wire), "timeout_ms": timeout_ms})
            return deepcopy(self.results[key])
