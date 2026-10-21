"""Bounded research-pilot dispatch; inert without a separately frozen permit.

Credentials are supplied by an explicit callback after checks, never discovered.
No default permit, account scan, credential printing or competitive draft mode.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sys
import time

from ..core.config import canonical, digest, integer
from .native import endpoint_for
from .research_service import prepare_tool
from .transport import bounded_process


ROOT = Path(__file__).resolve().parents[3]
SOURCE_FILES = (
    "core/config.py", "core/budget.py", "core/journal.py", "core/request_cache.py", "core/replay_cache.py", "draft/board.py", "draft/native_plan.py", "draft/trial.py", "draft/timing.py",
    "draft/research.py", "providers/native.py", "providers/additional.py", "providers/fixtures.py",
    "providers/research.py", "providers/research_task.py", "providers/research_service.py", "providers/research_transport.py",
    "providers/research_worker.py", "providers/http_worker.py", "providers/transport.py", "providers/worker_limits.py")


def code_snapshot():
    paths = ["src/fantasy_basketball/" + name for name in SOURCE_FILES]
    paths += ["src/fantasy_basketball/__init__.py", "src/fantasy_basketball/core/__init__.py",
              "src/fantasy_basketball/draft/__init__.py", "src/fantasy_basketball/providers/__init__.py",
              "workstreams/build/private_key_setup.py", "workstreams/build/research_entry.py"]
    if any((ROOT / name).resolve() != (ROOT / name).absolute() for name in paths):
        raise ValueError("research sources must not redirect")
    return tuple((name, hashlib.sha256((ROOT / name).read_bytes()).hexdigest()) for name in sorted(paths))


LIVE_MODES = ("LIVE_RESEARCH_PILOT", "LIVE_COUNTED_DRAFT")
COUNTED_FILES = ("src/fantasy_basketball/providers/counted_task.py", "workstreams/build/counted_entry.py",
                 "workstreams/build/incident_recovery.py", "workstreams/build/quota_native.py",
                 "workstreams/build/quota_recovery.py", "workstreams/build/resilient_research.py")


def counted_snapshot():
    """Pilot inventory plus every counted runtime extension, in one sorted tuple."""
    if any((ROOT / name).resolve() != (ROOT / name).absolute() for name in COUNTED_FILES):
        raise ValueError("counted sources must not redirect")
    extra = tuple((name, hashlib.sha256((ROOT / name).read_bytes()).hexdigest()) for name in COUNTED_FILES)
    return tuple(sorted(code_snapshot() + extra))


def expected_sources(config):
    return counted_snapshot() if config.mode == "LIVE_COUNTED_DRAFT" else code_snapshot()


@dataclass(frozen=True)
class ResearchPermit:
    config_hash: str
    not_before_ms: int
    expires_ms: int
    budget_micro_usd: int
    max_model_requests: int
    max_service_requests: int
    reserved_service_credits: int
    owner_record: str
    review_record: str
    paid_execution_authorized: bool = False
    schema: str = "basketball-research-pilot-permit/1"

    def __post_init__(self):
        if (self.schema != "basketball-research-pilot-permit/1"
                or type(self.paid_execution_authorized) is not bool
                or not isinstance(self.config_hash, str) or not re.fullmatch("[0-9a-f]{64}", self.config_hash)):
            raise ValueError("research permit shape")
        for name in ("not_before_ms", "expires_ms", "budget_micro_usd", "max_model_requests", "max_service_requests", "reserved_service_credits"):
            integer(getattr(self, name))
        if self.not_before_ms >= self.expires_ms:
            raise ValueError("research permit window")
        for prefix, value in (("owner:", self.owner_record), ("review:", self.review_record)):
            if (not isinstance(value, str) or not value.startswith(prefix) or len(value) <= len(prefix)
                    or not re.fullmatch("[A-Za-z0-9_.:-]{1,100}", value)):
                raise ValueError("research attestation")

    def check_binding(self, config):
        if (config.mode != "LIVE_RESEARCH_PILOT" or self.paid_execution_authorized is not True
                or config.hash != self.config_hash
                or self.budget_micro_usd != config.budget_micro_usd
                or self.max_model_requests != config.max_model_calls * config.opportunities
                or self.max_service_requests != (config.max_searches + config.max_reads) * config.opportunities
                or self.reserved_service_credits != self.max_service_requests
                or config.sources != code_snapshot()):
            raise ValueError("research authority absent, changed or expired")

    def check(self, config, now_ms):
        integer(now_ms)
        self.check_binding(config)
        if not self.not_before_ms <= now_ms < self.expires_ms:
            raise ValueError("research authority absent, changed or expired")


@dataclass(frozen=True)
class CountedPermit:
    """Counted-draft authority. Never interchangeable with a pilot permit."""
    config_hash: str
    not_before_ms: int
    expires_ms: int
    budget_micro_usd: int
    max_model_requests: int
    max_service_requests: int
    reserved_service_credits: int
    owner_record: str
    review_record: str
    counted_record: str
    paid_execution_authorized: bool = False
    schema: str = "basketball-counted-draft-permit/1"

    def __post_init__(self):
        if (self.schema != "basketball-counted-draft-permit/1"
                or type(self.paid_execution_authorized) is not bool
                or not isinstance(self.config_hash, str) or not re.fullmatch("[0-9a-f]{64}", self.config_hash)):
            raise ValueError("counted permit shape")
        for name in ("not_before_ms", "expires_ms", "budget_micro_usd", "max_model_requests",
                     "max_service_requests", "reserved_service_credits"):
            integer(getattr(self, name))
        if self.not_before_ms >= self.expires_ms:
            raise ValueError("counted permit window")
        for prefix, value in (("owner:", self.owner_record), ("review:", self.review_record),
                              ("counted:", self.counted_record)):
            if (not isinstance(value, str) or not value.startswith(prefix) or len(value) <= len(prefix)
                    or not re.fullmatch("[A-Za-z0-9_.:-]{1,100}", value)):
                raise ValueError("counted attestation")

    def check_binding(self, config):
        from ..draft.research import counted_limit
        if (config.mode != "LIVE_COUNTED_DRAFT" or self.paid_execution_authorized is not True
                or config.hash != self.config_hash
                or self.budget_micro_usd != config.budget_micro_usd
                or self.max_model_requests != config.max_model_calls * config.opportunities
                or self.max_service_requests != (config.max_searches + config.max_reads) * config.opportunities
                or self.reserved_service_credits != self.max_service_requests
                or self.expires_ms > counted_limit(config)
                or config.sources != counted_snapshot()):
            raise ValueError("counted authority absent, changed or expired")

    def check(self, config, now_ms):
        integer(now_ms)
        self.check_binding(config)
        if not self.not_before_ms <= now_ms < self.expires_ms:
            raise ValueError("counted authority absent, changed or expired")


def permit_type(config):
    return CountedPermit if config.mode == "LIVE_COUNTED_DRAFT" else ResearchPermit


class LiveResearchTransport:
    mode = "LIVE_RESEARCH_PILOT"

    def __init__(self, config, permit, credential_provider):
        if type(permit) is not permit_type(config) or not callable(credential_provider):
            raise ValueError("explicit research permit and private credential callback required")
        self.config, self.permit, self.credential_provider = config, permit, credential_provider

    def authorize(self, now_ms):
        self.permit.check(self.config, now_ms)

    def send(self, key, kind, wire, timeout_ms, *, launch_guard=None, provider=None, discovered_urls=()):
        if launch_guard is None:
            raise ValueError("durable research startup guard required")
        now = time.time_ns() // 1000000
        self.authorize(now)
        integer(timeout_ms, 1)
        remaining = min(timeout_ms, self.permit.expires_ms - now)
        profiles = self.config.draft_plan.profiles if self.config.draft_plan else (self.config.profile,)
        if kind == "model":
            matches = [p for p in profiles if p.hash == wire.get("profile_hash") and p.provider == provider]
            if len(matches) != 1:
                raise ValueError("research profile binding")
            profile = matches[0]
            if (wire.get("schema") != "basketball-research-request/1"
                    or wire.get("endpoint") != endpoint_for(profile)
                    or wire.get("request_hash") != digest(wire["body"])
                    or wire.get("request_bytes") != len(canonical(wire["body"]).encode())
                    or wire["request_bytes"] > profile.request_bytes):
                raise ValueError("research model request binding")
            remaining = min(remaining, profile.timeout_ms, wire["deadline_ms"] - now)
            payload = {"endpoint": wire["endpoint"], "body": wire["body"], "timeout_ms": remaining,
                       "deadline_ms": now + remaining, "request_bytes": profile.request_bytes,
                       "response_bytes": profile.response_bytes}
            worker, output_cap, credential_name = "http_worker.py", profile.response_bytes * 6 + 2048, profile.provider
        elif kind in {"search", "read"}:
            if wire.get("action") != kind:
                raise ValueError("research service action binding")
            prepare_tool(wire, discovered_urls=discovered_urls, max_results=self.config.max_results,
                         timeout_ms=min(remaining, self.config.tool_timeout_ms))
            remaining = min(remaining, self.config.tool_timeout_ms)
            payload = {"action": wire, "discovered_urls": list(discovered_urls), "max_results": self.config.max_results,
                       "timeout_ms": remaining, "deadline_ms": now + remaining,
                       "response_bytes": self.config.service_response_bytes}
            worker, output_cap, credential_name = "research_worker.py", self.config.service_response_bytes * 6 + 2048, "tavily"
        else:
            raise ValueError("unknown research dispatch")
        if remaining <= 0:
            return {"outcome": "WORKER_TIMEOUT"}
        # This callback must be a noninteractive lookup of explicitly supplied
        # basketball entries. No prompting while a journal writer gate is held.
        with launch_guard() if launch_guard else nullcontext(True) as allowed:
            if not allowed:
                return {"outcome": "DISPATCH_SUPPRESSED"}
            self.authorize(time.time_ns() // 1000000)
            credential = self.credential_provider(credential_name)
        if (not isinstance(credential, str) or not 1 <= len(credential) <= 2560
                or any(not 33 <= ord(c) <= 126 for c in credential)):
            raise ValueError("research credential unavailable")
        payload["credential"] = credential
        document = canonical(payload).encode()
        payload.clear()
        credential = None
        if len(document) > (65536 if worker == "research_worker.py" else 4 * 1024 * 1024 + 32768):
            return {"outcome": "INVALID_REQUEST"}
        @contextmanager
        def authorized_launch():
            with launch_guard() if launch_guard else nullcontext(True) as allowed:
                if allowed:
                    self.authorize(time.time_ns() // 1000000)
                yield allowed
        return bounded_process([sys.executable, "-I", "-B", str(Path(__file__).with_name(worker))],
                               document, remaining, output_cap, launch_guard=authorized_launch)
