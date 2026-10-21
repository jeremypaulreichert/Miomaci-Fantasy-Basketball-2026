"""Cache helpers must be included in every native entry/worker source guard."""
import unittest
from pathlib import Path
from unittest.mock import patch
import json

import test_build_core
import test_build_quota_native as native_tests
from fantasy_basketball.core.replay_cache import VerifiedReplayJournal
from fantasy_basketball.providers.research_transport import code_snapshot
from quota_native import NativeQuotaOpportunity, quota_snapshot
import quota_entry
import research_entry


class CacheIntegrationTests(native_tests.NativeQuotaTests):
    # Inherited native tests intentionally exercise the integrated runtime too.
    def test_helpers_are_bound_at_entry_and_worker(self):
        helpers={'src/fantasy_basketball/core/request_cache.py',
                 'src/fantasy_basketball/core/replay_cache.py'}
        self.assertTrue(helpers <= set(research_entry.INVENTORY))
        self.assertTrue(helpers <= {n for n,h in code_snapshot()})
        self.assertTrue(helpers <= {n for n,h in quota_snapshot()})
        self.assertTrue(helpers <= set(quota_entry.QUOTA_SOURCES))
        cfg=self.quota_cfg()
        op=NativeQuotaOpportunity(self.directory,cfg,clock=self.clock)
        self.assertIsInstance(op.journal,VerifiedReplayJournal)

    def test_old_package_cannot_inherit_new_source_guard(self):
        root=Path(__file__).resolve().parents[1]
        old=json.loads((root/'workstreams/build/QUOTA_NATIVE_QP1_FINAL_2026-10-18/shared20/package.json').read_text())
        with patch('research_entry.private_credential',side_effect=AssertionError('no keys')):
            with self.assertRaisesRegex(ValueError,'QUOTA_SOURCE_CHANGED'):
                quota_entry.decode(old['config'])
