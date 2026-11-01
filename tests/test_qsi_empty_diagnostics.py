"""Empty Query Shape Insights results must be explained, retried and never silently 'available'."""
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def load_observability():
    """Load the collector by path: other test modules replace ``agent.skills`` with stubs."""
    import importlib.util
    name = "qsi_test_observability_collector"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "skills/mongodb-observability/scripts/observability.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]

NOW = datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc)
CONFIG = {"group_id": "g" * 24, "cluster_name": "stg-cluster01", "atlas_public_key": "pub",
          "atlas_private_key": "priv", "timezone": "Asia/Jerusalem"}
SHAPE = {"queryShapeHash": "A" * 64, "namespace": "db.c", "command": "find",
         "metrics": {"execCount": 5, "docsExaminedRatio": 50.0}}


class _Opener:
    """Answers by processIds: mapping value -> summaries; key None = cluster-wide call."""

    def __init__(self, by_process):
        self.by_process, self.urls = by_process, []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        pid = (parse_qs(urlsplit(request.full_url).query).get("processIds") or [None])[0]
        body = {"queryStatsCollection": {"mode": "RATE_LIMITED", "rateLimitPerSecond": 100},
                "summaries": self.by_process.get(pid, [])}
        return io.BytesIO(json.dumps(body).encode())


def _pids(opener):
    return [(parse_qs(urlsplit(u).query).get("processIds") or [None])[0] for u in opener.urls]


class CollectorDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.obs = load_observability()
        self.obs._CLUSTER_WIDE_CACHE.clear()

    def _collect(self, opener, pid, process):
        with mock.patch.object(self.obs, "_atlas_opener", return_value=opener):
            return self.obs.collect_api(CONFIG, process.get("hostname"), pid, "query_stats", "atlas_api",
                                        log_date="2026-10-30", now=NOW, process=process)

    def test_shard_mongod_empty_because_mongos_collects(self):
        opener = _Opener({None: [SHAPE]})
        shard = {"typeName": "SHARD_PRIMARY", "hostname": "h1", "port": 27017}
        result = self._collect(opener, "h1:27017", shard)
        self.assertEqual(result["diagnostics"]["reason"], "collected_on_mongos_only")
        self.assertIn("mongos", result["diagnostics"]["explanation"])
        # Cluster-wide list copied into the node file, explicitly not attributed to the node.
        self.assertEqual(result["scope"], "cluster")
        self.assertIsNone(result["processId"])
        self.assertEqual(result["requestedProcessId"], "h1:27017")
        self.assertIn("do not attribute", result["attribution_note"])
        self.assertEqual(result["data"]["summaries"][0]["queryShapeHash"], "A" * 64)
        self.assertEqual(result["diagnostics"]["fallback"], "cluster_wide_copy")
        self.assertEqual(result["cluster_wide"]["data"]["summaries"][0]["queryShapeHash"], "A" * 64)
        self.assertEqual(result["diagnostics"]["collection_mode"], "RATE_LIMITED")

    def test_cluster_wide_call_is_cached_per_window(self):
        opener = _Opener({None: [SHAPE]})
        for host in ("h1", "h2"):
            self._collect(opener, f"{host}:27017", {"typeName": "SHARD_SECONDARY", "hostname": host, "port": 27017})
        self.assertEqual(_pids(opener).count(None), 1)

    def test_alias_process_id_retry(self):
        opener = _Opener({"alias-1:27017": [SHAPE]})
        process = {"typeName": "REPLICA_PRIMARY", "hostname": "h1", "userAlias": "alias-1", "port": 27017}
        result = self._collect(opener, "h1:27017", process)
        self.assertEqual(result["diagnostics"]["reason"], "process_id_not_matched")
        self.assertEqual(result["processId"], "alias-1:27017")
        self.assertEqual(len(result["data"]["summaries"]), 1)
        self.assertNotIn("cluster_wide", result)

    def test_secondary_without_reads(self):
        opener = _Opener({"p:27017": [SHAPE], None: [SHAPE]})
        result = self._collect(opener, "s:27017", {"typeName": "REPLICA_SECONDARY", "hostname": "s", "port": 27017})
        self.assertEqual(result["diagnostics"]["reason"], "no_reads_on_this_process")

    def test_whole_cluster_empty(self):
        opener = _Opener({})
        result = self._collect(opener, "h1:27017", {"typeName": "REPLICA_PRIMARY", "hostname": "h1", "port": 27017})
        self.assertEqual(result["diagnostics"]["reason"], "no_shapes_in_cluster")
        self.assertNotIn("cluster_wide", result)

    def test_non_empty_makes_single_call(self):
        opener = _Opener({"h1:27017": [SHAPE]})
        result = self._collect(opener, "h1:27017", {"typeName": "REPLICA_PRIMARY", "hostname": "h1", "port": 27017})
        self.assertEqual(len(opener.urls), 1)
        self.assertNotIn("reason", result["diagnostics"])

    def test_diagnostics_can_be_disabled(self):
        opener = _Opener({None: [SHAPE]})
        with mock.patch.object(self.obs, "_atlas_opener", return_value=opener):
            self.obs.collect_api({**CONFIG, "query_shape_empty_diagnostics": False}, "h1", "h1:27017",
                                 "query_stats", "atlas_api", log_date="2026-10-30", now=NOW)
        self.assertEqual(len(opener.urls), 1)


