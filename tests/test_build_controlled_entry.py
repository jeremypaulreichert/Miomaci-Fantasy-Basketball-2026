"""Explicit diagnostic entry tested with fake authority, keys and HTTP worker."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support
from test_build_native_trial import configuration
from fantasy_basketball.draft.trial import NativeTrial
from fantasy_basketball.providers import entry
from fantasy_basketball.providers.qualification import synthetic_response
from fantasy_basketball.providers.transport import HTTPTransport, LiveAuthorization


class ControlledEntryTests(support.BuildCase):
    def setUp(self):
        super().setUp()
        self.config=configuration(mode='LIVE_DIAGNOSTIC')
        self.package=entry.PreparedDiagnostic(self.config,entry.source_snapshot(),2000000)
        self.permit=entry.EntryPermit(self.package.hash,self.config.hash,9999,11000,
            self.config.budget_micro_usd,self.config.max_requests,'owner:FAKE','review:FAKE','compatibility:FAKE',
            tuple(sorted({c.profile.hash for c in self.config.cases})),'API_METERED_USD_EXCLUDING_TAX',True)
        self.calls=[]
        def worker(command,payload,timeout,output_limit,**kwargs):
            with kwargs['launch_guard']() as allowed:
                self.assertTrue(allowed)
            self.calls.append(json.loads(payload))
            return {'outcome':'RESPONSE','raw':synthetic_response(self.config.cases[0].profile,'p05')}
        self.worker=worker
        self.addCleanup(patch.stopall)
        patch('socket.socket',side_effect=AssertionError('offline only')).start()
        patch('fantasy_basketball.providers.transport.bounded_process',side_effect=worker).start()
        patch('fantasy_basketball.providers.transport.time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000).start()

    def keys(self,provider): return 'FAKE_ENTRY_KEY'

    def never_keys(self,provider): raise AssertionError('credential access forbidden')

    def test_status_and_package_roundtrip_are_read_only(self):
        result=entry.status(self.package,self.directory,clock=self.clock)
        self.assertEqual(result['phase'],'NOT_STARTED')
        decoded=entry.decode_package(json.loads(json.dumps(asdict(self.package))))
        self.assertEqual(decoded,self.package)
        self.assertEqual(decoded.hash,self.package.hash)
        self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.calls,[])

    def test_missing_false_changed_and_expired_permits_reject_before_credentials(self):
        changes=({'paid_execution_authorized':False},{'package_hash':'0'*64},{'config_hash':'0'*64},
                 {'budget_micro_usd':self.config.budget_micro_usd+1},{'max_requests':1},
                 {'expires_ms':10000},{'not_before_ms':10001}, {'account_profile_hashes':()})
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ValueError):
                entry.resume(self.package,self.directory,replace(self.permit,**change),self.never_keys,clock=self.clock)
        with self.assertRaises(ValueError): entry.resume(self.package,self.directory,None,self.never_keys,clock=self.clock)
        self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.calls,[])

    def test_strict_boolean_billing_scope_and_nonsecret_references(self):
        for change in ({'paid_execution_authorized':1},{'expires_ms':True}, {'billing_scope':'UNKNOWN'},
                       {'owner_record':'FAKE_RAW_KEY'},{'review_record':'review:'},{'compatibility_record':'compatibility:two words'}):
            with self.subTest(change=change),self.assertRaises(ValueError): replace(self.permit,**change)

    def test_unresolved_documentation_source_drift_and_foreign_path_reject(self):
        blocked=replace(self.package,unresolved=('unverified price',))
        with self.assertRaises(ValueError): entry.resume(blocked,self.directory,replace(self.permit,package_hash=blocked.hash),self.never_keys,clock=self.clock)
        with patch.object(entry,'source_snapshot',return_value=()),self.assertRaisesRegex(ValueError,'source snapshot'):
            entry.resume(self.package,self.directory,self.permit,self.never_keys,clock=self.clock)
        with self.assertRaises(ValueError): entry.resume(self.package,self.directory.parent.parent.parent,self.permit,self.never_keys,clock=self.clock)
        self.assertEqual(self.calls,[])

    def test_approved_fake_run_and_completed_restart_need_no_second_key_or_call(self):
        result=entry.resume(self.package,self.directory,self.permit,self.keys,clock=self.clock)
        self.assertEqual(result['phase'],'COMPLETE')
        self.assertEqual(len(self.calls),1)
        trial=NativeTrial(self.directory,self.config,clock=self.clock)
        before=trial.journal.path.read_bytes()
        self.clock.advance(2000)
        self.assertEqual(entry.resume(self.package,self.directory,None,self.never_keys,clock=self.clock),result)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(trial.journal.path.read_bytes(),before)

    def test_prompt_expiry_or_cancellation_creates_no_journal_and_exposes_no_value(self):
        def slow(provider):
            self.clock.advance(2000)
            return 'FAKE_ENTRY_KEY'
        with self.assertRaises(ValueError): entry.resume(self.package,self.directory,self.permit,slow,clock=self.clock)
        self.clock.advance(-2000)
        def fail(provider): raise RuntimeError('FAKE_SECRET_VALUE_MUST_NOT_ESCAPE')
        with self.assertRaises(ValueError) as error:
            entry.resume(self.package,self.directory,self.permit,fail,clock=self.clock)
        self.assertNotIn('FAKE_SECRET_VALUE',str(error.exception))
        self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.calls,[])

    def crashed_trial(self,point):
        def fault(event):
            if event==point: raise SystemExit(77)
        trial=NativeTrial(self.directory,self.config,clock=self.clock,fault=fault)
        auth=entry.bound_authorization(self.package,self.permit)
        transport=HTTPTransport(self.config,auth,{'openai':'FAKE_ENTRY_KEY'})
        with self.assertRaises(SystemExit): trial.resume(transport)
        return trial

    def test_saved_timely_response_settles_after_expiry_without_key_or_resend(self):
        self.crashed_trial('after:RESPONSE')
        self.clock.advance(2000)
        result=entry.resume(self.package,self.directory,None,self.never_keys,clock=self.clock)
        self.assertEqual(result['phase'],'COMPLETE')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(result['results'][0]['action']['player_id'],'p05')

    def test_unknown_dispatch_pauses_locally_with_all_exposure_without_keys(self):
        trial=self.crashed_trial('after:DISPATCH')
        result=entry.resume(self.package,self.directory,None,self.never_keys,clock=self.clock)
        self.assertEqual(result['phase'],'PAUSED')
        self.assertEqual(result['pause']['reason'],'UNKNOWN_DISPATCH')
        self.assertEqual(result['accounting']['unresolved_exposure_micro_usd'],self.config.cases[0].profile.bound_micro_usd)
        before=trial.journal.path.read_bytes()
        self.assertEqual(entry.resume(self.package,self.directory,None,self.never_keys,clock=self.clock),result)
        self.assertEqual(trial.journal.path.read_bytes(),before)
        self.assertEqual(self.calls,[])

    def test_authority_change_is_rejected_before_keys_and_without_journal_edit(self):
        trial=self.crashed_trial('after:START')
        before=trial.journal.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'authority differs'):
            entry.resume(self.package,self.directory,replace(self.permit,owner_record='owner:OTHER'),self.never_keys,clock=self.clock)
        self.assertEqual(trial.journal.path.read_bytes(),before)
        self.assertEqual(self.calls,[])

    def test_document_expiry_and_unfunded_preparation_reject(self):
        expired=replace(self.package,documentation_valid_until_ms=10001)
        with self.assertRaises(ValueError): entry.resume(expired,self.directory,replace(self.permit,package_hash=expired.hash),self.never_keys,clock=self.clock)
        with self.assertRaises(ValueError):
            entry.PreparedDiagnostic(replace(self.config,budget_micro_usd=1),entry.source_snapshot(),2000000)

    def test_durable_authorization_binds_package_permit_and_compatibility(self):
        entry.resume(self.package,self.directory,self.permit,self.keys,clock=self.clock)
        state=NativeTrial(self.directory,self.config,clock=self.clock).journal.read()
        saved=json.loads(state['authorization']['owner_record'])
        self.assertEqual(saved['package_hash'],self.package.hash)
        self.assertEqual(saved['permit_hash'],support.digest(asdict(self.permit)))
        self.assertEqual(saved['compatibility'],self.permit.compatibility_record)

    def test_source_drift_during_key_prompt_prevents_journal_and_dispatch(self):
        def changed(provider):
            patch.object(entry,'source_snapshot',return_value=()).start()
            return 'FAKE_ENTRY_KEY'
        with self.assertRaisesRegex(ValueError,'source snapshot'):
            entry.resume(self.package,self.directory,self.permit,changed,clock=self.clock)
        self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.calls,[])

    def test_pause_during_key_prompt_prevents_dispatch_and_preserves_pause(self):
        trial=self.crashed_trial('after:START')
        def paused(provider):
            trial.journal.transact(lambda state:[('PAUSE',{'reason':'AUTHORIZATION_EXPIRED'})])
            return 'FAKE_ENTRY_KEY'
        result=entry.resume(self.package,self.directory,self.permit,paused,clock=self.clock)
        self.assertEqual(result['phase'],'PAUSED')
        self.assertEqual(result['pause']['reason'],'AUTHORIZATION_EXPIRED')
        self.assertEqual(self.calls,[])

    def test_saved_response_cannot_be_settled_under_a_changed_entry_package(self):
        trial=self.crashed_trial('after:RESPONSE')
        before=trial.journal.path.read_bytes()
        changed=replace(self.package,documentation_valid_until_ms=2000001)
        with self.assertRaisesRegex(ValueError,'entry package differs'):
            entry.resume(changed,self.directory,None,self.never_keys,clock=self.clock)
        self.assertEqual(trial.journal.path.read_bytes(),before)
        self.assertEqual(len(self.calls),1)
