"""Archive integrity, read-only historical projection and inert HTML evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('basketball_time_capsule',ROOT/'workstreams/build/time_capsule/capsule.py')
capsule=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(capsule)


class CapsuleTests(unittest.TestCase):
    def test_html_evidence_cannot_close_data_script_or_inject_markup(self):
        payload={'public_answer':'</script><script>alert(1)</script>','name':'Jokić & <b>test</b>'}
        html=capsule.render(payload)
        start=html.index('<script id="capsule" type="application/json">')
        encoded=html[start:].split('>',1)[1].split('</script>',1)[0]
        self.assertNotIn('<',encoded);self.assertEqual(json.loads(encoded),payload)
        self.assertIn("connect-src 'none'",html)

    def test_manifest_rejects_changed_content_and_parent_escape(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workstreams/build/state') as tmp:
            folder=Path(tmp);file=folder/'data.json';file.write_text('{}')
            capsule.save(folder/'manifest.json',{'files':{'data.json':capsule.sha(file)}})
            self.assertEqual(capsule.verify(folder),1)
            extra=folder/'unexpected';extra.write_text('added')
            with self.assertRaisesRegex(ValueError,'integrity'):capsule.verify(folder)
            extra.unlink()
            file.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError,'integrity'):capsule.verify(folder)
            (folder/'manifest.json').write_text(json.dumps({'files':{'../outside':'0'*64}}))
            with self.assertRaisesRegex(ValueError,'integrity'):capsule.verify(folder)

    def test_build_never_overwrites_existing_output_or_escapes_build(self):
        with self.assertRaises(ValueError):capsule.build(ROOT/'outside-build-capsule')
        with self.assertRaises(ValueError):capsule.build(ROOT/'workstreams/build/time_capsule')

    def test_before_record_does_not_invent_owner_predictions_or_preregistration(self):
        record=capsule.before_record('fixture-time')
        self.assertIsNone(record['owner_statement']);self.assertEqual(record['owner_predictions'],[])
        self.assertEqual(record['status'],'DRAFT_NOT_PREREGISTERED')
        self.assertEqual(record['official_freeze'],{})
        self.assertIn('already observed',record['already_known'])

    def test_real_pilot_projection_is_offline_preserves_prefix_and_journal(self):
        package=ROOT/'workstreams/build/LIVE_RESEARCH_PILOT_SESSION_2026-10-18/package.json'
        with patch('socket.socket',side_effect=AssertionError('no network')):
            run=capsule.evidence_run(package)
        self.assertEqual(len(run['turns']),3)
        self.assertEqual([t['provider'] for t in run['turns']],['anthropic','deepseek','gemini'])
        self.assertFalse(run['counted'])
        second=run['turns'][1]
        self.assertEqual(len(second['board']['ownership']),1)
        self.assertEqual(second['board']['ownership'][0][0],run['turns'][0]['observation']['choice']['player_id'])
        results=[e for e in second['trail'] if e['kind']=='model_result']
        self.assertTrue(results);self.assertTrue(all('raw' not in e['result'] for e in results))
        self.assertTrue(any('public_answer' in e['result'] for e in results))
        self.assertTrue(any(e['kind']=='model_intent' and 'body' in e['request'] for e in second['trail']))

    def test_old_oversized_read_usage_omission_is_annotated_without_rewriting_it(self):
        package=ROOT/'workstreams/build/LIVE_REMAINING_RESEARCH_CYCLES_2026-10-18/qwen/package.json'
        with patch('socket.socket',side_effect=AssertionError('no network')):run=capsule.evidence_run(package)
        reads=[e for e in run['turns'][0]['trail'] if e['kind']=='read_result']
        self.assertEqual(len(reads),1)
        self.assertIn('UNKNOWN_BODY_DISCARDED',reads[0]['result']['native_service_usage'])
        self.assertEqual(run['accounting']['service_usage_missing'],0)  # Original historical omission, explicitly annotated.
        self.assertTrue(run['audit_annotations'])

    def test_explicit_owner_input_is_preserved_but_cannot_claim_formal_freeze(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workstreams/build/state') as tmp:
            path=Path(tmp)/'owner.json';record={'schema':'basketball-owner-diary/1','entries':[{'text':'My own words.'}],'prompts':[]}
            capsule.save(path,record)
            self.assertEqual(capsule.owner_record(path,'basketball-owner-diary/1'),record)
            record=capsule.before_record('fixture');record['status']='PREREGISTERED'
            path.write_text(json.dumps(record),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'freeze is separate'):capsule.owner_record(path,'basketball-before-counted-record/1')

    def test_historical_inventory_rejects_unknown_duplicate_and_missing_sources(self):
        source=ROOT/'workstreams/build/LIVE_RESEARCH_PILOT_SESSION_2026-10-18/package.json'
        original=json.loads(source.read_text(encoding='utf-8'))
        import copy
        changes=[]
        value=copy.deepcopy(original);value['config']['sources'].append(['unexpected.py','0'*64]);changes.append(value)
        value=copy.deepcopy(original);value['config']['sources'].append(value['config']['sources'][0]);changes.append(value)
        value=copy.deepcopy(original);value['config']['sources'].pop();changes.append(value)
        value=copy.deepcopy(original);value['config']['sources'][0][1]='0'*64;changes.append(value)
        with tempfile.TemporaryDirectory(dir=ROOT/'workstreams/build/state') as tmp:
            path=Path(tmp)/'package.json'
            for value in changes:
                path.write_text(json.dumps(value),encoding='utf-8')
                with patch('socket.socket',side_effect=AssertionError('no network')):
                    with self.assertRaises(ValueError):capsule.evidence_run(path)

    def test_changed_historical_archive_rejects_before_journal_or_credentials(self):
        package=ROOT/'workstreams/build/LIVE_RESEARCH_PILOT_SESSION_2026-10-18/package.json'
        original=capsule.sha
        def changed(path):
            if 'EXHIBIT_2026-10-18_FINAL/archive' in path.as_posix():return '0'*64
            return original(path)
        with patch.object(capsule,'sha',side_effect=changed), \
             patch('study_observer.read_events',side_effect=AssertionError('no journal read')), \
             patch('research_entry.private_credential',side_effect=AssertionError('no credentials')), \
             patch('socket.socket',side_effect=AssertionError('no network')):
            with self.assertRaisesRegex(ValueError,'unavailable historical source'):
                capsule.evidence_run(package)


if __name__=='__main__':unittest.main()
