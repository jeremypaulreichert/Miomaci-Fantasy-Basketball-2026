"""Isolated, single-request HTTPS worker. Credentials arrive only on stdin.

No retries, redirects, proxies, shell commands or credential discovery. This
file is invoked by transport.py, never by importing the package.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fantasy_basketball.core.config import canonical, integer
from fantasy_basketball.providers.fixtures import strict_action
from fantasy_basketball.providers.native import endpoint_provider
from fantasy_basketball.providers.worker_limits import check_support, MAX_INPUT_BYTES


def contains_secret(value, credential):
    if isinstance(value, str):
        return credential in value
    if isinstance(value, dict):
        return any(contains_secret(k, credential) or contains_secret(v, credential) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_secret(v, credential) for v in value)
    return False


def perform(payload, connection_factory=None):
    """The injected factory is for offline tests; production always uses TLS."""
    connection = None
    try:
        endpoint = payload["endpoint"]
        provider = endpoint_provider(endpoint)
        if provider is None:
            return {"outcome": "INVALID_REQUEST"}
        timeout = integer(payload["timeout_ms"], 1)
        limit = integer(payload["response_bytes"], 1)
        request_limit = integer(payload["request_bytes"], 1)
        try:
            check_support(request_limit, limit, timeout)
        except ValueError:
            return {"outcome": "INVALID_REQUEST"}
        credential = payload["credential"]
        if not isinstance(credential, str) or not 1 <= len(credential) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in credential):
            return {"outcome": "INVALID_REQUEST"}
        body = canonical(payload["body"]).encode("utf-8")
        if len(body) > request_limit:
            return {"outcome": "INVALID_REQUEST"}
        url = urlsplit(endpoint)
        factory = connection_factory or http.client.HTTPSConnection
        connection = factory(url.hostname, timeout=timeout / 1000, context=ssl.create_default_context())
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}
        if provider == "anthropic":
            headers.update({"x-api-key": credential, "anthropic-version": "2023-06-01"})
        elif provider == "gemini":
            headers["x-goog-api-key"] = credential
        else:
            headers["Authorization"] = "Bearer " + credential
        connection.request("POST", url.path, body=body, headers=headers)
        response = connection.getresponse()
        # Bounded read applies even to errors, redirects and chunked responses.
        data = response.read(limit + 1)
        evidence = {"http_status": response.status, "body_sha256": hashlib.sha256(data).hexdigest()}
        if len(data) > limit:
            return {"outcome": "RESPONSE_TOO_LARGE", **evidence, "hash_is_prefix": True}
        if response.status != 200:
            # Do not persist arbitrary server errors, headers or exception text.
            return {"outcome": "HTTP_ERROR", **evidence}
        if credential.encode("ascii") in data:
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
        return {"outcome": "RESPONSE", "raw": raw, **evidence}
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
        payload = strict_action(data.decode("utf-8"))
        check_support(payload["request_bytes"], payload["response_bytes"], payload["timeout_ms"])
        remaining = min(integer(payload["timeout_ms"], 1),
                        integer(payload["deadline_ms"], 1) - time.time_ns() // 1000000)
        if remaining <= 0:
            result = {"outcome": "WORKER_TIMEOUT"}
        else:
            # Still ends a slow/trickling request if the controlling process dies.
            watchdog = threading.Timer(remaining / 1000, lambda: os._exit(124))
            watchdog.daemon = True
            watchdog.start()
            result = perform(payload)
        sys.stdout.buffer.write(canonical(result).encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.buffer.write(b'{"outcome":"WORKER_ERROR"}')
    finally:
        if watchdog:
            watchdog.cancel()


if __name__ == "__main__":
    main()
