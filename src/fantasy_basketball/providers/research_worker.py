"""Isolated one-request Tavily worker, not an execution permit or key reader.

Only api.tavily.com is contacted; source URLs go to its extraction API, never a
local browser/fetcher. No redirects, proxies, retries, files or credential lookup.
Parent entry must check its reviewed frozen permit before starting this worker.
"""
import hashlib
import http.client
import os
from pathlib import Path
import ssl
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fantasy_basketball.core.config import canonical, integer
from fantasy_basketball.providers.fixtures import strict_action
from fantasy_basketball.providers.http_worker import contains_secret
from fantasy_basketball.providers.research_service import prepare_tool


MAX_INPUT_BYTES = 65536
MAX_RESPONSE_BYTES = 1048576


def perform(payload, connection_factory=None):
    connection = None
    try:
        if (not isinstance(payload, dict) or set(payload) != {
                "action", "discovered_urls", "max_results", "timeout_ms", "deadline_ms", "response_bytes", "credential"}):
            return {"outcome": "INVALID_REQUEST"}
        timeout = integer(payload["timeout_ms"], 1)
        remaining = min(timeout, integer(payload["deadline_ms"], 1) - time.time_ns() // 1000000)
        limit = integer(payload["response_bytes"], 1)
        if remaining <= 0:
            return {"outcome": "WORKER_TIMEOUT"}
        if timeout > 60000 or limit > MAX_RESPONSE_BYTES:
            return {"outcome": "INVALID_REQUEST"}
        credential = payload["credential"]
        if (not isinstance(credential, str) or not 1 <= len(credential) <= 2560
                or any(not 33 <= ord(c) <= 126 for c in credential)):
            return {"outcome": "INVALID_REQUEST"}
        urls = payload["discovered_urls"]
        if type(urls) is not list or len(urls) > 50:
            return {"outcome": "INVALID_REQUEST"}
        wire = prepare_tool(payload["action"], discovered_urls=urls,
                            max_results=payload["max_results"], timeout_ms=remaining)
        if contains_secret(wire, credential):
            return {"outcome": "CREDENTIAL_IN_REQUEST"}
        factory = connection_factory or http.client.HTTPSConnection
        connection = factory("api.tavily.com", timeout=remaining / 1000, context=ssl.create_default_context())
        headers = {"Authorization": "Bearer " + credential, "Content-Type": "application/json",
                   "Accept": "application/json", "Accept-Encoding": "identity"}
        path = "/search" if payload["action"]["action"] == "search" else "/extract"
        connection.request("POST", path, body=canonical(wire["body"]).encode(), headers=headers)
        response = connection.getresponse()
        data = response.read(limit + 1)
        evidence = {"http_status": response.status, "body_sha256": hashlib.sha256(data).hexdigest()}
        if len(data) > limit:
            return {"outcome": "RESPONSE_TOO_LARGE", "hash_is_prefix": True, **evidence}
        if response.status != 200:
            return {"outcome": "HTTP_ERROR", **evidence}
        if credential.encode() in data:
            return {"outcome": "CREDENTIAL_ECHO", **evidence}
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            return {"outcome": "UNSUPPORTED_ENCODING", **evidence}
        try:
            raw = strict_action(data.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("object required")
            if contains_secret(raw, credential):
                return {"outcome": "CREDENTIAL_ECHO", **evidence}
        except (ValueError, UnicodeError, RecursionError):
            return {"outcome": "MALFORMED_JSON", **evidence}
        return {"outcome": "SERVICE_RESPONSE", "raw": raw,
                "retrieved_ms": time.time_ns() // 1000000, **evidence}
    except Exception:
        return {"outcome": "TRANSPORT_ERROR"}
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def main():
    watchdog = None
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("input bound")
        payload = strict_action(data.decode())
        remaining = min(integer(payload["timeout_ms"], 1),
                        integer(payload["deadline_ms"], 1) - time.time_ns() // 1000000)
        if not 0 < remaining <= 60000:
            result = {"outcome": "WORKER_TIMEOUT"}
        else:
            watchdog = threading.Timer(remaining / 1000, lambda: os._exit(124))
            watchdog.daemon = True
            watchdog.start()
            result = perform(payload)
        sys.stdout.buffer.write(canonical(result).encode())
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.buffer.write(b'{"outcome":"WORKER_ERROR"}')
    finally:
        if watchdog:
            watchdog.cancel()


if __name__ == "__main__":
    main()
