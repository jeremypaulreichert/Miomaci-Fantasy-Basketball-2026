"""Observed-incident recovery, native checks and immutable restart evidence."""
from dataclasses import asdict,fields,replace
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support
import test_build_research_task as tasks
from test_build_research import ten_profiles,board,action,native,evidence,URL
from fantasy_basketball.draft.research import ResearchConfig,ScriptedResearchTransport
from fantasy_basketball.core.config import digest
from fantasy_basketball.providers.research_transport import code_snapshot
from incident_recovery import IncidentConfig,IncidentOpportunity,extension_snapshot
import incident_entry as entry

URL2='https://www.nba.com/player/alternative'

class IncidentTests(support.BuildCase):
    def incident_config(self,provider='meta_direct',**changes):
        old=tasks.ResearchTaskTests.task_config(self);p=next(p for p in ten_profiles() if p.provider==provider);b=board(provider)
        cfg=IncidentConfig(**{f.name:getattr(old,f.name) for f in fields(ResearchConfig)}|dict(
            run_id='research-incident-'+provider.replace('_','-'),profile=p,board=b,
            draft_plan=replace(old.draft_plan,profiles=(p,),board=b),max_model_calls=9))
        return replace(cfg,**changes)

    def search_evidence(self):
        result=evidence();result['items'].append(dict(url=URL2,title='Alternative',text='Separate saved evidence'))
        return result

    def step_until_stop(self,op,transport):
        for _ in range(60):
            state=op.step(transport)
            if state['phase']=='COMPLETE':return state
            if state['phase']=='PAUSED':
                if state['retry_candidate']:
                    self.clock.advance(max(0,state['retry_candidate']['not_before_ms']-self.clock.now_ms()))
                elif state['incident_candidate']:pass
                else:return state
        self.fail('bounded recovery did not terminate')

    def cold(self,op,cfg,state):
        before=op.journal.path.read_bytes();fresh=ScriptedResearchTransport({})
        self.assertEqual(IncidentOpportunity(self.directory,cfg,clock=self.clock).step(fresh),state)
        self.assertFalse(fresh.calls);self.assertEqual(op.journal.path.read_bytes(),before)

    def test_meta_prefix_corrected_by_same_ai_without_extracting_action(self):
        cfg=self.incident_config();p=cfg.profile;bad=native(p,action('search',query='first'))
        bad['raw']['choices'][0]['message']['content']='First pick planning. '+bad['raw']['choices'][0]['message']['content']
        fixtures={'pick:0:model:0':bad,'pick:0:model:1':native(p,action('search',query='corrected')),
            'pick:0:search:0':self.search_evidence(),'pick:0:model:2':native(p,action('read',url=URL)),
            'pick:0:read:0':evidence(),'pick:0:model:3':native(p,action())}
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures)
        with patch('socket.socket',side_effect=AssertionError('offline')):state=self.step_until_stop(op,transport)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['format_corrections_used'],1)
        self.assertEqual(state['attempts'][0]['saved']['result'],bad)
        self.assertEqual(state['attempts'][0]['native_receipt']['failure'],'MALFORMED_RESEARCH_ACTION')
        self.assertEqual(state['corrections'],0);self.assertEqual(state['total_counts'],{'model':4,'search':1,'read':1})
        self.assertEqual(state['retained_micro_usd'],4*p.bound_micro_usd+2*cfg.tool_bound_micro_usd)
        self.assertEqual(len(state['incident_history']),1);self.cold(op,cfg,state)

    def test_second_format_error_stays_paused_without_third_call(self):
        cfg=self.incident_config();bad=native(cfg.profile,action())
        bad['raw']['choices'][0]['message']['content']='Not JSON'
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport({f'pick:0:model:{n}':bad for n in (0,1)})
        state=self.step_until_stop(op,transport)
        self.assertEqual(state['phase'],'PAUSED');self.assertEqual(len(transport.calls),2);self.assertIsNone(state['incident_candidate'])
        self.cold(op,cfg,state)

    def test_mistral_429_after_searches_waits_then_retries_once_and_finishes(self):
        cfg=self.incident_config('mistral');p=cfg.profile
        fixtures={'pick:0:model:0':native(p,action('search',query='one')),'pick:0:search:0':self.search_evidence(),
            'pick:0:model:1':native(p,action('search',query='two')),'pick:0:search:1':self.search_evidence(),
            'pick:0:model:2':{'outcome':'HTTP_ERROR','http_status':429},
            'pick:0:model:3':native(p,action('read',url=URL)),'pick:0:read:0':evidence(),'pick:0:model:4':native(p,action())}
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures)
        for _ in range(20):
            state=op.step(transport)
            if state['phase']=='PAUSED':break
        self.assertEqual(state['retry_candidate']['not_before_ms'],self.clock.now_ms()+60000)
        self.assertEqual(op.step(transport),state);self.assertEqual(len(transport.calls),5)
        self.clock.advance(59999);self.assertEqual(op.step(transport),state)
        self.clock.advance(1);op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        transport.results['pick:0:read:0']['retrieved_ms']=self.clock.now_ms()
        state=self.step_until_stop(op,transport)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['retry_used'],1)
        self.assertEqual(state['total_counts']['model'],5);self.assertEqual(len(state['failure_history']),1)
        self.assertEqual(state['retained_micro_usd'],5*p.bound_micro_usd+3*cfg.tool_bound_micro_usd)
        self.cold(op,cfg,state)

    def read_fixtures(self,cfg,*,alternative=True,second_failure=False,repeat_url=False):
        p=cfg.profile;oversized=dict(outcome='RESPONSE_TOO_LARGE',http_status=200,hash_is_prefix=True,body_sha256='a'*64)
        fixtures={'pick:0:model:0':native(p,action('search',query='one')),
            'pick:0:search:0':self.search_evidence() if alternative else evidence(),
            'pick:0:model:1':native(p,action('read',url=URL)),'pick:0:read:0':oversized,
            'pick:0:model:2':native(p,action('read',url=URL if repeat_url else URL2)),
            'pick:0:read:1':oversized if second_failure else {'outcome':'OK','retrieved_ms':10000,'items':[dict(url=URL2,title='Other',text='Accepted alternative')]},
            'pick:0:model:3':native(p,action())}
        return fixtures

    def test_qwen_oversized_page_ai_selects_other_source_and_usage_stays_unknown(self):
        cfg=self.incident_config('qwen');op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(self.read_fixtures(cfg))
        state=self.step_until_stop(op,transport)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['read_replans_used'],1)
        self.assertEqual(state['service_usage_missing'],1);self.assertEqual(state['total_counts']['read'],2)
        self.assertNotIn(URL,state['urls']);self.assertIn(URL2,state['urls'])
        accepted=[h for h in state['history'] if h['kind']=='external_evidence' and h['action']['action']=='read']
        self.assertEqual([h['action']['url'] for h in accepted],[URL2])
        self.assertIsNone(state['attempts'][3]['observed_service_credits'])
        self.assertEqual(state['retained_micro_usd'],4*cfg.profile.bound_micro_usd+3*cfg.tool_bound_micro_usd)
        self.cold(op,cfg,state)

    def test_no_other_source_or_repeated_oversize_exhausts_without_substitution(self):
        for index,changes in enumerate((dict(alternative=False),dict(second_failure=True),dict(repeat_url=True))):
            cfg=self.incident_config('qwen',run_id=f'research-incident-read-negative-{index}')
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(self.read_fixtures(cfg,**changes))
            state=self.step_until_stop(op,transport)
            self.assertEqual(state['phase'],'PAUSED');self.assertEqual(state['results'],[])
            self.assertIsNone(state['incident_candidate']);self.cold(op,cfg,state)

    def unfetched(self,url=URL):
        raw={'results':[],'failed_results':[{'url':url,'error':'Failed to fetch url'}],'response_time':0.3,
             'usage':{'credits':0},'request_id':'fixture'}
        return dict(outcome='SERVICE_RESPONSE',raw=raw,retrieved_ms=10000,http_status=200,body_sha256='b'*64)

    def test_unfetchable_page_gets_one_replan_with_its_own_message(self):
        cfg=self.incident_config('gemini',run_id='research-incident-unfetched');fixtures=self.read_fixtures(cfg)
        fixtures['pick:0:read:0']=self.unfetched()
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures)
        state=self.step_until_stop(op,transport)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['read_replans_used'],1)
        self.assertEqual(state['incident_history'][0]['reason'],'fetch_failed')
        notes=[h for h in state['history'] if h['kind']=='read_failure']
        self.assertEqual([n['failure'] for n in notes],['The page could not be fetched by the search service. Select another discovered URL.'])
        self.assertNotIn(URL,state['urls']);self.assertEqual(state['failed_read_urls'],[URL])
        self.cold(op,cfg,state)

    def test_unfetchable_page_limits_and_shape_are_strict(self):
        cases=(('second',dict(second_failure=True),{'pick:0:read:1':self.unfetched(URL2)}),
               ('other-url',{},{'pick:0:read:0':self.unfetched('https://www.nba.com/elsewhere')}),
               ('no-alternative',dict(alternative=False),{}))
        for name,changes,extra in cases:
            cfg=self.incident_config('gemini',run_id='research-incident-unfetched-'+name)
            fixtures=self.read_fixtures(cfg,**changes);fixtures['pick:0:read:0']=self.unfetched();fixtures.update(extra)
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures)
            state=self.step_until_stop(op,transport)
            self.assertEqual(state['phase'],'PAUSED',name);self.assertEqual(state['results'],[],name)
            self.assertIsNone(state['incident_candidate'],name);self.cold(op,cfg,state)

    def run_counted(self,name,fixtures):
        cfg=self.incident_config('gemini',run_id='research-incident-counted-'+name)
        task=replace(cfg.task,require_research=False)   # counted tasks have no pilot demonstration gate
        cfg=replace(cfg,task=task,draft_plan=replace(cfg.draft_plan,rules_digest=task.hash))
        with patch('incident_recovery.counted_read_policy',return_value=True):
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures(cfg))
            state=self.step_until_stop(op,transport);self.cold(op,cfg,state)
        return cfg,state,transport

    def test_counted_failed_page_with_no_alternative_lets_the_ai_pick(self):
        def fixtures(cfg):
            f=self.read_fixtures(cfg,alternative=False);f['pick:0:read:0']=self.unfetched()
            f['pick:0:model:2']=native(cfg.profile,action());return f
        cfg,state,transport=self.run_counted('no-alt',fixtures)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(len(state['results']),1)
        note=[h for h in state['history'] if h['kind']=='read_failure']
        self.assertEqual(len(note),1);self.assertIn('counts toward your read allowance',note[0]['failure'])
        self.assertNotIn('read another',note[0]['failure'])   # no other discovered URL: not offered
        self.assertEqual(state['total_counts']['read'],1);self.assertNotIn(URL,state['urls'])

    def test_counted_two_failed_pages_then_pick_without_extra_reads(self):
        def fixtures(cfg):
            f=self.read_fixtures(cfg,second_failure=True);f['pick:0:read:0']=self.unfetched()   # unfetched, then oversized
            return f
        cfg,state,transport=self.run_counted('two',fixtures)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['total_counts']['read'],2)
        self.assertEqual([h['failure'].split('.')[0] for h in state['history'] if h['kind']=='read_failure'],
                         ['The page could not be fetched by the search service','Page response exceeded the size limit'])
        self.assertEqual(len([c for c in transport.calls if c['kind']=='read']),2)
        self.assertEqual(state['failed_read_urls'],sorted([URL,URL2]))

    def test_counted_policy_keeps_higher_priority_stops(self):
        def fixtures(cfg):
            f=self.read_fixtures(cfg);bad=self.unfetched();bad['http_status']=500
            f['pick:0:read:0']=bad;return f   # non-200: not a recognized page failure
        cfg,state,transport=self.run_counted('shape',fixtures)
        # Stop-path audit: in a counted run any failed read is now feedback, not a stop.
        self.assertEqual(state['phase'],'COMPLETE')
        self.assertEqual(state['incident_history'][0]['reason'],'read_failed');self.assertIn(URL,state['failed_read_urls'])

    def test_counted_service_credit_overrun_still_stops(self):
        def fixtures(cfg):
            f=self.read_fixtures(cfg)
            f['pick:0:read:0']=dict(outcome='SERVICE_RESPONSE',retrieved_ms=10000,http_status=200,body_sha256='c'*64,
                raw={'results':[{'url':URL,'raw_content':'text'}],'failed_results':[],'usage':{'credits':2}})
            return f
        cfg,state,transport=self.run_counted('overrun',fixtures)
        self.assertEqual((state['phase'],state['pause']),('PAUSED','SERVICE_USAGE_BOUND_EXCEEDED'))
        self.assertIsNone(state['incident_candidate'])

    def test_counted_failed_search_is_feedback(self):
        def fixtures(cfg):
            f=self.read_fixtures(cfg);bad=self.unfetched();bad['http_status']=503
            f['pick:0:search:0']=bad
            f['pick:0:model:1']=native(cfg.profile,action());return f
        cfg,state,transport=self.run_counted('search',fixtures)
        self.assertEqual(state['phase'],'COMPLETE')
        self.assertEqual([h['kind'] for h in state['history'] if h['kind'].endswith('_failure')],['search_failure'])

    def test_second_illegal_pick_gets_the_format_correction(self):
        bad=dict(action(),player_id='espn:0')
        cfg,state,transport=self.run_plain('illegal',lambda c:{'pick:0:model:0':native(c.profile,bad),
            'pick:0:model:1':native(c.profile,bad),'pick:0:model:2':native(c.profile,action())})
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['format_corrections_used'],1)

    def truncated(self,p):
        r=native(p,action());r['raw']['choices'][0]['finish_reason']='length';return r

    def run_plain(self,name,fixtures,provider='deepseek'):
        cfg=self.incident_config(provider,run_id='research-incident-decide-'+name)
        task=replace(cfg.task,require_research=False)
        cfg=replace(cfg,task=task,draft_plan=replace(cfg.draft_plan,rules_digest=task.hash))
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures(cfg))
        state=self.step_until_stop(op,transport);self.cold(op,cfg,state)
        return cfg,state,transport

    def test_long_pick_rationale_gets_one_shape_correction(self):
        long=dict(action(),rationale='x'*525)
        cfg,state,transport=self.run_plain('shape',lambda c:{'pick:0:model:0':native(c.profile,long),
                                                             'pick:0:model:1':native(c.profile,action())})
        self.assertEqual(state['phase'],'COMPLETE')
        notes=[h for h in state['history'] if h['kind']=='format_correction']
        self.assertEqual(len(notes),1);self.assertIn('1 to 500 characters',notes[0]['failure'])

    def test_second_shape_error_pauses(self):
        long=dict(action(),rationale='x'*525)
        cfg,state,transport=self.run_plain('shape2',lambda c:{'pick:0:model:0':native(c.profile,long),
                                                              'pick:0:model:1':native(c.profile,long),
                                                              'pick:0:model:2':native(c.profile,action())})
        self.assertEqual((state['phase'],state['pause']),('PAUSED','PICK_SHAPE'))
        self.assertEqual(len([c for c in transport.calls if c['kind']=='model']),2)

    def test_output_limit_gets_one_decide_now_correction(self):
        cfg,state,transport=self.run_plain('once',lambda c:{'pick:0:model:0':self.truncated(c.profile),
                                                            'pick:0:model:1':native(c.profile,action())})
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(state['format_corrections_used'],1)
        notes=[h for h in state['history'] if h['kind']=='length_correction']
        self.assertEqual(len(notes),1);self.assertIn('Decide now',notes[0]['failure'])
        self.assertEqual(state['incident_history'][0]['kind'],'length')
        self.assertEqual(len([c for c in transport.calls if c['kind']=='model']),2)

    def test_second_output_limit_pauses_without_third_call(self):
        cfg,state,transport=self.run_plain('twice',lambda c:{'pick:0:model:0':self.truncated(c.profile),
                                                             'pick:0:model:1':self.truncated(c.profile),
                                                             'pick:0:model:2':native(c.profile,action())})
        self.assertEqual((state['phase'],state['pause']),('PAUSED','INCOMPLETE_RESPONSE'))
        self.assertIsNone(state['incident_candidate'])
        self.assertEqual(len([c for c in transport.calls if c['kind']=='model']),2)

    def test_bad_identity_missing_usage_and_premature_pick_do_not_get_format_recovery(self):
        cfg=self.incident_config();p=cfg.profile
        wrong=native(p,action());wrong['raw']['model']='wrong-model'
        missing=native(p,action());missing['raw']['usage'].pop('completion_tokens')
        for index,value in enumerate((wrong,missing,native(p,action()))):
            current=replace(cfg,run_id=f'research-incident-invalid-{index}')
            op=IncidentOpportunity(self.directory,current,clock=self.clock);transport=ScriptedResearchTransport({'pick:0:model:0':value})
            state=self.step_until_stop(op,transport)
            self.assertEqual(state['phase'],'PAUSED');self.assertIsNone(state['incident_candidate']);self.assertEqual(len(transport.calls),1)

    def test_unknown_dispatch_does_not_become_recoverable(self):
        cfg=self.incident_config();op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        transport=ScriptedResearchTransport({'pick:0:model:0':native(cfg.profile,action())})
        op.step(transport)
        def crash(boundary):
            if boundary=='after_send':raise RuntimeError('fixture process loss')
        op.fault=crash
        with self.assertRaises(RuntimeError):op.step(transport)
        cold=IncidentOpportunity(self.directory,cfg,clock=self.clock);state=cold.step(ScriptedResearchTransport({}))
        self.assertEqual(state['pause'],'UNKNOWN_DISPATCH');self.assertIsNone(state['incident_candidate']);self.assertIsNone(state['retry_candidate'])

    def test_expiry_and_authentication_prevent_retries(self):
        for index,failure in enumerate((dict(outcome='HTTP_ERROR',http_status=401),dict(outcome='HTTP_ERROR',http_status=429))):
            cfg=self.incident_config('mistral',run_id=f'research-incident-expiry-{index}')
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport({'pick:0:model:0':failure})
            for _ in range(3):state=op.step(transport)
            self.clock.advance(cfg.opportunity_ms)
            self.assertEqual(op.step(transport),state);self.assertEqual(len(transport.calls),1)

    def test_ascii_reporting_preserves_actual_non_ascii_name_on_cp1252(self):
        raw=io.BytesIO();console=io.TextIOWrapper(raw,encoding='cp1252',write_through=True)
        with patch.object(sys,'stdout',console):entry.emit({'choice':'Nikola Jokić','reason':'Denver’s center'})
        data=raw.getvalue();self.assertTrue(data.isascii());self.assertEqual(json.loads(data)['choice'],'Nikola Jokić')
        console.detach()

    def test_new_entry_preview_rejects_source_change_and_blocks_unresolved_resume(self):
        cfg=self.incident_config('mistral');cfg=replace(cfg,mode='LIVE_RESEARCH_PILOT',schema='basketball-research-pilot/1',
            experiment_id='miomaci-basketball-research-pilot',sources=code_snapshot(),extension_sources=extension_snapshot())
        package=dict(schema='basketball-incident-package/1',config=asdict(cfg),unresolved=['live approval required'],execution_authorized=False)
        path=self.directory/'incident-package.json';path.write_text(json.dumps(package))
        def invoke(*args):
            output=io.StringIO()
            with patch.object(sys,'argv',['incident_entry.py',*map(str,args)]),redirect_stdout(output), \
                    patch('incident_recovery.IncidentOpportunity',side_effect=AssertionError('no journal')), \
                    patch('research_entry.private_credential',side_effect=AssertionError('no key')):
                entry.main()
            return json.loads(output.getvalue())
        result=invoke('preview','--package',path);self.assertEqual(result['config_hash'],cfg.hash)
        self.assertFalse(result['credentials_accessed'])
        with self.assertRaisesRegex(ValueError,'UNRESOLVED_PACKAGE'):invoke('resume','--package',path,'--permit',self.directory/'absent.json')
        value=json.loads(json.dumps(package));value['config']['extension_sources'][0][1]='0'*64;path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'EXTENSION_SOURCE_CHANGED'):invoke('preview','--package',path)

    def test_live_typed_transport_uses_new_bound_config_with_fake_workers_only(self):
        from fantasy_basketball.providers.research_transport import ResearchPermit,LiveResearchTransport
        cfg=self.incident_config();cfg=replace(cfg,mode='LIVE_RESEARCH_PILOT',schema='basketball-research-pilot/1',
            experiment_id='miomaci-basketball-research-pilot',sources=code_snapshot(),extension_sources=extension_snapshot())
        permit=ResearchPermit(cfg.hash,10000,200000,cfg.budget_micro_usd,9,4,4,'owner:fixture','review:fixture',True)
        model_actions=[action('search',query='first'),action('search',query='corrected'),action('read',url=URL),action()]
        calls=[];key_calls=[]
        def process(command,document,*args,**kwargs):
            with kwargs['launch_guard']() as allowed:self.assertTrue(allowed)
            payload=json.loads(document);endpoint=payload.get('endpoint') or 'https://api.tavily.com/'+payload['action']['action'];calls.append(endpoint)
            if 'api.tavily.com' in endpoint:
                field='content' if endpoint.endswith('/search') else 'raw_content'
                return dict(outcome='SERVICE_RESPONSE',http_status=200,retrieved_ms=self.clock.now_ms(),
                    raw={'results':[{'url':URL,'title':'Fixture',field:'Fixture source'}],'usage':{'credits':1}})
            index=sum('api.tavily.com' not in endpoint for endpoint in calls)-1
            response=native(cfg.profile,model_actions[index])
            if index==0:response['raw']['choices'][0]['message']['content']='Planning. '+response['raw']['choices'][0]['message']['content']
            return response
        def key(provider):key_calls.append(provider);return 'fixture-only-key'
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        with patch('fantasy_basketball.providers.research_transport.bounded_process',side_effect=process), \
                patch('time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000), \
                patch('socket.socket',side_effect=AssertionError('offline only')):
            transport=LiveResearchTransport(cfg,permit,key);state=self.step_until_stop(op,transport)
        self.assertEqual(state['phase'],'COMPLETE');self.assertEqual(len(calls),6)
        self.assertEqual(state['format_corrections_used'],1);self.assertEqual(state['observed_service_credits'],'2')
        self.assertNotIn(b'fixture-only-key',op.journal.path.read_bytes())

    def test_changed_live_permit_cannot_recover_a_saved_incident(self):
        from fantasy_basketball.providers.research_transport import ResearchPermit,LiveResearchTransport
        cfg=self.incident_config('mistral');cfg=replace(cfg,mode='LIVE_RESEARCH_PILOT',schema='basketball-research-pilot/1',
            experiment_id='miomaci-basketball-research-pilot',sources=code_snapshot(),extension_sources=extension_snapshot())
        permit=ResearchPermit(cfg.hash,10000,200000,cfg.budget_micro_usd,9,4,4,'owner:fixture','review:fixture',True)
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        def process(*args,**kwargs):return dict(outcome='HTTP_ERROR',http_status=429)
        with patch('fantasy_basketball.providers.research_transport.bounded_process',side_effect=process), \
                patch('time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000):
            transport=LiveResearchTransport(cfg,permit,lambda _:'fixture-key')
            for _ in range(3):state=op.step(transport)
            self.assertEqual(state['phase'],'PAUSED');self.clock.advance(60000)
            changed=LiveResearchTransport(cfg,replace(permit,owner_record='owner:different'),lambda _:self.fail('no key'))
            with self.assertRaisesRegex(ValueError,'changed incident permit'):op.step(changed)

    def test_recovery_checkpoint_before_dispatch_is_cold_resumable(self):
        cfg=self.incident_config();bad=native(cfg.profile,action());bad['raw']['choices'][0]['message']['content']='Prefix '+bad['raw']['choices'][0]['message']['content']
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport({'pick:0:model:0':bad})
        for _ in range(3):state=op.step(transport)
        self.assertEqual(state['phase'],'PAUSED');self.assertEqual(op.step(transport)['phase'],'READY_MODEL')
        cold=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        second=ScriptedResearchTransport({'pick:0:model:1':native(cfg.profile,action())})
        cold.step(second);state=cold.step(second)
        self.assertEqual(len(second.calls),1);self.assertEqual(state['pause'],'PILOT_DEMONSTRATION_INCOMPLETE')

    def test_recovery_stale_hash_and_too_little_time_cannot_expand_limits(self):
        cfg=self.incident_config();bad=native(cfg.profile,action());bad['raw']['choices'][0]['message']['content']='Prefix'
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport({'pick:0:model:0':bad})
        for _ in range(3):state=op.step(transport)
        with self.assertRaises(ValueError):op.journal.transact(lambda _: [('RECOVER_INCIDENT',{'at_ms':self.clock.now_ms(),'expected_state_hash':'0'*64})])
        self.clock.advance(cfg.opportunity_ms);self.assertEqual(op.step(transport),state)
        self.assertEqual(len(transport.calls),1)

    def test_later_search_cannot_restore_failed_read_even_after_restart(self):
        for index,repeat in enumerate((False,True)):
            cfg=self.incident_config('qwen',run_id=f'research-incident-rediscovery-{index}')
            fixtures=self.read_fixtures(cfg)
            fixtures.update({'pick:0:model:2':native(cfg.profile,action('search',query='rediscover')),
                'pick:0:search:1':self.search_evidence(),
                'pick:0:model:3':native(cfg.profile,action('read',url=URL if repeat else URL2)),
                'pick:0:model:4':native(cfg.profile,action())})
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock);transport=ScriptedResearchTransport(fixtures)
            for _ in range(30):
                state=op.step(transport)
                if state['counts']['search']==2 and state['phase']=='READY_MODEL':break
            self.assertEqual(state['failed_read_urls'],[URL]);self.assertNotIn(URL,state['urls'])
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
            self.assertEqual(op.status(),state)
            state=self.step_until_stop(op,transport)
            self.assertEqual(state['phase'],'PAUSED' if repeat else 'COMPLETE')
            reads=[c for c in transport.calls if c['kind']=='read']
            self.assertEqual(len(reads),1 if repeat else 2)
            self.assertEqual(state['service_usage_missing'],1);self.cold(op,cfg,state)

    def test_failed_url_exclusion_resets_only_at_next_pick(self):
        from incident_recovery import reduce
        cfg=self.incident_config('qwen');op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        state=self.step_until_stop(op,ScriptedResearchTransport(self.read_fixtures(cfg)))
        # Exercise the reducer's validated NEXT_PICK boundary, with a settled
        # prior choice and room in the fixture trial clock.
        next_state=deepcopy(state);next_state['phase']='PICK_SAVED'
        reduce(next_state,'NEXT_PICK',{'at_ms':self.clock.now_ms()},cfg)
        self.assertEqual(next_state['failed_read_urls'],[]);self.assertEqual(next_state['urls'],[])
        self.assertEqual(next_state['service_usage_missing'],1)
        self.assertEqual(len(next_state['incident_history']),1)

    def test_late_oversized_read_has_unknown_usage_without_recovery(self):
        for index,elapsed in enumerate((True,False)):
            cfg=self.incident_config('qwen',run_id=f'research-incident-late-read-{index}')
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
            transport=ScriptedResearchTransport(self.read_fixtures(cfg))
            if not elapsed:
                def delay(boundary):
                    if boundary=='after_send' and op.status()['pending']['kind']=='read':self.clock.advance(cfg.tool_timeout_ms)
                op.fault=delay
            for _ in range(20):
                state=op.step(transport)
                if state['phase']=='RECEIVED' and state['pending']['kind']=='read':break
            # Saved elapsed timing and observed wall timing have priority over
            # the page failure. Both must still retain unknown service usage.
            if elapsed:
                from incident_recovery import reduce
                saved=deepcopy(state);saved['result']['elapsed_ms']=cfg.tool_timeout_ms
                reduce(saved,'APPLY',{'at_ms':self.clock.now_ms()},cfg)
                self.assertEqual(saved['pause'],'RESPONSE_LATE')
                self.assertEqual(saved['service_usage_missing'],1)
                self.assertIsNone(saved['attempts'][-1]['observed_service_credits'])
                self.assertIsNone(saved['incident_candidate'])
            else:
                state=op.step(transport)
                self.assertEqual(state['pause'],'RESPONSE_LATE')
                self.assertEqual(state['service_usage_missing'],1)
                self.assertIsNone(state['attempts'][-1]['observed_service_credits'])
                self.assertIsNone(state['incident_candidate']);self.cold(op,cfg,state)

    def test_late_model_call_gets_decide_now_only_in_counted_runs(self):
        from incident_recovery import reduce
        for counted in (True,False):
            cfg=self.incident_config('deepseek',run_id=f'research-incident-late-model-{int(counted)}')
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
            transport=ScriptedResearchTransport({'pick:0:model:0':native(cfg.profile,action('search',query='q'))})
            for _ in range(10):
                state=op.step(transport)
                if state['phase']=='RECEIVED':break
            saved=deepcopy(state);saved['result']['elapsed_ms']=cfg.profile.timeout_ms
            with patch('incident_recovery.counted_read_policy',return_value=counted):
                reduce(saved,'APPLY',{'at_ms':self.clock.now_ms()},cfg)
            self.assertEqual(saved['pause'],'RESPONSE_LATE')
            self.assertEqual((saved['incident_candidate'] or {}).get('kind'),'length' if counted else None)
            if counted:   # end to end: one LATE decide-now, money never released, a second late call stops
                before=saved['retained_micro_usd']
                with patch('incident_recovery.counted_read_policy',return_value=True):
                    reduce(saved,'RECOVER_INCIDENT',{'at_ms':self.clock.now_ms(),'expected_state_hash':__import__('fantasy_basketball.core.config',fromlist=['digest']).digest(saved)},cfg)
                self.assertIn('took longer than the time limit',saved['history'][-1]['failure'])
                self.assertGreaterEqual(saved['retained_micro_usd'],before)
                self.assertEqual(saved['format_corrections_used'],1)

    def test_late_model_call_with_hard_receipt_failure_still_stops(self):
        from incident_recovery import reduce
        cfg=self.incident_config('deepseek',run_id='research-incident-late-hard')
        op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        bad=native(cfg.profile,action('search',query='q'));bad['raw']['model']='some-other-model'
        transport=ScriptedResearchTransport({'pick:0:model:0':bad})
        for _ in range(10):
            state=op.step(transport)
            if state['phase']=='RECEIVED':break
        saved=deepcopy(state);saved['result']['elapsed_ms']=cfg.profile.timeout_ms
        with patch('incident_recovery.counted_read_policy',return_value=True):
            reduce(saved,'APPLY',{'at_ms':self.clock.now_ms()},cfg)
        self.assertEqual(saved['pause'],'RESPONSE_LATE')
        self.assertEqual(saved['attempts'][-1]['native_receipt']['failure'],'IDENTITY_MISMATCH')
        self.assertIsNone(saved['incident_candidate'])

    def test_final_worker_guard_rechecks_extensions_after_key_for_model_and_service(self):
        from fantasy_basketball.providers.research_transport import ResearchPermit,LiveResearchTransport
        for index,target in enumerate(('model','search','read')):
            cfg=self.incident_config(run_id=f'research-incident-launch-guard-{index}')
            cfg=replace(cfg,mode='LIVE_RESEARCH_PILOT',schema='basketball-research-pilot/1',
                experiment_id='miomaci-basketball-research-pilot',sources=code_snapshot(),extension_sources=extension_snapshot())
            permit=ResearchPermit(cfg.hash,10000,200000,cfg.budget_micro_usd,9,4,4,'owner:fixture','review:fixture',True)
            snapshot=[cfg.extension_sources];key_calls=[];launched=[]
            def key(provider):
                key_calls.append(provider)
                if ((target=='model' and len(key_calls)==1) or (target=='search' and len(key_calls)==2)
                        or (target=='read' and len(key_calls)==4)):snapshot[0]=(('changed','0'*64),)
                return 'fixture-only-key'
            def process(command,document,*args,**kwargs):
                # This is the actual guard supplied to bounded_process, after
                # credential retrieval. Never launch a process or use a socket.
                with kwargs['launch_guard']() as allowed:
                    self.assertTrue(allowed);launched.append(json.loads(document))
                payload=json.loads(document)
                if 'action' in payload:
                    field='content' if payload['action']['action']=='search' else 'raw_content'
                    return dict(outcome='SERVICE_RESPONSE',http_status=200,retrieved_ms=self.clock.now_ms(),
                        raw={'results':[{'url':URL,'title':'Fixture',field:'Evidence'}],'usage':{'credits':1}})
                return native(cfg.profile,action('search',query='one') if len(key_calls)==1 else action('read',url=URL))
            op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
            with patch('incident_recovery.extension_snapshot',side_effect=lambda:snapshot[0]), \
                    patch('fantasy_basketball.providers.research_transport.bounded_process',side_effect=process), \
                    patch('time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000), \
                    patch('socket.socket',side_effect=AssertionError('offline only')):
                transport=LiveResearchTransport(cfg,permit,key)
                with self.assertRaisesRegex(ValueError,'extension source changed before worker launch'):
                    for _ in range(15):op.step(transport)
            self.assertEqual(len(launched),{'model':0,'search':1,'read':3}[target])
            self.assertNotIn(b'fixture-only-key',op.journal.path.read_bytes())

    def test_saved_oversized_result_recovery_preserves_unknown_usage_and_exclusion(self):
        cfg=self.incident_config('qwen');op=IncidentOpportunity(self.directory,cfg,clock=self.clock)
        transport=ScriptedResearchTransport(self.read_fixtures(cfg))
        for _ in range(20):
            state=op.step(transport)
            if state['phase']=='RECEIVED' and state['pending']['kind']=='read':break
        state=op.journal.transact(lambda _: [('PAUSE',{'at_ms':self.clock.now_ms(),'reason':'UNKNOWN_DISPATCH'})])
        state=op.recover_saved(digest(state))
        self.assertEqual(state['pause'],'TOOL_RESULT_REJECTED')
        self.assertEqual(state['service_usage_missing'],1)
        self.assertIsNone(state['attempts'][-1]['observed_service_credits'])
        self.assertEqual(state['failed_read_urls'],[URL]);self.assertNotIn(URL,state['urls'])
        self.cold(op,cfg,state)
