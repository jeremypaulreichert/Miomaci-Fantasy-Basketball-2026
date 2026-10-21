"""Native envelopes beyond the original three; no HTTP or model selection.

Models, accepted identities, rate ceilings and limits must be supplied in a
frozen profile. Documentation compatibility and fixtures are not qualification.
"""
from ..core.config import canonical, digest, integer
from ..core.budget import charge


def request_body(profile, system, user, grammar):
    if profile.provider == "anthropic":
        return {"model": profile.model, "stream": False, "max_tokens": profile.output_tokens,
                "system": system, "messages": [{"role": "user", "content": user}],
                "thinking": {"type": "adaptive"}, "service_tier": "standard_only",
                "output_config": {"effort": profile.effort, "format": {"type": "json_schema", "schema": grammar}}}
    if profile.provider == "gemini":
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"candidateCount": 1, "maxOutputTokens": profile.output_tokens,
                                     "responseMimeType": "application/json",
                                     "thinkingConfig": {"thinkingLevel": profile.effort}}}
    if profile.provider in {"moonshot", "zai", "qwen", "meta", "meta_direct"}:
        body = {"model": profile.model, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                profile.fixture_limits().output_field: profile.output_tokens,
                "reasoning_effort": profile.effort, "response_format": {"type": "json_object"}}
        if profile.provider == "zai":
            body["thinking"] = {"type": "enabled", "clear_thinking": True}
        elif profile.provider == "qwen":
            body.update(enable_thinking=True, preserve_thinking=False)
        elif profile.provider == "meta":
            # Hosted Llama is a Meta seat, using one explicit Together model.
            body.pop("reasoning_effort")
            body.update(n=1, context_length_exceeded_behavior="error")
        elif profile.provider == "meta_direct":
            # Explicit standard Muse route, never a Together/Contributor fallback.
            # Use plain text plus the shared local action validator; no optional
            # structured-output dialect is assumed on this initial contract.
            body.pop("response_format")
            body["n"] = 1
        return body
    raise ValueError("unsupported additional native contract")


