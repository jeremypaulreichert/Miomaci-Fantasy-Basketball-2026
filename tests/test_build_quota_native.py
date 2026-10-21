"""Native-entry integration with fake workers; no network or real credentials."""
from dataclasses import asdict,fields,replace
from contextlib import redirect_stdout
import io
import json
import sys
from unittest.mock import patch

import test_build_core as support
import test_build_incident_recovery as incidents
from test_build_research import action,native,URL
from fantasy_basketball.providers.research_transport import LiveResearchTransport,ResearchPermit,code_snapshot
from incident_recovery import extension_snapshot
from quota_native import NativeQuotaConfig,NativeQuotaOpportunity,quota_snapshot
import quota_entry


class NativeQuotaTests(support.BuildCase):
    def quota_cfg(self,**changes):
        old=incidents.IncidentTests.incident_config(self,'mistral')
        values={f.name:getattr(old,f.name) for f in fields(old)}
        values.update(run_id='research-incident-quota-native-test',mode='LIVE_RESEARCH_PILOT',
            schema='basketball-research-pilot/1',experiment_id='miomaci-basketball-research-pilot',
            sources=code_snapshot(),extension_sources=extension_snapshot())
        values.update(changes)
        return NativeQuotaConfig(**values,quota_sources=quota_snapshot())

    def test_decode_preview_and_unresolved_entry_never_reads_keys(self):
        cfg=self.quota_cfg();package=dict(schema='basketball-quota-package/1',config=json.loads(json.dumps(asdict(cfg))),
            execution_authorized=False,unresolved=['Independent review/owner execution still required'])
        self.assertEqual(quota_entry.decode(package['config']),cfg)
        path=self.directory/'quota-package.json';path.write_text(json.dumps(package))
        output=io.StringIO()
        with patch.object(sys,'argv',['quota_entry','preview','--package',str(path)]),redirect_stdout(output),patch('research_entry.private_credential',side_effect=AssertionError('no key')):
            quota_entry.main()
        self.assertFalse(json.loads(output.getvalue())['credentials_accessed'])
        with patch.object(sys,'argv',['quota_entry','resume','--package',str(path),'--permit','nonexistent']),patch('research_entry.private_credential',side_effect=AssertionError('no key')):
            with self.assertRaisesRegex(ValueError,'UNRESOLVED'):quota_entry.main()
        package['config']['quota_sources'][0][1]='0'*64
        with self.assertRaisesRegex(ValueError,'QUOTA_SOURCE'):quota_entry.decode(package['config'])

    def test_typed_live_transport_completes_quota_recovery_with_fake_workers(self):
        cfg=self.quota_cfg();p=cfg.profile
        permit=ResearchPermit(cfg.hash,10000,200000,cfg.budget_micro_usd,9,4,4,'owner:fixture','review:fixture',True)
        acts=iter([action('search',query='one'),action('search',query='two'),action('search',query='blocked'),action('read',url=URL),action()])
        launched=[]
        def process(command,document,*args,**kwargs):
            with kwargs['launch_guard']() as allowed:self.assertTrue(allowed)
            value=json.loads(document);launched.append(value)
            if 'action' in value:
                field='content' if value['action']['action']=='search' else 'raw_content'
                return dict(outcome='SERVICE_RESPONSE',http_status=200,retrieved_ms=self.clock.now_ms(),raw={
                    'results':[dict(url=URL,title='Fixture',**{field:'Fixture evidence'})],'usage':{'credits':1}})
            return native(p,next(acts))
        op=NativeQuotaOpportunity(self.directory,cfg,clock=self.clock)
        with patch('fantasy_basketball.providers.research_transport.bounded_process',side_effect=process),patch('time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000),patch('socket.socket',side_effect=AssertionError('offline')):
            transport=LiveResearchTransport(cfg,permit,lambda p:'fixture-only-key')
            for _ in range(45):
                state=op.step(transport)
                if state['phase']=='COMPLETE':break
            self.assertEqual(state['phase'],'COMPLETE')
            self.assertEqual(state['total_counts'],dict(model=5,search=2,read=1))
            self.assertEqual(len(state['quota_recoveries']),1)
            before=op.journal.path.read_bytes();count=len(launched)
            self.assertEqual(NativeQuotaOpportunity(self.directory,cfg,clock=self.clock).step(transport),state)
            self.assertEqual(len(launched),count);self.assertEqual(op.journal.path.read_bytes(),before)
        self.assertNotIn(b'fixture-only-key',op.journal.path.read_bytes())

    def test_final_worker_guard_binds_quota_sources_after_key_lookup(self):
        for index,target in enumerate(('model','search','read')):
            cfg=self.quota_cfg(run_id=f'research-incident-quota-guard-{index}')
            permit=ResearchPermit(cfg.hash,10000,200000,cfg.budget_micro_usd,9,4,4,'owner:fixture','review:fixture',True)
            snapshot=[cfg.quota_sources];keys=[];launched=[]
            def key(provider):
                keys.append(provider)
                if len(keys)=={'model':1,'search':2,'read':4}[target]:snapshot[0]=(('changed','0'*64),)
                return 'fixture-only-key'
            def process(command,document,*args,**kwargs):
                with kwargs['launch_guard']() as allowed:self.assertTrue(allowed);launched.append(json.loads(document))
                payload=json.loads(document)
                if 'action' in payload:
                    field='content' if payload['action']['action']=='search' else 'raw_content'
                    return dict(outcome='SERVICE_RESPONSE',http_status=200,retrieved_ms=self.clock.now_ms(),raw={
                        'results':[dict(url=URL,title='Fixture',**{field:'Evidence'})],'usage':{'credits':1}})
                return native(cfg.profile,action('search',query='one') if len(keys)==1 else action('read',url=URL))
            op=NativeQuotaOpportunity(self.directory,cfg,clock=self.clock)
            with patch('quota_native.quota_snapshot',side_effect=lambda:snapshot[0]),patch('fantasy_basketball.providers.research_transport.bounded_process',side_effect=process),patch('time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000),patch('socket.socket',side_effect=AssertionError('offline')):
                with self.assertRaisesRegex(ValueError,'quota/incident source changed'):
                    for _ in range(15):op.step(LiveResearchTransport(cfg,permit,key))
            self.assertEqual(len(launched),{'model':0,'search':1,'read':3}[target])

    def test_previous_config_and_unmatched_permit_cannot_reopen(self):
        cfg=self.quota_cfg();op=NativeQuotaOpportunity(self.directory,cfg,clock=self.clock)
        permit=ResearchPermit('0'*64,10000,200000,cfg.budget_micro_usd,9,4,4,'owner:fixture','review:fixture',True)
        with patch('socket.socket',side_effect=AssertionError('offline')):
            with self.assertRaises(ValueError):op.step(LiveResearchTransport(cfg,permit,lambda p: (_ for _ in ()).throw(AssertionError('no key'))))
        old=incidents.IncidentTests.incident_config(self,'mistral')
        with self.assertRaises(ValueError):NativeQuotaOpportunity(self.directory,old,clock=self.clock)
