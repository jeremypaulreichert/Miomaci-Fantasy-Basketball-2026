"""Candidate Tavily search/extract wire contract, pure and disabled for dispatch.

Official contracts checked October 18, 2026. No API key is accepted by these
functions. No SDK, socket, HTTP client, account creation or credential lookup.
"""
from copy import deepcopy

from ..core.config import canonical, digest, integer
from .research import parse_action, public_url, tool_result


SOURCES = (
    "https://docs.tavily.com/documentation/api-reference/endpoint/search",
    "https://docs.tavily.com/documentation/api-reference/endpoint/extract",
    "https://docs.tavily.com/documentation/api-credits",
)


def credit_usage(raw):
    """Known native usage, including a reported overrun, or explicit unknown."""
    from decimal import Decimal, InvalidOperation
    if not isinstance(raw, dict) or not isinstance(raw.get("usage"), dict):
        return None
    value = raw["usage"].get("credits")
    if type(value) is bool or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or not 0 <= number <= 1000000:
        return None
    return number


def prepare_tool(action, *, discovered_urls, max_results=3, timeout_ms=20000):
    integer(max_results, 1)
    integer(timeout_ms, 1)
    if max_results > 5 or timeout_ms > 60000:
        raise ValueError("service bound")
    parsed = parse_action(canonical(action), (), discovered_urls)
    if parsed["action"] == "search":
        endpoint = "https://api.tavily.com/search"
        body = {"query": parsed["query"], "search_depth": "basic", "topic": "general",
                "max_results": max_results, "auto_parameters": False, "include_answer": False,
                "include_raw_content": False, "include_images": False, "include_usage": True}
    elif parsed["action"] == "read":
        endpoint = "https://api.tavily.com/extract"
        body = {"urls": [parsed["url"]], "extract_depth": "basic", "format": "markdown",
                "include_images": False, "include_usage": True}
    else:
        raise ValueError("search or read only")
    return {"schema": "basketball-research-service-request/1", "service": "tavily-candidate",
            "method": "POST", "endpoint": endpoint, "body": body, "request_hash": digest(body),
            "timeout_ms": timeout_ms, "reserved_credits": 1, "execution_authorized": False}


def normalize_tool(action, raw, *, retrieved_ms, text_bytes=4096, max_results=3, response_bytes=262144):
    """Return exactly what the AI would see plus a separate retained receipt.

    Full bounded service JSON is retained in the returned receipt. Deterministic
    UTF-8 truncation is disclosed, never presented as the full source page.
    Credit usage remains null if missing; one reserved credit is not an invoice.
    """
    integer(retrieved_ms)
    integer(text_bytes, 1)
    integer(response_bytes, 1)
    integer(max_results, 1)
    if not isinstance(raw, dict) or len(canonical(raw).encode()) > response_bytes:
        raise ValueError("service response bytes")
    if action.get("action") not in {"search", "read"}:
        raise ValueError("service action")
    rows = raw.get("results")
    if (type(rows) is not list or not rows or len(rows) > max_results
            or (action["action"] == "read" and (len(rows) != 1 or raw.get("failed_results")))):
        raise ValueError("service results")
    items, truncations, skipped = [], [], []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("service row")
        if action["action"] == "search":
            # Owner decision (October 19, ~4:10 p.m.): one search result with a disallowed URL (e.g. plain
            # http://) is dropped and recorded, instead of rejecting the whole search and stopping the draft.
            # The URL policy itself is unchanged; dropped URLs never become readable.
            try:
                public_url(row.get("url"))
            except ValueError as exc:
                skipped.append({"url": str(row.get("url"))[:2048], "reason": str(exc)})
                continue
        url = public_url(row.get("url"))
        if action["action"] == "read" and url != action["url"]:
            raise ValueError("unverified extracted redirect")
        text = row.get("content" if action["action"] == "search" else "raw_content")
        title = row.get("title", "")
        if not isinstance(text, str) or not text.strip() or not isinstance(title, str):
            raise ValueError("service text missing")
        encoded = text.encode()
        bounded = encoded[:text_bytes].decode("utf-8", "ignore")
        items.append({"url": url, "title": title[:500], "text": bounded})
        truncations.append({"url": url, "source_text_bytes": len(encoded),
                            "returned_text_bytes": len(bounded.encode()), "truncated": len(encoded) > text_bytes})
    if not items:
        raise ValueError("service results")
    observed = raw.get("usage", {}).get("credits") if isinstance(raw.get("usage", {}), dict) else None
    if observed is not None:
        # Credit units may be fractional; store exact decimal text, no billing
        # inference from a float or successful extraction count.
        from decimal import Decimal, InvalidOperation
        try:
            parsed = Decimal(str(observed))
        except InvalidOperation as exc:
            raise ValueError("service usage") from exc
        if type(observed) is bool or not parsed.is_finite() or parsed < 0 or parsed > 1:
            raise ValueError("service usage bound")
        observed = str(parsed)
    evidence = tool_result(action, {"outcome": "OK", "retrieved_ms": retrieved_ms, "items": items},
                           text_bytes=text_bytes, max_results=max_results)
    return {"evidence": evidence, "receipt": {"service": "tavily-candidate", "raw": deepcopy(raw),
            "response_hash": digest(raw), "retrieved_ms": retrieved_ms, "observed_credits": observed,
            "reserved_credits": 1, "invoice_confirmed_micro_usd": 0, "content_bounds": truncations,
            "evidence_hash": digest(evidence), "execution_authorized": False}
            | ({"skipped_results": skipped} if skipped else {})}
