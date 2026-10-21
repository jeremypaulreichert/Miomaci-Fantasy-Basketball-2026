"""Exact standard Wonder support without model fallback or live qualification."""
from dataclasses import asdict,replace
import json
import hashlib
from pathlib import Path
import test_build_core as support
from fantasy_basketball.core.config import canonical
from fantasy_basketball.providers.muse import candidate_profile,wonder_candidate_profile
from fantasy_basketball.providers.research import request,inspect_action
from fantasy_basketball.providers.qualification import synthetic_response
from test_build_research import board,action,URL


class FrontierMetaTests(support.BuildCase):
    def test_only_exact_standard_ids_accepted(self):
        p=wonder_candidate_profile()
        for model in ('muse-wonder-full','muse-wonder-1.0-contributor','muse-spark-1.3-contributor','unknown'):
            with self.assertRaisesRegex(ValueError,'standard Muse'):
                replace(p,model=model,accepted_models=(model,))
        self.assertEqual(p.accepted_models,('muse-wonder-1.0',))

    def test_existing_default_profile_matches_saved_original(self):
        root=Path(__file__).resolve().parents[1]
        folder=root/'workstreams/build/FRONTIER_ADAPTER_CANDIDATE_2026-10-18'
        name='src/fantasy_basketball/providers/muse.py'
        raw=(folder/'original-source'/name).read_bytes()
        expected=json.loads((folder/'preservation.json').read_text())['originals'][name]
        self.assertEqual(hashlib.sha256(raw).hexdigest(),expected)
        namespace=dict(__name__='fantasy_basketball.providers.archived_muse',
                       __package__='fantasy_basketball.providers')
        exec(compile(raw,'archived-muse.py','exec'),namespace)
        self.assertEqual(asdict(candidate_profile()),asdict(namespace['candidate_profile']()))

    def test_wonder_search_read_pick_and_native_caps(self):
        p=wonder_candidate_profile();current=board('meta_direct')
        wire=request(p,current,[],dict(searches=2,reads=2,model_calls_including_this=9),10000,20000)
        self.assertEqual(wire['body']['model'],'muse-wonder-1.0')
        self.assertEqual(wire['body']['max_completion_tokens'],p.output_tokens)
        self.assertEqual(wire['body']['reasoning_effort'],'high')
        self.assertEqual(wire['body']['n'],1)
        self.assertNotIn('tools',wire['body'])
        for value in (action('search',query='Independent query'),action('read',url=URL),action()):
            receipt=inspect_action(p,synthetic_response(p,'a',text=canonical(value)),current.legal_ids(),(URL,))
            self.assertIsNone(receipt['failure']);self.assertEqual(receipt['action'],value)
        self.assertEqual(p.input_usd_per_million,'4');self.assertEqual(p.output_usd_per_million,'20')
        self.assertEqual(p.input_tokens,p.context_ceiling)

    def test_spark_identity_does_not_qualify_wonder(self):
        p=wonder_candidate_profile();current=board('meta_direct')
        raw=synthetic_response(candidate_profile(),'a',text=canonical(action()))
        receipt=inspect_action(p,raw,current.legal_ids(),())
        self.assertIsNotNone(receipt['failure']);self.assertIsNone(receipt['action'])
