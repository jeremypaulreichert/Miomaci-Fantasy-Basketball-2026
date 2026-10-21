"""SR1/SR2 adversarial saved-evidence checks; originals never modified."""
from copy import deepcopy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT/'workstreams/build'
spec = importlib.util.spec_from_file_location('screening_reference_repair', BUILD/'audit_screening_repair.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def set_answer(provider, raw, text):
    if provider in ('openai', 'xai'):
        next(p for item in raw['output'] if item.get('type') == 'message'
             for p in item['content'] if p.get('type') == 'output_text')['text'] = text
    elif provider == 'anthropic':
        next(p for p in raw['content'] if p.get('type') == 'text')['text'] = text
    elif provider == 'gemini':
        next(p for p in raw['candidates'][0]['content']['parts']
             if p.get('thought') is not True and 'text' in p)['text'] = text
    elif provider == 'mistral' and isinstance(raw['choices'][0]['message']['content'], list):
        next(p for p in raw['choices'][0]['message']['content'] if p.get('type') == 'text')['text'] = text
    else:
        raw['choices'][0]['message']['content'] = text


class SavedAuditRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = audit.audit()
        cls.original_hashes = cls.baseline['source_pins']
        cls.temp = tempfile.TemporaryDirectory(prefix='audit-repair-', dir=BUILD/'state')
        cls.root = Path(cls.temp.name)
        for name in (*cls.original_hashes,
                     'workstreams/build/LIVE_SCREENING_SESSION_2026-10-17/approval.json'):
            target = cls.root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/name, target)
        cls.records, cls.raw = {}, {}
        for provider in audit.PROVIDERS:
            path = cls.root/'workstreams/build/LIVE_SCREENING_SESSION_2026-10-17'/(provider+'-result.json')
            cls.records[provider] = (path, path.read_bytes())
            run = json.loads(path.read_text())['status']['run_id']
            db_path = next((cls.root/'workstreams/build/state/qualification'/run).glob('*.sqlite3'))
            with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                events = [json.loads(row[0]) for row in db.execute('SELECT document FROM events')]
            cls.raw[provider] = next(e['data']['response']['raw'] for e in events if e['kind'] == 'RESPONSE')

    @classmethod
    def tearDownClass(cls):
        try:
            for name, checksum in cls.original_hashes.items():
                if audit.sha(ROOT/name) != checksum:
                    raise AssertionError('original evidence changed: '+name)
        finally:
            cls.temp.cleanup()

    def setUp(self):
        self.socket = patch('socket.socket', side_effect=AssertionError('offline only'))
        self.connect = patch('socket.create_connection', side_effect=AssertionError('offline only'))
        self.socket.start(); self.connect.start()
        self.addCleanup(self.socket.stop); self.addCleanup(self.connect.stop)

    def reject_mutations(self, mutate):
        for provider in audit.PROVIDERS:
            with self.subTest(provider=provider):
                path, original = self.records[provider]
                data = json.loads(original)
                mutate(data)
                try:
                    path.write_text(json.dumps(data), encoding='utf-8')
                    with self.assertRaises(AssertionError):
                        audit.audit(root=self.root)
                finally:
                    path.write_bytes(original)

    def test_originals_reconcile_with_exact_accounting(self):
        result = audit.audit(root=self.root)
        self.assertEqual(result['inference_requests'], 10)
        self.assertEqual(result['estimated_charge_micro_usd'], 851707)
        self.assertEqual(result['retained_exposure_micro_usd'], 28704062)
        self.assertEqual(result['invoice_confirmed_micro_usd'], 0)

    def test_sr1_both_derived_choices_differ_from_unchanged_native_answer(self):
        def mutate(data):
            s = data['status']
            selected = s['results'][0]['action']['player_id']
            replacement = {'player_id': 'q0610' if selected == 'q0000' else 'q0000',
                           'rationale': 'Valid legal fixture choice but not the native answer'}
            s['attempts'][0]['receipt']['action'] = replacement
            s['results'][0]['action'] = replacement
        self.reject_mutations(mutate)

    def test_sr1_both_derived_rationales_differ_from_native_answer(self):
        def mutate(data):
            s = data['status']
            s['attempts'][0]['receipt']['action']['rationale'] = 'Changed rationale'
            s['results'][0]['action']['rationale'] = 'Changed rationale'
        self.reject_mutations(mutate)

    def test_sr2_reduced_exposure_without_invoice(self):
        def mutate(data):
            a = data['status']['accounting']
            a['unresolved_exposure_micro_usd'] = a['estimated_charge_micro_usd']
        self.reject_mutations(mutate)

    def test_sr2_reduced_exposure_and_committed_together(self):
        def mutate(data):
            a = data['status']['accounting']
            a['unresolved_exposure_micro_usd'] = a['committed_micro_usd'] = a['estimated_charge_micro_usd']
        self.reject_mutations(mutate)

    def test_sr2_committed_amount_alone_cannot_shrink(self):
        def mutate(data):
            a = data['status']['accounting']
            a['committed_micro_usd'] = a['estimated_charge_micro_usd']
        self.reject_mutations(mutate)

    def test_sr2_invoice_claim_without_journal_evidence_rejects(self):
        def mutate(data):
            a = data['status']['accounting']
            a.update(invoice_confirmed_micro_usd=1, unresolved_exposure_micro_usd=0,
                     committed_micro_usd=1)
        self.reject_mutations(mutate)

    def test_qwen_ten_token_slack_is_reserved(self):
        p = json.loads((BUILD/'PRACTICE_DRAFT_2026-10-17/first-screening/qwen-package.json').read_text())['config']['cases'][0]['profile']
        self.assertEqual(audit.reference_charge(p, p['input_tokens'], p['output_tokens']+10), 2200516)
        self.assertEqual(audit.reference_charge(p, p['input_tokens'], p['output_tokens']), 2200456)

    def test_each_native_envelope_extracts_exact_public_action(self):
        for provider, raw in self.raw.items():
            with self.subTest(provider=provider):
                text, action = audit.public_action(provider, raw)
                saved = json.loads(self.records[provider][1])['status']['attempts'][0]['receipt']
                self.assertEqual(text, saved['answer_text'])
                self.assertEqual(action, saved['action'])

    def test_frozen_answer_wrapper_supported_for_every_envelope(self):
        for provider, raw in self.raw.items():
            with self.subTest(provider=provider):
                text, action = audit.public_action(provider, raw)
                changed = deepcopy(raw)
                set_answer(provider, changed, json.dumps({'answer': text}))
                self.assertEqual(audit.public_action(provider, changed)[1], action)

    def test_duplicate_fields_nonfinite_hold_and_bad_rationale_reject(self):
        invalid = ['{"player_id":"q0000","player_id":"q0610","rationale":"x"}',
                   '{"player_id":"q0000","rationale":NaN}',
                   '{"player_id":null,"rationale":"hold"}',
                   '{"player_id":"q0000","rationale":"   "}',
                   json.dumps({'player_id': 'q0000', 'rationale': 'x'*501}),
                   '{"player_id":"q0000","rationale":"x","extra":1}']
        for provider, raw in self.raw.items():
            for text in invalid:
                with self.subTest(provider=provider, text=text[:60]):
                    changed = deepcopy(raw); set_answer(provider, changed, text)
                    with self.assertRaises((ValueError, AssertionError)):
                        audit.public_action(provider, changed)


if __name__ == '__main__':
    unittest.main()
