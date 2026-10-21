"""Small event-sourced draft seam. Only a recorded fixture response selects a player."""
import time

from ..core.budget import record_estimate, request_bound, reserve, totals
from ..core.config import canonical, digest, identifier, integer
from ..core.journal import Journal
from ..providers.fixtures import FixtureTransport, inspect_response, request


def initial_state():
    return {"opportunities": [], "accounts": {}, "pause": None, "picks": []}


def attempt_key(index, attempt):
    return f"pick:{index}:attempt:{attempt}"


def protected_work(config, index, attempts_after_reservation):
    current = config.at_pick(index)
    remaining = (current.max_attempts - attempts_after_reservation) * request_bound(current)
    return remaining + sum(request_bound(config.at_pick(i)) * config.at_pick(i).max_attempts
                           for i in range(index + 1, config.total_picks))


def pause(state, op, reason):
    state["pause"] = {"reason": reason, "index": op["index"]}
    op["state"] = "PAUSED"


def reduce_event(state, kind, data, config):
    if kind == "OPEN":
        index = len(state["opportunities"])
        if state["pause"] or (index and state["opportunities"][-1]["state"] != "SETTLED"):
            raise ValueError("previous opportunity unresolved")
        config.at_pick(index)
        legal = data["legal_ids"]
        if type(legal) is not list or not legal or len(set(legal)) != len(legal):
            raise ValueError("unique nonempty legal choices required")
        for player in legal:
            identifier(player)
        if set(legal) & {p["player_id"] for p in state["picks"]}:
            raise ValueError("owned player in legal packet")
        integer(data["opened_ms"])
        state["opportunities"].append({
            "index": index, "legal_ids": legal, "packet_hash": digest({"legal_ids": legal, "index": index, "config": config.hash}),
            "opened_ms": data["opened_ms"], "last_wall_ms": data["opened_ms"], "deadline_ms": data["opened_ms"] + config.opportunity_ms,
            "attempts": [], "correction": None, "state": "OPEN", "action": None,
        })
        return
    if not state["opportunities"]:
        raise ValueError("no opportunity")
    op = state["opportunities"][-1]
    limits = config.at_pick(op["index"])
    if kind != "RESPONSE" and (state["pause"] or op["state"] == "SETTLED"):
        raise ValueError("terminal opportunity")
    if kind == "PAUSE":
        if data["reason"] not in {"UNKNOWN_DISPATCH", "DEADLINE", "CLOCK_ROLLBACK", "BUDGET", "INPUT_BOUND", "RESPONSE_TOO_LARGE", "MALFORMED_ENVELOPE"}:
            raise ValueError("unknown pause class")
        pause(state, op, data["reason"])
    elif kind == "ATTEMPT":
        if op["state"] not in {"OPEN", "CORRECTING"} or len(op["attempts"]) >= limits.max_attempts:
            raise ValueError("attempt policy exhausted")
        expected = request(config, op)
        if expected != data["wire"]:
            raise ValueError("request/config binding mismatch")
        key = attempt_key(op["index"], len(op["attempts"]))
        reserve(state["accounts"], key, limits, config, protected_work(config, op["index"], len(op["attempts"]) + 1))
        op["attempts"].append({"key": key, "wire": expected, "state": "RESERVED", "response": None})
        op["state"] = "READY"
    elif kind == "DISPATCH":
        if op["state"] != "READY" or op["attempts"][-1]["state"] != "RESERVED":
            raise ValueError("duplicate dispatch")
        integer(data["at_ms"])
        if not op["last_wall_ms"] <= data["at_ms"] < op["deadline_ms"]:
            raise ValueError("dispatch outside deadline")
        timeout = min(limits.timeout_ms, op["deadline_ms"] - data["at_ms"])
        if data["timeout_ms"] != timeout:
            raise ValueError("timeout/config mismatch")
        op["attempts"][-1].update(state="DISPATCHED", dispatched_ms=data["at_ms"], timeout_ms=timeout)
        op["last_wall_ms"] = data["at_ms"]
        op["state"] = "DISPATCHED"
    elif kind == "RESPONSE":
        attempt = op["attempts"][-1]
        if attempt["state"] != "DISPATCHED":
            raise ValueError("response without unique dispatch")
        integer(data["received_ms"])
        integer(data["elapsed_ms"])
        if len(canonical(data["response"]).encode("utf-8")) > limits.response_bytes:
            raise ValueError("response too large")
        attempt.update(state="RESPONDED", response=data["response"], response_hash=digest(data["response"]),
                       received_ms=data["received_ms"], elapsed_ms=data["elapsed_ms"])
        # Usage is evidence even when recovery has already paused selection.
        _, _, estimate = inspect_response(limits, attempt["response"], op["legal_ids"],
                                          action_profile=config.schema)
        if estimate is not None:
            record_estimate(state["accounts"], attempt["key"], estimate)
        op["last_wall_ms"] = max(op["last_wall_ms"], data["received_ms"])
        # A late receipt can be preserved after a concurrent recovery paused the run.
        if state["pause"] is None:
            op["state"] = "RESPONDED"
    elif kind == "RESOLVE":
        if op["state"] != "RESPONDED":
            raise ValueError("no response to resolve")
        attempt = op["attempts"][-1]
        failure, action, estimate = inspect_response(limits, attempt["response"], op["legal_ids"],
                                                    action_profile=config.schema)
        if estimate is not None and estimate > state["accounts"][attempt["key"]]["bound"]:
            failure = "USAGE_BOUND_EXCEEDED"
        if attempt["received_ms"] < attempt["dispatched_ms"]:
            failure = "CLOCK_ROLLBACK"
        elif (attempt["received_ms"] >= op["deadline_ms"]
              or attempt["received_ms"] - attempt["dispatched_ms"] >= attempt["timeout_ms"]
              or attempt["elapsed_ms"] >= attempt["timeout_ms"]):
            failure = "LATE_RESPONSE"
        attempt.update(state="RESOLVED", failure=failure)
        if failure is None:
            if action["player_id"] in {p["player_id"] for p in state["picks"]}:
                raise ValueError("duplicate player settlement")
            pick = {**action, "index": op["index"], "manager": limits.manager,
                    "model": limits.model, "origin": "SIMULATED_AI", "response_hash": attempt["response_hash"],
                    "request_hash": attempt["wire"]["request_hash"], "attempt": len(op["attempts"]) - 1}
            state["picks"].append(pick)
            op.update(state="SETTLED", action=pick)
        elif failure in config.correction_failures and len(op["attempts"]) < limits.max_attempts:
            op.update(state="CORRECTING", correction={"failure": failure, "response_hash": attempt["response_hash"],
                       "previous_text": attempt["response"].get("text")})
        else:
            pause(state, op, failure)
    else:
        raise ValueError("unknown journal event")


