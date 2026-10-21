"""Cold source gates, non-writing reads and independently calculated proposal."""
from dataclasses import asdict, replace
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workstreams/build'))
import qualification_entry as cli
from qualification_proposal import build
from fantasy_basketball.providers.entry import decode_package
from fantasy_basketball.providers import entry
from fantasy_basketball.providers.transport import HTTPTransport, LiveAuthorization
from fantasy_basketball.providers.qualification import synthetic_response
from fantasy_basketball.draft.trial import NativeTrial, RecoveryDecision
from test_build_native_trial import configuration


class EntryCLITests(support.BuildCase):
    def copied(self):
        manifest=json.loads((cli.ROOT/cli.ENGINE).read_text(encoding='utf-8'))
        paths=[*manifest['files'],cli.ENGINE,'workstreams/build/qualification_entry.py',cli.DEFAULT_PACKAGE]
        for name in paths:
            destination=self.directory/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(cli.ROOT/name,destination)
        return self.directory/'workstreams/build/qualification_entry.py'

    def run_cli(self,script,*args):
        return subprocess.run([sys.executable,'-I','-B',str(script),*args],cwd=self.directory,
                              capture_output=True,text=True,timeout=20)

    def test_preview_and_missing_status_write_nothing_and_resume_needs_permit(self):
        script=self.copied()
        before={p:p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        preview=self.run_cli(script,'preview')
        self.assertEqual(preview.returncode,0,preview.stderr)
        data=json.loads(preview.stdout)
        self.assertEqual((data['cases'],data['max_requests'],data['budget_micro_usd']),(40,80,220000000))
        self.assertIs(data['execution_authorized'],False)
        self.assertIs(data['credentials_accessed'],False)
        result=self.run_cli(script,'status')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'],'NOT_STARTED')
        rejected=self.run_cli(script,'resume')
        self.assertEqual(rejected.returncode,2)
        self.assertNotIn('API key',rejected.stderr)
        self.assertEqual(before,{p:p.read_bytes() for p in self.directory.rglob('*') if p.is_file()})
        self.assertFalse((self.directory/'workstreams/build/state').exists())

    def test_changed_entry_or_worker_is_rejected_before_import_marker(self):
        script=self.copied();marker=self.directory/'unexpected-import'
        for name in ('src/fantasy_basketball/providers/entry.py','src/fantasy_basketball/providers/http_worker.py',
                     'workstreams/build/qualification_proposal.py'):
            path=self.directory/name;original=path.read_bytes()
            path.write_bytes(original+("\nfrom pathlib import Path\nPath("+repr(str(marker))+").write_text('executed')\n").encode())
            result=self.run_cli(script,'preview')
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertFalse(marker.exists())
            path.write_bytes(original)

    def test_duplicate_nonfinite_and_unknown_data_do_not_echo_inputs(self):
        script=self.copied();bad=self.directory/'bad.json'
        for payload in ('{"schema":1,"schema":2,"secret":"FAKE_PRIVATE_VALUE"}',
                        '{"value":NaN,"secret":"FAKE_PRIVATE_VALUE"}',
                        '{"unexpected":"FAKE_PRIVATE_VALUE"}'):
            bad.write_text(payload,encoding='utf-8')
            result=self.run_cli(script,'preview','--package',str(bad))
            self.assertEqual(result.returncode,2)
            self.assertNotIn('FAKE_PRIVATE_VALUE',result.stderr+result.stdout)

    def test_proposal_is_deterministic_and_budget_uses_independent_fraction_bounds(self):
        package,summary=build()
        self.assertEqual(package.hash,build()[0].hash)
        decoded=decode_package(json.loads(json.dumps(asdict(package))))
        self.assertEqual(decoded,package)
        self.assertEqual(len({c.profile.provider for c in package.config.cases}),10)
        total=0
        for case in package.config.cases:
            p=case.profile
            out=p.output_tokens+(10 if p.provider=='qwen' else 0)
            bound=Fraction(p.input_usd_per_million)*p.input_tokens+Fraction(p.output_usd_per_million)*out
            total+=(-(-bound.numerator//bound.denominator)+p.safety_micro_usd)*p.max_attempts
        self.assertEqual(summary['max_exposure_micro_usd'],total)
        self.assertEqual(summary['proposed_budget_micro_usd'],(-(-total//1000000))*1000000)
        self.assertEqual(summary['max_requests'],80)
        for flag in ('execution_authorized','competitive_authorization','models_qualified','accounts_verified'):
            self.assertIs(summary[flag],False)

    def test_recovery_preserves_package_binding_and_allows_expired_same_package(self):
        script=self.copied()
        for variant in ('unchanged','documentation','unresolved','legacy','malformed','missing'):
            with self.subTest(variant=variant):
                config=replace(configuration(mode='LIVE_DIAGNOSTIC'),run_id='entry-recovery-'+variant)
                package=entry.PreparedDiagnostic(config,entry.source_snapshot(),2000000)
                permit=entry.EntryPermit(package.hash,config.hash,9999,11000,config.budget_micro_usd,
                    config.max_requests,'owner:FAKE','review:FAKE','compatibility:FAKE',
                    tuple(sorted({c.profile.hash for c in config.cases})),'API_METERED_USD_EXCLUDING_TAX',True)
                directory=self.directory/'workstreams/build/state/qualification'/config.run_id
                trial=NativeTrial(directory,config,clock=self.clock)
                calls=[]
                def worker(command,payload,timeout,output_limit,**kwargs):
                    with kwargs['launch_guard']() as allowed: self.assertTrue(allowed)
                    trial.journal.transact(lambda state:[('PAUSE',{'reason':'UNKNOWN_DISPATCH'})])
                    calls.append(1)
                    return {'outcome':'RESPONSE','raw':synthetic_response(config.cases[0].profile,'p05')}
                authorization=entry.bound_authorization(package,permit)
                if variant in {'legacy','malformed'}:
                    authorization=LiveAuthorization(config.hash,11000,
                        'owner:LEGACY' if variant=='legacy' else '{}','review:FAKE')
                with patch('socket.socket',side_effect=AssertionError('offline only')),patch(
                        'fantasy_basketball.providers.transport.bounded_process',side_effect=worker),patch(
                        'fantasy_basketball.providers.transport.time.time_ns',return_value=10000000000):
                    if variant!='missing':
                        trial.resume(HTTPTransport(config,authorization,{'openai':'FAKE_KEY'}))
                selected=replace(package,documentation_valid_until_ms=2000001) if variant=='documentation' else (
                    replace(package,unresolved=('changed compatibility context',)) if variant=='unresolved' else package)
                decision=RecoveryDecision('entry-recovery',config.hash,
                    trial.explain_pause()['state_hash'] if variant!='missing' else '0'*64,'SETTLE_SAVED','owner:FAKE')
                package_path=self.directory/'selected.json';recovery_path=self.directory/'decision.json'
                package_path.write_text(json.dumps(asdict(selected)),encoding='utf-8')
                recovery_path.write_text(json.dumps(asdict(decision)),encoding='utf-8')
                before=trial.journal.path.read_bytes()
                result=self.run_cli(script,'recover','--package',str(package_path),'--recovery',str(recovery_path))
                self.assertEqual(result.returncode,0 if variant=='unchanged' else 2,result.stderr)
                self.assertNotIn('API key',result.stdout+result.stderr)
                self.assertEqual(len(calls),0 if variant=='missing' else 1)
                if variant=='unchanged':
                    self.assertEqual(trial.status()['phase'],'COMPLETE')
                    self.assertEqual(len(trial.status()['results']),1)
                else:
                    self.assertEqual(trial.journal.path.read_bytes(),before)
