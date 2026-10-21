"""Direct Meta, delayed local settlement and gated evolving qualification."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workstreams/build'))
from native_draft_check import configuration
from fantasy_basketball.draft.trial import NativeTrial, RecoveryDecision
from fantasy_basketball.providers import entry
from fantasy_basketball.providers.draft_entry import PreparedDraft,decode_package,roster
from fantasy_basketball.providers.muse import candidate_profile
from fantasy_basketball.providers.native import inspect,prepare,endpoint_for,endpoint_provider
from fantasy_basketball.providers.qualification import synthetic_response
from fantasy_basketball.providers.transport import HTTPTransport
from provider_cases import cases


class AwayWorkTests(support.BuildCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(patch.stopall)
        patch('socket.socket',side_effect=AssertionError('offline only')).start()
        patch('fantasy_basketball.providers.transport.time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000).start()

    def draft(self):
        config=replace(configuration('away-live-fake'),mode='LIVE_DIAGNOSTIC',
                       schema='basketball-native-draft/2',experiment_id='miomaci-basketball-native-draft-qualification')
        return PreparedDraft(config,entry.source_snapshot(),9000000,'1'*64,
                             tuple((p.hash,'2'*64) for p in config.profiles),
                             'source:OFFLINE_FAKE','rules:OFFLINE_FAKE','cohort:OFFLINE_FAKE')

    def permit(self,package):
        config=package.config
        return entry.EntryPermit(package.hash,config.hash,0,900000,config.budget_micro_usd,config.max_requests,
            'owner:OFFLINE_FAKE','review:OFFLINE_FAKE','compatibility:OFFLINE_FAKE',
            tuple(sorted({c.profile.hash for c in config.cases})),'API_METERED_USD_EXCLUDING_TAX',True)

    def test_meta_direct_serialization_identity_usage_and_no_route_substitution(self):
        p=candidate_profile();b=cases('meta_direct')['owned_star']
        request=prepare(p,b,now_ms=10000,deadline_ms=1210000)
        self.assertEqual(request['endpoint'],'https://api.meta.ai/v1/chat/completions')
        self.assertEqual(endpoint_provider(request['endpoint']),'meta_direct')
        self.assertEqual(endpoint_provider('https://api.together.ai/v1/chat/completions'),'meta')
        body=request['body']
        self.assertEqual(body['max_completion_tokens'],16384)
        self.assertEqual(body['reasoning_effort'],'medium')
        self.assertFalse(body['stream'])
        self.assertNotIn('context_length_exceeded_behavior',body)
        raw=synthetic_response(p,'p04')
        raw['usage']['completion_tokens_details']={'reasoning_tokens':90}
        receipt=inspect(p,raw,b.legal_ids())
        self.assertIsNone(receipt['failure'])
        self.assertEqual(receipt['usage']['reasoning_tokens'],90)
        self.assertEqual(receipt['estimated_charge_micro_usd'],6675)  # 1250 input + 425 output + 5000 safety.
        raw['model']='meta-llama/Llama-3.3-70B-Instruct-Turbo'
        self.assertEqual(inspect(p,raw,b.legal_ids())['failure'],'IDENTITY_MISMATCH')
        for change in ({'model':'muse-spark-1.3-contributor'},{'effort':'none'}):
            with self.assertRaises(ValueError):replace(p,**change)

    def test_meta_missing_identity_usage_and_cutoff_pause_contract(self):
        p=candidate_profile();b=cases('meta_direct')['owned_star']
        for field in ('model','usage','id'):
            raw=synthetic_response(p,'p04');raw.pop(field)
            self.assertIsNotNone(inspect(p,raw,b.legal_ids())['failure'])
        self.assertEqual(inspect(p,synthetic_response(p,'p04',stop='length'),b.legal_ids())['failure'],'INCOMPLETE_RESPONSE')
        raw=synthetic_response(p,'p04');raw['usage']['completion_tokens_details']={'reasoning_tokens':101}
        self.assertIsNotNone(inspect(p,raw,b.legal_ids())['failure'])

    def worker(self,package,calls,*,correction=False,unknown=False):
        def send(command,payload,timeout,output_limit,**kwargs):
            with kwargs['launch_guard']() as allowed:self.assertTrue(allowed)
            state=NativeTrial(self.directory,package.config,clock=self.clock).journal.read()
            op=state['op'];i=op['index'];attempt=len(op['attempts'])-1
            calls.append((i,attempt))
            if unknown:return {'outcome':'WORKER_TIMEOUT'}
            picks=('p4','p2','p1','p3')
            selected='p4' if correction and i==1 and attempt==0 else picks[i]
            return {'outcome':'RESPONSE','raw':synthetic_response(package.config.cases[i].profile,selected)}
        return send

    def test_evolving_draft_uses_native_transport_correction_and_zero_call_restart(self):
        package=self.draft();permit=self.permit(package);calls=[];keys=[]
        def credentials(provider):keys.append(provider);return 'FAKE_DRAFT_KEY'
        with patch('fantasy_basketball.providers.transport.bounded_process',side_effect=self.worker(package,calls,correction=True)):
            result=entry.resume(package,self.directory,permit,credentials,clock=self.clock)
        self.assertEqual(result['phase'],'COMPLETE')
        self.assertEqual(calls,[(0,0),(1,0),(1,1),(2,0),(3,0)])
        self.assertEqual(len(keys),2)
        self.assertEqual(len({r['action']['player_id'] for r in result['results']}),4)
        trial=NativeTrial(self.directory,package.config,clock=self.clock);before=trial.journal.path.read_bytes()
        self.assertEqual(entry.resume(package,self.directory,None,lambda _:self.fail('no restart keys'),clock=self.clock),result)
        self.assertEqual(before,trial.journal.path.read_bytes())
        evidence=roster(package,self.directory)
        self.assertIs(evidence['competitive_authorization'],False)
        self.assertIs(evidence['simulated'],False)
        with self.assertRaises(ValueError):trial.export_roster()
        self.assertEqual(decode_package(json.loads(json.dumps(asdict(package)))),package)

    def test_draft_incomplete_gates_and_drift_reject_without_keys_or_state(self):
        package=self.draft()
        with self.assertRaises(ValueError):replace(package,qualification_evidence=())
        pending=replace(package,qualification_evidence=(),unresolved=('representative qualification missing',))
        for selected,permit in ((package,None),(pending,self.permit(pending)),
                                (replace(package,criteria_hash='3'*64),self.permit(package))):
            with self.assertRaises(ValueError):entry.resume(selected,self.directory,permit,lambda _:self.fail('no keys'),clock=self.clock)
        self.assertEqual(list(self.directory.iterdir()),[])
        with self.assertRaises(ValueError):replace(package.config,mode='LIVE_COMPETITION')

    def test_unknown_draft_preserves_exposure_and_never_retries(self):
        package=self.draft();calls=[]
        with patch('fantasy_basketball.providers.transport.bounded_process',side_effect=self.worker(package,calls,unknown=True)):
            result=entry.resume(package,self.directory,self.permit(package),lambda _:'FAKE_KEY',clock=self.clock)
        self.assertEqual(result['phase'],'PAUSED')
        self.assertEqual(result['accounting']['unresolved_exposure_micro_usd'],package.config.profiles[0].bound_micro_usd)
        self.assertEqual(entry.resume(package,self.directory,None,lambda _:self.fail('no keys'),clock=self.clock),result)
        self.assertEqual(calls,[(0,0)])

    def test_saved_last_draft_response_settles_after_expiry_without_dispatch(self):
        package=self.draft();calls=[];responses=[]
        def fault(point):
            if point=='after:RESPONSE':
                responses.append(1)
                if len(responses)==4:raise SystemExit(77)
        trial=NativeTrial(self.directory,package.config,clock=self.clock,fault=fault)
        transport=HTTPTransport(package.config,entry.bound_authorization(package,self.permit(package)),
                                {p.provider:'FAKE_KEY' for p in package.config.profiles})
        with patch('fantasy_basketball.providers.transport.bounded_process',side_effect=self.worker(package,calls)):
            with self.assertRaises(SystemExit):trial.resume(transport)
        self.clock.advance(package.config.trial_ms+900000)
        changed=replace(package,criteria_hash='3'*64)
        before=trial.journal.path.read_bytes()
        with self.assertRaises(ValueError):entry.resume(changed,self.directory,None,lambda _:self.fail('no keys'),clock=self.clock)
        self.assertEqual(before,trial.journal.path.read_bytes())
        result=entry.resume(package,self.directory,None,lambda _:self.fail('no keys'),clock=self.clock)
        self.assertEqual(result['phase'],'COMPLETE');self.assertEqual(len(calls),4)

    def test_ca1_new_bound_criteria_allow_delayed_local_settlement(self):
        from canary_proposal_v2 import proposals,CRITERIA
        package=proposals()[0][1];permit=self.permit(package);calls=[]
        def fault(point):
            if point=='after:RESPONSE':raise SystemExit(77)
        trial=NativeTrial(self.directory,package.config,clock=self.clock,fault=fault)
        def send(command,payload,timeout,output_limit,**kwargs):
            with kwargs['launch_guard']() as allowed:self.assertTrue(allowed)
            calls.append(1)
            return {'outcome':'RESPONSE','raw':synthetic_response(package.config.cases[0].profile,'p04')}
        with patch('fantasy_basketball.providers.transport.bounded_process',side_effect=send):
            with self.assertRaises(SystemExit):trial.resume(HTTPTransport(package.config,entry.bound_authorization(package,permit),{'openai':'FAKE_KEY'}))
        self.clock.advance(1800001)
        self.assertEqual(entry.resume(package,self.directory,None,lambda _:self.fail('no keys'),clock=self.clock)['phase'],'COMPLETE')
        timing=trial.timing_report()['picks'][0]
        self.assertEqual(timing['valid_response_since_open_ms'],0)
        self.assertEqual(timing['open_to_commit_wall_ms_including_pauses'],1800001)
        self.assertEqual(len(calls),1)
        self.assertEqual(CRITERIA['schema'],'basketball-canary-criteria/2')

    def test_stress_boards_preserve_data_and_independent_tight_center_legality(self):
        from qualification_scenarios import scenarios
        boards=scenarios('meta_direct')
        self.assertEqual([len(b.ownership) for b in boards],[0,65,129,129])
        self.assertTrue(all(len(b.players)==627 and b.roster_size==13 and len(b.slots)==8 for b in boards))
        tight=boards[-1];unavailable=set(dict(tight.ownership))|set(tight.locked_ids)
        expected={p.player_id for p in tight.players if 'C' in p.positions and p.player_id not in unavailable}
        self.assertEqual(set(tight.legal_ids()),expected)
        packet=json.dumps(tight.compact_packet(),ensure_ascii=False)
        self.assertIn('José 雪',packet);self.assertIn('Ignore prior instructions',packet)
        self.assertNotIn('q0000',tight.legal_ids())

    def test_cold_draft_cli_is_explicit_and_read_only_until_a_permit(self):
        from test_build_entry_cli import EntryCLITests
        script=EntryCLITests.copied(self)
        package=self.draft();path=self.directory/'draft-package.json'
        path.write_text(json.dumps(asdict(package)),encoding='utf-8')
        before={p:p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        for command in ('preview','status','explain','timing','roster'):
            result=EntryCLITests.run_cli(self,script,command,'--draft','--package',str(path))
            self.assertEqual(result.returncode,0,result.stderr)
        for args in (('resume','--draft','--package',str(path)),('preview','--package',str(path)),('preview','--draft')):
            result=EntryCLITests.run_cli(self,script,*args)
            self.assertEqual(result.returncode,2)
            self.assertNotIn('API key',result.stderr+result.stdout)
        self.assertEqual(before,{p:p.read_bytes() for p in self.directory.rglob('*') if p.is_file()})
        self.assertFalse((self.directory/'workstreams/build/state').exists())
        source=self.directory/'src/fantasy_basketball/providers/draft_entry.py'
        marker=self.directory/'unexpected-import'
        source.write_bytes(source.read_bytes()+("\nfrom pathlib import Path\nPath("+repr(str(marker))+").write_text('bad')\n").encode())
        result=EntryCLITests.run_cli(self,script,'preview','--draft','--package',str(path))
        self.assertEqual(result.returncode,2);self.assertFalse(marker.exists())

    def interrupted_intermediate(self,package,permit):
        def fault(point):
            if point=='after:RESPONSE':raise SystemExit(77)
        trial=NativeTrial(self.directory,package.config,clock=self.clock,fault=fault)
        calls=[]
        transport=HTTPTransport(package.config,entry.bound_authorization(package,permit),
                                {p.provider:'FAKE_KEY' for p in package.config.profiles})
        with patch('fantasy_basketball.providers.transport.bounded_process',side_effect=self.worker(package,calls)):
            with self.assertRaises(SystemExit):trial.resume(transport)
        trial.fault=None;trial.journal.fault=None
        self.assertEqual(len(calls),1)
        return trial,calls

    def test_expired_intermediate_settles_then_pauses_before_permit_or_keys(self):
        for variant in ('trial','authority'):
            with self.subTest(variant=variant):
                self.clock.advance(10000-self.clock.now_ms())
                package=self.draft();package=replace(package,config=replace(package.config,run_id='intermediate-'+variant))
                permit=self.permit(package)
                if variant=='trial':permit=replace(permit,expires_ms=4000000)
                trial,calls=self.interrupted_intermediate(package,permit)
                self.clock.advance(package.config.trial_ms+1 if variant=='trial' else 900001)
                for supplied in (None,permit):
                    result=entry.resume(package,self.directory,supplied,lambda _:self.fail('expired run requested keys'),clock=self.clock)
                    self.assertEqual(result['phase'],'PAUSED')
                    self.assertEqual(result['pause']['reason'],'DEADLINE' if variant=='trial' else 'AUTHORIZATION_EXPIRED')
                    self.assertEqual(len(result['results']),1)
                    before=trial.journal.path.read_bytes()
                    self.assertEqual(entry.resume(package,self.directory,supplied,lambda _:self.fail('no keys'),clock=self.clock),result)
                    self.assertEqual(before,trial.journal.path.read_bytes())
                self.assertEqual(len(calls),1)

    def test_explicit_saved_recovery_also_records_remaining_expiry_pause(self):
        package=self.draft();permit=replace(self.permit(package),expires_ms=4000000)
        trial,calls=self.interrupted_intermediate(package,permit)
        trial.journal.transact(lambda state:[('PAUSE',{'reason':'UNKNOWN_DISPATCH'})])
        self.clock.advance(package.config.trial_ms+1)
        decision=RecoveryDecision('saved-expired',package.config.hash,trial.explain_pause()['state_hash'],'SETTLE_SAVED','owner:OFFLINE_FAKE')
        result=entry.recover(package,self.directory,decision,clock=self.clock)
        self.assertEqual(result['phase'],'PAUSED');self.assertEqual(result['pause']['reason'],'DEADLINE')
        self.assertEqual(len(result['results']),1);self.assertEqual(len(calls),1)
        before=trial.journal.path.read_bytes()
        self.assertEqual(entry.recover(package,self.directory,decision,clock=self.clock),result)
        self.assertEqual(before,trial.journal.path.read_bytes())

    def test_expired_started_draft_pauses_without_saved_response_or_credentials(self):
        package=self.draft();permit=replace(self.permit(package),expires_ms=4000000)
        def fault(point):
            if point=='after:START':raise SystemExit(77)
        trial=NativeTrial(self.directory,package.config,clock=self.clock,fault=fault)
        with self.assertRaises(SystemExit):trial.resume(HTTPTransport(package.config,entry.bound_authorization(package,permit),
            {p.provider:'FAKE_KEY' for p in package.config.profiles}))
        self.clock.advance(package.config.trial_ms+1)
        result=entry.resume(package,self.directory,permit,lambda _:self.fail('no keys'),clock=self.clock)
        self.assertEqual(result['phase'],'PAUSED');self.assertEqual(result['pause']['reason'],'DEADLINE')
        self.assertEqual(result['reserved_requests'],0)

    def test_settlement_and_deadline_pause_are_one_atomic_transaction(self):
        package=self.draft();permit=replace(self.permit(package),expires_ms=4000000)
        trial,calls=self.interrupted_intermediate(package,permit)
        before=trial.journal.path.read_bytes();self.clock.advance(package.config.trial_ms+1)
        def fault(point):
            if point=='before_commit:PAUSE':raise SystemExit(78)
        trial.journal.fault=fault
        with patch.object(entry,'NativeTrial',return_value=trial):
            with self.assertRaises(SystemExit):entry.resume(package,self.directory,None,lambda _:self.fail('no keys'),clock=self.clock)
        self.assertEqual(before,trial.journal.path.read_bytes())
        self.assertEqual(len(trial.journal.read()['results']),0)
        trial.journal.fault=None
        result=entry.resume(package,self.directory,None,lambda _:self.fail('no keys'),clock=self.clock)
        self.assertEqual(result['phase'],'PAUSED');self.assertEqual(len(result['results']),1)
        self.assertEqual(len(calls),1)
