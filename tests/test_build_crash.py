"""Kill actual child processes at transaction/dispatch boundaries, then resume."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

from test_build_core import BuildCase, Clock, configuration
from fantasy_basketball.draft.opportunity import Draft
from fantasy_basketball.providers.fixtures import FixtureTransport, response
from fantasy_basketball.core.config import canonical


BOUNDARIES = {
    "before_commit:OPEN": (0, 1, "COMPLETE"),
    "after:OPEN": (0, 1, "COMPLETE"),
    "before_commit:ATTEMPT": (0, 1, "COMPLETE"),
    "after:ATTEMPT": (4308, 1, "COMPLETE"),
    "before_commit:DISPATCH": (4308, 1, "COMPLETE"),
    "after:DISPATCH": (4308, 0, "PAUSED"),
    "before_commit:RESPONSE": (4308, 0, "PAUSED"),
    "after:RESPONSE": (4308, 0, "COMPLETE"),
    "before_commit:RESOLVE": (4308, 0, "COMPLETE"),
    "after:RESOLVE": (4308, 0, "COMPLETE"),
}


class ProcessRecoveryTests(BuildCase):
    def test_export_bytes_survive_process_exit_after_settlement(self):
        child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--crash", "export-settled", str(self.directory)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 23, child.stderr)
        before = (self.directory / "before-crash.json").read_bytes()
        draft = self.draft()
        unused = FixtureTransport({})
        draft.resume(unused)
        self.assertEqual(canonical(draft.export_roster()).encode("utf-8"), before)
        self.assertEqual(unused.calls, [])

    def test_correction_response_replays_without_repeating_first_call(self):
        self.check_correction("correction-resolved")

    def test_reserved_correction_survives_restart(self):
        self.check_correction("correction-reserved")

    def check_correction(self, boundary):
        child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--crash", boundary, str(self.directory)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 23, child.stderr)
        draft = self.draft()
        transport = FixtureTransport({"pick:0:attempt:1": response(self.config.providers[0], "p1")})
        result = draft.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual([call["key"] for call in transport.calls], ["pick:0:attempt:1"])
        self.assertEqual(result["picks"][0]["attempt"], 1)
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 8616)

    def check_boundary(self, boundary):
        child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--crash", boundary, str(self.directory)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 23, child.stderr)
        committed, calls, phase = BOUNDARIES[boundary]
        draft = self.draft()
        self.assertEqual(draft.status()["accounting"]["committed_micro_usd"], committed)
        if not draft.journal.read()["opportunities"]:
            draft.open(["p1"])
        transport = FixtureTransport({"pick:0:attempt:0": response(self.config.providers[0], "p1")})
        result = draft.resume(transport)
        self.assertEqual(len(transport.calls), calls)
        self.assertEqual(result["phase"], phase)
        if phase == "PAUSED":
            self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH")
            self.assertEqual(result["picks"], [])
            self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4308)
        else:
            self.assertEqual(len(result["picks"]), 1)
        unused = FixtureTransport({})
        self.assertEqual(self.draft().resume(unused), result)
        self.assertEqual(unused.calls, [])


for boundary in BOUNDARIES:
    def check(self, boundary=boundary):
        self.check_boundary(boundary)
    setattr(ProcessRecoveryTests, "test_" + boundary.replace(":", "_"), check)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--crash":
    boundary, directory = sys.argv[2:]
    attempts = 0
    def terminate(point):
        global attempts
        if boundary == "export-settled" and point == "after:RESOLVE":
            (Path(directory) / "before-crash.json").write_bytes(canonical(draft.export_roster()).encode("utf-8"))
            os._exit(23)
        if point == "after:ATTEMPT":
            attempts += 1
        if boundary == "correction-resolved" and point == "after:RESOLVE":
            os._exit(23)
        if boundary == "correction-reserved" and point == "after:ATTEMPT" and attempts == 2:
            os._exit(23)
        if point == boundary:
            os._exit(23)
    config = configuration()
    draft = Draft(directory, config, clock=Clock(), fault=terminate)
    draft.open(["p1"])
    first_choice = "not-legal" if boundary.startswith("correction-") else "p1"
    draft.resume(FixtureTransport({"pick:0:attempt:0": response(config.providers[0], first_choice),
                                   "pick:0:attempt:1": response(config.providers[0], "p1")}))
    raise SystemExit("crash boundary was not reached")
elif __name__ == "__main__":
    unittest.main()