class _Store:
    def __init__(self):
        self.data = {}

    def put_text(self, key, text, content_type=None):
        self.data[key] = text
        return "mem://" + key

    def exists(self, key):
        return key in self.data

    def get_text(self, key):
        return self.data[key]

    def uri(self, key):
        return "mem://" + key


class StageAndReportTest(unittest.TestCase):
    def test_stage_routes_mongos_and_stores_cluster_file_once(self):
        from agent import observability_stage
        from agent.common import Layout

        calls = []

        class Collector:
            def collect(self, config, host, process_id, kind, source, log_date=None, process=None):
                calls.append((host, process_id, kind, (process or {}).get("typeName")))
                return {"data": {"summaries": []}, "window": {"log_date": log_date},
                        "diagnostics": {"reason": "no_shapes_in_cluster"},
                        "cluster_wide": {"scope": "cluster", "data": {"summaries": [SHAPE]}}}

        class Atlas:
            @staticmethod
            def target_hosts(config):
                return [{"hostname": "h1", "port": 27017, "typeName": "SHARD_PRIMARY"},
                        {"hostname": "h1", "port": 27016, "typeName": "SHARD_MONGOS"},
                        {"hostname": "h2", "port": 27017, "typeName": "SHARD_SECONDARY"}]

            @staticmethod
            def safe_filename(host):
                return host

        store = _Store()
        config = {"observability_sources": {"query_stats": "atlas_api"}, "prefix": "p"}
        with mock.patch.object(observability_stage.skills, "atlas_logs", return_value=Atlas, create=True), \
                mock.patch.object(observability_stage.skills, "observability", return_value=Collector(),
                                  create=True), \
                mock.patch("agent.observability_contract.resolve_source",
                           side_effect=lambda cfg, kind: "atlas_api" if kind == "query_stats" else "disabled"):
            nodes = observability_stage._discover_atlas_nodes(config)
            out = observability_stage.collect_nodes(config, "2026-10-30", store, nodes)
        self.assertIn(("h1", "h1:27016", "query_stats", "SHARD_MONGOS"), calls)
        self.assertIn(("h2", "h2:27017", "query_stats", "SHARD_SECONDARY"), calls)
        layout = Layout.from_config(config)
        self.assertIn(layout.cluster_query_stats("2026-10-30"), store.data)
        self.assertIn("cluster_query_stats", out)
        node = out["nodes"][0]["artifacts"]["query_stats"]
        self.assertEqual(node["empty_reason"], "no_shapes_in_cluster")
        self.assertNotIn("cluster_wide", json.loads(store.data[layout.query_stats("2026-10-30", "h1")]))

    def test_summary_marks_empty_with_reason_and_adds_cluster_scope(self):
        from agent.common import Layout
        from agent.report_stage import summarize_query_stats

        layout = Layout.from_config({"prefix": "p"})
        store = _Store()
        store.put_text(layout.query_stats("2026-10-30", "h1"), json.dumps({
            "data": {"queryStatsCollection": {"mode": "RATE_LIMITED", "rateLimitPerSecond": 100}, "summaries": []},
            "diagnostics": {"reason": "collected_on_mongos_only", "explanation": "mongos only"}}))
        store.put_text(layout.cluster_query_stats("2026-10-30"), json.dumps({"data": {"summaries": [SHAPE]}}))
        out = summarize_query_stats(store, layout, "2026-10-30", [("h1", "mongod")])
        self.assertEqual(out[0]["status"], "empty")
        self.assertEqual(out[0]["reason"], "collected_on_mongos_only")
        self.assertEqual(out[0]["rate_limit_per_second"], 100)
        self.assertEqual(out[1]["scope"], "cluster")
        self.assertEqual(out[1]["shapes_found"], 1)


class ManualScriptTest(unittest.TestCase):
    def setUp(self):
        root = ROOT / "scripts"
        sys.path.insert(0, str(root))
        import atlas_collect_observability as script
        self.script = script
        script._QSI_CLUSTER_CACHE.clear()

    def tearDown(self):
        sys.path.pop(0)

    def test_manual_shard_node_reason_and_cluster_payload(self):
        script = self.script

        class Client:
            def __init__(self):
                self.calls = []

            def get(self, path, params=None):
                pid = dict(params or []).get("processIds")
                self.calls.append(pid)
                return {"summaries": [SHAPE] if pid is None else []}

        class Args:
            group_id, cluster_name, qsi_max_summaries, qsi_details = "g" * 24, "c", 100, 0

        client = Client()
        node = {"host": "h1", "host_dir": "h1", "process_id": "h1:27017", "port": 27017,
                "type": "SHARD_PRIMARY", "user_alias": "c-shard-00-00.x.mongodb.net"}
        since = datetime(2026, 10, 29, 22, tzinfo=timezone.utc)
        until = datetime(2026, 10, 30, 22, tzinfo=timezone.utc)
        out = script.collect_qsi(client, Args, node, since, until, "log_day_window", {}, NOW)
        self.assertEqual(out["diagnostics"]["reason"], "collected_on_mongos_only")
        self.assertEqual(out["scope"], "cluster")
        self.assertEqual(len(out["data"]["summaries"]), 1)
        self.assertEqual(client.calls, ["h1:27017", "c-shard-00-00.x.mongodb.net:27017", None])
        self.assertEqual(len(out["_cluster_wide"]["data"]["summaries"]), 1)


if __name__ == "__main__":
    unittest.main()
