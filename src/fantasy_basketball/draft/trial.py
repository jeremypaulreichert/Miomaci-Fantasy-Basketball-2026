"""Shared durable native lifecycle for diagnostics and offline snake drafts.

Uses the reusable journal and conservative budget primitives. A diagnostic case
is an isolated decision; a NativeDraftConfig derives evolving ownership instead.
Neither configuration authorizes competitive execution or qualifies a provider.
"""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from functools import cached_property
import re
import time

from ..core.budget import record_estimate, reserve, totals
from ..core.config import canonical, digest, identifier, integer
from ..core.journal import Journal
from ..providers.native import NativeProfile, inspect, prepare
from ..providers.transport import HTTPTransport, LiveAuthorization
from .board import Board
from .native_plan import NativeDraftConfig


@dataclass(frozen=True)
class TrialCase:
    profile: NativeProfile
    board: Board

    def __post_init__(self):
        if type(self.profile) is not NativeProfile or type(self.board) is not Board:
            raise ValueError("immutable case required")
        if self.profile.provider != self.board.manager or not self.board.legal_ids():
            raise ValueError("invalid case manager or legality")


@dataclass(frozen=True)
class TrialConfig:
    run_id: str
    cases: tuple[TrialCase, ...]
    budget_micro_usd: int
    trial_ms: int
    source_plan_hash: str
    mode: str = "OFFLINE_FIXTURE"
    experiment_id: str = "miomaci-basketball-native-diagnostics"
    schema: str = "basketball-native-trial/1"

    def __post_init__(self):
        identifier(self.run_id)
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", self.run_id)
                or self.run_id.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}):
            raise ValueError("safe run ID required")
        if self.experiment_id != "miomaci-basketball-native-diagnostics" or self.schema != "basketball-native-trial/1":
            raise ValueError("native diagnostic namespace required")
        if self.mode not in {"OFFLINE_FIXTURE", "LIVE_DIAGNOSTIC"}:
            raise ValueError("explicit execution mode required")
        if type(self.cases) is not tuple or not 1 <= len(self.cases) <= 100 or any(type(c) is not TrialCase for c in self.cases):
            raise ValueError("bounded immutable cases required")
        if len({(c.profile.profile_id, c.board.case_id) for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate diagnostic case")
        integer(self.budget_micro_usd)
        integer(self.trial_ms, 1)
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_plan_hash):
            raise ValueError("source plan hash required")

    @cached_property
    def hash(self):
        return digest(asdict(self))

    @property
    def max_requests(self):
        return sum(c.profile.max_attempts for c in self.cases)


def initial():
    return {"started_ms": None, "deadline_ms": None, "last_ms": None,
            "op": None, "results": [], "accounts": {}, "pause": None, "history": [], "authorization": None,
            "recoveries": []}


@dataclass(frozen=True)
class RecoveryDecision:
    decision_id: str
    config_hash: str
    expected_state_hash: str
    action: str
    owner_record: str

    def __post_init__(self):
        identifier(self.decision_id)
        identifier(self.owner_record)  # A non-secret record ID, not approval verification.
        for value in (self.config_hash, self.expected_state_hash):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("exact recovery binding required")
        if self.action not in {"KEEP_PAUSED", "SETTLE_SAVED", "AUTHORIZE_CORRECTION"}:
            raise ValueError("unsupported recovery action")


def recovery_token(state, config):
    return digest({"config_hash": config.hash, "state": state})


def attempt_key(index, attempt):
    return f"case:{index}:attempt:{attempt}"


def board_at(config, index, results):
    return (config.board_at(index, results) if type(config) is NativeDraftConfig
            else config.cases[index].board)


def protected(config, index, used):
    profile = config.cases[index].profile
    return ((profile.max_attempts - used) * profile.bound_micro_usd
            + sum(c.profile.max_attempts * c.profile.bound_micro_usd for c in config.cases[index + 1:]))


def pause(state, reason):
    if state["pause"] is None:
        state["pause"] = {"reason": reason, "case_index": len(state["results"])}


def time_failure(state, now):
    if now < state["last_ms"]:
        return "CLOCK_ROLLBACK"
    if now >= state["deadline_ms"] or (state["op"] and now >= state["op"]["deadline_ms"]):
        return "DEADLINE"
    return None


def response_time_failure(state, op, attempt):
    """Receiving a response has a deadline; local replay of it does not."""
    received = attempt["received_ms"]
    if received < attempt["dispatched_ms"]:
        return "CLOCK_ROLLBACK"
    if (received >= min(state["deadline_ms"], op["deadline_ms"])
            or received - attempt["dispatched_ms"] >= attempt["timeout_ms"]
            or attempt["elapsed_ms"] >= attempt["timeout_ms"]):
        return "DEADLINE"
    return None


def recovery_report(state, config, now):
    """Explain evidence and frozen-policy options without changing state."""
    integer(now)
    op = state["op"]
    attempt = op["attempts"][-1] if op and op["attempts"] else None
    reason = state["pause"]["reason"] if state["pause"] else None
    explanations = {
        "UNKNOWN_DISPATCH": "A request was recorded as started, but safe completion was not established when recovery paused it.",
        "DEADLINE": "The original request, case or trial deadline was reached.",
        "CLOCK_ROLLBACK": "The clock moved behind previously saved evidence.",
        "BUDGET": "The spending ceiling cannot cover this request and the protected remaining work.",
        "NO_LEGAL_PICK": "The current board has no legal roster completion; no replacement player can be selected.",
        "AUTHORIZATION_EXPIRED": "The recorded authority for new requests has expired.",
        "ILLEGAL_PICK": "The AI selected a player outside the legal choices and its permitted corrections ended.",
        "MALFORMED_ACTION": "The AI did not return an accepted action within its permitted corrections.",
        "AI_HOLD": "The AI declined to select a player.",
        "MODEL_REFUSAL": "The model refused the request; the organizer cannot select a substitute.",
        "INCOMPLETE_RESPONSE": "The native response did not finish normally; output allowance may have been exhausted.",
        "TRANSPORT_ERROR": "The request failed or authority expired at startup; its outcome may be uncertain. See saved evidence before any further action.",
        "WORKER_TIMEOUT": "The request worker exceeded its allowed time; stopping it does not prove remote cancellation.",
        "HTTP_ERROR": "The provider returned an unsuccessful HTTP response; no replacement selection is allowed.",
        "IDENTITY_MISMATCH": "The response did not establish the configured model identity.",
        "MISSING_OR_INVALID_USAGE": "The response did not supply usable native billing evidence; reserved exposure remains.",
        "USAGE_BOUND_EXCEEDED": "Reported usage exceeds the configured bound; the larger exposure is retained.",
    }
    actions, blockers = (["KEEP_PAUSED"] if reason else []), []
    if reason:
        if reason != "UNKNOWN_DISPATCH":
            blockers.append("This pause has no safe recovery action under the frozen policy.")
        elif not attempt or attempt["state"] != "RESPONDED":
            blockers.append("No saved response resolves the unknown request. Do not resend it.")
        elif not attempt["receipt"]:
            blockers.append("The saved transport outcome contains no validated AI answer.")
        elif response_time_failure(state, op, attempt):
            blockers.append("The saved response did not arrive within the original timing bounds.")
        elif now < state["last_ms"]:
            blockers.append("The current clock is behind the saved evidence.")
        elif attempt["receipt"]["failure"] is None:
            actions.append("SETTLE_SAVED")
        elif attempt["receipt"]["failure"] in {"MALFORMED_ACTION", "ILLEGAL_PICK"}:
            profile = config.cases[op["index"]].profile
            if len(op["attempts"]) >= profile.max_attempts:
                blockers.append("The original attempt allowance is exhausted.")
            elif time_failure(state, now):
                blockers.append("A new correction would exceed the original deadline.")
            elif config.mode == "LIVE_DIAGNOSTIC" and now >= state["authorization"]["expires_ms"]:
                blockers.append("The original authority for new requests has expired.")
            elif totals(state["accounts"])["committed_micro_usd"] + protected(config, op["index"], len(op["attempts"])) > config.budget_micro_usd:
                blockers.append("A correction and protected future work exceed the spending ceiling.")
            else:
                actions.append("AUTHORIZE_CORRECTION")
        else:
            blockers.append("This saved AI failure is not correctable under the original policy.")
    return {"state_hash": recovery_token(state, config), "reason": reason,
            "explanation": explanations.get(reason, "The provider or response failed validation; the draft is paused." if reason else "No durable pause requires adjudication."),
            "settled_cases": len(state["results"]), "attempt_key": attempt["key"] if attempt else None,
            "saved_response": bool(attempt and attempt["response"] is not None),
            "saved_response_hash": digest(attempt["response"]) if attempt and attempt["response"] is not None else None,
            "available_actions": actions, "blockers": blockers,
            "accounting": totals(state["accounts"]), "decision_count": len(state["recoveries"]),
            "decisions": state["recoveries"],
            "recovery_sends_requests": False, "resume_requires_original_limits_and_authority": True}


def reduce_event(state, kind, data, config):
    if kind == "START":
        integer(data["at_ms"])
        if state["started_ms"] is not None:
            raise ValueError("trial already started")
        if config.mode == "LIVE_DIAGNOSTIC":
            LiveAuthorization(**data["authorization"]).check(config, data["at_ms"])
        elif data["authorization"] is not None:
            raise ValueError("offline trial cannot claim live authorization")
        state["authorization"] = data["authorization"]
        state.update(started_ms=data["at_ms"], last_ms=data["at_ms"], deadline_ms=data["at_ms"] + config.trial_ms)
        return
    if state["started_ms"] is None:
        raise ValueError("trial not started")
    if kind == "RECOVER":
        if set(data) != {"at_ms", "decision"}:
            raise ValueError("exact recovery event required")
        decision = RecoveryDecision(**data["decision"])
        at = integer(data["at_ms"])
        if (decision.config_hash != config.hash or decision.expected_state_hash != recovery_token(state, config)
                or any(r["decision"]["decision_id"] == decision.decision_id for r in state["recoveries"])):
            raise ValueError("recovery binding is stale or already used")
        if decision.action not in recovery_report(state, config, at)["available_actions"]:
            raise ValueError("recovery action unavailable under original policy")
        prior_pause = dict(state["pause"])
        if decision.action != "KEEP_PAUSED":
            state["pause"] = None
            # Reuse ordinary validation/settlement. The recovery event and its
            # effect commit together; no transport or additional reservation.
            reduce_event(state, "RESOLVE", {"at_ms": at}, config)
        state["recoveries"].append({"decision": asdict(decision), "at_ms": at, "prior_pause": prior_pause})
        # Even KEEP_PAUSED is durable evidence of observed time. A later clock
        # rollback must not revive deadlines or authority already seen expired.
        state["last_ms"] = max(state["last_ms"], at)
        return
    if kind == "PAUSE":
        if data["reason"] not in {"CLOCK_ROLLBACK", "DEADLINE", "BUDGET", "REQUEST_BOUND", "UNKNOWN_DISPATCH", "AUTHORIZATION_EXPIRED", "NO_LEGAL_PICK"}:
            raise ValueError("unrecognized pause")
        pause(state, data["reason"])
        return
    if state["pause"] and kind != "RESPONSE":
        raise ValueError("trial paused")
    at = integer(data["at_ms"])
    if kind not in {"RESPONSE", "RESOLVE"} and time_failure(state, at):
        raise ValueError("event outside original deadline")
    if kind == "OPEN":
        if state["op"] or len(state["results"]) >= len(config.cases):
            raise ValueError("no new case available")
        index = len(state["results"])
        case = config.cases[index]
        state["op"] = {"index": index, "opened_ms": at,
                       "deadline_ms": min(state["deadline_ms"], at + case.profile.opportunity_ms),
                       "attempts": [], "correction": None}
    else:
        op = state["op"]
        if op is None:
            raise ValueError("no open case")
        case = config.cases[op["index"]]
        profile = case.profile
        if kind == "RESERVE":
            if len(op["attempts"]) >= profile.max_attempts or (op["attempts"] and op["attempts"][-1]["state"] != "CORRECTING"):
                raise ValueError("attempt unavailable")
            expected = prepare(profile, board_at(config, op["index"], state["results"]),
                               now_ms=at, deadline_ms=op["deadline_ms"], correction=op["correction"])
            if data["native_request"] != expected:
                raise ValueError("native request differs from frozen context")
            key = attempt_key(op["index"], len(op["attempts"]))
            reserve(state["accounts"], key, profile.fixture_limits(), config,
                    protected(config, op["index"], len(op["attempts"]) + 1))
            op["attempts"].append({"key": key, "state": "RESERVED", "native_request": expected,
                                   "response": None, "receipt": None})
        elif kind == "DISPATCH":
            attempt = op["attempts"][-1]
            if attempt["state"] != "RESERVED":
                raise ValueError("duplicate dispatch")
            timeout = min(profile.timeout_ms, op["deadline_ms"] - at, state["deadline_ms"] - at)
            if data["timeout_ms"] != timeout:
                raise ValueError("timeout differs from frozen bounds")
            attempt.update(state="DISPATCHED", dispatched_ms=at, timeout_ms=timeout)
        elif kind == "RESPONSE":
            attempt = op["attempts"][-1]
            if attempt["state"] != "DISPATCHED":
                raise ValueError("response without unique dispatch")
            elapsed = integer(data["elapsed_ms"])
            if data.get("transport_elapsed_ms") is not None:
                if integer(data["transport_elapsed_ms"]) > elapsed:
                    raise ValueError("transport duration exceeds attempt duration")
            response = data["response"]
            if len(canonical(response).encode()) > profile.response_bytes * 6 + 2048:
                raise ValueError("unbounded transport response")
            receipt = inspect(profile, response["raw"], board_at(config, op["index"], state["results"]).legal_ids()) if response.get("outcome") == "RESPONSE" else None
            attempt.update(state="RESPONDED", response=response, receipt=receipt,
                           received_ms=at, elapsed_ms=elapsed)
            if receipt and receipt["estimated_charge_micro_usd"] is not None:
                record_estimate(state["accounts"], attempt["key"], receipt["estimated_charge_micro_usd"])
            failure = time_failure(state, at) or response_time_failure(state, op, attempt)
            if failure:
                pause(state, failure)
        elif kind == "RESOLVE":
            attempt = op["attempts"][-1]
            if attempt["state"] != "RESPONDED":
                raise ValueError("response must precede resolution")
            failure = "CLOCK_ROLLBACK" if at < state["last_ms"] else response_time_failure(state, op, attempt)
            if failure:
                pause(state, failure)
            elif attempt["receipt"] is None:
                pause(state, attempt["response"]["outcome"])
            else:
                receipt = attempt["receipt"]
                failure = receipt["failure"]
                if failure is None:
                    state["results"].append({"case_index": op["index"], "case_id": case.board.case_id,
                                             "profile_id": profile.profile_id, "action": receipt["action"],
                                             "request_hash": attempt["native_request"]["request_hash"],
                                             "response_hash": receipt["response_hash"],
                                             "observed_model": receipt["observed_model"]})
                    state["history"].append(op)
                    state["op"] = None
                elif failure in {"MALFORMED_ACTION", "ILLEGAL_PICK"} and len(op["attempts"]) < profile.max_attempts:
                    if time_failure(state, at):
                        pause(state, time_failure(state, at))
                    else:
                        attempt["state"] = "CORRECTING"
                        op["correction"] = {"failure": failure, "response_hash": receipt["response_hash"],
                                            "previous_text": receipt["answer_text"]}
                else:
                    pause(state, failure)
        else:
            raise ValueError("unknown native trial event")
    state["last_ms"] = max(state["last_ms"], at)


class Clock:
    def now_ms(self):
        return time.time_ns() // 1000000

    def monotonic_ms(self):
        return time.monotonic_ns() // 1000000


class NativeTrial:
    def __init__(self, directory, config, *, clock=None, fault=None):
        if type(config) not in {TrialConfig, NativeDraftConfig}:
            raise ValueError("native trial configuration required")
        self.config, self.clock, self.fault = config, clock or Clock(), fault
        schema = "native-draft/1" if type(config) is NativeDraftConfig else "native-trial/1"
        self.journal = Journal(directory, config, reduce_event, initial, schema=schema, fault=fault)

    def _after(self, kind):
        if self.fault:
            self.fault("after:" + kind)

    def status(self):
        state = self.journal.read()
        phase = ("PAUSED" if state["pause"] else "COMPLETE" if len(state["results"]) == len(self.config.cases)
                 else "NOT_STARTED" if state["started_ms"] is None else "RUNNING")
        attempts = [a for op in [*state["history"], *([state["op"]] if state["op"] else [])] for a in op["attempts"]]
        return {"schema": "basketball-native-trial-status/1", "config_hash": self.config.hash,
                "run_id": self.config.run_id, "mode": self.config.mode,
                "simulated": self.config.mode == "OFFLINE_FIXTURE", "competitive_authorization": False,
                "phase": phase, "pause": state["pause"], "results": state["results"],
                "authorization": state["authorization"],
                "deadline_ms": state["deadline_ms"], "max_requests": self.config.max_requests,
                "reserved_requests": len(attempts), "accounting": totals(state["accounts"]),
                "attempts": [{"key": a["key"], "state": a["state"],
                              "request_hash": a["native_request"]["request_hash"],
                              "profile_hash": a["native_request"]["profile_hash"],
                              "receipt": a["receipt"],
                              "transport_outcome": a["response"]["outcome"] if a["response"] else None} for a in attempts]}

    def explain_pause(self):
        return recovery_report(self.journal.read(), self.config, self.clock.now_ms())

    def timing_report(self):
        from .timing import report
        state, events = self.journal.read_with_events()
        return report(self.config, state, events)

    def export_roster(self):
        """Project a complete offline native draft into the existing receipt.

        Synthetic model labels keep the Data consumer's fixture contract honest.
        Original native identities and usage remain in the durable trial receipts.
        """
        config = self.config
        if type(config) is not NativeDraftConfig or config.mode != "OFFLINE_FIXTURE":
            raise ValueError("only an offline native draft exports a simulated roster")
        state = self.journal.read()
        if state["pause"] or state["op"] or len(state["results"]) != config.total_picks:
            raise ValueError("complete settled native draft required")
        picks = []
        for index, result in enumerate(state["results"]):
            case = config.cases[index]
            op = state["history"][index]
            if (result["action"]["player_id"] not in config.board_at(index, state["results"]).legal_ids()
                    or result["observed_model"] not in case.profile.accepted_models):
                raise ValueError("native draft settlement differs")
            picks.append({"index": index, "manager": case.profile.provider,
                          "model": case.profile.fixture_limits().model,
                          "player_id": result["action"]["player_id"],
                          "rationale": result["action"]["rationale"], "origin": "SIMULATED_AI",
                          "attempt": len(op["attempts"]) - 1,
                          "request_hash": result["request_hash"], "response_hash": result["response_hash"]})
        rosters = [{"manager_id": p.provider, "model_id": p.fixture_limits().model,
                    "player_ids": [row["player_id"] for row in picks if row["manager"] == p.provider]}
                   for p in config.profiles]
        if (len({row["player_id"] for row in picks}) != config.total_picks
                or any(len(r["player_ids"]) != config.board.roster_size for r in rosters)):
            raise ValueError("duplicate or incomplete native roster")
        receipt = {"schema_version": "miomaci.basketball.settled-roster/1",
                   "experiment_id": config.experiment_id, "run_id": config.run_id,
                   "config_hash": config.hash, "draft_rules_digest": config.rules_digest,
                   "execution_mode": config.mode, "simulated": True, "provisional": True,
                   "competitive_authorization": False, "draft_status": "COMPLETE",
                   "roster_size": config.board.roster_size, "total_picks": config.total_picks,
                   "rosters": rosters, "picks": picks}
        return {**receipt, "receipt_hash": digest(receipt)}

    def recover(self, decision):
        """Record one explicit decision; never call a provider or widen limits."""
        if type(decision) is not RecoveryDecision or decision.config_hash != self.config.hash:
            raise ValueError("matching recovery decision required")
        committed = []
        def decide(state):
            previous = next((r for r in state["recoveries"] if r["decision"]["decision_id"] == decision.decision_id), None)
            if previous:
                if previous["decision"] != asdict(decision):
                    raise ValueError("recovery decision ID reused with different content")
                return []
            # Reducer validates fresh evidence and time under the writer lock.
            committed.append(True)
            return [("RECOVER", {"at_ms": self.clock.now_ms(), "decision": asdict(decision)})]
        self.journal.transact(decide)
        if committed:
            self._after("RECOVER")
        return self.status()

    def resume(self, transport):
        if getattr(transport, "mode", None) != self.config.mode:
            raise ValueError("transport execution mode differs")
        prior_state = self.journal.read()
        recovery_generation = len(prior_state["recoveries"])
        if self.config.mode == "LIVE_DIAGNOSTIC":
            if type(transport) is not HTTPTransport:
                raise ValueError("bounded HTTP transport required for live mode")
            prior = prior_state["authorization"]
            # Existing authority still authenticates saved evidence. Its expiry
            # gates new work below, not local settlement of a timely response.
            transport.authorize(self.config, prior_state["started_ms"] if prior is not None else self.clock.now_ms())
            if prior is not None and prior != asdict(transport.authorization):
                raise ValueError("recorded authorization changed")
        while True:
            dispatch = []
            event_kinds = []
            def decide(state):
                now = self.clock.now_ms()
                events = []
                # START may have been committed by another controller since
                # the preflight read. Bind authority under the writer lock too.
                if (self.config.mode == "LIVE_DIAGNOSTIC" and state["authorization"] is not None
                        and state["authorization"] != asdict(transport.authorization)):
                    raise ValueError("recorded authorization changed")
                # A concurrent adjudication ends this controller's invocation.
                # Recovery alone must not make an old loop launch fresh work.
                if len(state["recoveries"]) != recovery_generation:
                    return events
                if state["pause"] or len(state["results"]) == len(self.config.cases):
                    return events
                if state["started_ms"] is None:
                    authorization = asdict(transport.authorization) if self.config.mode == "LIVE_DIAGNOSTIC" else None
                    events = [("START", {"at_ms": now, "authorization": authorization})]
                elif (state["op"] and state["op"]["attempts"]
                      and state["op"]["attempts"][-1]["state"] == "RESPONDED"):
                    # Settle saved evidence before imposing deadlines on new work.
                    events = [("RESOLVE", {"at_ms": now})]
                elif time_failure(state, now):
                    events = [("PAUSE", {"reason": time_failure(state, now)})]
                elif (self.config.mode == "LIVE_DIAGNOSTIC"
                      and now >= transport.authorization.expires_ms):
                    events = [("PAUSE", {"reason": "AUTHORIZATION_EXPIRED"})]
                elif state["op"] is None:
                    events = [("OPEN", {"at_ms": now})]
                else:
                    op = state["op"]
                    case = self.config.cases[op["index"]]
                    attempt = op["attempts"][-1] if op["attempts"] else None
                    if attempt is None or attempt["state"] == "CORRECTING":
                        required = case.profile.bound_micro_usd + protected(self.config, op["index"], len(op["attempts"]) + 1)
                        if totals(state["accounts"])["committed_micro_usd"] + required > self.config.budget_micro_usd:
                            events = [("PAUSE", {"reason": "BUDGET"})]
                        else:
                            try:
                                board = board_at(self.config, op["index"], state["results"])
                                if not board.legal_ids():
                                    events = [("PAUSE", {"reason": "NO_LEGAL_PICK"})]
                                else:
                                    native = prepare(case.profile, board, now_ms=now, deadline_ms=op["deadline_ms"], correction=op["correction"])
                                    events = [("RESERVE", {"at_ms": now, "native_request": native})]
                            except ValueError:
                                events = [("PAUSE", {"reason": "REQUEST_BOUND"})]
                    elif attempt["state"] == "RESERVED":
                        timeout = min(case.profile.timeout_ms, op["deadline_ms"] - now, state["deadline_ms"] - now)
                        dispatch.append((attempt["key"], attempt["native_request"], timeout, self.clock.monotonic_ms(), case.profile))
                        events = [("DISPATCH", {"at_ms": now, "timeout_ms": timeout})]
                    elif attempt["state"] == "DISPATCHED":
                        events = [("PAUSE", {"reason": "UNKNOWN_DISPATCH"})]
                event_kinds.extend(kind for kind, _ in events)
                return events
            self.journal.transact(decide)
            for kind in event_kinds:
                self._after(kind)
            if not event_kinds:
                return self.status()
            if dispatch:
                key, native, timeout, started, profile = dispatch[0]
                # A concurrent recovery may have stopped this still-avoidable
                # dispatch after intent committed. Keep its reservation/intent.
                if self.journal.read()["pause"]:
                    return self.status()
                elapsed = max(0, self.clock.monotonic_ms() - started)
                transport_elapsed = None
                if elapsed >= timeout:
                    outcome = {"outcome": "WORKER_TIMEOUT"}
                else:
                    try:
                        send_options = {}
                        if self.config.mode == "LIVE_DIAGNOSTIC":
                            @contextmanager
                            def launch_guard():
                                with self.journal.guard() as state:
                                    if state["pause"]:
                                        yield False
                                    else:
                                        op = state["op"]
                                        attempt = op["attempts"][-1] if op and op["attempts"] else None
                                        if (attempt is None or attempt["key"] != key
                                                or attempt["state"] != "DISPATCHED"
                                                or attempt["native_request"] != native
                                                or state["authorization"] != asdict(transport.authorization)):
                                            raise ValueError("dispatch intent changed before startup")
                                        yield True
                            send_options["launch_guard"] = launch_guard
                        transport_started = self.clock.monotonic_ms()
                        try:
                            outcome = transport.send(key, native, timeout - elapsed, **send_options)
                        finally:
                            transport_elapsed = max(0, self.clock.monotonic_ms() - transport_started)
                        if outcome == {"outcome": "DISPATCH_SUPPRESSED"}:
                            return self.status()
                        allowed = {"RESPONSE", "HTTP_ERROR", "RESPONSE_TOO_LARGE", "UNSUPPORTED_ENCODING", "MALFORMED_JSON",
                                   "TRANSPORT_ERROR", "WORKER_ERROR", "WORKER_TIMEOUT", "INVALID_REQUEST", "CREDENTIAL_ECHO"}
                        if (not isinstance(outcome, dict) or outcome.get("outcome") not in allowed
                                or (outcome["outcome"] == "RESPONSE" and "raw" not in outcome)
                                or len(canonical(outcome).encode()) > profile.response_bytes * 6 + 2048):
                            outcome = {"outcome": "WORKER_ERROR"}
                    except Exception:
                        outcome = {"outcome": "TRANSPORT_ERROR"}
                data = {"at_ms": self.clock.now_ms(), "elapsed_ms": max(0, self.clock.monotonic_ms() - started), "response": outcome}
                if transport_elapsed is not None:
                    data["transport_elapsed_ms"] = transport_elapsed
                self.journal.transact(lambda state: [("RESPONSE", data)])
                self._after("RESPONSE")
