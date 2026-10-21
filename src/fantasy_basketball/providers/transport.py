"""Bounded child-process transport with explicit injected live authority."""
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from ..core.config import canonical, digest, integer
from .fixtures import strict_action
from .native import endpoint_for
from .worker_limits import check_support, MAX_INPUT_BYTES


@dataclass(frozen=True)
class LiveAuthorization:
    config_hash: str
    expires_ms: int
    owner_record: str
    independent_review_record: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.config_hash):
            raise ValueError("explicit configuration approval required")
        integer(self.expires_ms, 1)
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1000
               for v in (self.owner_record, self.independent_review_record)):
            raise ValueError("owner and independent review record references required")

    def check(self, config, now_ms):
        if config.mode != "LIVE_DIAGNOSTIC" or config.hash != self.config_hash or now_ms >= self.expires_ms:
            raise ValueError("live authority absent, changed or expired")


def bounded_process(command, payload, timeout_ms, output_limit, *, launch_guard=None):
    """Own and reap a worker; terminating it does not cancel remote billing."""
    integer(timeout_ms, 1)
    integer(output_limit, 1)
    started = time.monotonic()
    # Inherit only OS/runtime necessities, never ambient API credentials/proxies.
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATH") if name in os.environ}
    process = None
    try:
        # Serialize the last eligibility read and child creation with pause
        # commits. Release before stdin delivery/response waiting. An already
        # started child remains potentially billable if a later pause commits.
        with launch_guard() if launch_guard else nullcontext(True) as allowed:
            if not allowed:
                return {"outcome": "DISPATCH_SUPPRESSED"}
            if (time.monotonic() - started) * 1000 >= timeout_ms:
                return {"outcome": "WORKER_TIMEOUT"}
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        remaining = timeout_ms / 1000 - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout_ms / 1000)
        output, _ = process.communicate(payload, timeout=remaining)
        if (time.monotonic() - started) * 1000 >= timeout_ms:
            return {"outcome": "WORKER_TIMEOUT"}
        if process.returncode == 124:
            return {"outcome": "WORKER_TIMEOUT"}
        if process.returncode != 0 or len(output) > output_limit:
            return {"outcome": "WORKER_ERROR"}
        try:
            result = strict_action(output.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("worker object required")
            return result
        except (ValueError, UnicodeError, RecursionError):
            return {"outcome": "WORKER_ERROR"}
    except subprocess.TimeoutExpired:
        return {"outcome": "WORKER_TIMEOUT"}
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            # This owned child cannot spawn descendants. Reap before another request.
            process.communicate(timeout=5)


class HTTPTransport:
    mode = "LIVE_DIAGNOSTIC"

    def __init__(self, config, authorization, credentials):
        if type(authorization) is not LiveAuthorization or type(credentials) is not dict:
            raise ValueError("explicit authority and in-memory credentials required")
        if set(credentials) != {c.profile.provider for c in config.cases}:
            raise ValueError("credentials must match this trial's providers")
        for value in credentials.values():
            if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("invalid credential shape")
        for case in config.cases:
            check_support(case.profile.request_bytes, case.profile.response_bytes, case.profile.timeout_ms)
        self.config = config
        self.config_hash = config.hash
        self.authorization = authorization
        self.credentials = dict(credentials)

    def authorize(self, config, now_ms):
        self.authorization.check(config, now_ms)
        if config.hash != self.config_hash:
            raise ValueError("transport/config mismatch")

    def send(self, key, request, timeout_ms, *, launch_guard=None):
        now = time.time_ns() // 1000000
        self.authorize(self.config, now)
        matches = [c.profile for c in self.config.cases if c.profile.hash == request["profile_hash"]]
        if not matches:
            raise ValueError("unapproved profile")
        profile = matches[0]
        if (request["endpoint"] != endpoint_for(profile)
                or request["request_hash"] != digest(request["body"])
                or len(canonical(request["body"]).encode()) > profile.request_bytes):
            raise ValueError("request binding differs")
        remaining = min(timeout_ms, profile.timeout_ms, request["deadline_ms"] - now,
                        self.authorization.expires_ms - now)
        if remaining <= 0:
            return {"outcome": "WORKER_TIMEOUT"}
        # Supplied for this basketball run only. No environment/file discovery.
        credential = self.credentials[profile.provider]
        payload = canonical({"endpoint": request["endpoint"], "body": request["body"],
                             "timeout_ms": remaining, "deadline_ms": now + remaining, "request_bytes": profile.request_bytes,
                             "response_bytes": profile.response_bytes, "credential": credential}).encode()
        credential = None
        if len(payload) > MAX_INPUT_BYTES:
            return {"outcome": "INVALID_REQUEST"}
        worker = str(Path(__file__).with_name("http_worker.py"))
        @contextmanager
        def authorized_launch():
            with launch_guard() if launch_guard else nullcontext(True) as allowed:
                if allowed:
                    # Waiting for another writer must not extend live authority.
                    self.authorize(self.config, time.time_ns() // 1000000)
                yield allowed
        return bounded_process([sys.executable, "-I", "-B", worker], payload, remaining,
                               profile.response_bytes * 6 + 2048, launch_guard=authorized_launch)


class ScriptedNativeTransport:
    mode = "OFFLINE_FIXTURE"

    def __init__(self, responses):
        self.responses = deepcopy(responses)
        self.calls = []

    def send(self, key, request, timeout_ms):
        self.calls.append({"key": key, "request": deepcopy(request), "timeout_ms": timeout_ms})
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)
