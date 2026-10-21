"""Separate provider canaries retain bounded entry and durable pause semantics."""
from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workstreams/build'))
from canary_proposal import CRITERIA, proposals, write
from fantasy_basketball.core.config import digest
from fantasy_basketball.draft.trial import NativeTrial
from fantasy_basketball.providers.entry import EntryPermit, resume
from fantasy_basketball.providers.qualification import synthetic_response


class CanaryProposalTests(support.BuildCase):
    def test_packages_are_isolated_and_criteria_are_bound(self):
        rows=proposals()
        self.assertEqual(len(rows),10)
        self.assertEqual(len({package.config.run_id for _,package,_ in rows}),10)
        total=0
        for provider,package,row in rows:
            config=package.config;case=config.cases[0];p=case.profile
            self.assertEqual(len(config.cases),1)
            self.assertEqual((p.provider,case.board.case_id),(provider,'owned-star'))
            self.assertEqual(config.max_requests,2)
            self.assertGreater(config.trial_ms,p.opportunity_ms)
            out=p.output_tokens+(10 if provider=='qwen' else 0)
            exact=Fraction(p.input_usd_per_million)*p.input_tokens+Fraction(p.output_usd_per_million)*out
            bound=2*(-(-exact.numerator//exact.denominator)+p.safety_micro_usd)
            self.assertEqual(config.budget_micro_usd,bound)
            self.assertIsNone(row['physical_cost_bound'])
            self.assertEqual(config.source_plan_hash,digest({'purpose':'ISOLATED_CANARY_ONLY','criteria':CRITERIA,
                'case':asdict(case),'trial_ms':1800000}))
            total+=bound
        self.assertEqual(total,54954130)

    def test_written_templates_cannot_authorize_and_existing_evidence_is_preserved(self):
        destination=self.directory/'proposal'
        summary=write(destination)
        self.assertIs(summary['execution_authorized'],False)
        self.assertEqual(summary['max_requests'],20)
        for provider,package,_ in proposals():
            data=json.loads((destination/(provider+'-permit-template.json')).read_text(encoding='utf-8'))
            data['account_profile_hashes']=tuple(data['account_profile_hashes'])
            permit=EntryPermit(**data)
            with self.assertRaises(ValueError): permit.check(package,self.clock.now_ms())
        before={p.name:p.read_bytes() for p in destination.iterdir()}
        with self.assertRaises(ValueError): write(destination)
        self.assertEqual(before,{p.name:p.read_bytes() for p in destination.iterdir()})

    def test_first_provider_pause_does_not_block_the_other_nine(self):
        calls=[];keys=[]
        with patch('socket.socket',side_effect=AssertionError('offline only')),patch(
                'fantasy_basketball.providers.transport.time.time_ns',side_effect=lambda:self.clock.now_ms()*1000000):
            for index,(provider,package,_) in enumerate(proposals()):
                # An offline fake permit is not a saved owner approval.
                config=package.config
                permit=EntryPermit(package.hash,config.hash,0,11000,config.budget_micro_usd,2,
                    'owner:OFFLINE_FAKE','review:OFFLINE_FAKE','compatibility:OFFLINE_FAKE',
                    (config.cases[0].profile.hash,),'API_METERED_USD_EXCLUDING_TAX',True)
                directory=self.directory/provider
                def credentials(selected):
                    keys.append(selected)
                    return 'FAKE_CANARY_KEY'
                def worker(command,payload,timeout,output_limit,**kwargs):
                    with kwargs['launch_guard']() as allowed:self.assertTrue(allowed)
                    calls.append(provider)
                    return {'outcome':'WORKER_TIMEOUT'} if index==0 else {
                        'outcome':'RESPONSE','raw':synthetic_response(config.cases[0].profile,'p04')}
                with patch('fantasy_basketball.providers.transport.bounded_process',side_effect=worker):
                    result=resume(package,directory,permit,credentials,clock=self.clock)
                self.assertEqual(result['phase'],'PAUSED' if index==0 else 'COMPLETE')
                if index==0:
                    self.assertEqual(result['pause']['reason'],'WORKER_TIMEOUT')
                trial=NativeTrial(directory,config,clock=self.clock)
                before=trial.journal.path.read_bytes()
                self.assertEqual(resume(package,directory,None,lambda _:self.fail('restart key access'),clock=self.clock),result)
                self.assertEqual(before,trial.journal.path.read_bytes())
        self.assertEqual(len(calls),10)
        self.assertEqual(calls,keys)
