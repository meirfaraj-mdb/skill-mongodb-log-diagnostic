import importlib.util
import json
import logging
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('logging_test_agent')
pkg.__path__ = [str(ROOT / 'agent')]
sys.modules[pkg.__name__] = pkg
common = types.ModuleType(pkg.__name__ + '.common')
common.logger = logging.getLogger('safe_logging_test')
sys.modules[common.__name__] = common
contract = types.ModuleType(pkg.__name__ + '.observability_contract')
contract.ARTIFACTS = ('query_stats', 'index_stats')
sys.modules[contract.__name__] = contract
spec = importlib.util.spec_from_file_location(pkg.__name__ + '.safe_logging', ROOT / 'agent/safe_logging.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class Store:
    scheme = 'gs'
    def uri(self, key):
        bucket = 'results' if '/extracts/' in key or '/reports/' in key else 'inputs'
        return f'gs://{bucket}/{key}'
    def upload(self, *args): return 'receipt'
    def upload_stream(self, *args): return 'receipt'
    def put_text(self, *args): return 'receipt'

class SafeLoggingTests(unittest.TestCase):
    def test_allowlist_excludes_all_credentials_and_queries(self):
        cfg = dict(bucket='results', input_bucket='inputs', prefix='logs', cloud='gcp',
                   atlas_private_key='SECRET1', atlas_public_key='SECRET2',
                   ops_manager_public_key='SECRET3', ops_manager_private_key='SECRET4',
                   mongodb_uri_template='mongodb://user:SECRET5@host/',
                   query_plan_requests=[{'password': 'SECRET6'}],
                   api_key='SECRET7', nested={'password':'SECRET8'})
        with self.assertLogs(common.logger, level='INFO') as logs: mod.log_config(cfg)
        text = '\n'.join(logs.output)
        for i in range(1,9): self.assertNotIn('SECRET'+str(i), text)
        self.assertIn('inputs', text)
        self.assertIn('results', text)
    def test_uri_credentials_removed(self):
        self.assertEqual(mod.safe_locator('https://user:password@host/a?token=secret#fragment'), 'https://host/a')
        self.assertEqual(mod.safe_locator('mongodb+srv://user:pass@host'), '[REDACTED_CONNECTION_STRING]')
    def test_routed_bucket_and_return_values(self):
        store = mod.LoggingStore(Store())
        with self.assertLogs(common.logger, level='INFO') as logs:
            self.assertEqual(store.put_text('logs/date/node/queryStats/data.json', 'SENSITIVE_CONTENT', 'application/json'), 'receipt')
            self.assertEqual(store.put_text('logs/date/node/reports/mongodb/report.md', 'PII_CONTENT', 'text/markdown'), 'receipt')
        self.assertIn('"destination_bucket": "inputs"', logs.output[0])
        self.assertIn('"destination_bucket": "results"', logs.output[1])
        self.assertNotIn('SENSITIVE_CONTENT', str(logs.output))
        self.assertNotIn('PII_CONTENT', str(logs.output))
    def test_generated_file_path(self):
        with self.assertLogs(common.logger, level='INFO') as logs:
            mod.LoggingStore(Store()).upload('/tmp/extract.json', 'logs/date/node/extracts/mongodb/data.json', 'application/json', {'api_key':'NOPE'})
        self.assertIn('/tmp/extract.json', logs.output[0])
        self.assertNotIn('NOPE', str(logs.output))
    def test_stream_not_read_by_logger(self):
        class Stream:
            def read(self, *args): raise AssertionError('Logger must not consume stream')
        with self.assertLogs(common.logger, level='INFO'):
            self.assertEqual(mod.LoggingStore(Store()).upload_stream(Stream(), 'logs/date/node/mongodb/log.gz', 'application/gzip'), 'receipt')
    def test_failure_not_logged_as_success(self):
        class Broken(Store):
            def put_text(self, *args): raise RuntimeError('SECRET')
        with self.assertNoLogs(common.logger, level='INFO'):
            with self.assertRaises(RuntimeError): mod.LoggingStore(Broken()).put_text('x', 'secret', 'text/plain')
    def test_wrap_idempotent(self):
        wrapped = mod.with_write_logging(Store())
        self.assertIs(mod.with_write_logging(wrapped), wrapped)

if __name__ == '__main__': unittest.main()