class SystemClock:
    def now_ms(self):
        return time.time_ns() // 1_000_000

    def monotonic_ms(self):
        return time.monotonic_ns() // 1_000_000


class Draft:
    def __init__(self, directory, config, *, clock=None, fault=None):
        self.config, self.clock, self.fault = config, clock or SystemClock(), fault
        self.journal = Journal(directory, config, reduce_event, initial_state, fault=fault)

    def _after(self, kind):
        if self.fault:
            self.fault("after:" + kind)

    def open(self, legal_ids):
        self.journal.transact(lambda state: [("OPEN", {"legal_ids": list(legal_ids), "opened_ms": self.clock.now_ms()})])
        self._after("OPEN")
        return self.status()

    def status(self):
        state = self.journal.read()
        phase = "PAUSED" if state["pause"] else "COMPLETE" if len(state["picks"]) == self.config.total_picks else "READY"
        return {"phase": phase, "pause": state["pause"], "picks": state["picks"],
                "total_picks": self.config.total_picks, "accounting": totals(state["accounts"]),
                "config_hash": self.config.hash, "mode": self.config.mode}

    def export_roster(self):
        """Return a deterministic, complete fixture roster from one verified snapshot."""
        state = self.journal.read()
        picks = state["picks"]
        if (state["pause"] is not None or len(picks) != self.config.total_picks
                or len(state["opportunities"]) != self.config.total_picks
                or any(op["state"] != "SETTLED" for op in state["opportunities"])):
            raise ValueError("complete settled draft required for roster export")
        if len({pick["player_id"] for pick in picks}) != len(picks):
            raise ValueError("duplicate player ownership in roster export")
        if [pick["index"] for pick in picks] != list(range(self.config.total_picks)):
            raise ValueError("nonsequential settled picks")
        for pick in picks:
            provider = self.config.at_pick(pick["index"])
            if (pick["manager"] != provider.manager or pick["model"] != provider.model
                    or pick["origin"] != "SIMULATED_AI"):
                raise ValueError("settled pick identity mismatch")
        rosters = [{"manager_id": provider.manager, "model_id": provider.model,
                    "player_ids": [pick["player_id"] for pick in picks if pick["manager"] == provider.manager]}
                   for provider in self.config.providers]
        if any(len(roster["player_ids"]) != self.config.roster_size for roster in rosters):
            raise ValueError("incomplete manager roster")
        receipt = {
            "schema_version": "miomaci.basketball.settled-roster/1",
            "experiment_id": self.config.experiment_id, "run_id": self.config.run_id,
            "config_hash": self.config.hash, "draft_rules_digest": self.config.rules_digest,
            "execution_mode": self.config.mode, "simulated": True, "provisional": True,
            "competitive_authorization": False, "draft_status": "COMPLETE",
            "roster_size": self.config.roster_size, "total_picks": self.config.total_picks,
            "rosters": rosters, "picks": picks,
        }
        return {**receipt, "receipt_hash": digest(receipt)}

    def resume(self, transport):
        if not isinstance(transport, FixtureTransport):
            raise TypeError("only explicitly simulated fixture transport is supported")
        while True:
            dispatched, appended = {}, []

            def decide(state):
                if state["pause"] or not state["opportunities"] or state["opportunities"][-1]["state"] == "SETTLED":
                    return []
                op = state["opportunities"][-1]
                limits = self.config.at_pick(op["index"])
                now = self.clock.now_ms()
                if op["state"] == "RESPONDED":
                    event = ("RESOLVE", {})
                elif op["state"] == "DISPATCHED":
                    event = ("PAUSE", {"reason": "UNKNOWN_DISPATCH"})
                elif now < op["last_wall_ms"]:
                    event = ("PAUSE", {"reason": "CLOCK_ROLLBACK"})
                elif now >= op["deadline_ms"]:
                    event = ("PAUSE", {"reason": "DEADLINE"})
                elif op["state"] in {"OPEN", "CORRECTING"}:
                    try:
                        wire = request(self.config, op)
                    except ValueError:
                        event = ("PAUSE", {"reason": "INPUT_BOUND"})
                    else:
                        protected = protected_work(self.config, op["index"], len(op["attempts"]) + 1)
                        if totals(state["accounts"])["committed_micro_usd"] + request_bound(limits) + protected > self.config.budget_micro_usd:
                            event = ("PAUSE", {"reason": "BUDGET"})
                        else:
                            event = ("ATTEMPT", {"wire": wire})
                else:
                    timeout = min(limits.timeout_ms, op["deadline_ms"] - now)
                    event = ("DISPATCH", {"at_ms": now, "timeout_ms": timeout})
                    dispatched.update(key=op["attempts"][-1]["key"], wire=op["attempts"][-1]["wire"],
                                      timeout=timeout, monotonic_start=self.clock.monotonic_ms())
                appended.append(event[0])
                return [event]

            self.journal.transact(decide)
            if not appended:
                return self.status()
            self._after(appended[0])
            if dispatched:
                current = self.journal.read()
                if current["pause"]:
                    return self.status()
                op = current["opportunities"][-1]
                now = self.clock.now_ms()
                if now < op["last_wall_ms"]:
                    self._pause("CLOCK_ROLLBACK")
                    return self.status()
                # Time spent persisting dispatch intent must not extend the call window.
                elapsed_before_send = self.clock.monotonic_ms() - dispatched["monotonic_start"]
                if elapsed_before_send < 0:
                    self._pause("CLOCK_ROLLBACK")
                    return self.status()
                remaining = min(min(op["deadline_ms"], op["attempts"][-1]["dispatched_ms"] + dispatched["timeout"]) - now,
                                dispatched["timeout"] - elapsed_before_send)
                if remaining <= 0:
                    self._pause("DEADLINE")
                    return self.status()
                started = dispatched["monotonic_start"]
                try:
                    raw = transport.send(dispatched["key"], dispatched["wire"], remaining)
                except Exception:
                    # Exception text may contain secrets. Preserve the class of outcome only.
                    self._pause("UNKNOWN_DISPATCH")
                    return self.status()
                elapsed = self.clock.monotonic_ms() - started
                received = self.clock.now_ms()
                try:
                    size = len(canonical(raw).encode("utf-8"))
                except (TypeError, ValueError, RecursionError):
                    self._pause("MALFORMED_ENVELOPE")
                    return self.status()
                limits = self.config.provider(dispatched["wire"]["body"]["input"]["manager"])
                if size > limits.response_bytes:
                    self._pause("RESPONSE_TOO_LARGE")
                    return self.status()
                if elapsed < 0:
                    self._pause("CLOCK_ROLLBACK")
                    return self.status()
                self.journal.transact(lambda state: [("RESPONSE", {"response": raw, "received_ms": received, "elapsed_ms": elapsed})])
                self._after("RESPONSE")

    def _pause(self, reason):
        self.journal.transact(lambda state: [] if state["pause"] else [("PAUSE", {"reason": reason})])
