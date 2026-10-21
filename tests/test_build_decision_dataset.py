"""Stable identities, bounded projections, privacy and historical denominators."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('decision_reference_export',ROOT/'workstreams/build/decision_dataset/export.py')
export=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(export)


class DatasetTests(unittest.TestCase):
    def sample(self):
        return dict(run_id='research-sample',config_hash='a'*64,index=0,provider='mistral',model='mistral-small-2603',
            observation=dict(status='paused',choice=None,wall_ms=None,pause_history=['RESEARCH_LIMIT'],
                intent_counts={'model':1,'search':0,'read':0},receipt_counts={'model':1,'search':0,'read':0},
                accounting={'estimated_model_micro_usd':12,'retained_micro_usd':500,'invoice_confirmed_micro_usd':0},
                private_secret='DO_NOT_EXPORT_OWNER_NOTE'),
            board={'players':[{'secret_stat':'DO_NOT_EXPORT_PLAYER_FACT'}],'ownership':[]},
            trail=[dict(kind='model_result',key='pick:0:model:0',at_ms=1000,elapsed_ms=10,
                result={'outcome':'RESPONSE','public_answer':'{"action":"search","query":"DO_NOT_EXPORT_QUERY","rationale":"DO_NOT_EXPORT_QUOTE"}',
                        'raw':'DO_NOT_EXPORT_NATIVE','reasoning':'DO_NOT_EXPORT_THOUGHT'})],
            native=[{'key':'pick:0:model:0','observed_model':'mistral-small-2603','usage':{'input_tokens':12,'output_tokens':3,'reasoning_text':'DO_NOT_EXPORT_THOUGHT'}}])

    def test_id_stable_across_serialization_changes_and_distinct_for_identity_changes(self):
        first=export.identifier('research-sample','a'*64,0)
        self.assertEqual(first,export.identifier('research-sample','a'*64,0))
        self.assertEqual(len(first),69)
        self.assertEqual(len({first,export.identifier('research-sample','b'*64,0),export.identifier('research-sample','a'*64,1),export.identifier('research-other','a'*64,0)}),4)
        for bad in (-1,True):
            with self.assertRaises(ValueError):export.identifier('research-sample','a'*64,bad)

    def test_nested_private_values_never_cross_projection(self):
        value=export.record(**self.sample());text=json.dumps(value)
        self.assertNotIn('DO_NOT_EXPORT',text)
        self.assertEqual(value['events'][0]['declared_action'],'search')
        self.assertEqual(value['failure_codes'],['RESEARCH_LIMIT'])
        self.assertIsNone(value['public_explanation']);self.assertIsNone(value['native_usage'][0]['usage']['reasoning_tokens'])
        self.assertEqual(value['accounting']['invoice_confirmed_micro_usd'],0)

    def test_evidence_ids_survive_other_event_insertions_and_duplicates_reject(self):
        args=self.sample();first=export.record(**args)['events'][0]['event_id']
        inserted={'kind':'search_intent','key':'pick:0:search:0','at_ms':900}
        extra=export.record(**(args|{'trail':[inserted,*args['trail']]}))
        self.assertEqual(extra['events'][1]['event_id'],first)
        with self.assertRaisesRegex(ValueError,'duplicate evidence boundary'):
            export.record(**(args|{'trail':args['trail']*2}))

    def test_invalid_metadata_and_ambiguous_public_action_reject_or_remain_unknown(self):
        for changes in ({'provider':'unknown'},{'model':'sk-credentialvalue123456789'},{'config_hash':'bad'}):
            with self.assertRaises(ValueError):export.record(**(self.sample()|changes))
        self.assertIsNone(export.action_label('Planning. {"action":"search"}'))
        self.assertIsNone(export.action_label('{"action":"pick","action":"search"}'))
        self.assertIsNone(export.action_label('{"action":"invented"}'))

    def test_existing_outputs_and_foreign_paths_reject_before_reading_sources(self):
        with patch.object(export,'sources',side_effect=AssertionError('no source access')):
            for p in (ROOT/'workstreams/build/decision_dataset',ROOT/'unowned-output'):
                with self.assertRaises(ValueError):export.build(p)

    def test_manifest_changed_bytes_and_escape_reject(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workstreams/build/state') as tmp:
            root=Path(tmp);p=root/'data';p.write_text('original')
            export.save(root/'manifest.json',{'files':{'data':export.sha(p)}})
            export.verify_manifest(root,root/'manifest.json');p.write_text('changed')
            with self.assertRaises(ValueError):export.verify_manifest(root,root/'manifest.json')
            (root/'manifest.json').write_text(json.dumps({'files':{'../foreign':'a'*64}}))
            with self.assertRaises(ValueError):export.verify_manifest(root,root/'manifest.json')

    def test_real_sources_keep_failed_episodes_denominators_and_native_usage(self):
        with patch('socket.socket',side_effect=AssertionError('offline')):records,runs=export.sources()
        self.assertEqual(len(records),14);self.assertEqual(len(runs),12)
        self.assertEqual(len({r['provider'] for r in records}),10)
        self.assertEqual(sum(r['status']=='settled' for r in records),9)
        self.assertEqual(sum(r['status']=='paused' for r in records),5)
        scheduled=sum(r['scheduled'] for r in runs)
        self.assertGreater(scheduled,14)
        self.assertTrue(all(r['counted'] is False for r in records))
        newer=next(r for r in records if r['provider']=='mistral' and len(r['failure_codes'])==2)
        self.assertEqual(newer['failure_codes'],['MODEL_TRANSPORT_FAILED','RESEARCH_LIMIT'])
        self.assertEqual(newer['reserved_attempt_counts'],{'model':4,'search':2,'read':0})
        self.assertEqual(newer['received_result_counts'],{'model':4,'search':2,'read':0})
        self.assertEqual(newer['events'][-1]['declared_action'],'search')
        model_intents=[e for e in newer['events'] if e['stage']=='model' and e['boundary']=='intent']
        self.assertEqual(model_intents[-1]['remaining_allowance'],{'searches':0,'reads':2,'model_calls_including_this':6})
        self.assertEqual(newer['recorded_recoveries'],{'model_retry':1,'format':0,'read_replan':0})
        self.assertIsNone(newer['selected_player_id'])
        self.assertEqual(sum(r['selected_player_id'] is not None for r in records),9)
        unknown=[r for r in runs if r['accounting']['oversized_read_usage_unknown']]
        self.assertEqual(len(unknown),1);self.assertEqual(unknown[0]['accounting']['missing_service_usage_as_reported'],0)
        self.assertTrue(all(r['configuration']['output_token_cap']>0 for r in records))

    def test_static_html_escapes_evidence_and_stays_unpublished(self):
        html=export.page('Title <script>', '<p>fixed body</p>', {'name':'</script><script>alert(1)</script>'})
        self.assertIn('Title &lt;script&gt;',html);self.assertIn('noindex,nofollow',html)
        embedded=html.split('type="application/ld+json">')[1].split('</script>')[0]
        self.assertNotIn('<',embedded)

    def test_dd1_three_native_hashes_match_independent_turn_reconstruction(self):
        import hashlib
        from copy import deepcopy
        records,_=export.sources()
        for provider in ('meta_direct','mistral','qwen'):
            cfg=json.loads((export.RECHECK/provider/'package.json').read_text(encoding='utf-8'))['config']
            actual=deepcopy(cfg['draft_plan']['board'])
            actual['case_id']='pick-0'
            actual['pick_index']=0
            actual['manager']=cfg['draft_plan']['profiles'][0]['provider']
            reference=hashlib.sha256(json.dumps(actual,ensure_ascii=False,sort_keys=True,
                separators=(',',':'),allow_nan=False).encode()).hexdigest()
            row=next(r for r in records if r['run_id']==cfg['run_id'])
            self.assertEqual(row['board']['snapshot_sha256'],reference)
            self.assertNotEqual(reference,export.digest(cfg['board']))

    def test_dd1_saved_wire_mismatch_rejected(self):
        from copy import deepcopy
        cfg=json.loads((export.RECHECK/'mistral/package.json').read_text(encoding='utf-8'))['config']
        board=deepcopy(cfg['board']);board['case_id']='pick-0'
        decision={k:board[k] for k in ('case_id','pick_index','manager','ownership','locked_ids','source_label')}
        decision['board_hash']=export.digest(board)
        body={'messages':[{'content':json.dumps({'decision':decision})}]}
        trail=[dict(kind='model_intent',key='pick:0:model:0',request={'body':body,'request_hash':export.digest(body)})]
        self.assertEqual(export.recheck_board(cfg,trail),board)
        for field in ('board_hash','case_id'):
            bad=deepcopy(trail);payload=json.loads(bad[0]['request']['body']['messages'][0]['content'])
            payload['decision'][field]='wrong';bad[0]['request']['body']['messages'][0]['content']=json.dumps(payload)
            bad[0]['request']['request_hash']=export.digest(bad[0]['request']['body'])
            with self.assertRaises(ValueError):export.recheck_board(cfg,bad)
        bad=deepcopy(trail);bad[0]['request']['request_hash']='0'*64
        with self.assertRaises(ValueError):export.recheck_board(cfg,bad)
        with self.assertRaises(ValueError):export.recheck_board(cfg,[])


if __name__=='__main__':unittest.main()
