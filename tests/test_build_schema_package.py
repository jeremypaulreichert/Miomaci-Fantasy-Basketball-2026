"""Actual generated-package preview and unchanged dispatch gates; offline."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_build_core as support

BUILD = Path(__file__).resolve().parents[1]/'workstreams/build'
sys.path.insert(0,str(BUILD))
import research_entry as entry
PACKAGE = BUILD/'SCHEMA_INTEGRATION_GS1_2026-10-18/package.json'

class SchemaPackageTests(support.BuildCase):
    def invoke(self, *arguments):
        output=io.StringIO()
        with patch.object(sys,'argv',['research_entry.py',*map(str,arguments)]),redirect_stdout(output), \
                patch.object(entry,'private_credential',side_effect=AssertionError('no credential lookup')), \
                patch('fantasy_basketball.draft.research.ResearchOpportunity',side_effect=AssertionError('no journal')), \
                patch('socket.socket',side_effect=AssertionError('offline only')):
            entry.main()
        return json.loads(output.getvalue())

    def test_generated_saved_package_previews_without_credentials_or_journal(self):
        document=json.loads(PACKAGE.read_text())
        before=PACKAGE.read_bytes()
        result=self.invoke('preview','--package',PACKAGE)
        self.assertEqual(result['config_hash'],entry.decode_config(document['config']).hash)
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['credentials_accessed'])
        self.assertEqual(result['unresolved'],document['unresolved'])
        self.assertEqual(PACKAGE.read_bytes(),before)

    def test_reintroduced_extra_field_is_rejected_before_decode(self):
        document=json.loads(PACKAGE.read_text())
        document['config_hash']='0'*64
        path=self.directory/'bad-package.json';path.write_text(json.dumps(document))
        with patch.object(entry,'decode_config',side_effect=AssertionError('shape rejected first')):
            with self.assertRaisesRegex(ValueError,'PACKAGE_SHAPE'):
                self.invoke('preview','--package',path)

    def test_unresolved_saved_package_still_blocks_resume_before_permit_or_keys(self):
        permit=self.directory/'unused-permit.json';permit.write_text('{}')
        with self.assertRaisesRegex(ValueError,'UNRESOLVED_PACKAGE'):
            self.invoke('resume','--package',PACKAGE,'--permit',permit)