def inspect_response(profile, raw, legal_ids):
    receipt = {"schema": "basketball-native-receipt/1", "profile_hash": profile.hash,
               "provider": profile.provider, "observed_model": None, "response_id": None,
               "response_hash": None, "usage": None, "stop": None, "answer_text": None,
               "failure": None, "action": None, "estimated_charge_micro_usd": None}
    def fail(reason):
        receipt["failure"] = reason
        return receipt
    try:
        if not isinstance(raw, dict) or len(canonical(raw).encode()) > profile.response_bytes:
            return fail("MALFORMED_OR_OVERSIZED_NATIVE_RESPONSE")
        receipt["response_hash"] = digest(raw)
        model = raw.get("modelVersion" if profile.provider == "gemini" else "model")
        identity = raw.get("responseId" if profile.provider == "gemini" else "id")
        receipt["observed_model"] = model if isinstance(model, str) else None
        receipt["response_id"] = identity if isinstance(identity, str) else None
        if receipt["observed_model"] not in profile.accepted_models or not receipt["response_id"]:
            return fail("IDENTITY_MISMATCH")
        cached, written, reasoning = None, None, None
        if profile.provider == "anthropic":
            usage = raw["usage"]
            uncached = integer(usage["input_tokens"])
            # These are distinct input populations, not subsets of input_tokens.
            cached = integer(usage["cache_read_input_tokens"])
            written = integer(usage["cache_creation_input_tokens"])
            inp = uncached + cached + written
            out = integer(usage["output_tokens"])
            reasoning = usage.get("output_tokens_details", {}).get("thinking_tokens")
            if any(integer(value) for value in usage.get("server_tool_use", {}).values()):
                return fail("UNREQUESTED_NATIVE_TOOL_USAGE")
        elif profile.provider == "gemini":
            usage = raw["usageMetadata"]
            inp = integer(usage["promptTokenCount"])
            visible = integer(usage["candidatesTokenCount"])
            total = integer(usage["totalTokenCount"])
            reasoning = usage.get("thoughtsTokenCount")
            cached = usage.get("cachedContentTokenCount")
            if integer(usage.get("toolUsePromptTokenCount", 0)):
                return fail("UNREQUESTED_NATIVE_TOOL_USAGE")
            out = total - inp
            integer(out)
            if out < visible or (reasoning is not None and out != visible + integer(reasoning)):
                return fail("MISSING_OR_INVALID_USAGE")
        else:
            usage = raw["usage"]
            inp, out = integer(usage["prompt_tokens"]), integer(usage["completion_tokens"])
            if integer(usage["total_tokens"]) != inp + out:
                return fail("MISSING_OR_INVALID_USAGE")
            reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens")
            details = usage.get("prompt_tokens_details", {})
            cached = details.get("cached_tokens")
            written = details.get("cache_write_tokens")
            if profile.provider == "moonshot" and usage.get("cached_tokens") is not None:
                if cached is not None and cached != usage["cached_tokens"]:
                    return fail("MISSING_OR_INVALID_USAGE")
                cached = usage["cached_tokens"]
        for value, bound in ((cached, inp), (written, inp), (reasoning, out)):
            if value is not None and integer(value) > bound:
                return fail("MISSING_OR_INVALID_USAGE")
        if cached is not None and written is not None and cached + written > inp:
            return fail("MISSING_OR_INVALID_USAGE")
        receipt["usage"] = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                            "reasoning_tokens": reasoning, "cached_input_tokens": cached,
                            "cache_write_input_tokens": written}
        # Frozen input price must cover every applicable input/cache-write tier;
        # charge all input at that upper rate, never assume a cache discount.
        receipt["estimated_charge_micro_usd"] = charge(profile.fixture_limits(), inp, out, 0)
        if inp > profile.input_tokens or out > profile.fixture_limits().output_tokens:
            return fail("USAGE_BOUND_EXCEEDED")
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return fail("MISSING_OR_INVALID_USAGE")
    try:
        texts = []
        if profile.provider == "anthropic":
            receipt["stop"] = raw.get("stop_reason")
            if receipt["stop"] != "end_turn":
                return fail("MODEL_REFUSAL" if receipt["stop"] == "refusal" else "INCOMPLETE_RESPONSE")
            if raw.get("role") != "assistant" or raw.get("type") != "message":
                return fail("AMBIGUOUS_NATIVE_ACTION")
            if (raw.get("stop_details") or {}).get("type") == "refusal":
                return fail("MODEL_REFUSAL")
            for block in raw["content"]:
                if block.get("type") == "text":
                    texts.append(block["text"])
                elif block.get("type") not in {"thinking", "redacted_thinking"}:
                    return fail("AMBIGUOUS_NATIVE_ACTION")
        elif profile.provider == "gemini":
            candidates = raw["candidates"]
            if len(candidates) != 1 or candidates[0].get("index", 0) != 0:
                return fail("AMBIGUOUS_NATIVE_ACTION")
            candidate = candidates[0]
            receipt["stop"] = candidate.get("finishReason")
            if receipt["stop"] != "STOP":
                return fail("INCOMPLETE_RESPONSE")
            if candidate["content"].get("role") != "model":
                return fail("AMBIGUOUS_NATIVE_ACTION")
            for block in candidate["content"]["parts"]:
                if set(block) - {"text", "thought", "thoughtSignature"}:
                    return fail("AMBIGUOUS_NATIVE_ACTION")
                if block.get("thought") is not True and "text" in block:
                    texts.append(block["text"])
        else:
            choices = raw["choices"]
            if len(choices) != 1 or choices[0].get("index") != 0:
                return fail("AMBIGUOUS_NATIVE_ACTION")
            item = choices[0]
            receipt["stop"] = item.get("finish_reason")
            if receipt["stop"] != "stop":
                return fail("INCOMPLETE_RESPONSE")
            message = item["message"]
            if message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call"):
                return fail("AMBIGUOUS_NATIVE_ACTION")
            texts.append(message["content"])
        if len(texts) != 1 or not isinstance(texts[0], str):
            return fail("AMBIGUOUS_NATIVE_ACTION")
        receipt["answer_text"] = texts[0]
    except (KeyError, TypeError, ValueError, AttributeError):
        return fail("MALFORMED_NATIVE_RESPONSE")
    from .native import validate_action
    return validate_action(receipt, texts[0], legal_ids)
