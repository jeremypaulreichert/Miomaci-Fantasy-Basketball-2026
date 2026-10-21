"""Freeze conservative full-pool byte bounds before creating a draft journal.

This is a serialization bound, not a tokenizer estimate or spending permission.
Existing profiles and journals are never modified in place.
"""
from dataclasses import replace

from ..core.config import digest, identifier, integer
from .native import prepare


def freeze_envelope(profile, board, *, max_picks, managers):
    """Return a new compact profile and the assumptions deriving its byte cap.

Two JSON layers expand one UTF-8 input byte by at most seven bytes (a
    control byte becomes a six-character escape and its backslash is escaped).
    Eight is deliberately conservative. Bound previous_text by the existing
    response_bytes allowance, even if no native envelope can contain that much
    text. Include all pool IDs and every possible ownership row independently;
    this overcounts overlapping legal/owned IDs instead of assuming monotonic
    legality. IDs/managers are validated ASCII identifiers with no JSON escapes.
    """
    integer(max_picks, 1)
    if board.ownership or board.pick_index != 0 or max_picks > len(board.players):
        raise ValueError("empty initial board and bounded draft size required")
    if type(managers) is not tuple or not managers or len(set(managers)) != len(managers):
        raise ValueError("frozen unique manager IDs required")
    for manager in managers:
        identifier(manager)
    if profile.provider not in managers:
        raise ValueError("profile manager absent from frozen cohort")
    measure = replace(profile, schema="basketball-native-profile/2", request_bytes=2**53 - 1)
    initial = replace(board, manager=profile.provider)
    base = prepare(measure, initial, now_ms=0, deadline_ms=profile.timeout_ms)["request_bytes"]
    empty_correction = {"failure": "MALFORMED_ACTION", "response_hash": "0" * 64, "previous_text": ""}
    corrected = prepare(measure, initial, now_ms=0, deadline_ms=profile.timeout_ms,
                        correction=empty_correction)["request_bytes"]
    max_id = max(len(p.player_id) for p in board.players)
    components = {
        "opening_bytes": base,
        "all_legal_ids_allowance": len(board.players) * (max_id + 8),
        "ownership_allowance": max_picks * (max_id + max(map(len, managers)) + 32),
        "case_manager_index_allowance": 512,
        "correction_metadata_bytes": corrected - base,
        "correction_text_allowance": 8 * profile.response_bytes,
    }
    cap = ((sum(components.values()) + 4095) // 4096) * 4096
    frozen = replace(profile, schema="basketball-native-profile/2", request_bytes=cap)
    evidence = {"schema": "basketball-request-envelope/1", "initial_board_hash": board.hash,
                "original_profile_hash": profile.hash, "frozen_profile_hash": frozen.hash,
                "max_picks": max_picks, "managers": managers, "components": components,
                "request_bytes": cap, "response_bytes": profile.response_bytes,
                "input_token_bound_changed": False, "spending_bound_changed": False,
                "execution_authorized": False,
                "scope": "Frozen pool/facts/slots/locks/source, up to max_picks ownership rows; canonical compact packet; original native correction policy and response bound."}
    assert frozen.bound_micro_usd == profile.bound_micro_usd
    evidence["envelope_hash"] = digest(evidence)
    return frozen, evidence
