"""Frontier profiles, sealed forecast/consistency runner and solo rosters. Offline fixtures only."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "workstreams/build"
sys.path[:0] = [str(ROOT / "src"), str(BUILD)]

from fantasy_basketball.core.config import canonical
from fantasy_basketball.core.journal import STATE_ROOT
from fantasy_basketball.providers.qualification import synthetic_response
from frontier_profiles import CHANGED, RETAINED, frontier_profiles
from prepare_counted_draft import (FIRST_TIPOFF_MS, HARD_LIMIT_MS, OWNER_POLICIES, build_config, load_profiles,
                                   real_board, to_byte_basis)

SEED = "0123456789abcdef" * 4
CAP = 430080
PILOT = BUILD / "QUOTA_FULL_NATIVE_INTEGRATION_2026-10-18/package.json"   # latest reviewed native profiles
CREDENTIAL = "TEST-CREDENTIAL-NEVER-STORED-9f3a"


def raw_for(profile, text):
    raw = synthetic_response(profile, None, text=text)
    if profile.provider == "deepseek":
        raw["usage"].update(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=raw["usage"]["prompt_tokens"])
    return raw


def counted_cfg():
    return build_config(tuple(to_byte_basis(p, CAP) for p in frontier_profiles()), seed=SEED, live=True,
                        run_id="research-incident-quota-counted-frontier-test", policies=OWNER_POLICIES,
                        hard_limit_ms=HARD_LIMIT_MS, budget_gate="RUNTIME_CEILING_GATED",
                        budget_micro_usd=1_000_000_000)


class FrontierProfileTests(unittest.TestCase):
    def test_cohort_has_ten_seats_and_retained_profiles_are_byte_identical(self):
        profiles = frontier_profiles()
        self.assertEqual(len(profiles), 10)
        pilot = {p.provider: p for p in load_profiles(PILOT)}
        for p in profiles:
            if p.provider in RETAINED and p.provider not in ("xai", "qwen"):
                self.assertEqual(p.hash, replace(pilot[p.provider], timeout_ms=1200000, opportunity_ms=1800000).hash)   # only the call limit differs
        by = {p.provider: p for p in profiles}
        self.assertEqual((by["xai"].output_tokens, by["xai"].response_bytes, by["xai"].profile_id),
                         (65536, 1048576, "qualification-xai-out64k-medium"))
        self.assertEqual((by["qwen"].output_tokens, by["qwen"].profile_id), (32768, "qualification-qwen-out32k-medium"))
        self.assertEqual((by["xai"].effort, by["qwen"].effort, by["mistral"].effort), ("medium", "medium", "high"))
        self.assertEqual({p.timeout_ms for p in profiles}, {1200000})
        for name in ("xai", "qwen"):   # everything else about the seat is unchanged
            keep = ("model", "input_tokens", "input_usd_per_million", "output_usd_per_million")
            self.assertEqual([getattr(by[name], k) for k in keep], [getattr(pilot[name], k) for k in keep])
        models = {p.provider: p.model for p in profiles}
        ids = {p.provider: p.profile_id for p in profiles}
        self.assertEqual(ids["gemini"], "qualification-gemini-research-schema-v1")   # the MALFORMED_FUNCTION fix
        self.assertEqual(models["openai"], "gpt-6-astra")
        self.assertEqual(models["anthropic"], "claude-fable-5-1")
        self.assertEqual(models["mistral"], "mistral-large-4-0")
        self.assertEqual(models["deepseek"], "deepseek-v4-pro")
        self.assertEqual(models["meta_direct"], "muse-wonder-1.0")

    def test_changed_seats_use_maximum_applicable_rates_and_high_effort(self):
        rates = {p.provider: (p.input_usd_per_million, p.output_usd_per_million) for p in CHANGED}
        self.assertEqual(rates["openai"], ("25", "75"))      # >272K tier and cache-write maximum
        self.assertEqual(rates["anthropic"], ("20", "50"))   # 1-hour cache-write maximum
        self.assertEqual(rates["mistral"], ("1.36", "4.18"))  # list, not the open-ended sale
        self.assertEqual(rates["deepseek"], ("1.32", "3.96"))  # peak
        self.assertTrue(all(p.effort == "high" for p in CHANGED))
        outputs = {p.provider: p.output_tokens for p in CHANGED}
        self.assertEqual(outputs["mistral"], 32768)   # owner decision after the 16K thinking cut-off
        self.assertEqual({v for k, v in outputs.items() if k != "mistral"}, {16384})
        wonder = next(p for p in CHANGED if p.provider == "meta_direct")
        self.assertEqual(wonder.bound_micro_usd, 4526984)    # independently reviewed foundation arithmetic

    def test_gemini_requests_carry_the_response_schema(self):
        from fantasy_basketball.providers.counted_task import counted_request
        cfg = counted_cfg()
        gemini = next(p for p in cfg.draft_plan.profiles if p.provider == "gemini")
        wire = counted_request(gemini, replace(cfg.board, manager="gemini"), [], {"search": 2, "read": 2}, 0, 1200000,
                               cfg.counted_task)
        self.assertIn("responseJsonSchema", wire["body"]["generationConfig"])

    def test_counted_read_policy_is_on_for_counted_and_solo_and_off_for_pilot(self):
        import prepare_frontier_launch as pfl
        from incident_recovery import counted_read_policy
        self.assertTrue(counted_read_policy(counted_cfg()))
        byte = tuple(to_byte_basis(p, CAP) for p in frontier_profiles())
        self.assertTrue(counted_read_policy(pfl.solo_config(byte[0], SEED, FIRST_TIPOFF_MS, HARD_LIMIT_MS, OWNER_POLICIES)))
        self.assertFalse(counted_read_policy(pfl.pilot20(pfl.pilot_base(), byte, SEED)))

    def test_forecast_retry_raises_only_the_named_seats(self):
        import prepare_frontier_launch as pfl
        retry = pfl.forecast_retry_profiles(frontier_profiles(), ["anthropic", "gemini"])
        self.assertEqual([p.provider for p in retry], ["anthropic", "gemini"])
        self.assertTrue(all(p.output_tokens == 32768 and p.profile_id.endswith("-forecast-out32k") for p in retry))
        with self.assertRaises(ValueError):
            pfl.forecast_retry_profiles(frontier_profiles(), ["nobody"])

    def test_changed_seat_wires_fit_cap_and_keep_reviewed_envelopes(self):
        cfg = counted_cfg()
        from fantasy_basketball.providers.counted_task import counted_request
        allowance = {"searches": 2, "reads": 2, "model_calls_including_this": 9}
        for p in (c.profile for c in cfg.draft_plan.cases[:10] if c.profile.provider in {x.provider for x in CHANGED}):
            board, _, _ = real_board(p.provider)
            wire = counted_request(p, board, [], allowance, 0, 10**13, cfg.counted_task)
            self.assertLessEqual(wire["request_bytes"], CAP)
            body = wire["body"]
            self.assertEqual(body.get("model"), p.model)
            if p.provider == "anthropic":
                self.assertEqual(body["thinking"], {"type": "adaptive"})
                self.assertEqual(body["output_config"]["effort"], "high")
                self.assertNotIn("tool_choice", body)
            if p.provider == "openai":
                self.assertEqual(body["reasoning"], {"effort": "high"})
                self.assertFalse({"temperature", "top_p"} & set(body))
            if p.provider in {"mistral", "deepseek"}:
                self.assertEqual(body["reasoning_effort"], "high")

    def test_native_receipts_for_changed_seats_parse(self):
        from fantasy_basketball.providers.research import inspect_action
        board, _, _ = real_board("openai")
        pick = board.legal_ids()[0]
        for p in CHANGED:
            b = replace(board, manager=p.provider)
            receipt = inspect_action(p, raw_for(p, canonical(dict(action="pick", query=None, url=None, player_id=pick,
                                                                  rationale="test"))), b.legal_ids(), [])
            self.assertIsNone(receipt["failure"], p.provider)
            wrong = raw_for(p, "{}")
            wrong["model"] = "some-other-model"
            self.assertEqual(inspect_action(p, wrong, b.legal_ids(), [])["failure"], "IDENTITY_MISMATCH")

    def test_counted_frontier_package_is_gated_and_runs_past_tipoff(self):
        cfg = counted_cfg()
        self.assertEqual(cfg.budget_micro_usd, 1_000_000_000)
        self.assertLess(cfg.budget_micro_usd, cfg.worst_case_micro_usd)
        self.assertEqual(cfg.counted_hard_limit_ms, HARD_LIMIT_MS)
        self.assertIn("after the final pick", cfg.counted_task.counted_calendar)


def forecast_answer(pool, players=None, winners=None, rationale="Short public reason."):
    from fantasy_basketball.providers.forecast import COMPANIES, TOP_N
    players = players if players is not None else [
        dict(player_id=pid, projected_points=3000 - i * 50, probability_top_25=round(0.9 - i * 0.02, 3))
        for i, pid in enumerate(pool[:TOP_N])]
    names = sorted(COMPANIES.values())
    winners = winners if winners is not None else [dict(company=n, probability=0.1) for n in names]
    return canonical(dict(top_players=players, league_winner=winners, rationale=rationale))


class ForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = counted_cfg()
        cls.board, _, _ = real_board("openai")
        cls.pool = [p.player_id for p in cls.board.players]

    def test_requests_for_all_ten_fit_and_carry_rules_without_legal_list(self):
        from fantasy_basketball.providers.forecast import forecast_request
        for p in frontier_profiles():
            board = replace(self.board, manager=p.provider)
            wire = forecast_request(to_byte_basis(p, CAP), board, self.cfg.counted_task, 0, 10**13)
            self.assertLessEqual(wire["request_bytes"], CAP)
            text = canonical(wire["body"])
            self.assertIn("league_winner", text)
            self.assertIn("No web research", text)
            self.assertIn("after the final pick", text)
            self.assertNotIn("legal_ids", text)

    def test_valid_forecast_parses_through_native_checks_for_every_provider(self):
        from fantasy_basketball.providers.forecast import inspect_forecast
        for p in frontier_profiles():
            receipt = inspect_forecast(p, raw_for(p, forecast_answer(self.pool)), self.pool)
            self.assertIsNone(receipt["failure"], p.provider)
            self.assertEqual(len(receipt["action"]["top_players"]), 25)

    def test_invalid_forecasts_are_labelled_and_never_repaired(self):
        from fantasy_basketball.providers.forecast import inspect_forecast
        p = frontier_profiles()[0]
        good = json.loads(forecast_answer(self.pool))
        cases = {
            "FORECAST_PLAYERS": forecast_answer(self.pool, players=good["top_players"][:24]),
            "FORECAST_NUMBERS": forecast_answer(self.pool, players=[dict(e, probability_top_25=1.5) for e in good["top_players"]]),
            "FORECAST_WINNER": forecast_answer(self.pool, winners=[dict(w, probability=0.2) for w in good["league_winner"]]),
            "FORECAST_RATIONALE": forecast_answer(self.pool, rationale=" "),
            "FORECAST_SHAPE": canonical(dict(top_players=[], rationale="x")),
        }
        dup = list(good["top_players"])
        dup[1] = dict(dup[0])
        cases_extra = [forecast_answer(self.pool, players=dup),
                       forecast_answer(self.pool, players=[dict(e, player_id="not-a-player") if i == 0 else e
                                                           for i, e in enumerate(good["top_players"])])]
        for label, text in cases.items():
            self.assertEqual(inspect_forecast(p, raw_for(p, text), self.pool)["failure"], label)
        for text in cases_extra:
            self.assertEqual(inspect_forecast(p, raw_for(p, text), self.pool)["failure"], "FORECAST_PLAYERS")
        bare_pick = canonical(dict(player_id=self.pool[0], rationale="x"))
        self.assertEqual(inspect_forecast(p, raw_for(p, bare_pick), self.pool)["failure"], "FORECAST_SHAPE")
        incomplete = synthetic_response(p, None, text=forecast_answer(self.pool), stop="length")
        self.assertNotIn(inspect_forecast(p, incomplete, self.pool)["failure"], {None})


class SealedRunnerTests(unittest.TestCase):
    def setUp(self):
        from prepare_frontier_launch import sealed_config
        self.cfg_counted = counted_cfg()
        _, sha, _ = real_board("openai")
        self.sha = sha
        self.forecast = sealed_config("FORECAST", frontier_profiles(), self.cfg_counted.counted_task, sha, FIRST_TIPOFF_MS)
        self.consistency = sealed_config("CONSISTENCY", frontier_profiles(), self.cfg_counted.counted_task, sha,
                                         FIRST_TIPOFF_MS)
        self.tmp = Path(tempfile.mkdtemp(dir=STATE_ROOT, prefix="sealed-test-"))
        self.now = FIRST_TIPOFF_MS - 6 * 3600 * 1000
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def permit(self, cfg, **kw):
        from sealed_calls import SealedPermit
        values = dict(config_hash=cfg.hash, not_before_ms=self.now - 1000, expires_ms=FIRST_TIPOFF_MS,
                      budget_micro_usd=cfg.budget_micro_usd, max_model_requests=cfg.max_model_requests,
                      owner_record="owner:test", paid_execution_authorized=True)
        values.update(kw)
        return SealedPermit(**values)

    def dispatch_ok(self, kind):
        board, _, _ = real_board("openai")
        pool = [p.player_id for p in board.players]

        def dispatch(profile, wire, now, credential):
            key = credential(profile.provider)
            assert key == CREDENTIAL
            self.calls.append(profile.provider)
            if kind == "FORECAST":
                text = forecast_answer(pool)
            else:
                b = replace(board, manager=profile.provider)
                text = canonical(dict(action="pick", query=None, url=None, player_id=b.legal_ids()[0], rationale="r"))
            return {"outcome": "RESPONSE", "raw": raw_for(profile, text), "http_status": 200, "body_sha256": "0" * 64}
        return dispatch

    def run_(self, cfg, permit=None, dispatch=None, clock=None):
        from sealed_calls import resume
        with patch("socket.socket", side_effect=AssertionError("offline only")):
            return resume(cfg, permit or self.permit(cfg), self.tmp, clock=clock or (lambda: self.now),
                          dispatch=dispatch or self.dispatch_ok(cfg.kind), credential=lambda name: CREDENTIAL)

    def test_forecast_happy_path_seal_and_no_second_dispatch(self):
        from sealed_calls import seal
        status = self.run_(self.forecast)
        self.assertEqual({c["state"] for c in status["calls"]}, {"DONE"})
        self.assertTrue(all(c["failure"] is None for c in status["calls"]))
        self.assertEqual(len(self.calls), 10)
        self.run_(self.forecast)
        self.assertEqual(len(self.calls), 10)   # completed calls are never repeated
        out = self.tmp / "seal.json"
        record = seal(self.forecast, self.tmp, out)
        self.assertEqual(len(record["fingerprint"]), 64)
        with self.assertRaises(FileExistsError):
            seal(self.forecast, self.tmp, out)
        for f in self.tmp.rglob("*.json"):
            self.assertNotIn(CREDENTIAL, f.read_text())

    def test_consistency_asks_each_ai_three_times_with_no_research(self):
        status = self.run_(self.consistency)
        self.assertEqual(len(status["calls"]), 30)
        self.assertEqual(sorted(self.calls), sorted([p.provider for p in frontier_profiles()] * 3))
        from sealed_calls import Store
        result = Store(self.tmp, self.consistency).read("openai:0", "result")
        user = json.loads(result["request"]["body"]["input"][1]["content"])
        self.assertEqual(user["remaining_allowance"], {"searches": 0, "reads": 0, "model_calls_including_this": 1})
        self.assertIn("manager_task", user)

    def test_research_action_with_no_allowance_is_recorded_not_followed(self):
        def dispatch(profile, wire, now, credential):
            self.calls.append(profile.provider)
            text = canonical(dict(action="search", query="injuries", url=None, player_id=None, rationale=None))
            return {"outcome": "RESPONSE", "raw": raw_for(profile, text)}
        status = self.run_(self.consistency, dispatch=dispatch)
        self.assertEqual({c["failure"] for c in status["calls"]}, {"RESEARCH_WITH_NO_ALLOWANCE"})
        self.assertEqual(len(self.calls), 30)

    def test_unknown_outcome_is_never_resent(self):
        from sealed_calls import Store
        store = Store(self.tmp, self.forecast)
        store.write("openai:0", "intent", dict(key="openai:0", bound=1, at_ms=self.now))
        status = self.run_(self.forecast)
        self.assertNotIn("openai", self.calls)
        row = next(c for c in status["calls"] if c["key"] == "openai:0")
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(status["unknown_calls_never_resent"], 1)

    def test_pre_launch_rejection_is_recorded_as_not_sent(self):
        def dispatch(profile, wire, now, credential):
            raise ValueError("CREDENTIAL_UNAVAILABLE")
        status = self.run_(self.forecast, dispatch=dispatch)
        self.assertEqual({c["outcome"] for c in status["calls"]}, {"NOT_SENT"})

    def test_permit_bindings_reject(self):
        bad = [self.permit(self.forecast, config_hash="0" * 64),
               self.permit(self.forecast, budget_micro_usd=self.forecast.budget_micro_usd + 1),
               self.permit(self.forecast, expires_ms=FIRST_TIPOFF_MS + 1),
               self.permit(self.forecast, paid_execution_authorized=False),
               self.permit(self.forecast, not_before_ms=self.now + 1)]
        for permit in bad:
            with self.assertRaises(ValueError):
                self.run_(self.forecast, permit=permit)
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):  # consistency permit cannot authorize the forecast
            self.run_(self.forecast, permit=self.permit(self.consistency))

    def test_no_call_starts_if_the_permit_would_cut_it_off(self):
        late = FIRST_TIPOFF_MS - 60_000
        status = self.run_(self.forecast, clock=lambda: late)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(c["state"] == "NOT_STARTED" for c in status["calls"]))

    def test_source_change_stops_before_any_key_or_dispatch(self):
        import sealed_calls
        with patch.object(sealed_calls, "sealed_snapshot", return_value=(("x", "0" * 64),)):
            with self.assertRaises(ValueError):
                self.run_(self.forecast)
        self.assertEqual(self.calls, [])

    def test_rebuilt_config_never_reuses_or_seals_old_results(self):  # review P2-2
        from sealed_calls import Store, seal
        self.run_(self.forecast)
        self.assertEqual(len(self.calls), 10)
        rebuilt = replace(self.forecast, cutoff_ms=self.forecast.cutoff_ms - 1)  # same run ID, new hash
        self.assertEqual(rebuilt.run_id, self.forecast.run_id)
        self.assertEqual({c["state"] for c in Store(self.tmp, rebuilt).status()["calls"]}, {"NOT_STARTED"})
        record = seal(rebuilt, self.tmp, self.tmp / "rebuilt-seal.json")
        self.assertFalse(record["complete"])
        self.assertEqual(json.loads((self.tmp / "rebuilt-seal.json").read_text())["files"], {})

    def test_foreign_intent_record_is_refused(self):
        from sealed_calls import Store
        store = Store(self.tmp, self.forecast)
        store.write("openai:0", "intent", dict(key="openai:0", bound=1, config_hash="f" * 64))
        with self.assertRaises(ValueError):
            store.read("openai:0", "intent")

    def test_unparseable_response_is_saved_not_lost(self):
        from sealed_calls import Store
        def dispatch(profile, wire, now, credential):
            self.calls.append(profile.provider)
            return {"outcome": "RESPONSE", "raw": raw_for(profile, "[" * 200000 + "]" * 200000)}
        status = self.run_(self.forecast, dispatch=dispatch)
        self.assertEqual({c["state"] for c in status["calls"]}, {"DONE"})
        result = Store(self.tmp, self.forecast).read("openai:0", "result")
        self.assertIsNotNone(result["raw"])
        # A crashed parse must never look like a clean success, in status or in the seal.
        from sealed_calls import seal
        self.assertTrue(all(c["inspect_error"] or c["failure"] for c in status["calls"]))
        self.assertFalse(seal(self.forecast, self.tmp, self.tmp / "crash-seal.json")["complete"]
                         if any(c["inspect_error"] for c in status["calls"]) else False)

    def test_budget_must_cover_every_ceiling(self):
        from sealed_calls import SealedConfig
        with self.assertRaises(ValueError):
            replace(self.forecast, budget_micro_usd=self.forecast.worst_case_micro_usd - 1)
        with self.assertRaises(ValueError):
            replace(self.forecast, kind="CONSISTENCY")   # repeats and run ID are bound to the kind


class SoloRosterTests(unittest.TestCase):
    def test_solo_configs_share_the_counted_task_and_are_gated(self):
        from prepare_frontier_launch import solo_config
        counted = counted_cfg()
        for p in counted.draft_plan.profiles:
            cfg = solo_config(p, SEED, FIRST_TIPOFF_MS, HARD_LIMIT_MS, OWNER_POLICIES)
            self.assertEqual(cfg.counted_task.hash, counted.counted_task.hash)
            self.assertEqual(cfg.opportunities, 13)
            self.assertEqual({c.profile.provider for c in cfg.draft_plan.cases}, {p.provider})
            floor = cfg.max_model_calls * p.bound_micro_usd + 4 * cfg.tool_bound_micro_usd
            self.assertGreaterEqual(cfg.budget_micro_usd, floor)
            self.assertNotEqual(cfg.run_id, counted.run_id)

    def test_solo_draft_builds_a_legal_private_roster_offline(self):
        from prepare_frontier_launch import solo_config
        from fantasy_basketball.draft.research import ScriptedResearchTransport
        from incident_acceptance_check import Clock
        from quota_native import NativeQuotaOpportunity
        p = next(x for x in counted_cfg().draft_plan.profiles if x.provider == "mistral")
        live = solo_config(p, SEED, FIRST_TIPOFF_MS, HARD_LIMIT_MS, OWNER_POLICIES)
        cfg = replace(live, mode="OFFLINE_FIXTURE", experiment_id="miomaci-basketball-research-offline",
                      schema="basketball-research-offline/1", counted_cutoff_ms=None, counted_hard_limit_ms=None,
                      sources=(), extension_sources=(), quota_sources=())
        fixtures, prefix = {}, []
        for index, case in enumerate(cfg.draft_plan.cases):
            board = cfg.draft_plan.board_at(index, prefix)
            pick = board.legal_ids()[0]
            fixtures[f"pick:{index}:model:0"] = dict(outcome="RESPONSE", raw=raw_for(case.profile, canonical(
                dict(action="pick", query=None, url=None, player_id=pick, rationale="solo"))))
            prefix.append(dict(case_index=index, case_id=board.case_id, profile_id=case.profile.profile_id,
                               action=dict(player_id=pick)))
        with tempfile.TemporaryDirectory(dir=STATE_ROOT, prefix="solo-test-") as name, \
                patch("socket.socket", side_effect=AssertionError("offline only")):
            op = NativeQuotaOpportunity(Path(name), cfg, clock=Clock())
            transport = ScriptedResearchTransport(fixtures)
            for _ in range(200):
                state = op.step(transport)
                if state["phase"] in {"COMPLETE", "PAUSED"}:
                    break
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(len({r["action"]["player_id"] for r in state["results"]}), 13)
        self.assertLessEqual(state["retained_micro_usd"], cfg.budget_micro_usd)


if __name__ == "__main__":
    unittest.main()
