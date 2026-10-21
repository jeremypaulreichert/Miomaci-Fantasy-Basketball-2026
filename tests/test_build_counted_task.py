"""Neutral operational instructions; no live counted execution or rule adoption."""
from dataclasses import replace
import json
import test_build_core as support
from counted_task import CountedTask,prepare_counted_request
from fantasy_basketball.providers.research_task import pilot_task


class CountedTaskTests(support.BuildCase):
    def task(self):
        rules=replace(pilot_task((('G1',('G',)),),2),require_research=False)
        return CountedTask(rules,'a'*64,'b'*64,'Example period, not adopted.',
            'Example week boundaries.','Example eligible games.','Example tie rule.',
            'Example unresolved results rule.','Example correction rule.')

    def test_public_packet_contains_rules_without_pilot_or_observer_priming(self):
        task=self.task();packet=task.packet();text=json.dumps(packet).lower()
        for field in ('scoring_weights','lineup_slots','counted_calendar','week_policy',
                      'event_policy','tie_policy','unresolved_result_policy','correction_policy'):
            self.assertIn(field,packet)
        for phrase in ('uncounted','capability demonstration','benchmark','accuracy grading',
                       'cost ranking','speed ranking','evaluation score','private reasoning'):
            self.assertNotIn(phrase,text)
        self.assertIn('may select without',packet['research_policy'])
        self.assertNotIn('unobserved',text)

    def test_each_public_policy_and_source_release_is_bound(self):
        task=self.task()
        for name in ('counted_calendar','week_policy','event_policy','tie_policy',
                     'unresolved_result_policy','correction_policy'):
            self.assertNotEqual(replace(task,**{name:'Different frozen rule.'}).hash,task.hash)
        self.assertNotEqual(replace(task,player_packet_sha256='c'*64).hash,task.hash)
        self.assertNotEqual(replace(task,rules_release_sha256='d'*64).hash,task.hash)
        self.assertEqual(task.packet()['task_hash'],task.hash)

    def test_missing_policies_releases_and_forced_research_reject(self):
        task=self.task()
        for changes in ({'week_policy':''},{'rules_release_sha256':'unknown'},
                        {'rules':replace(task.rules,require_research=True)}):
            with self.assertRaises(ValueError):replace(task,**changes)

    def test_pilot_wording_and_runtime_gate_remain_separate(self):
        task=self.task()
        self.assertIn('uncounted',task.rules.packet()['scope'])
        from test_build_research import ResearchTests
        cfg=ResearchTests.draft_config(self)
        with self.assertRaisesRegex(ValueError,'manager task'):
            replace(cfg,task=task)

    def test_all_ten_native_wires_bind_rules_and_preserve_private_history(self):
        from test_build_research import ten_profiles,board
        from fantasy_basketball.core.config import canonical,digest
        from fantasy_basketball.providers.research import request
        task=self.task();history=[dict(kind='external_evidence',text='private returned evidence')]
        allowance=dict(searches=2,reads=2,model_calls_including_this=9)
        for profile in ten_profiles():
            current=replace(board(profile.provider),roster_size=2)
            base=request(profile,current,history,allowance,10000,20000)
            wire=prepare_counted_request(profile,current,history,allowance,10000,20000,task)
            text=canonical(wire['body'])
            self.assertIn(task.hash,text);self.assertIn('private returned evidence',text)
            self.assertNotIn('capability demonstration',text);self.assertNotIn('uncounted',text)
            self.assertEqual(wire['request_hash'],digest(wire['body']))
            self.assertEqual(wire['request_bytes'],len(text.encode()))
            self.assertEqual(wire['profile_hash'],base['profile_hash'])
            self.assertEqual(request(profile,current,history,allowance,10000,20000),base)
            with self.assertRaisesRegex(ValueError,'counted rules'):
                prepare_counted_request(profile,replace(current,roster_size=3),history,allowance,10000,20000,task)
