import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Load only the replacement modules, without importing enterprise SDKs.
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_bucket_test_agent'
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / 'agent')]
sys.modules[PACKAGE] = pkg
common = types.ModuleType(PACKAGE + '.common')
import logging
common.logger = logging.getLogger('test')
sys.modules[common.__name__] = common
skills = types.ModuleType(PACKAGE + '.skills')
sys.modules[skills.__name__] = skills
spec = importlib.util.spec_from_file_location(PACKAGE + '.providers', ROOT / 'agent/providers.py')
providers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = providers
spec.loader.exec_module(providers)


class InputBucketTests(unittest.TestCase):
    def config(self, **values):
        env = {'MONGODB_LOG_DIAG_BUCKET': values.pop('bucket', '/unused'),
               'MONGODB_LOG_DIAG_INPUT_MODE': 'existing_bucket',
               'MONGODB_LOG_DIAG_PREFIX': 'atlas-logs'}
        env.update({'MONGODB_LOG_DIAG_' + k.upper(): v for k, v in values.items()})
        with patch.dict(os.environ, env, clear=True):
            return providers.normalize_config({}, 'local')

    def test_default_and_blank(self):
        for extra in ({}, {'input_bucket': ' '}):
            cfg = self.config(**extra)
            self.assertEqual(cfg['bucket'], cfg['input_bucket'])
            self.assertIsInstance(providers.get_store(cfg), providers.LocalStore)

    def test_scheme_and_provider_validation(self):
        cfg = self.config(bucket='gs://results', input_bucket='gs://sources')
        self.assertEqual(cfg['input_bucket'], 'sources')
        self.assertEqual(cfg['storage_provider'], 'gcs')
        with self.assertRaises(ValueError):
            self.config(bucket='gs://results', input_bucket='s3://sources')

    def test_read_write_discovery_and_baselines(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.config(bucket=root + '/out', input_bucket=root + '/in')
            routed = providers.get_store(cfg)
            base = 'atlas-logs/2026-10-10/node/'
            raw = base + 'mongodb/mongodb.gz'
            stats = base + 'queryStats/query-stats.json'
            other = base + 'Stats/metrics.json'
            extract = base + 'extracts/mongodb/extractionOccurence.json'
            report = base + 'reports/mongodb/report.md'
            cluster = 'atlas-logs/2026-10-10/cluster/reports/cluster-summary.md'
            baseline = 'atlas-logs/2026-10-09/node/extracts/mongodb/extractionshort.json'
            for k in (raw, stats, other):
                routed.put_text(k, 'input', 'text/plain')
                self.assertTrue(routed.inputs.exists(k))
                self.assertFalse(routed.outputs.exists(k))
                self.assertEqual(routed.get_text(k), 'input')
            for k in (extract, report, cluster, baseline):
                routed.put_text(k, 'result', 'text/plain')
                self.assertTrue(routed.outputs.exists(k))
                self.assertFalse(routed.inputs.exists(k))
                self.assertEqual(routed.get_text(k), 'result')
            stale = base + 'extracts/mongodb/old.json'
            routed.inputs.put_text(stale, 'stale', 'text/plain')
            stray = base + 'Stats/output-only.json'
            routed.outputs.put_text(stray, 'stray', 'text/plain')
            keys = routed.list_keys('atlas-logs/')
            self.assertNotIn(stale, keys)
            self.assertNotIn(stray, keys)
            self.assertFalse(routed.exists(stale))
            self.assertEqual(routed.get_text(baseline), 'result')
            self.assertIn('/out/', routed.uri(cluster))
            self.assertIn('/in/', routed.uri(stats))

    def test_stream_upload_and_download(self):
        import io
        with tempfile.TemporaryDirectory() as root:
            cfg = self.config(bucket=root + '/out', input_bucket=root + '/in')
            store = providers.get_store(cfg)
            for section in ('mongodb', 'extracts'):
                key = 'atlas-logs/2026-10-10/node/' + section + '/test.gz'
                store.upload_stream(io.BytesIO(b'data'), key, 'application/gzip')
                target = Path(root) / (section + '.download')
                store.download(key, target)
                self.assertEqual(target.read_bytes(), b'data')
                expected = store.outputs if section == 'extracts' else store.inputs
                self.assertTrue(expected.exists(key))

    def test_existing_options_remain_available(self):
        for field in ('bucket', 'prefix', 'bedrock_region', 'vertex_model',
                      'cluster_summary', 'input_mode', 'report_max_tokens'):
            self.assertIn(field, providers._ENV_FIELDS)
        self.assertEqual(providers._ENV_FIELDS['input_bucket'], 'INPUT_BUCKET')


if __name__ == '__main__':
    unittest.main()
