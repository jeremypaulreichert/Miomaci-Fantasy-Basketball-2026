"""Native family evidence, bounds and recovery; no provider requests."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workstreams/build'))
from provider_expansion_check import profiles, configuration, responses, check
from fantasy_basketball.core.config import canonical, digest
from fantasy_basketball.draft.trial import NativeTrial
from fantasy_basketball.providers.native import ENDPOINTS, endpoint_for, endpoint_provider, inspect, prepare
from fantasy_basketball.providers.qualification import synthetic_response
from fantasy_basketball.providers import http_worker
from fantasy_basketball.providers.transport import ScriptedNativeTransport


class ExpansionTests(support.BuildCase):
    def test_ten_contract_requests_match_frozen_caps_and_exact_routes(self):
        config, _ = configuration()
        for case in config.cases[:10]:
            p = case.profile
            with self.subTest(provider=p.provider):
                request = prepare(p, case.board, now_ms=1, deadline_ms=600001)
                body = request['body']
                field = p.fixture_limits().output_field
                actual = body['generationConfig'][field] if p.provider == 'gemini' else body[field]
                self.assertEqual(actual, p.output_tokens)
                self.assertEqual(request['reservation_micro_usd'], p.bound_micro_usd)
                self.assertEqual(endpoint_provider(request['endpoint']), p.provider)
                self.assertNotIn('tools', body)
                self.assertNotIn('thinking_budget', body)
                if p.provider == 'meta':
                    self.assertEqual(body['context_length_exceeded_behavior'], 'error')
                    self.assertNotIn('reasoning_effort', body)
                if p.provider == 'qwen':
                    self.assertTrue(body['enable_thinking'])
                    self.assertFalse(body['preserve_thinking'])
                    self.assertNotIn('max_tokens', body)

    def test_native_identity_and_accounting_for_every_family(self):
        for p in profiles():
            with self.subTest(provider=p.provider):
                raw = synthetic_response(p, 'legal')
                receipt = inspect(p, raw, ('legal',))
                self.assertIsNone(receipt['failure'])
                self.assertEqual(receipt['usage']['input_tokens'], 1000)
                self.assertEqual(receipt['usage']['output_tokens'], 100)
                self.assertEqual(receipt['estimated_charge_micro_usd'], 17000)
                for field in ('modelVersion', 'responseId') if p.provider == 'gemini' else ('model', 'id'):
                    broken = deepcopy(raw)
                    broken.pop(field)
                    result = inspect(p, broken, ('legal',))
                    self.assertEqual(result['failure'], 'IDENTITY_MISMATCH')
                    self.assertIsNone(result['action'])

    def test_anthropic_cache_populations_and_missing_evidence(self):
        p = next(p for p in profiles() if p.provider == 'anthropic')
        raw = synthetic_response(p, 'legal')
        raw['usage'].update(input_tokens=10, cache_creation_input_tokens=20, cache_read_input_tokens=30)
        result = inspect(p, raw, ('legal',))
        self.assertEqual(result['usage']['input_tokens'], 60)
        self.assertEqual(result['estimated_charge_micro_usd'], 7600)
        raw['usage'].pop('cache_creation_input_tokens')
        self.assertEqual(inspect(p, raw, ('legal',))['failure'], 'MISSING_OR_INVALID_USAGE')

    def test_gemini_thought_totals_continuation_and_unrequested_tools(self):
        p = next(p for p in profiles() if p.provider == 'gemini')
        raw = synthetic_response(p, 'legal')
        raw['candidates'][0]['finishReason'] = 'CONTINUATION'
        result = inspect(p, raw, ('legal',))
        self.assertEqual(result['failure'], 'INCOMPLETE_RESPONSE')
        self.assertEqual(result['estimated_charge_micro_usd'], 17000)
        raw['usageMetadata']['thoughtsTokenCount'] = 51
        self.assertEqual(inspect(p, raw, ('legal',))['failure'], 'MISSING_OR_INVALID_USAGE')
        raw = synthetic_response(p, 'legal')
        raw['usageMetadata']['toolUsePromptTokenCount'] = 1
        self.assertEqual(inspect(p, raw, ('legal',))['failure'], 'UNREQUESTED_NATIVE_TOOL_USAGE')

    def test_chat_cache_write_and_read_are_subsets_not_extra_input(self):
        p = next(p for p in profiles() if p.provider == 'moonshot')
        raw = synthetic_response(p, 'legal')
        raw['usage']['prompt_tokens_details'] = dict(cached_tokens=300, cache_write_tokens=500)
        self.assertEqual(inspect(p, raw, ('legal',))['estimated_charge_micro_usd'], 17000)
        raw['usage']['prompt_tokens_details']['cache_write_tokens'] = 800
        self.assertEqual(inspect(p, raw, ('legal',))['failure'], 'MISSING_OR_INVALID_USAGE')

    def test_no_family_invents_selection_or_discards_known_usage(self):
        for p in profiles():
            with self.subTest(provider=p.provider):
                for text, failure in [(canonical({'player_id': None, 'rationale': 'Hold'}), 'AI_HOLD'),
                                      ('```json\n{}\n```', 'MALFORMED_ACTION'),
                                      (canonical({'player_id': 'owned', 'rationale': 'Choice'}), 'ILLEGAL_PICK')]:
                    result = inspect(p, synthetic_response(p, 'unused', text=text), ('legal',))
                    self.assertEqual(result['failure'], failure)
                    self.assertIsNone(result['action'])
                    self.assertEqual(result['estimated_charge_micro_usd'], 17000)
                result = inspect(p, synthetic_response(p, 'legal', stop='length'), ('legal',))
                self.assertEqual(result['failure'], 'INCOMPLETE_RESPONSE')
                self.assertEqual(result['estimated_charge_micro_usd'], 17000)

    def test_qwen_documented_ten_token_overrun_is_reserved_and_bounded(self):
        p = next(p for p in profiles() if p.provider == 'qwen')
        self.assertEqual(p.fixture_limits().output_tokens, p.output_tokens + 10)
        ordinary = next(p for p in profiles() if p.provider == 'moonshot')
        self.assertEqual(p.bound_micro_usd - ordinary.bound_micro_usd, 200)
        self.assertIsNone(inspect(p, synthetic_response(p, 'legal', output_tokens=p.output_tokens+10), ('legal',))['failure'])
        result = inspect(p, synthetic_response(p, 'legal', output_tokens=p.output_tokens+11), ('legal',))
        self.assertEqual(result['failure'], 'USAGE_BOUND_EXCEEDED')
        self.assertIsNotNone(result['estimated_charge_micro_usd'])

    def test_all_native_auth_headers_use_single_exact_tls_endpoint(self):
        for p in profiles():
            seen = []
            raw = canonical(synthetic_response(p, 'legal')).encode()
            class Response:
                status = 200
                def read(self, size): return raw
                def getheader(self, name, default): return default
            class Connection:
                def __init__(self, host, **kwargs): seen.append(host)
                def request(self, method, path, **kwargs): seen.append((method, path, kwargs))
                def getresponse(self): return Response()
                def close(self): pass
            result = http_worker.perform(dict(endpoint=endpoint_for(p), credential='FAKE_TEST_KEY',
                body={'model': p.model}, timeout_ms=600000, request_bytes=10000, response_bytes=262144), Connection)
            self.assertEqual(result['outcome'], 'RESPONSE')
            headers = seen[1][2]['headers']
            name = 'x-api-key' if p.provider == 'anthropic' else 'x-goog-api-key' if p.provider == 'gemini' else 'Authorization'
            self.assertIn('FAKE_TEST_KEY', headers[name])
            self.assertNotIn('FAKE_TEST_KEY', seen[1][1])
            self.assertNotIn('FAKE_TEST_KEY', canonical(result))
            self.assertEqual(seen[1][0], 'POST')

    def test_forbidden_route_variants_and_hosted_model_names_fail_closed(self):
        for endpoint in ENDPOINTS.values():
            endpoint = endpoint.replace('{model}', 'synthetic')
            for bad in (endpoint+'?api-key=bad', endpoint+'/', endpoint.replace('https:', 'http:'), endpoint.replace('/v1', '.evil/v1')):
                if bad != endpoint:
                    self.assertIsNone(endpoint_provider(bad), bad)
        meta = next(p for p in profiles() if p.provider == 'meta')
        for bad in ('other-vendor/model', 'meta-llama/../model', 'meta-llama/model?x=1'):
            with self.assertRaises(ValueError): replace(meta, model=bad)
        gemini = next(p for p in profiles() if p.provider == 'gemini')
        with self.assertRaises(ValueError): replace(gemini, model='meta-llama/model')

    def test_all_ten_recover_and_unknown_pauses_without_dispatch(self):
        with patch('socket.socket', side_effect=AssertionError('offline only')):
            result = check()
        self.assertEqual(result['settled_picks'], 20)
        self.assertEqual(result['simulated_calls'], 30)
        self.assertEqual(result['same_ai_corrections'], 10)
        self.assertEqual(result['restart_calls'], 0)
        self.assertEqual(result['unknown_case']['settled'], 4)
        self.assertGreater(result['unknown_case']['accounting']['unresolved_exposure_micro_usd'], 0)
