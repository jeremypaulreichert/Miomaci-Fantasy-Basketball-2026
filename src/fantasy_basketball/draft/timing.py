"""Read-only timing projection, including failures and incomplete attempts."""
from ..core.config import digest


def report(config, state, events):
    opportunities = [*state["history"], *([state["op"]] if state["op"] else [])]
    transport_times, committed = {}, {}
    index, attempt = -1, -1
    for event in events:
        kind, data = event["kind"], event["data"]
        if kind == "OPEN":
            index += 1
            attempt = -1
        elif kind == "RESERVE":
            attempt += 1
        elif kind == "RESPONSE":
            transport_times[(index, attempt)] = data.get("transport_elapsed_ms")
        elif kind == "RESOLVE" or (kind == "RECOVER" and data["decision"]["action"] == "SETTLE_SAVED"):
            op = opportunities[index]
            if (index < len(state["results"]) and attempt == len(op["attempts"]) - 1
                    and op["attempts"][-1]["receipt"] and op["attempts"][-1]["receipt"]["failure"] is None):
                committed[index] = data["at_ms"]
    rows = []
    for op in opportunities:
        case = config.cases[op["index"]]
        settled = op["index"] < len(state["results"])
        attempts = []
        for number, a in enumerate(op["attempts"]):
            receipt = a["receipt"]
            attempts.append({"key": a["key"], "state": a["state"], "correction": number > 0,
                             "dispatched_ms": a.get("dispatched_ms"), "received_ms": a.get("received_ms"),
                             "elapsed_ms_including_dispatch_overhead": a.get("elapsed_ms"),
                             "transport_call_elapsed_ms_including_startup": transport_times.get((op["index"], number)),
                             "outcome": a["response"]["outcome"] if a["response"] else "NO_SAVED_RESPONSE",
                             "validation_failure": receipt["failure"] if receipt else None})
        finish = committed.get(op["index"])
        last = op["attempts"][-1] if op["attempts"] else None
        known = [a["elapsed_ms_including_dispatch_overhead"] for a in attempts
                 if a["elapsed_ms_including_dispatch_overhead"] is not None]
        rows.append({"pick_index": op["index"], "manager": case.profile.provider,
                     "profile_id": case.profile.profile_id, "settled": settled,
                     "opened_ms": op["opened_ms"], "committed_ms": finish,
                     "valid_response_since_open_ms": last["received_ms"] - op["opened_ms"] if settled else None,
                     "open_to_commit_wall_ms_including_pauses": finish - op["opened_ms"] if finish is not None else None,
                     "known_attempt_elapsed_ms": sum(known), "all_attempt_durations_known": len(known) == len(attempts),
                     "attempt_count": len(attempts), "correction_count": max(0, len(attempts) - 1), "attempts": attempts})
    managers = {}
    for row in rows:
        total = managers.setdefault(row["manager"], {"opened_picks": 0, "settled_picks": 0, "attempts": 0,
                                                   "corrections": 0, "attempts_without_saved_response": 0,
                                                   "known_attempt_elapsed_ms": 0})
        total["opened_picks"] += 1
        total["settled_picks"] += int(row["settled"])
        total["attempts"] += row["attempt_count"]
        total["corrections"] += row["correction_count"]
        total["attempts_without_saved_response"] += sum(a["received_ms"] is None for a in row["attempts"])
        total["known_attempt_elapsed_ms"] += row["known_attempt_elapsed_ms"]
    result = {"schema": "basketball-draft-timing/1", "config_hash": config.hash,
              "run_id": config.run_id, "simulated": config.mode == "OFFLINE_FIXTURE",
              "pause": state["pause"], "picks": rows, "managers": managers,
              "interpretation": "Operational timing, not hidden thinking or intelligence. Attempt elapsed includes local dispatch overhead; transport call includes worker startup and network/provider time. Open-to-commit includes any human downtime. Missing durations remain unknown; unfinished and failed attempts are retained. Historical events have no transport-call measurement."}
    result["report_hash"] = digest(result)
    return result
