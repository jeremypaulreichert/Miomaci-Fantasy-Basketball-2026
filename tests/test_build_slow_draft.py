"""Full-pool bounds, cache isolation/integrity and honest durable timing."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workstreams/build"))
from native_draft_check import configuration, replies, scale_board
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.draft.trial import NativeTrial, RecoveryDecision
from fantasy_basketball.providers.envelope import freeze_envelope
from fantasy_basketball.providers.native import prepare
from fantasy_basketball.providers.qualification import candidate_profiles
from fantasy_basketball.providers.transport import ScriptedNativeTransport


class CacheTests(support.BuildCase):
    def test_cached_board_keeps_prefix_validation_and_branch_isolation(self):
        config = configuration()
        first = {"case_index": 0, "case_id": "pick-0", "profile_id": config.profiles[0].profile_id,
                 "action": {"player_id": "p4"}}
        original = config.board_at(1, [first])
        before = canonical(asdict(config))
        self.assertIs(config.board_at(1, [first]), original)
        alternate = config.board_at(1, [{**first, "action": {"player_id": "p1"}}])
        self.assertNotEqual(original.ownership, alternate.ownership)
        self.assertIn("p4", alternate.legal_ids())
        self.assertNotIn("p4", original.legal_ids())
        for field, value in (("case_index", 1), ("case_id", "wrong"), ("profile_id", "wrong")):
            with self.assertRaises(ValueError):
                config.board_at(1, [{**first, field: value}])
        self.assertEqual(canonical(asdict(config)), before)
        self.assertEqual(config.hash, digest(asdict(config)))

    def test_bounded_cache_handles_concurrent_different_prefixes(self):
        from fantasy_basketball.draft.native_plan import NativeDraftConfig
        board = scale_board()
        profiles = tuple(replace(p, schema="basketball-native-profile/2") for p in candidate_profiles()[::2])
        config = NativeDraftConfig("cache", profiles, board, 1000000000, 10000000, "1" * 64, "2" * 64)
        def branch(i):
            first = {"case_index": 0, "case_id": "pick-0", "profile_id": profiles[0].profile_id,
                     "action": {"player_id": f"p{i:04d}"}}
            return config.board_at(1, [first]).ownership
        with ThreadPoolExecutor(max_workers=8) as pool:
            actual = list(pool.map(branch, range(300)))
        self.assertEqual(actual, [((f"p{i:04d}", "openai"),) for i in range(300)])
        self.assertLessEqual(len(config._board_cache[0]), 256)

    def test_warm_cache_does_not_hide_modified_journal_even_with_rehashed_event(self):
        config = configuration()
        trial = NativeTrial(self.directory, config, clock=self.clock)
        state = trial.resume(ScriptedNativeTransport(replies(config)))
        self.assertEqual(trial.status(), state)  # warm immutable board cache
        with closing(sqlite3.connect(trial.journal.path)) as db, db:
            db.execute("DROP TRIGGER immutable_events_UPDATE")
            sequence, document = db.execute("SELECT sequence, document FROM events WHERE document LIKE '%RESERVE%' ORDER BY sequence LIMIT 1").fetchone()
            event = json.loads(document)
            event["data"]["native_request"]["body"]["model"] = "tampered-model"
            db.execute("UPDATE events SET document=?, hash=? WHERE sequence=?", (canonical(event), digest(event), sequence))
        with self.assertRaisesRegex(ValueError, "native request differs"):
            trial.status()
        with self.assertRaises(ValueError):
            trial.timing_report()


class EnvelopeTests(support.BuildCase):
    def test_full_pool_and_direct_maximum_correction_strings_fit_frozen_cap(self):
        board = scale_board()
        for original in candidate_profiles()[::2]:
            frozen, evidence = freeze_envelope(original, board, max_picks=130,
                                               managers=("openai", "deepseek", "mistral", *[f"seat-{i}" for i in range(7)]))
            self.assertEqual(original.request_bytes, 24000)
            self.assertEqual(original.bound_micro_usd, frozen.bound_micro_usd)
            self.assertEqual(evidence["envelope_hash"], digest({k: v for k, v in evidence.items() if k != "envelope_hash"}))
            opening = replace(board, manager=frozen.provider)
            owned = tuple((f"p{i:04d}", "seat-0") for i in range(129))
            late = replace(opening, pick_index=129, case_id="x" * 100, ownership=owned)
            for current in (opening, late):
                for char in ('x', '"', '\\', '\x01', '雪'):
                    text = char * (frozen.response_bytes // len(char.encode()))
                    wire = prepare(frozen, current, now_ms=0, deadline_ms=120000,
                                   correction={"failure": "MALFORMED_ACTION", "response_hash": "0" * 64, "previous_text": text})
                    self.assertLessEqual(wire["request_bytes"], frozen.request_bytes)
                    self.assertFalse(wire["execution_authorized"])

    def test_envelope_rejects_unfrozen_initial_context_and_existing_journal_drift(self):
        config = configuration()
        profile = config.profiles[0]
        for changes in ({"max_picks": 0}, {"max_picks": 5}, {"managers": ["openai"]},
                        {"managers": ("deepseek",)}, {"managers": ("openai", "openai")}):
            kwargs = {"max_picks": 4, "managers": ("openai", "deepseek"), **changes}
            with self.assertRaises(ValueError):
                freeze_envelope(profile, config.board, **kwargs)
        NativeTrial(self.directory, config, clock=self.clock)
        frozen, _ = freeze_envelope(profile, config.board, max_picks=4, managers=("openai", "deepseek"))
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            NativeTrial(self.directory, replace(config, profiles=(frozen, config.profiles[1])), clock=self.clock)


class TimingTests(support.BuildCase):
    def transport(self, config, *, correction=False):
        transport = ScriptedNativeTransport(replies(config, correction=correction))
        send = transport.send
        def measured(*args, **kwargs):
            self.clock.advance(7)
            return send(*args, **kwargs)
        transport.send = measured
        return transport

    def test_corrections_and_failed_attempts_are_counted_and_restart_projection_is_read_only(self):
        config = configuration()
        trial = NativeTrial(self.directory, config, clock=self.clock)
        trial.resume(self.transport(config, correction=True))
        before = trial.journal.path.read_bytes()
        report = trial.timing_report()
        self.assertEqual([r["attempt_count"] for r in report["picks"]], [1, 2, 1, 1])
        self.assertEqual(report["picks"][1]["valid_response_since_open_ms"], 14)
        self.assertEqual(report["picks"][1]["open_to_commit_wall_ms_including_pauses"], 14)
        self.assertEqual(report["picks"][1]["attempts"][0]["validation_failure"], "ILLEGAL_PICK")
        self.assertEqual(sum(r["known_attempt_elapsed_ms"] for r in report["picks"]), 35)
        self.assertEqual(report, NativeTrial(self.directory, config, clock=self.clock).timing_report())
        self.assertEqual(before, trial.journal.path.read_bytes())
        self.assertEqual(report["report_hash"], digest({k: v for k, v in report.items() if k != "report_hash"}))
        self.assertNotIn("answer_text", canonical(report))

    def test_dispatch_persistence_overhead_is_separate_from_transport_call(self):
        config = configuration()
        def overhead(point):
            if point == "after:DISPATCH":
                self.clock.advance(3)
        trial = NativeTrial(self.directory, config, clock=self.clock, fault=overhead)
        trial.resume(self.transport(config))
        attempt = trial.timing_report()["picks"][0]["attempts"][0]
        self.assertEqual(attempt["elapsed_ms_including_dispatch_overhead"], 10)
        self.assertEqual(attempt["transport_call_elapsed_ms_including_startup"], 7)

    def test_unknown_attempt_duration_is_null_not_zero_and_prior_picks_remain(self):
        config = configuration()
        def stop(point):
            if point == "after:DISPATCH" and len(trial.journal.read()["results"]) == 1:
                raise InterruptedError("simulated controller stop")
        trial = NativeTrial(self.directory, config, clock=self.clock, fault=stop)
        with self.assertRaises(InterruptedError):
            trial.resume(self.transport(config))
        reopened = NativeTrial(self.directory, config, clock=self.clock)
        unused = ScriptedNativeTransport({})
        reopened.resume(unused)
        report = reopened.timing_report()
        self.assertEqual(report["pause"]["reason"], "UNKNOWN_DISPATCH")
        self.assertEqual(len(report["picks"]), 2)
        self.assertIsNone(report["picks"][1]["attempts"][0]["elapsed_ms_including_dispatch_overhead"])
        self.assertFalse(report["picks"][1]["all_attempt_durations_known"])
        self.assertEqual(report["managers"]["deepseek"]["attempts_without_saved_response"], 1)
        self.assertFalse(unused.calls)

    def test_saved_response_recovery_separates_response_from_human_delay(self):
        config = configuration()
        trial = NativeTrial(self.directory, config, clock=self.clock)
        transport = self.transport(config)
        send = transport.send
        def racing(*args, **kwargs):
            NativeTrial(self.directory, config, clock=self.clock).resume(ScriptedNativeTransport({}))
            return send(*args, **kwargs)
        transport.send = racing
        trial.resume(transport)
        self.clock.advance(1000)
        decision = RecoveryDecision("timing-settle", config.hash, trial.explain_pause()["state_hash"], "SETTLE_SAVED", "TEST_OWNER")
        trial.recover(decision)
        pick = trial.timing_report()["picks"][0]
        self.assertEqual(pick["valid_response_since_open_ms"], 7)
        self.assertEqual(pick["open_to_commit_wall_ms_including_pauses"], 1007)

    def test_legacy_events_keep_transport_duration_unknown_without_state_hash_change(self):
        config = configuration()
        trial = NativeTrial(self.directory, config, clock=self.clock)
        trial.resume(self.transport(config))
        state, events = trial.journal.read_with_events()
        from fantasy_basketball.draft.trial import initial, reduce_event
        legacy = initial()
        for event in events:
            data = {k: v for k, v in event["data"].items() if k != "transport_elapsed_ms"}
            reduce_event(legacy, event["kind"], data, config)
            event["data"] = data
        self.assertEqual(legacy, state)
        from fantasy_basketball.draft.timing import report
        self.assertTrue(all(a["transport_call_elapsed_ms_including_startup"] is None
                            for row in report(config, legacy, events)["picks"] for a in row["attempts"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
