"""Fixture envelopes exercise native-limit semantics without certifying APIs."""
from copy import deepcopy
import json
from ..core.config import canonical, digest, integer
from ..core.budget import charge


def request(config, opportunity):
    limits = config.at_pick(opportunity["index"])
    attempt = len(opportunity["attempts"])
    body = {
        "model": limits.model, limits.output_field: limits.output_tokens,
        "reasoning": {"mode": limits.reasoning_mode, "separate_token_allowance": limits.separate_reasoning_tokens},
        "input": {"legal_ids": opportunity["legal_ids"], "manager": limits.manager,
                  "index": opportunity["index"], "correction": opportunity["correction"]},
    }
    if config.schema == "basketball-build/2":
        body["response_schema"] = {
            "type": "object", "additionalProperties": False,
            "required": ["player_id", "rationale"],
            "properties": {
                "player_id": {"type": ["string", "null"], "enum": [*opportunity["legal_ids"], None]},
                "rationale": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        }
        body["input"]["instructions"] = (
            "Return one JSON object with player_id and a brief rationale. "
            "Choose a player_id from legal_ids, or use null to HOLD. "
            "A rationale must contain non-whitespace text. The ID order is not a ranking. "
            "If correcting an answer, use the same legal_ids and the recorded failure; "
            "do not choose an unavailable player."
        )
    # Fixture accounting convention only, not a real-model tokenization proof.
    if len(canonical(body).encode("utf-8")) > limits.input_tokens:
        raise ValueError("FIXTURE_INPUT_BOUND")
    return {"origin": "SIMULATED", "config_hash": config.hash, "body": body,
            "attempt": attempt, "request_hash": digest(body), "packet_hash": opportunity["packet_hash"]}


def strict_action(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite number")))


def inspect_response(limits, response, legal_ids, *, action_profile="basketball-build/1"):
    """Return failure, parsed action, estimate; never infer absent identity/usage."""
    if action_profile not in {"basketball-build/1", "basketball-build/2"}:
        raise ValueError("unknown action profile")
    if not isinstance(response, dict) or response.get("origin") != "SIMULATED":
        return "MISSING_IDENTITY", None, None
    if response.get("model") != limits.model or response.get("manager") != limits.manager or not isinstance(response.get("response_id"), str) or not response["response_id"]:
        return "IDENTITY_MISMATCH", None, None
    usage = response.get("usage")
    try:
        if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "reasoning_tokens"}:
            raise ValueError("missing usage")
        for value in usage.values():
            integer(value)
        inp, out, reasoning = (usage[k] for k in ("input_tokens", "output_tokens", "reasoning_tokens"))
        if limits.reasoning_mode == "combined" and reasoning > out:
            raise ValueError("inconsistent usage")
    except ValueError:
        return "MISSING_OR_INVALID_USAGE", None, None
    estimate = charge(limits, inp, out, reasoning)
    if inp > limits.input_tokens or out > limits.output_tokens or (limits.reasoning_mode == "separate" and reasoning > limits.separate_reasoning_tokens):
        return "USAGE_BOUND_EXCEEDED", None, estimate
    if response.get("stop") != "completed":
        return "INCOMPLETE_RESPONSE", None, estimate
    try:
        action = strict_action(response["text"])
        if action_profile == "basketball-build/2" and isinstance(action, dict) and set(action) == {"answer"}:
            if not isinstance(action["answer"], str):
                raise ValueError("answer wrapper must contain JSON text")
            # One exact envelope only. The unchanged validator checks its content.
            # Do not recurse, extract prose, invent fields or change a choice.
            action = strict_action(action["answer"])
        if not isinstance(action, dict) or set(action) != {"player_id", "rationale"} or not isinstance(action["rationale"], str) or not action["rationale"].strip() or len(action["rationale"]) > 500:
            raise ValueError("bad action schema")
        if action["player_id"] is None:
            return "AI_HOLD", None, estimate
        if not isinstance(action["player_id"], str):
            raise ValueError("bad player id")
    except (KeyError, ValueError, TypeError):
        return "MALFORMED_ACTION", None, estimate
    if action["player_id"] not in legal_ids:
        return "ILLEGAL_PICK", None, estimate
    return None, action, estimate


class FixtureTransport:
    def __init__(self, responses):
        self.responses = deepcopy(responses)
        self.calls = []

    def send(self, key, wire, timeout_ms):
        self.calls.append({"key": key, "request": deepcopy(wire), "timeout_ms": timeout_ms})
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def response(limits, player_id, *, text=None, input_tokens=50, output_tokens=20, reasoning_tokens=0):
    return {"origin": "SIMULATED", "manager": limits.manager, "model": limits.model,
            "response_id": "fixture-response", "stop": "completed",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "reasoning_tokens": reasoning_tokens},
            "text": canonical({"player_id": player_id, "rationale": "Synthetic fixture choice."}) if text is None else text}
