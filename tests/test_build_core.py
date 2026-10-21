"""Offline Build checks; all databases live under the basketball Build state root."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_basketball.core.config import ProviderLimits, RunConfig, canonical, digest
from fantasy_basketball.core.budget import charge, request_bound, reserve, totals
from fantasy_basketball.core.journal import Journal, STATE_ROOT
from fantasy_basketball.draft.opportunity import Draft
from fantasy_basketball.providers.fixtures import FixtureTransport, response


def configuration(**changes):
    limits = ProviderLimits("ai-a", "fixture-a", "max_output_tokens", 100, 4096,
                            "combined", 0, "1", "2", "3", 5, 7, 1000, 4096, 2)
    return replace(RunConfig("miomaci-basketball-v1", "test-run", (limits,), 1, 100000, 5000,
                             digest({"legality": "explicit-ids-only", "rules": "synthetic-test-v1"})), **changes)


class Clock:
    def __init__(self):
        self.wall = 10000
        self.monotonic = 0

    def now_ms(self):
        return self.wall

    def monotonic_ms(self):
        return self.monotonic

    def advance(self, milliseconds):
        self.wall += milliseconds
        self.monotonic += milliseconds


class BuildCase(unittest.TestCase):
    def setUp(self):
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = configuration()
        self.clock = Clock()

    def draft(self, config=None, **kwargs):
        return Draft(self.directory, config or self.config, clock=self.clock, **kwargs)


class ConfigBudgetTests(BuildCase):
    def test_exact_decimal_ceiling_and_reasoning_semantics(self):
        profile = self.config.providers[0]
        self.assertEqual(request_bound(profile), 4308)  # 4096*1 + 100*2 + 5 + 7
        separate = replace(profile, reasoning_mode="separate", separate_reasoning_tokens=30)
        self.assertEqual(request_bound(separate), 4398)  # additional 30*3
        self.assertEqual(charge(profile, 10, 20, 15), 62)  # reasoning already inside output
        fractional = replace(profile, input_usd_per_million="0.1", fixed_micro_usd=0, safety_micro_usd=0)
        self.assertEqual(charge(fractional, 3, 0, 0), 1)

    def test_reject_unknown_or_mutable_configuration(self):
        profile = self.config.providers[0]
        for changes in ({"output_tokens": True}, {"reasoning_mode": "unknown"},
                        {"separate_reasoning_tokens": 1}, {"input_usd_per_million": "NaN"},
                        {"output_usd_per_million": "Infinity"}, {"timeout_ms": 0},
                        {"input_usd_per_million": -1}, {"model": "real-model"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(profile, **changes)
        for changes in ({"providers": [profile]}, {"mode": "LIVE"}, {"experiment_id": "football-v1"},
                        {"correction_failures": ("UNKNOWN_DISPATCH",)}, {"roster_size": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                configuration(**changes)

    def test_request_and_reservation_share_changed_limits(self):
        profile = replace(self.config.providers[0], output_tokens=250, reasoning_mode="separate",
                          separate_reasoning_tokens=40, timeout_ms=700, output_field="max_completion_tokens")
        config = configuration(providers=(profile,), opportunity_ms=600)
        draft = self.draft(config)
        draft.open(["p1"])
        transport = FixtureTransport({"pick:0:attempt:0": response(profile, "p1")})
        result = draft.resume(transport)
        wire = transport.calls[0]["request"]["body"]
        self.assertEqual(wire["max_completion_tokens"], 250)
        self.assertEqual(wire["reasoning"]["separate_token_allowance"], 40)
        self.assertEqual(transport.calls[0]["timeout_ms"], 600)
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4728)
        self.assertEqual(result["accounting"]["invoice_confirmed_micro_usd"], 0)
        self.assertEqual(result["accounting"]["estimated_charge_micro_usd"], 102)

    def test_snake_size_is_derived(self):
        second = replace(self.config.providers[0], manager="ai-b", model="fixture-b")
        config = configuration(providers=(self.config.providers[0], second), roster_size=3)
        self.assertEqual(config.total_picks, 6)
        self.assertEqual([config.at_pick(i).manager for i in range(6)], ["ai-a", "ai-b", "ai-b", "ai-a", "ai-a", "ai-b"])

    def test_budget_protects_all_remaining_attempts(self):
        draft = self.draft(configuration(budget_micro_usd=4308))
        draft.open(["p1"])
        transport = FixtureTransport({})
        result = draft.resume(transport)
        self.assertEqual(result["pause"]["reason"], "BUDGET")
        self.assertEqual(transport.calls, [])
        self.assertEqual(result["accounting"]["committed_micro_usd"], 0)


def ledger_reduce(state, kind, data, config):
    if kind != "RESERVE":
        raise ValueError("unknown event")
    reserve(state, data["key"], config.providers[0], config)


class JournalTests(BuildCase):
    def test_concurrent_reservations_cannot_overspend(self):
        config = configuration(budget_micro_usd=5000)
        def attempt(index):
            journal = Journal(self.directory, config, ledger_reduce, dict, schema="ledger-test/1")
            try:
                journal.transact(lambda state: [("RESERVE", {"key": str(index)})])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(sum(pool.map(attempt, range(10))), 1)
        journal = Journal(self.directory, config, ledger_reduce, dict, schema="ledger-test/1")
        self.assertEqual(totals(journal.read())["committed_micro_usd"], 4308)

    def test_same_reservation_key_is_unique(self):
        def attempt(_):
            journal = Journal(self.directory, self.config, ledger_reduce, dict, schema="ledger-test/1")
            try:
                journal.transact(lambda state: [("RESERVE", {"key": "same"})])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(attempt, range(5))), 1)

    def test_foreign_path_and_database_are_rejected(self):
        with self.assertRaises(ValueError):
            Draft(self.directory.parent.parent, self.config)
        path = self.directory / "test-run.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE foreign_state (value TEXT)")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.draft()
        self.assertEqual(path.read_bytes(), before)

    def test_config_and_namespace_drift_rejected(self):
        self.draft().open(["p1"])
        for changes in ({"budget_micro_usd": 200000}, {"opportunity_ms": 7000},
                        {"experiment_id": "miomaci-basketball-other"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.draft(configuration(**changes))

    def test_events_are_immutable_and_tampering_detected(self):
        draft = self.draft()
        draft.open(["p1"])
        with closing(sqlite3.connect(draft.journal.path)) as db, db:
            for sql in ("DELETE FROM events", "UPDATE events SET hash='bad'", "UPDATE binding SET value='bad'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
            # Simulate privileged tampering; hash verification must still fail.
            db.execute("DROP TRIGGER immutable_events_UPDATE")
            db.execute("UPDATE events SET hash='bad'")
        with self.assertRaises(ValueError):
            self.draft()


class RecoveryTests(BuildCase):
    def test_concurrent_resume_does_not_duplicate_remote_opportunity(self):
        entered, release = threading.Event(), threading.Event()
        class WaitingFixture(FixtureTransport):
            def send(self, *args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test synchronization timeout")
                return super().send(*args)
        draft = self.draft()
        draft.open(["p1"])
        transport = WaitingFixture({"pick:0:attempt:0": response(self.config.providers[0], "p1")})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(draft.resume, transport)
            try:
                self.assertTrue(entered.wait(5))
                second_transport = FixtureTransport({})
                second = self.draft().resume(second_transport)
                self.assertEqual(second["pause"]["reason"], "UNKNOWN_DISPATCH")
                self.assertEqual(second_transport.calls, [])
            finally:
                release.set()
            result = first.result(timeout=5)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["picks"], [])
        state = self.draft().journal.read()
        self.assertIsNotNone(state["opportunities"][0]["attempts"][0]["response"])
        self.assertEqual(state["pause"]["reason"], "UNKNOWN_DISPATCH")

    def test_deadline_can_expire_after_intent_before_send(self):
        def delay(point):
            if point == "after:DISPATCH":
                self.clock.advance(5000)
        draft = self.draft(fault=delay)
        draft.open(["p1"])
        transport = FixtureTransport({})
        result = draft.resume(transport)
        self.assertEqual(result["pause"]["reason"], "DEADLINE")
        self.assertEqual(transport.calls, [])
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4308)

    def test_monotonic_budget_includes_persistence_time_despite_wall_shift(self):
        def persistence_delay(point):
            if point == "after:DISPATCH":
                self.clock.monotonic += 600
        clock = self.clock
        class LateFixture(FixtureTransport):
            def send(self, *args):
                clock.monotonic += 500
                clock.wall += 500
                return super().send(*args)
        draft = self.draft(fault=persistence_delay)
        draft.open(["p1"])
        transport = LateFixture({"pick:0:attempt:0": response(self.config.providers[0], "p1")})
        result = draft.resume(transport)
        self.assertEqual(transport.calls[0]["timeout_ms"], 400)
        self.assertEqual(result["pause"]["reason"], "LATE_RESPONSE")
        self.assertEqual(result["picks"], [])

    def test_input_and_response_size_bounds_pause(self):
        tiny = replace(self.config.providers[0], input_tokens=10)
        config = configuration(providers=(tiny,), run_id="tiny-input")
        draft = self.draft(config)
        draft.open(["p1"])
        transport = FixtureTransport({})
        self.assertEqual(draft.resume(transport)["pause"]["reason"], "INPUT_BOUND")
        self.assertEqual(transport.calls, [])
        draft = self.draft()
        draft.open(["p1"])
        raw = response(self.config.providers[0], "p1", text="x" * 5000)
        result = draft.resume(FixtureTransport({"pick:0:attempt:0": raw}))
        self.assertEqual(result["pause"]["reason"], "RESPONSE_TOO_LARGE")
        self.assertEqual(result["accounting"]["estimated_charge_micro_usd"], 0)

    def test_correction_can_be_disabled(self):
        config = configuration(correction_failures=())
        draft = self.draft(config)
        draft.open(["p1"])
        transport = FixtureTransport({"pick:0:attempt:0": response(config.providers[0], "other")})
        result = draft.resume(transport)
        self.assertEqual(result["pause"]["reason"], "ILLEGAL_PICK")
        self.assertEqual(len(transport.calls), 1)

    def test_valid_pick_survives_resume_without_dispatch(self):
        draft = self.draft()
        draft.open(["p1", "p2"])
        transport = FixtureTransport({"pick:0:attempt:0": response(self.config.providers[0], "p2")})
        settled = draft.resume(transport)
        self.assertEqual(settled["picks"][0]["player_id"], "p2")
        self.assertEqual(settled["picks"][0]["origin"], "SIMULATED_AI")
        unused = FixtureTransport({})
        self.assertEqual(self.draft().resume(unused), settled)
        self.assertEqual(unused.calls, [])

    def test_one_same_ai_correction_keeps_packet_and_original_evidence(self):
        draft = self.draft()
        draft.open(["p1"])
        profile = self.config.providers[0]
        transport = FixtureTransport({"pick:0:attempt:0": response(profile, "owned"),
                                      "pick:0:attempt:1": response(profile, "p1")})
        result = draft.resume(transport)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(len(transport.calls), 2)
        first, second = [c["request"] for c in transport.calls]
        self.assertEqual(first["packet_hash"], second["packet_hash"])
        self.assertEqual(first["body"]["model"], second["body"]["model"])
        self.assertEqual(second["body"]["input"]["correction"]["failure"], "ILLEGAL_PICK")
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 8616)
        self.assertEqual(result["picks"][0]["attempt"], 1)
        self.assertEqual(len(draft.journal.read()["opportunities"][0]["attempts"]), 2)

    def test_invalid_second_answer_pauses_durably(self):
        draft = self.draft()
        draft.open(["p1"])
        bad = response(self.config.providers[0], "p1", text='{"player_id":"p1","player_id":"p2","rationale":"x"}')
        transport = FixtureTransport({"pick:0:attempt:0": bad, "pick:0:attempt:1": bad})
        result = draft.resume(transport)
        self.assertEqual(result["pause"]["reason"], "MALFORMED_ACTION")
        self.assertEqual(result["picks"], [])
        unused = FixtureTransport({})
        self.assertEqual(self.draft().resume(unused), result)
        self.assertEqual(unused.calls, [])
        with self.assertRaises(ValueError):
            self.draft().open(["p2"])

    def test_unknown_transport_retains_exposure_without_error_text(self):
        draft = self.draft()
        draft.open(["p1"])
        transport = FixtureTransport({"pick:0:attempt:0": RuntimeError("sensitive-error-text")})
        result = draft.resume(transport)
        self.assertEqual(result["pause"]["reason"], "UNKNOWN_DISPATCH")
        self.assertEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4308)
        self.assertNotIn(b"sensitive-error-text", draft.journal.path.read_bytes())

    def test_missing_usage_identity_hold_and_exhaustion_never_pick(self):
        profile = self.config.providers[0]
        cases = []
        for field, value, expected in (("usage", None, "MISSING_OR_INVALID_USAGE"),
                                       ("model", "fixture-other", "IDENTITY_MISMATCH"),
                                       ("stop", "length", "INCOMPLETE_RESPONSE")):
            raw = response(profile, "p1")
            raw[field] = value
            cases.append((raw, expected))
        cases.append((response(profile, None), "AI_HOLD"))
        cases.append((response(profile, "p1", output_tokens=10000), "USAGE_BOUND_EXCEEDED"))
        for index, (raw, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                config = configuration(run_id=f"failure-{index}")
                draft = self.draft(config)
                draft.open(["p1"])
                result = draft.resume(FixtureTransport({"pick:0:attempt:0": raw}))
                self.assertEqual(result["pause"]["reason"], expected)
                self.assertEqual(result["picks"], [])
                self.assertGreaterEqual(result["accounting"]["unresolved_exposure_micro_usd"], 4308)

    def test_deadline_and_timeout_equality_rejected(self):
        profile = self.config.providers[0]
        clock = self.clock
        class SlowFixture(FixtureTransport):
            def send(self, *args):
                clock.advance(1000)
                return super().send(*args)
        draft = self.draft()
        draft.open(["p1"])
        result = draft.resume(SlowFixture({"pick:0:attempt:0": response(profile, "p1")}))
        self.assertEqual(result["pause"]["reason"], "LATE_RESPONSE")
        self.assertEqual(result["picks"], [])
        self.assertIsNotNone(draft.journal.read()["opportunities"][0]["attempts"][0]["response"])

    def test_restart_cannot_reset_deadline(self):
        draft = self.draft()
        draft.open(["p1"])
        self.clock.advance(5000)
        transport = FixtureTransport({})
        result = self.draft().resume(transport)
        self.assertEqual(result["pause"]["reason"], "DEADLINE")
        self.assertEqual(transport.calls, [])

    def test_clock_rollback_pauses(self):
        draft = self.draft()
        draft.open(["p1"])
        self.clock.wall -= 1
        result = draft.resume(FixtureTransport({}))
        self.assertEqual(result["pause"]["reason"], "CLOCK_ROLLBACK")

    def test_four_pick_fixture_draft(self):
        second = replace(self.config.providers[0], manager="ai-b", model="fixture-b")
        config = configuration(providers=(self.config.providers[0], second), roster_size=2)
        available = ["p1", "p2", "p3", "p4"]
        draft = self.draft(config)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            for index in range(4):
                draft.open(available)
                selected = available.pop()
                result = draft.resume(FixtureTransport({f"pick:{index}:attempt:0": response(config.at_pick(index), selected)}))
                draft = self.draft(config)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual([p["manager"] for p in result["picks"]], ["ai-a", "ai-b", "ai-b", "ai-a"])
        self.assertEqual(len({p["player_id"] for p in result["picks"]}), 4)
        self.assertEqual(result["accounting"]["invoice_confirmed_micro_usd"], 0)
        with self.assertRaises(ValueError):
            draft.open(["p5"])


if __name__ == "__main__":
    unittest.main()
