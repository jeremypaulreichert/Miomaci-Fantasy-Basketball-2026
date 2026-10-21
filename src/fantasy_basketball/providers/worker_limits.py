"""Shared process resource ceilings, distinct from injected run permissions.

Profiles still supply actual request/response/time limits. These ceilings only
bound what this worker implementation can support, never authorize a request.
"""
from ..core.config import integer

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TIMEOUT_MS = 60 * 60 * 1000
# A body is embedded as a JSON object, not a quoted second copy. An accepted
# 8,192-character printable credential can require 16,384 JSON bytes. Reserve
# additional bounded room for endpoint/limit fields; actual body caps stay fixed.
MAX_INPUT_BYTES = MAX_REQUEST_BYTES + 32768


def check_support(request_bytes, response_bytes, timeout_ms):
    for value, ceiling in ((request_bytes, MAX_REQUEST_BYTES),
                           (response_bytes, MAX_RESPONSE_BYTES), (timeout_ms, MAX_TIMEOUT_MS)):
        if integer(value, 1) > ceiling:
            raise ValueError("profile exceeds bounded worker support")
