import importlib.util
import pathlib
import unittest
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts/update_offline_driver_cves.py'
spec = importlib.util.spec_from_file_location('update_offline_driver_cves', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'$schema': mod.SCHEMA, 'catalog_name': 'reviewed snapshot', 'entries': [
            {'driver': 'mongo-go-driver', 'cve': 'CVE-2026-88031', 'affected': [{'introduced': '2.0.0', 'fixed': '2.9.1'}],
             'source': 'https://vendor.example/advisory', 'reviewed': True, 'summary': 'Reviewed', 'scope_note': 'GridFS only'}]}
        self.record = {'id': 'GHSA-test', 'aliases': ['CVE-2026-88031'], 'summary': 'OSV text', 'affected': [
            {'package': {'ecosystem': 'Go', 'name': 'go.mongodb.org/mongo-driver/v2'},
             'ranges': [{'type': 'SEMVER', 'events': [{'introduced': '2.0.0'}, {'fixed': '2.9.1'}]}]}]}
    def fetch(self, url, body=None):
        if url.endswith('/query'):
            return {'vulns': [{'id': 'GHSA-test'}]} if body['package']['name'] == 'go.mongodb.org/mongo-driver/v2' else {'vulns': []}
        return self.record
    def test_preserves_review_only_when_exact_range_same(self):
        result, stats = mod.refresh(self.catalog, self.fetch, '2026-10-04')
        self.assertEqual(stats['reviewed_unchanged'], 1)
        self.assertEqual(result['entries'][0]['source'], 'https://vendor.example/advisory')
        self.assertEqual(result['entries'][0]['scope_note'], 'GridFS only')
    def test_changed_range_is_unreviewed(self):
        self.record['affected'][0]['ranges'][0]['events'][1]['fixed'] = '2.9.2'
        result, stats = mod.refresh(self.catalog, self.fetch, '2026-10-04')
        self.assertEqual(stats['needs_review'], 1)
        self.assertEqual(stats['previously_reviewed_not_retained'], ['CVE-2026-88031'])
        self.assertFalse(result['entries'][0]['reviewed'])
    def test_wrong_package_and_open_range_ignored(self):
        self.record['affected'][0]['package']['name'] = 'unrelated'
        with self.assertRaisesRegex(ValueError, 'No representable'):
            mod.refresh(self.catalog, self.fetch, '2026-10-04')
        self.record['affected'][0]['package']['name'] = 'go.mongodb.org/mongo-driver/v2'
        self.record['affected'][0]['ranges'][0]['events'] = [{'introduced': '2.0.0'}]
        with self.assertRaisesRegex(ValueError, 'No representable'):
            mod.refresh(self.catalog, self.fetch, '2026-10-04')
    def test_full_language_coverage_declares_gaps(self):
        self.assertEqual(set(mod.COVERAGE), {"C", "C++", ".NET/C#", "Go", "Java", "Kotlin", "Node.js/JS/TS", "PHP", "Python", "Ruby", "Rust", "Scala"})
        self.assertEqual(mod.COVERAGE['C'][0], 'gap')
        self.assertEqual(mod.COVERAGE['C++'][0], 'gap')
        self.assertEqual(mod.COVERAGE['PHP'][0], 'partial')
        self.assertEqual(len(mod.PACKAGES), 16)
        self.assertEqual(len(mod.PACKAGES), len(set((ecosystem, package) for _, ecosystem, package in mod.PACKAGES)))
        result, stats = mod.refresh(self.catalog, self.fetch, '2026-10-04')
        self.assertEqual(stats['packages_queried'], 16)
        self.assertEqual(stats['coverage']['C++']['status'], 'gap')
        self.assertIn('PHP native extension', result['coverage_note'])

    def test_unrelated_dependency_record_is_not_attributed_to_driver(self):
        self.record['affected'].append({'package': {'ecosystem': 'Maven', 'name': 'org.mongodb:mongodb-driver-core'},
            'ranges': [{'type': 'ECOSYSTEM', 'events': [{'introduced': '1.0.0'}, {'fixed': '8.0.0'}]}]})
        result, _ = mod.refresh(self.catalog, self.fetch, '2026-10-04')
        self.assertEqual(result['entries'][0]['driver'], 'mongo-go-driver')
        self.assertEqual(result['entries'][0]['affected'], [{'introduced': '2.0.0', 'fixed': '2.9.1'}])

    def test_network_failure_does_not_produce_catalog(self):
        with self.assertRaisesRegex(OSError, 'network'):
            mod.refresh(self.catalog, lambda *args: (_ for _ in ()).throw(OSError('network')))
    def test_repeated_pagination_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'pagination'):
            mod.refresh(self.catalog, lambda *args: {'vulns': [], 'next_page_token': 'same'})

if __name__ == '__main__':
    unittest.main()
