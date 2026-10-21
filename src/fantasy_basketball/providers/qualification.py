"""Candidate native profiles and a fixture-only durable-engine bridge.

This module has no live execution path. Profile names/rates are proposals from
official documentation read on 2026-10-15, not account or launch authorization.
"""
from copy import deepcopy
from dataclasses import asdict
from ..core.config import RunConfig, digest
from .fixtures import FixtureTransport
from .native import NativeProfile, inspect, prepare


def candidate_profiles():
    rows = (
        ("openai", "gpt-6.1-sol", "medium", 1050000, "5", "15", (
            "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
            "https://developers.openai.com/api/docs/guides/reasoning",
            "https://developers.openai.com/api/docs/pricing")),
        ("deepseek", "deepseek-flash", "high", 1048576, "0.3", "1.2", (
            "https://api-docs.deepseek.com/api/create-chat-completion/",
            "https://api-docs.deepseek.com/guides/thinking_mode/",
            "https://api-docs.deepseek.com/quick_start/pricing/")),
        ("mistral", "mistral-small-2603", "high", 262144, "0.15", "0.6", (
            "https://docs.mistral.ai/models/mistral-small-4-0-26-03",
            "https://docs.mistral.ai/studio/conversations/reasoning",
            "https://docs.mistral.ai/inference/pricing")),
    )
    return tuple(NativeProfile(f"{provider}-output-{cap}", provider, model, (model,), effort,
                               ceiling, ceiling, cap, inp, out, 120000, 240000, 2,
                               24000, 262144, 5000, sources)
                 for provider, model, effort, ceiling, inp, out, sources in rows for cap in (4096, 16384))


def fixture_config(profile, board, run_id):
    """One isolated opportunity, including any pre-existing synthetic roster."""
    return RunConfig("miomaci-basketball-native-diagnostics", run_id, (profile.fixture_limits(),),
                     1, profile.bound_micro_usd * profile.max_attempts, profile.opportunity_ms,
                     digest({"native_profile": asdict(profile), "board": asdict(board)}),
                     schema="basketball-build/2")


class NativeFixtureTransport(FixtureTransport):
    """Only supplied native-shaped responses; never invokes a provider.

The journal's config digest binds the full board and native profile. Native
requests and sanitized receipts are stored with fixture responses. Native
identity stays in native_evidence; fixture model names are explicitly synthetic.
"""
    def __init__(self, profile, board, config, responses, clock):
        super().__init__(responses)
        expected = fixture_config(profile, board, config.run_id)
        if config != expected:
            raise ValueError("fixture protocol must bind full board and native profile")
        self.profile, self.board, self.config, self.clock = profile, board, config, clock
        self.native_calls = []

    def send(self, key, wire, timeout_ms):
        if (wire["config_hash"] != self.config.hash
                or wire["body"]["input"]["legal_ids"] != list(self.board.legal_ids())
                or wire["body"]["model"] != self.profile.fixture_limits().model
                or wire["body"]["input"]["manager"] != self.profile.provider):
            raise ValueError("fixture/native context binding differs")
        now = self.clock.now_ms()
        native = prepare(self.profile, self.board, now_ms=now, deadline_ms=now + timeout_ms,
                         correction=wire["body"]["input"]["correction"])
        self.native_calls.append(deepcopy(native))
        raw = super().send(key, wire, timeout_ms)
        receipt = inspect(self.profile, raw, self.board.legal_ids())
        usage = receipt["usage"]
        action_failure = receipt["failure"] in {None, "MALFORMED_ACTION", "ILLEGAL_PICK", "AI_HOLD"}
        # Missing breakdown stays null in native evidence. For a combined total,
        # fixture accounting needs no additional reasoning term (always zero).
        normalized_usage = None if usage is None else {
            "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
            "reasoning_tokens": usage["reasoning_tokens"] if usage["reasoning_tokens"] is not None else 0}
        return {"origin": "SIMULATED", "manager": self.profile.provider,
                "model": self.profile.fixture_limits().model if receipt["observed_model"] in self.profile.accepted_models else "fixture-identity-mismatch",
                "response_id": receipt["response_id"] or "", "usage": normalized_usage,
                "stop": "completed" if action_failure else "native_failure",
                "text": receipt["answer_text"], "native_evidence": receipt,
                "native_request": native, "native_fixture_only": True}


def synthetic_response(profile, player_id, *, text=None, stop="completed", output_tokens=100):
    """Native envelope examples for local tests, never qualification evidence."""
    from ..core.config import canonical
    answer = canonical({"player_id": player_id, "rationale": "Synthetic test choice."}) if text is None else text
    base = {"id": "synthetic-native-response", "model": profile.model}
    if profile.provider in {"openai", "xai"}:
        base.update(status="completed" if stop == "completed" else "incomplete",
                    incomplete_details=None if stop == "completed" else {"reason": "max_output_tokens"},
                    output=[{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": answer}]}],
                    usage={"input_tokens": 1000, "output_tokens": output_tokens, "total_tokens": 1000 + output_tokens,
                           "input_tokens_details": {"cached_tokens": 0},
                           "output_tokens_details": {"reasoning_tokens": output_tokens // 2}})
    elif profile.provider == "anthropic":
        base.update(type="message", role="assistant", stop_reason="end_turn" if stop == "completed" else "max_tokens",
                    content=[{"type": "thinking", "thinking": "SYNTHETIC_TRACE_NOT_EXPORTED"}, {"type": "text", "text": answer}],
                    usage={"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "output_tokens": output_tokens, "output_tokens_details": {"thinking_tokens": output_tokens // 2}})
    elif profile.provider == "gemini":
        base = {"responseId": "synthetic-native-response", "modelVersion": profile.model,
                "candidates": [{"index": 0, "finishReason": "STOP" if stop == "completed" else "MAX_TOKENS",
                                "content": {"role": "model", "parts": [{"text": answer}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": output_tokens - output_tokens // 2,
                                  "thoughtsTokenCount": output_tokens // 2, "totalTokenCount": 1000 + output_tokens}}
    else:
        content = answer if profile.provider != "mistral" else [
            {"type": "thinking", "thinking": [{"type": "text", "text": "SYNTHETIC_TRACE_NOT_EXPORTED"}]},
            {"type": "text", "text": answer}]
        base.update(choices=[{"index": 0, "finish_reason": "stop" if stop == "completed" else "length",
                              "message": {"role": "assistant", "content": content}}],
                    usage={"prompt_tokens": 1000, "completion_tokens": output_tokens,
                           "total_tokens": 1000 + output_tokens})
        if profile.provider == "deepseek":
            base["usage"].update(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1000,
                                 completion_tokens_details={"reasoning_tokens": output_tokens // 2})
    return base
