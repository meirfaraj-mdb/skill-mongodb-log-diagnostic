"""No-network tests for new observability contracts and pipeline wiring."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Never run original archive entry points. Load only changed/new modules with stub dependencies.
package = types.ModuleType('agent'); package.__path__ = [str(ROOT / 'agent')]
sys.modules['agent'] = package
skills = types.ModuleType('agent.skills'); sys.modules['agent.skills'] = skills
from agent.observability_contract import sanitize, resolve_source
from agent.observability_analysis import build_analysis, append_sections
from agent.common import Layout
from agent import observability_stage, providers

spec = importlib.util.spec_from_file_location('collector_v2', ROOT / 'skills/mongodb-observability/scripts/observability.py')
collector = importlib.util.module_from_spec(spec); spec.loader.exec_module(collector)


class Store:
    def __init__(self, items=None): self.items = items or {}; self.writes = []
    def exists(self, key): return key in self.items
    def get_text(self, key): return json.dumps(self.items[key])
    def put_text(self, key, value, content_type):
        self.writes.append(key)
        self.items[key] = json.loads(value) if content_type == 'application/json' else value
        return key
    def list_keys(self, prefix): return [key for key in self.items if key.startswith(prefix)]
    def uri(self, key): return 'mock://' + key


class Tests(unittest.TestCase):
    def test_env_keys_and_keys_only_validator(self):
        with patch.dict(os.environ, {'MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED':'true',
              'MONGODB_LOG_DIAG_OBSERVABILITY_SOURCES':'{"query_stats":"mongodb"}',
              'MONGODB_LOG_DIAG_OBSERVABILITY_HOSTS':'a,b'}, clear=True):
            cfg = providers._env_config()
            self.assertTrue(cfg['observability_enabled']); self.assertEqual(cfg['observability_hosts'], ['a','b'])
            self.assertEqual(cfg['observability_sources']['query_stats'], 'mongodb')
            with self.assertRaises(ValueError): providers.normalize_config({'bucket':'bad'}, 'local')

    def test_bucket_never_collects_any_kind(self):
        layout = Layout('p'); items = {layout.observability('2026-10-12','n', k): {} for k in
                       ('query_stats','index_definitions','index_stats','coll_stats','query_plans','metrics','advisor')}
        store = Store(items)
        skills.observability = lambda: self.fail('bucket mode called live collector')
        result = observability_stage.run({'prefix':'p','input_mode':'existing_bucket'}, '2026-10-12', store)
        self.assertEqual(result['status'], 'reused_existing_bucket'); self.assertEqual(store.writes, [])
        self.assertEqual(len(result['nodes'][0]['artifacts']), 8)

    def test_dispatch_supported_and_unsupported_sources(self):
        self.assertEqual(resolve_source({'deployment_type':'atlas'}, 'query_stats'), 'atlas_api')
        with self.assertRaises(ValueError): resolve_source({'observability_sources':{'query_stats':'ops_manager'}}, 'query_stats')
        with patch.object(collector, 'collect_mongodb', return_value={'ok':True}) as mock:
            self.assertEqual(collector.collect({}, 'n', None, 'query_stats', 'mongodb'), {'ok':True})
            mock.assert_called_once()

    def test_redaction_preserves_attribution_and_namespace(self):
        data = sanitize({'key': {'client': {'application': {'name':'MongoDB Compass'}, 'remote':'1.2.3.4'},
                                'queryShape': {'cmdNs': {'db':'shop','coll':'orders'}, 'command':'find', 'filter': {'name':'alice'}}},
                         'password':'secret','uri':'mongodb://user:pass@host'})
        text = json.dumps(data)
        self.assertNotIn('alice', text); self.assertNotIn('1.2.3.4', text); self.assertNotIn('secret', text)
        self.assertEqual(data['key']['queryShape']['cmdNs']['db'], 'shop')
        self.assertEqual(data['key']['client']['application']['name'], 'MongoDB Compass')

    def test_index_coverage_not_uptime(self):
        stats = {'retrievedAt':'2026-10-12T00:00:00Z','collections':[{'namespace':'db.c','indexes':[
             {'name':'x','accesses':{'ops':0,'since':'2025-10-11T00:00:00Z'}},
             {'name':'_id_','accesses':{'ops':0,'since':'2026-10-10T00:00:00Z'}},
             {'name':'unknown','accesses':{'ops':0}}]}]}
        a = build_analysis({'index_stats':stats}, {}, [])
        self.assertIn('year', a['indexes'][0]['classification'])
        self.assertTrue(a['indexes'][1]['protected_semantics'])
        self.assertEqual(a['indexes'][2]['classification'], 'Usage/coverage unknown')

    def test_index_delta_reset_and_hidden_age(self):
        since='2026-10-01T00:00:00Z'
        def snapshot(day, ops, start=since): return {'retrievedAt':day,'collections':[{'namespace':'db.c','indexes':[{'name':'x','accesses':{'ops':ops,'since':start}}]}]}
        cur=snapshot('2026-10-12T00:00:00Z',20); old=snapshot('2026-10-11T00:00:00Z',10)
        definitions={'collections':[{'namespace':'db.c','indexes':[{'name':'x','hidden':True}]}]}
        a=build_analysis({'index_stats':cur,'index_definitions':definitions},{'index_stats':[old]},[])
        self.assertEqual(a['indexes'][0]['last_24h_ops'],10); self.assertIsNone(a['indexes'][0]['hidden_days'])
        old['collections'][0]['indexes'][0]['accesses']['since']='2026-09-30T00:00:00Z'
        self.assertIsNone(build_analysis({'index_stats':cur},{'index_stats':[old]},[])['indexes'][0]['last_24h_ops'])

    def test_top10_filters_tools_ranks_total_and_delta(self):
        def row(hash, count, total, app):
            return {'queryShapeHash':hash,'key':{'client':{'application':{'name':app}},
                     'queryShape':{'cmdNs':{'db':'shop','coll':'orders'},'command':'find'}},
                     'metrics':{'execCount':count,'totalExecMicros':{'sum':total*1000},'firstSeenTimestamp':'2026-10-01T00:00:00Z'}}
        cur={'retrievedAt':'2026-10-12T00:00:00Z','temporal_basis':'cumulative_since_entry_creation_or_reset',
             'data':[row('a',20,2000,'web'),row('b',2,200,'MongoDB Compass'),row('c',3,100,'worker')]}
        old={'retrievedAt':'2026-10-11T00:00:00Z','data':[row('a',10,500,'web')]}
        a=build_analysis({'query_stats':sanitize(cur)},{'query_stats':[sanitize(old)]},[])
        self.assertEqual(a['excluded_tooling_queries'],1); self.assertEqual(a['top_queries'][0]['total_ms'],1500)
        self.assertEqual(a['top_queries'][0]['avg_ms'],150); self.assertEqual(a['top_queries'][0]['exec_count'],10)

    def test_storage_growth_and_no_fragmentation_claim(self):
        def snap(day,size):return {'retrievedAt':day,'collections':[{'namespace':'db.c','stats':[{'storageStats':{'storageSize':size,'freeStorageSize':30}}]}]}
        a=build_analysis({'coll_stats':snap('2026-10-12T00:00:00Z',120)},
                         {'coll_stats':[snap('2026-10-11T00:00:00Z',100)]},[])
        self.assertEqual(a['collections'][0]['storage_growth_bytes'],20)
        self.assertIn('not proof', ' '.join(a['limitations']))

    def test_sections_conditional(self):
        a=build_analysis({'metrics':{}},{},[])
        self.assertNotIn('## Index analysis', append_sections('# Report',a))
        a=build_analysis({'advisor':{},'query_stats':{}},{},[])
        rendered=append_sections('# Report',a)
        self.assertIn('## Index analysis',rendered); self.assertIn('## Top 10',rendered)

    def test_partial_failure_keeps_other_artifacts_and_no_secrets(self):
        def collect(config,host,pid,kind,source,log_date=None):
            if kind=='metrics':raise RuntimeError('mongodb://user:password@host')
            return {'kind':kind}
        skills.observability=lambda:types.SimpleNamespace(collect=collect)
        cfg={'prefix':'p','observability_sources':{k:'bucket' for k in ('query_stats','query_plans','coll_stats','profiler')},
             'mongodb_uri_template':'unused','deployment_type':'atlas'}
        result=observability_stage.collect_nodes(cfg,'2026-10-12',Store(),[{'host':'n','host_dir':'n'}])
        self.assertEqual(result['status'],'partial'); self.assertNotIn('password',json.dumps(result))
        self.assertEqual(result['nodes'][0]['artifacts']['advisor']['status'],'collected')

    def test_advisor_existing_index_and_profiler_counts(self):
        inventory={'collections':[{'namespace':'db.c','indexes':[{'name':'x','key':{'x':1},'hidden':True}]}]}
        advisor={'data':[{'suggestedIndexes':[{'namespace':'db.c','indexes':[{'key':{'x':1}}]}]}]}
        q={'queryShapeHash':'abc','namespace':'db.c','metrics':{'execCount':2,'totalExecMicros':{'sum':2000}}}
        profiler={'collections':[{'records':[{'ts':'2026-10-12T00:00:00Z','millis':1,'ns':'db.c','queryShapeHash':'abc'}]}]}
        a=build_analysis({'index_definitions':inventory,'advisor':advisor,'query_stats':{'data':[q]},'profiler':profiler},{},[])
        self.assertEqual(a['missing_index_candidates'][0]['existing_same_key'][0]['name'],'x')
        self.assertEqual(a['top_queries'][0]['profiler_sample_count'],1)

    def test_mongo_node_local_uses_direct_connection(self):
        mock_client=unittest.mock.Mock()
        with patch.dict(sys.modules, {'pymongo':types.SimpleNamespace(MongoClient=mock_client)}):
            collector._mongo_client({'mongodb_uri_template':'mongodb://u:p@{host}:27017'}, 'node')
            self.assertTrue(mock_client.call_args.kwargs['directConnection'])
        with self.assertRaises(ValueError):
            collector._mongo_client({'mongodb_uri_template':'mongodb+srv://{host}'}, 'node')

    def test_api_query_window_metadata_and_dispatch(self):
        with patch.object(collector,'collect_query_shapes',return_value={'source':'atlas_query_shape_insights','window':{'hours':24},'data':{}}):
            value=collector.collect({},'node','node:27017','query_stats','atlas_api')
            self.assertEqual(value['temporal_basis'],'requested_api_window')
            self.assertEqual(value['window']['hours'],24)

    def test_api_metrics_and_advisor_urls_have_windows(self):
        cfg={'group_id':'g','api_version':'2025-03-12','ops_manager_base_url':'https://ops.example/api/public/v1.0',
             'ops_manager_host_ids':{'node':'host-id'}}
        with patch.object(collector,'_api_pages',return_value=([{'measurements':[]}],False)) as api:
            value=collector.collect_api(cfg,'node','node:27017','metrics','atlas_api')
            url=[c.args[1] for c in api.call_args_list if '/measurements?' in c.args[1]][-1]
            self.assertIn('/processes/node%3A27017/measurements?',url)
            self.assertIn('granularity=PT5M',url); self.assertIn('&m=',url); self.assertEqual(value['window']['hours'],24)
            self.assertTrue(api.call_args.args[1].endswith('/processes/node%3A27017/disks'))
            collector.collect_api(cfg,'node',None,'advisor','ops_manager')
            self.assertIn('/hosts/host-id/performanceAdvisor/suggestedIndexes?',api.call_args.args[1])

    def test_changed_report_integration_with_stub_model(self):
        for name in ('diffing','offline_cve'):
            module=types.ModuleType('agent.'+name)
            sys.modules['agent.'+name]=module
        sys.modules['agent.offline_cve'].check=lambda payload:{'matches':[]}
        skills.diagnostic_skill_dir=lambda: ROOT/'skills/mongodb-log-diagnostic'
        from agent import report_stage
        layout=Layout('p'); store=Store({
            layout.extract('2026-10-12','node','mongodb','extractionOccurence.json'):{'slow':{'operations':[]}},
            layout.query_stats('2026-10-12','node'):{'data':[{'queryShapeHash':'abc','namespace':'db.c','metrics':{'execCount':2,'totalExecMicros':{'sum':2000}}}]}
        })
        llm=types.SimpleNamespace(generate=unittest.mock.Mock(return_value='# Report'))
        result=report_stage.report_node(store,layout,llm,'system','2026-10-12','node','mongodb',600000)
        self.assertIn('## Top 10 workload queries',result['report_md'])
        self.assertIn('observability_analysis',llm.generate.call_args.args[1])
        self.assertEqual(llm.generate.call_count,1)

    def test_existing_array_payload_not_crashing(self):
        a=build_analysis({'query_stats':[{'queryShapeHash':'abc','namespace':'db.c','execCount':2,'avgWorkingMillis':4}],
                          'index_stats':[], 'coll_stats':[]},{},[])
        self.assertEqual(a['top_queries'][0]['total_ms'],8)


if __name__=='__main__':unittest.main()
