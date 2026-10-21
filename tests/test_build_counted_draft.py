"""Counted-draft namespace, request-specific bounds and renewal. Offline fixtures only."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "workstreams/build"
sys.path[:0] = [str(ROOT / "src"), str(BUILD)]

from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.core.journal import STATE_ROOT
from fantasy_basketball.draft.research import (
    BYTE_BOUND_OVERHEAD_TOKENS, ResearchConfig, ScriptedResearchTransport, call_bound, ceiling_excess, make_intent)
from fantasy_basketball.providers.qualification import synthetic_response
from fantasy_basketball.providers.research_transport import CountedPermit, ResearchPermit, counted_snapshot
from incident_acceptance_check import Clock
from prepare_counted_draft import FIRST_TIPOFF_MS, build_config, load_profiles, to_byte_basis
from quota_native import NativeQuotaOpportunity
from quota_recovery import quota_initial
from research_integration_check import action

PILOT_PACKAGE = BUILD / "LIVE_RESEARCH_PILOT_SESSION_2026-10-18/package.json"
SEED = "0123456789abcdef" * 4
CAP = 430080


def profiles(byte=True):
    base = load_profiles(PILOT_PACKAGE)
    return tuple(to_byte_basis(p, CAP) for p in base) if byte else base


def offline(**kw):
    kw.setdefault("excess_reserve_calls", 2)
    return build_config(profiles(), seed=SEED, live=False, run_id="research-incident-quota-counted-test", **kw)


def live(**kw):
    return build_config(profiles(), seed=SEED, live=True, run_id="research-incident-quota-counted-live-test", **kw)


def native(profile, act, *, input_tokens=None):
    raw = synthetic_response(profile, act.get("player_id"), text=canonical(act))
    if input_tokens is not None:
        if profile.provider in {"openai", "xai", "anthropic"}:
            raw["usage"]["input_tokens"] = input_tokens
            if profile.provider != "anthropic":
                raw["usage"]["total_tokens"] = input_tokens + raw["usage"]["output_tokens"]
        elif profile.provider == "gemini":
            u = raw["usageMetadata"]; u["totalTokenCount"] += input_tokens - u["promptTokenCount"]; u["promptTokenCount"] = input_tokens
        else:
            u = raw["usage"]; u["total_tokens"] += input_tokens - u["prompt_tokens"]; u["prompt_tokens"] = input_tokens
            if profile.provider == "deepseek":
                u["prompt_cache_miss_tokens"] = input_tokens
    return {"outcome": "RESPONSE", "raw": raw}


class Runner:
    def __init__(self, cfg):
        self.tmp = tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="counted-test-")
        self.cfg, self.clock = cfg, Clock()
        self.op = NativeQuotaOpportunity(Path(self.tmp.name), cfg, clock=self.clock)

    def run(self, results, steps=60):
        transport = ScriptedResearchTransport(results)
        with patch("socket.socket", side_effect=AssertionError("offline only")):
            for _ in range(steps):
                state = self.op.step(transport)
                if state["retry_candidate"]:
                    self.clock.wall = max(self.clock.wall, state["retry_candidate"]["not_before_ms"])
                if state["phase"] == "PAUSED" and not any(state[k] for k in ("quota_candidate", "retry_candidate", "incident_candidate")):
                    break
                if state["phase"] in {"COMPLETE", "PICK_SAVED"} and len(state["results"]) >= 1:
                    break
        return transport, state

    def close(self):
        self.tmp.cleanup()


class CountedNamespaceTests(unittest.TestCase):
    def test_counted_live_config_binds_sources_cutoff_and_full_draft(self):
        cfg = live()
        self.assertEqual(cfg.mode, "LIVE_COUNTED_DRAFT")
        self.assertEqual(cfg.opportunities, 130)
        self.assertEqual(cfg.sources, counted_snapshot())
        self.assertEqual(cfg.counted_cutoff_ms, FIRST_TIPOFF_MS)
        with self.assertRaises(ValueError):
            replace(cfg, sources=cfg.sources[:-1])
        with self.assertRaises(ValueError):
            replace(cfg, counted_cutoff_ms=None)
        with self.assertRaises(ValueError):
            replace(cfg, pick_limit=20)
        with self.assertRaises(ValueError):
            replace(cfg, run_id="research-incident-quota-other")

    def test_pilot_namespace_still_rejects_counted_task_and_large_drafts(self):
        cfg = offline()
        with self.assertRaises(ValueError):
            replace(cfg, mode="LIVE_RESEARCH_PILOT", experiment_id="miomaci-basketball-research-pilot",
                    schema="basketball-research-pilot/1")
        with self.assertRaises(ValueError):
            replace(cfg, counted_cutoff_ms=FIRST_TIPOFF_MS)  # offline cannot claim a counted cutoff

    def test_permits_are_not_interchangeable(self):
        cfg = live()
        pilot = ResearchPermit(cfg.hash, 0, 1, cfg.budget_micro_usd, 1170, 520, 520, "owner:x", "review:x", True)
        with self.assertRaises(ValueError):
            pilot.check_binding(cfg)
        good = CountedPermit(cfg.hash, 0, FIRST_TIPOFF_MS, cfg.budget_micro_usd, 1170, 520, 520,
                             "owner:x", "review:x", "counted:x", True)
        good.check_binding(cfg)
        for change in (dict(expires_ms=FIRST_TIPOFF_MS + 1), dict(budget_micro_usd=cfg.budget_micro_usd + 1),
                       dict(paid_execution_authorized=False), dict(max_model_requests=1169)):
            with self.assertRaises(ValueError):
                replace(good, **change).check_binding(cfg)

    def test_counted_request_injects_task_and_frozen_row_order(self):
        cfg = offline()
        state = quota_initial() | dict(phase="READY_MODEL", deadline_ms=10**13, trial_deadline_ms=10**13, last_ms=0)
        intent = make_intent(state, cfg, 1000)
        body = intent["request"]["body"]
        text = user_text(cfg.draft_plan.cases[0].profile, body)
        payload = json.loads(text)
        self.assertIn("manager_task", payload)
        self.assertEqual(payload["manager_task"]["task_hash"], cfg.counted_task.hash)
        self.assertNotIn("uncounted real-player pilot", text)
        self.assertEqual(intent["request"]["request_hash"], digest(body))
        rows = [r[0] for r in payload["decision"]["players"]]
        self.assertEqual(rows, list(cfg.counted_task.player_order))
        self.assertNotEqual(rows, sorted(rows))


def user_text(profile, body):
    if profile.provider in {"openai", "xai"}:
        return body["input"][1]["content"]
    if profile.provider == "anthropic":
        return body["messages"][0]["content"]
    if profile.provider == "gemini":
        return body["contents"][0]["parts"][0]["text"]
    return body["messages"][1]["content"]


class RequestBoundTests(unittest.TestCase):
    def test_reservation_is_request_specific_and_never_above_ceiling(self):
        cfg = offline()
        state = quota_initial() | dict(phase="READY_MODEL", deadline_ms=10**13, trial_deadline_ms=10**13, last_ms=0)
        intent = make_intent(state, cfg, 1000)
        profile = cfg.draft_plan.cases[0].profile
        size = intent["request"]["request_bytes"]
        self.assertEqual(intent["input_token_bound"], size + BYTE_BOUND_OVERHEAD_TOKENS)
        self.assertEqual(intent["bound"], call_bound(profile, size))
        self.assertLess(intent["bound"], profile.bound_micro_usd)
        self.assertEqual(intent["bound"] + ceiling_excess(profile, intent["bound"]), profile.bound_micro_usd)
        documented = load_profiles(PILOT_PACKAGE)[0]
        self.assertEqual(call_bound(documented, 1), documented.bound_micro_usd)

    def _first(self, cfg, result):
        r = Runner(cfg)
        try:
            transport, state = r.run({"pick:0:model:0": result})
            return transport, state
        finally:
            r.close()

    def test_validated_usage_within_bound_keeps_tight_reservation(self):
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        pick = cfg.board.legal_ids()[0]
        transport, state = self._first(cfg, native(p, action("pick", pick)))
        self.assertEqual(len(state["results"]), 1)
        self.assertLess(state["retained_micro_usd"], p.bound_micro_usd)

    def test_usage_above_request_bound_warns_retains_estimate_and_continues(self):
        # Owner decision (October 19): a disproved request bound is a recorded warning,
        # not a stop. The estimate is retained and the pick is kept.
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        state0 = quota_initial() | dict(phase="READY_MODEL", deadline_ms=10**13, trial_deadline_ms=10**13, last_ms=0)
        bound_tokens = make_intent(state0, cfg, 10000)["input_token_bound"]
        transport, state = self._first(cfg, native(p, action("pick", cfg.board.legal_ids()[0]), input_tokens=bound_tokens + 1))
        self.assertIsNone(state["pause"])
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(len(state["request_bound_warnings"]), 1)
        warning = state["request_bound_warnings"][0]
        self.assertEqual(warning["input_tokens"], bound_tokens + 1)
        self.assertGreaterEqual(state["retained_micro_usd"], state["estimated_model_micro_usd"])
        self.assertLessEqual(state["retained_micro_usd"], cfg.budget_micro_usd)
        self.assertEqual(len(transport.calls), 1)
        # Review P2-1: the warning is visible in every status report, never silent.
        from counted_entry import report as counted_report
        self.assertEqual(counted_report(state)["request_bound_warnings"], state["request_bound_warnings"])

    def test_bound_warning_never_lets_dispatch_skip_the_full_ceiling_gate(self):
        # After a warning, the next call still needs retained + full ceiling <= budget.
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        state0 = quota_initial() | dict(phase="READY_MODEL", deadline_ms=10**13, trial_deadline_ms=10**13, last_ms=0)
        bound_tokens = make_intent(state0, cfg, 10000)["input_token_bound"]
        tight = replace(cfg, budget_micro_usd=cfg.budget_micro_usd, budget_gate="RUNTIME_CEILING_GATED")
        floor = (tight.max_model_calls * max(c.profile.bound_micro_usd for c in tight.draft_plan.cases)
                 + 4 * tight.tool_bound_micro_usd)
        tight = replace(tight, budget_micro_usd=floor)
        r = Runner(tight)
        try:
            transport, state = r.run({"pick:0:model:0": native(p, action("search", "x"), input_tokens=bound_tokens + 1),
                                      "pick:0:search:0": dict(outcome="OK", retrieved_ms=10000, items=[dict(
                                          url="https://www.nba.com/a", title="t", text="x")]),
                                      "pick:0:model:1": native(p, action("pick", tight.board.legal_ids()[0]))})
        finally:
            r.close()
        self.assertEqual(len(state["request_bound_warnings"]), 1)
        self.assertLessEqual(state["retained_micro_usd"], tight.budget_micro_usd)
        self.assertIn(state["pause"], {None, "BUDGET"})

    def test_outcome_without_usage_retains_documented_ceiling(self):
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        r = Runner(cfg)
        try:
            transport, state = r.run({"pick:0:model:0": {"outcome": "HTTP_ERROR", "http_status": 503},
                                      "pick:0:model:1": native(p, action("pick", cfg.board.legal_ids()[0]))})
        finally:
            r.close()
        self.assertEqual(len(state["retry_history"]), 1)
        self.assertGreaterEqual(state["retained_micro_usd"], p.bound_micro_usd)

    def test_unknown_dispatch_pause_retains_documented_ceiling(self):
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        r = Runner(cfg)
        try:
            class Crash(Exception):
                pass
            def fault(name):
                if name == "after_intent":
                    raise Crash()
            op = NativeQuotaOpportunity(Path(r.tmp.name), cfg, clock=r.clock, fault=fault)
            op.step(ScriptedResearchTransport({}))  # START
            with self.assertRaises(Crash):
                op.step(ScriptedResearchTransport({}))
            state = NativeQuotaOpportunity(Path(r.tmp.name), cfg, clock=r.clock).step(ScriptedResearchTransport({}))
        finally:
            r.close()
        self.assertEqual(state["pause"], "UNKNOWN_DISPATCH")
        self.assertEqual(state["retained_micro_usd"], p.bound_micro_usd)

    def test_budget_shortfall_pauses_without_dispatch_or_exception(self):
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        with self.assertRaises(ValueError):
            replace(cfg, budget_micro_usd=cfg.worst_case_micro_usd - 1)
        short = replace(cfg, run_id="research-incident-quota-counted-short")
        # Bypass construction validation only to place the runtime below one ceiling.
        object.__setattr__(short, "budget_micro_usd", p.bound_micro_usd - 1)
        r = Runner(short)
        try:
            transport, state = r.run({})
        finally:
            r.close()
        self.assertEqual(state["pause"], "BUDGET")
        self.assertEqual(transport.calls, [])
        self.assertEqual(state["retained_micro_usd"], 0)

    def test_runtime_gated_budget_allows_owner_amount_and_pauses_before_overrun(self):
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        with self.assertRaises(ValueError):  # gate requires an explicit owner amount
            build_config(profiles(), seed=SEED, live=False, run_id="research-incident-quota-counted-gate",
                         budget_gate="RUNTIME_CEILING_GATED")
        # Review P3-1: the floor is one complete opportunity (all model calls plus tools).
        floor = cfg.max_model_calls * max(c.profile.bound_micro_usd for c in cfg.draft_plan.cases) + 4 * cfg.tool_bound_micro_usd
        with self.assertRaises(ValueError):
            build_config(profiles(), seed=SEED, live=False, run_id="research-incident-quota-counted-gate",
                         budget_gate="RUNTIME_CEILING_GATED", budget_micro_usd=floor - 1)
        gated = build_config(profiles(), seed=SEED, live=False, run_id="research-incident-quota-counted-gate",
                             budget_gate="RUNTIME_CEILING_GATED", budget_micro_usd=floor)
        self.assertLess(gated.budget_micro_usd, gated.worst_case_micro_usd)
        pick = gated.board.legal_ids()[0]
        r = Runner(gated)
        try:
            transport, state = r.run({"pick:0:model:0": native(p, action("search", "x")),
                                      "pick:0:search:0": dict(outcome="OK", retrieved_ms=10000, items=[dict(
                                          url="https://www.nba.com/a", title="t", text="x")]),
                                      "pick:0:model:1": native(p, action("pick", pick))})
        finally:
            r.close()
        self.assertLessEqual(state["retained_micro_usd"], gated.budget_micro_usd)
        self.assertIn(state["pause"], {None, "BUDGET"})
        with self.assertRaises(ValueError):
            replace(offline(), budget_gate="RUNTIME_CEILING_GATED", counted_task=None)

    def test_retained_never_exceeds_budget_and_covers_estimates(self):
        rng = random.Random(7)
        cfg = offline(excess_reserve_calls=1)
        p = cfg.draft_plan.cases[0].profile
        pick = cfg.board.legal_ids()[0]
        for trial in range(12):
            outcomes = {}
            for n in range(9):
                choice = rng.choice(["ok", "big", "503", "garbled"])
                act = action("pick", pick)
                outcomes[f"pick:0:model:{n}"] = (native(p, act) if choice == "ok" else
                                                 native(p, act, input_tokens=10**6) if choice == "big" else
                                                 {"outcome": "HTTP_ERROR", "http_status": 503} if choice == "503" else
                                                 {"outcome": "RESPONSE", "raw": {"bad": True}})
            r = Runner(cfg)
            try:
                transport, state = r.run(outcomes)
                self.assertLessEqual(state["retained_micro_usd"], cfg.budget_micro_usd)
                self.assertGreaterEqual(state["retained_micro_usd"], state["estimated_model_micro_usd"])
                self.assertLessEqual(len(transport.calls), 9)
            finally:
                r.close()

    def test_worst_case_request_fits_enforced_cap(self):
        """Body bytes <= base (empty history) + 2 x history_bytes; check the largest base in the draft."""
        cfg = offline()
        from fantasy_basketball.providers.counted_task import counted_request
        plan = cfg.draft_plan
        results, worst = [], 0
        for index in range(plan.total_picks):
            case = plan.cases[index]
            board = plan.board_at(index, results)
            if True:
                allowance = {"searches": 2, "reads": 2, "model_calls_including_this": 9}
                base = counted_request(case.profile, board, [], allowance, 0, 10**13, cfg.counted_task)["request_bytes"]
                worst = max(worst, base)
            results.append(dict(case_index=index, case_id=board.case_id, profile_id=case.profile.profile_id,
                                action=dict(player_id=board.legal_ids()[0], rationale="x")))
        self.assertLessEqual(worst + 2 * cfg.history_bytes + 8192, CAP)


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.cfg = live(trial_ms=600000)
        self.tmp = tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="counted-renew-")
        self.clock = Clock()
        self.op = NativeQuotaOpportunity(Path(self.tmp.name), self.cfg, clock=self.clock)
        self.permit = CountedPermit(self.cfg.hash, 0, 20000, self.cfg.budget_micro_usd, 1170, 520, 520,
                                    "owner:a", "review:a", "counted:a", True)
        self.op.journal.transact(lambda s: [("START", {"at_ms": 10000, "authorization": asdict(self.permit)})])
        self.op.journal.transact(lambda s: [("PAUSE", {"at_ms": 20000, "reason": "AUTHORIZATION_EXPIRED"})])

    def tearDown(self):
        self.tmp.cleanup()

    def renew(self, permit, at=21000, expected=None):
        state = self.op.status()
        return self.op.journal.transact(lambda s: [("RENEW_AUTHORIZATION", dict(
            at_ms=at, expected_state_hash=expected or digest(s), authorization=asdict(permit)))])

    def test_renewal_extends_authority_and_preserves_prefix_and_exposure(self):
        before = self.op.status()
        state = self.renew(replace(self.permit, not_before_ms=0, expires_ms=400000, owner_record="owner:b"))
        self.assertEqual(state["phase"], "READY_MODEL")
        self.assertEqual(state["authorization"]["expires_ms"], 400000)
        self.assertEqual(len(state["authorization_history"]), 1)
        for key in ("results", "retained_micro_usd", "total_counts", "deadline_ms", "trial_deadline_ms"):
            self.assertEqual(state[key], before[key])
        self.assertLessEqual(state["trial_deadline_ms"], FIRST_TIPOFF_MS)

    def test_renewal_rejections(self):
        bad = [replace(self.permit, expires_ms=20000),                     # not later
               replace(self.permit, expires_ms=400000, budget_micro_usd=self.cfg.budget_micro_usd + 1),
               replace(self.permit, expires_ms=FIRST_TIPOFF_MS + 1),      # beyond cutoff
               replace(self.permit, expires_ms=400000, not_before_ms=30000)]  # not yet valid at renewal
        for permit in bad:
            with self.assertRaises(ValueError):
                self.renew(permit)
        with self.assertRaises(ValueError):
            self.renew(replace(self.permit, expires_ms=400000), expected="0" * 64)
        self.assertEqual(self.op.status()["pause"], "AUTHORIZATION_EXPIRED")

    def test_pilot_and_offline_runs_cannot_renew(self):
        cfg = offline()
        r = Runner(cfg)
        try:
            r.op.step(ScriptedResearchTransport({}))
            with self.assertRaises(ValueError):
                r.op.journal.transact(lambda s: [("RENEW_AUTHORIZATION", dict(
                    at_ms=10000, expected_state_hash=digest(s), authorization=asdict(self.permit)))])
        finally:
            r.close()

    def test_start_after_cutoff_rejected(self):
        tmp = tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="counted-cutoff-")
        try:
            cfg = replace(self.cfg, run_id="research-incident-quota-counted-cutoff")
            op = NativeQuotaOpportunity(Path(tmp.name), cfg, clock=self.clock)
            permit = replace(self.permit, config_hash=cfg.hash, not_before_ms=0, expires_ms=FIRST_TIPOFF_MS)
            with self.assertRaises(ValueError):
                op.journal.transact(lambda s: [("START", {"at_ms": FIRST_TIPOFF_MS, "authorization": asdict(permit)})])
        finally:
            tmp.cleanup()


def counted_permit(cfg, nb, ex, tag="a"):
    return CountedPermit(cfg.hash, nb, ex, cfg.budget_micro_usd, cfg.max_model_calls * 130,
                         (cfg.max_searches + cfg.max_reads) * 130, (cfg.max_searches + cfg.max_reads) * 130,
                         "owner:" + tag, "review:" + tag, "counted:" + tag, True)


class Box:
    """Drives the real reducers event by event in a disposable journal."""
    def __init__(self, cfg, clock=None):
        self.tmp = tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="counted-review-fix-")
        self.cfg = cfg
        self.op = NativeQuotaOpportunity(Path(self.tmp.name), cfg, clock=clock or Clock())

    def ev(self, kind, data):
        return self.op.journal.transact(lambda s: [(kind, data)])

    def intent(self, at):
        return self.ev("INTENT", {"at_ms": at, "intent": make_intent(self.op.status(), self.cfg, at)})

    def result(self, at, res):
        key = self.op.status()["pending"]["key"]
        return self.ev("RESULT", {"at_ms": at, "key": key, "elapsed_ms": 1, "result": res})

    def renew(self, at, permit):
        return self.ev("RENEW_AUTHORIZATION", {"at_ms": at, "expected_state_hash": digest(self.op.status()),
                                               "authorization": asdict(permit)})

    def search_round(self, t, query):
        p = self.cfg.draft_plan.cases[0].profile
        self.intent(t); self.result(t + 1, native(p, action("search", query))); self.ev("APPLY", {"at_ms": t + 2})
        self.intent(t + 3)
        self.result(t + 4, {"outcome": "SERVICE_RESPONSE", "retrieved_ms": t + 4, "raw": {
            "results": [{"url": "https://www.nba.com/" + query, "title": "t", "content": "x"}], "usage": {"credits": 1}}})
        return self.ev("APPLY", {"at_ms": t + 5})


class ReviewFixTests(unittest.TestCase):
    """Regressions for the independent review of October 19 (P1-1, P1-2, P2-1; P3-1 is in the gated-budget test)."""

    def test_renewal_after_completed_search_restores_model_turn(self):  # P1-1
        cfg = live(trial_ms=600000)
        b = Box(cfg)
        try:
            b.ev("START", {"at_ms": 10000, "authorization": asdict(counted_permit(cfg, 0, 20000))})
            s = b.search_round(10001, "celtics")
            self.assertEqual((s["phase"], s["counts"]["search"]), ("READY_MODEL", 1))
            b.ev("PAUSE", {"at_ms": 20000, "reason": "AUTHORIZATION_EXPIRED"})
            s = b.renew(21000, counted_permit(cfg, 0, 400000, "b"))
            self.assertEqual(s["phase"], "READY_MODEL")
            self.assertNotIn("resume_phase", s)
            self.assertEqual(make_intent(s, cfg, 21001)["kind"], "model")
        finally:
            b.tmp.cleanup()

    def test_renewal_after_exhausted_searches_does_not_end_draft(self):  # P1-1 (A2)
        cfg = live(trial_ms=600000)
        b = Box(cfg)
        try:
            b.ev("START", {"at_ms": 10000, "authorization": asdict(counted_permit(cfg, 0, 20000))})
            b.search_round(10001, "q1"); b.search_round(10011, "q2")
            b.ev("PAUSE", {"at_ms": 20000, "reason": "AUTHORIZATION_EXPIRED"})
            s = b.renew(21000, counted_permit(cfg, 0, 400000, "b"))
            self.assertEqual(make_intent(s, cfg, 21001)["kind"], "model")
        finally:
            b.tmp.cleanup()

    def test_renewal_restores_pending_tool_turn(self):  # exact phase, not inferred
        cfg = live(trial_ms=600000)
        p = cfg.draft_plan.cases[0].profile
        b = Box(cfg)
        try:
            b.ev("START", {"at_ms": 10000, "authorization": asdict(counted_permit(cfg, 0, 20000))})
            b.intent(10001); b.result(10002, native(p, action("search", "celtics"))); b.ev("APPLY", {"at_ms": 10003})
            b.ev("PAUSE", {"at_ms": 20000, "reason": "AUTHORIZATION_EXPIRED"})
            s = b.renew(21000, counted_permit(cfg, 0, 400000, "b"))
            self.assertEqual(s["phase"], "READY_TOOL")
            self.assertEqual(make_intent(s, cfg, 21001)["kind"], "search")
        finally:
            b.tmp.cleanup()

    def test_retry_candidate_survives_expiry_through_renewal(self):  # P1-2 (C)
        cfg = live(trial_ms=600000)
        b = Box(cfg)
        try:
            b.ev("START", {"at_ms": 10000, "authorization": asdict(counted_permit(cfg, 0, 20000))})
            b.intent(10001); b.result(10002, {"outcome": "HTTP_ERROR", "http_status": 429})
            s = b.ev("APPLY", {"at_ms": 10003})
            candidate = s["retry_candidate"]
            self.assertIsNotNone(candidate)
            with self.assertRaises(ValueError):  # authority not yet expired: nothing to renew
                b.renew(15000, counted_permit(cfg, 0, 400000, "b"))
            s = b.renew(21000, counted_permit(cfg, 0, 400000, "b"))
            self.assertEqual((s["phase"], s["pause"], s["retry_candidate"]), ("PAUSED", "MODEL_TRANSPORT_FAILED", candidate))
            s = b.ev("RETRY_MODEL", {"at_ms": candidate["not_before_ms"], "expected_state_hash": digest(s)})
            self.assertEqual(s["phase"], "READY_MODEL")
        finally:
            b.tmp.cleanup()

    def test_live_step_never_starts_a_call_the_permit_would_truncate(self):  # P1-2 (D)
        import fantasy_basketball.providers.research_transport as RT
        from fantasy_basketball.providers.research_transport import LiveResearchTransport
        cfg = live(trial_ms=3600000)
        clock = Clock()
        b = Box(cfg, clock)
        launched = []
        def fake_bounded(*a, launch_guard=None, **k):
            launched.append(a)
            return {"outcome": "WORKER_TIMEOUT"}
        try:
            now = clock.now_ms()
            timeout = cfg.draft_plan.cases[0].profile.timeout_ms
            permit = counted_permit(cfg, now - 1000, now + timeout - 1)
            transport = LiveResearchTransport(cfg, permit, lambda name: "DUMMY-NOT-A-KEY")
            with patch.object(RT, "bounded_process", fake_bounded), \
                    patch("socket.socket", side_effect=AssertionError("offline only")):
                b.op.step(transport)            # START
                s = b.op.step(transport)        # would dispatch; must pause instead
            self.assertEqual((s["phase"], s["pause"]), ("PAUSED", "AUTHORIZATION_EXPIRED"))
            self.assertEqual(launched, [])
            self.assertEqual(s["retained_micro_usd"], 0)
            clock.wall = now + timeout
            s = b.renew(clock.now_ms(), counted_permit(cfg, now - 1000, now + 3 * timeout, "b"))
            self.assertEqual(s["phase"], "READY_MODEL")
        finally:
            b.tmp.cleanup()

    def test_recover_saved_without_usage_retains_ceiling_once(self):  # P2-1 (B)
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        b = Box(cfg)
        try:
            b.ev("START", {"at_ms": 10000}); b.intent(10001)
            s = b.ev("PAUSE", {"at_ms": 10002, "reason": "UNKNOWN_DISPATCH"})
            self.assertEqual(s["retained_micro_usd"], p.bound_micro_usd)
            s = b.result(10003, {"outcome": "HTTP_ERROR", "http_status": 503})
            s = b.ev("RECOVER_SAVED", {"at_ms": 10004, "expected_state_hash": digest(s)})
            self.assertEqual(s["retained_micro_usd"], p.bound_micro_usd)
        finally:
            b.tmp.cleanup()

    def test_recover_saved_with_usage_covers_estimate_without_double_count(self):  # P2-1 (B2)
        cfg = offline()
        p = cfg.draft_plan.cases[0].profile
        b = Box(cfg)
        try:
            b.ev("START", {"at_ms": 10000}); b.intent(10001)
            s = b.ev("PAUSE", {"at_ms": 10002, "reason": "UNKNOWN_DISPATCH"})
            s = b.result(10003, native(p, action("pick", cfg.board.legal_ids()[0])))
            s = b.ev("RECOVER_SAVED", {"at_ms": 10004, "expected_state_hash": digest(s)})
            self.assertEqual(s["retained_micro_usd"], max(p.bound_micro_usd, s["estimated_model_micro_usd"]))
            self.assertEqual(len(s["results"]), 1)
        finally:
            b.tmp.cleanup()

class HardLimitTests(unittest.TestCase):
    """Owner decision G (October 19): continue past the first tipoff up to a fixed hard limit."""

    def setUp(self):
        from prepare_counted_draft import HARD_LIMIT_MS
        self.limit = HARD_LIMIT_MS
        self.cfg = live(hard_limit_ms=HARD_LIMIT_MS)

    def test_default_keeps_tipoff_as_limit_and_digest_unchanged(self):
        base = live()
        self.assertIsNone(base.counted_hard_limit_ms)
        self.assertNotEqual(base.hash, self.cfg.hash)
        self.assertEqual(replace(self.cfg, counted_hard_limit_ms=None).hash, base.hash)
        with self.assertRaises(ValueError):
            counted_permit(base, 0, FIRST_TIPOFF_MS + 1).check_binding(base)

    def test_permit_and_start_may_run_past_tipoff_but_not_past_hard_limit(self):
        counted_permit(self.cfg, 0, self.limit).check_binding(self.cfg)
        with self.assertRaises(ValueError):
            counted_permit(self.cfg, 0, self.limit + 1).check_binding(self.cfg)
        b = Box(self.cfg)
        try:
            s = b.ev("START", {"at_ms": FIRST_TIPOFF_MS + 1000,
                               "authorization": asdict(counted_permit(self.cfg, 0, self.limit))})
            self.assertEqual(s["phase"], "READY_MODEL")
            self.assertLessEqual(s["trial_deadline_ms"], self.limit)
        finally:
            b.tmp.cleanup()
        b = Box(replace(self.cfg, run_id="research-incident-quota-counted-late"))
        try:
            with self.assertRaises(ValueError):
                b.ev("START", {"at_ms": self.limit, "authorization": asdict(
                    counted_permit(b.cfg, 0, self.limit))})
        finally:
            b.tmp.cleanup()

    def test_invalid_hard_limits_reject(self):
        with self.assertRaises(ValueError):
            live(hard_limit_ms=FIRST_TIPOFF_MS - 1)
        with self.assertRaises(ValueError):
            replace(offline(), counted_hard_limit_ms=self.limit)

    def test_roster_receipt_reports_tipoff_and_scoring_start(self):
        sys.path.insert(0, str(BUILD))
        from counted_entry import roster
        cfg = self.cfg
        late = FIRST_TIPOFF_MS + 3600 * 1000
        results = [{"case_index": i, "action": {"player_id": "p", "rationale": "r"}, "request_hash": "a",
                    "response_hash": "b", "settled_ms": (late if i == cfg.opportunities - 1 else FIRST_TIPOFF_MS - 1000)}
                   for i in range(cfg.opportunities)]
        receipt = roster(cfg, {"phase": "COMPLETE", "results": results})
        self.assertTrue(receipt["complete"])
        self.assertFalse(receipt["finished_before_first_tipoff"])
        self.assertEqual(receipt["scoring_counts_games_starting_after_ms"], late)
        early = [dict(r, settled_ms=FIRST_TIPOFF_MS - 1000) for r in results]
        self.assertTrue(roster(cfg, {"phase": "COMPLETE", "results": early})["finished_before_first_tipoff"])
        partial = roster(cfg, {"phase": "PAUSED", "results": results[:5]})
        self.assertFalse(partial["finished_before_first_tipoff"])
        self.assertIsNone(partial["scoring_counts_games_starting_after_ms"])


class EntryTests(unittest.TestCase):
    def test_preview_status_and_disabled_resume(self):
        cfg = live()
        folder = Path(tempfile.mkdtemp(dir=BUILD, prefix="counted-entry-test-"))
        try:
            path = folder / "package.json"
            path.write_text(json.dumps(dict(schema="basketball-counted-draft-package/1", config=asdict(cfg),
                                            unresolved=["owner approval"], execution_authorized=False)))
            run = lambda *a: subprocess.run([sys.executable, "-B", str(BUILD / "counted_entry.py"), *a],
                                            capture_output=True, text=True, cwd=ROOT)
            out = run("preview", "--package", str(path))
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            preview = json.loads(out.stdout)
            self.assertEqual(preview["config_hash"], cfg.hash)
            self.assertEqual(preview["opportunities"], 130)
            self.assertFalse(preview["credentials_accessed"])
            permit = folder / "permit.json"
            permit.write_text(json.dumps(asdict(CountedPermit(cfg.hash, 0, FIRST_TIPOFF_MS, cfg.budget_micro_usd,
                                                              1170, 520, 520, "owner:x", "review:x", "counted:x", True))))
            out = run("resume", "--package", str(path), "--permit", str(permit))
            self.assertEqual(out.returncode, 1)
            self.assertEqual(json.loads(out.stdout)["error"], "UNRESOLVED_PACKAGE")
            self.assertFalse((STATE_ROOT / "counted-draft" / (cfg.run_id + ".sqlite3")).exists())
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
