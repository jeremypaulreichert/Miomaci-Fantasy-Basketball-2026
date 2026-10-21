"""Immutable local synthetic releases; corrections form a checked hash chain."""
import json
import os
from pathlib import Path
import tempfile

from ..data.local import InputError
from .engine import canonical, digest, evaluate


def _read_one(path):
    release = json.loads(path.read_text(encoding="utf-8"))
    if type(release) is not dict or set(release) != {"payload", "sha256"} or digest(release["payload"]) != release["sha256"]:
        raise InputError("release hash mismatch")
    return release


def _publish(source, destination):
    # Windows rename is atomic and refuses an existing destination. POSIX rename
    # overwrites, so use an exclusive hard link there instead.
    if os.name == "nt":
        os.rename(source, destination)
    else:
        os.link(source, destination)


def read_release(directory, version):
    if type(version) is not int or not 1 <= version <= 999999:
        raise InputError("invalid release version")
    previous, result = None, None
    for n in range(1, version + 1):
        result = _read_one(Path(directory) / f"release-{n:06d}.json")
        payload = result["payload"]
        if (payload["version"] != n or payload["previous_sha256"] != previous
                or payload["result"]["state"] != "COMPLETE"
                or payload["result"]["mode"] not in {"SYNTHETIC_OFFLINE", "HISTORICAL_OFFLINE_CANDIDATE"}):
            raise InputError("invalid release chain")
        previous = result["sha256"]
    return result


def save_release(directory, dataset, rosters, rules, *, previous_version=None, reason):
    """Recompute before writing; exclusive atomic publication prevents overwrites.

    One directory is one series. Revisions keep rules and rosters fixed and must
    name the immediate prior version. A crash before publication leaves no final
    file; a crash after leaves a complete file. Directory power-loss durability
    and hostile local-file tampering are outside this synthetic checkpoint.
    """
    if type(reason) is not str or not reason.strip() or len(reason) > 2000:
        raise InputError("explicit bounded release/correction reason required")
    result = evaluate(dataset, rosters, rules)
    if not result["final_release_eligible"]:
        raise InputError("incomplete expected outcomes or illegal lineup block release")
    directory = Path(directory)
    version, previous = 1, None
    if previous_version is not None:
        parent = read_release(directory, previous_version)
        old = parent["payload"]["result"]
        if old["rules_hash"] != result["rules_hash"] or old["rosters"] != result["rosters"]:
            raise InputError("corrections must preserve rules and roster context")
        version, previous = previous_version + 1, parent["sha256"]
    if version > 999999:
        raise InputError("release version limit")
    payload = {"schema": "basketball-local-release/1", "version": version,
               "previous_sha256": previous, "reason": reason, "result": result}
    release = {"payload": payload, "sha256": digest(payload)}
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"release-{version:06d}.json"
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(release) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _publish(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return release
