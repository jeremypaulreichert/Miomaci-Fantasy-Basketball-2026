"""Versioned Gemini research schema and full-cycle fixture regression checks."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from unittest.mock import patch

import test_build_core as support
import test_build_research_task as tasks
from test_build_research import ten_profiles, board, action, native, evidence, URL
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.draft.research import ResearchOpportunity, ScriptedResearchTransport
from fantasy_basketball.providers.research import request, _cached_request
from gemini_action_probe import schema_candidate


class SchemaIntegrationTests(support.BuildCase):
    def profile(self):
        return replace(next(p for p in ten_profiles() if p.provider == 'gemini'),
                       profile_id='qualification-gemini-research-schema-v1')

    def test_historical_requests_all_ten_remain_byte_identical(self):
        path = Path(__file__).resolve().parents[1]/'workstreams/build/history/2026-10-18-before-schema-integration/research.py'
        spec = importlib.util.spec_from_file_location('fantasy_basketball.providers.historical_research',path)
        historical = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(historical)
        for profile in ten_profiles():
            with self.subTest(provider=profile.provider):
                args = (profile,board(profile.provider),[],{'searches':2,'reads':2},10000,20000)
                self.assertEqual(request(*args),historical.request(*args))

    def test_new_profile_matches_successful_probe_schema_and_binds_size(self):
        profile = self.profile()
        original = replace(profile,profile_id='qualification-gemini')
        args = (board('gemini'),[],{},10000,20000)
        expected = schema_candidate(request(original,*args),original.request_bytes)
        actual = request(profile,*args)
        self.assertEqual(actual['body'],expected['body'])
        self.assertEqual(actual['request_hash'],expected['request_hash'])
        self.assertNotEqual(actual['profile_hash'],expected['profile_hash'])
        self.assertEqual(actual['request_bytes'],len(canonical(actual['body']).encode()))
        with self.assertRaises(ValueError):
            request(replace(profile,request_bytes=actual['request_bytes']-1),*args)

    def test_full_search_read_pick_cycle_and_cold_restart(self):
        profile = self.profile()
        old = tasks.ResearchTaskTests.task_config(self)
        opening = board('gemini')
        cfg = replace(old,run_id='research-schema-cycle',profile=profile,board=opening,
                      draft_plan=replace(old.draft_plan,profiles=(profile,),board=opening))
        fixtures = {'pick:0:model:0':native(profile,action('search',query='fixture availability')),
                    'pick:0:search:0':evidence(),
                    'pick:0:model:1':native(profile,action('read',url=URL)),
                    'pick:0:read:0':evidence(), 'pick:0:model:2':native(profile,action())}
        with patch('socket.socket',side_effect=AssertionError('offline only')):
            op,transport,state = tasks.ResearchTaskTests.finish(self,cfg,fixtures)
            self.assertEqual(state['phase'],'COMPLETE')
            self.assertEqual(len(state['results']),1)
            model_calls = [c for c in transport.calls if c['kind']=='model']
            self.assertEqual(len(model_calls),3)
            for call in model_calls:
                self.assertEqual(call['request']['body']['generationConfig']['responseJsonSchema']['properties']['action']['enum'],
                                 ['search','read','pick'])
            before = op.journal.path.read_bytes()
            _cached_request.cache_clear()
            cold = ResearchOpportunity(self.directory,cfg,clock=self.clock)
            no_calls = ScriptedResearchTransport({})
            self.assertEqual(cold.step(no_calls),state)
            self.assertEqual(no_calls.calls,[])
            self.assertEqual(op.journal.path.read_bytes(),before)
            legacy_profile = replace(profile,profile_id='qualification-gemini')
            legacy = replace(cfg,profile=legacy_profile,draft_plan=replace(cfg.draft_plan,profiles=(legacy_profile,)))
            with self.assertRaises(ValueError): ResearchOpportunity(self.directory,legacy,clock=self.clock)

    def test_schema_does_not_bypass_missing_usage_or_early_pick(self):
        from fantasy_basketball.providers.research import inspect_action
        profile = self.profile()
        response = native(profile,action('search',query='fixture'))['raw']
        response['usageMetadata'].pop('candidatesTokenCount')
        self.assertEqual(inspect_action(profile,response,['a'],())['failure'],'MISSING_OR_INVALID_USAGE')
        old = tasks.ResearchTaskTests.task_config(self)
        opening = board('gemini')
        cfg = replace(old,run_id='research-schema-early',profile=profile,board=opening,
                      draft_plan=replace(old.draft_plan,profiles=(profile,),board=opening))
        _,transport,state = tasks.ResearchTaskTests.finish(self,cfg,{'pick:0:model:0':native(profile,action())})
        self.assertEqual(state['pause'],'PILOT_DEMONSTRATION_INCOMPLETE')
        self.assertEqual(state['results'],[])
        self.assertEqual(len(transport.calls),1)
