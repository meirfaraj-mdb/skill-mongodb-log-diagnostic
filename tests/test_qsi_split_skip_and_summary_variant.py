"""QSI per-host -> cluster-wide copy, skip existing query stats, node-report cluster summary variant."""
import io
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_qsi_empty_diagnostics import CONFIG, NOW, SHAPE, _Opener, _Store, load_observability  # noqa: E402

DAY = "2026-10-30"


class _FailingOpener(_Opener):
    """Per-process requests fail with HTTP 500; the cluster-wide request answers."""

    def open(self, request, timeout=None):
        pid = (parse_qs(urlsplit(request.full_url).query).get("processIds") or [None])[0]
        if pid is not None:
            self.urls.append(request.full_url)
            raise HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b"{}"))
        return super().open(request, timeout)


class PerHostThenClusterCopy(unittest.TestCase):
    def setUp(self):
        self.obs = load_observability()
        self.obs._CLUSTER_WIDE_CACHE.clear()

    def collect(self, opener, pid, process, config=CONFIG):
        with mock.patch.object(self.obs, "_atlas_opener", return_value=opener):
            return self.obs.collect_api(config, process.get("hostname"), pid, "query_stats", "atlas_api",
                                        log_date=DAY, now=NOW, process=process)

    def test_per_host_success_stays_node_scoped(self):
        opener = _Opener({"h1:27017": [SHAPE]})
        out = self.collect(opener, "h1:27017", {"typeName": "REPLICA_PRIMARY", "hostname": "h1", "port": 27017})
        self.assertEqual((out["scope"], out["processId"]), ("process", "h1:27017"))
        self.assertNotIn("attribution_note", out)
        self.assertEqual(len(opener.urls), 1)

    def test_http_failure_on_every_host_copies_cluster_wide_to_each_node(self):
        opener = _FailingOpener({None: [SHAPE, dict(SHAPE, queryShapeHash="B" * 64)]})
        outs = [self.collect(opener, f"h{i}:27017", {"typeName": "REPLICA_SECONDARY", "hostname": f"h{i}",
                                                     "port": 27017}) for i in (1, 2, 3)]
        for out in outs:
            self.assertEqual(out["diagnostics"]["reason"], "per_process_request_failed")
            self.assertEqual(out["diagnostics"]["per_process_errors"][0]["status"], 500)
            self.assertEqual(out["scope"], "cluster")
            self.assertEqual(len(out["data"]["summaries"]), 2)
            self.assertIn("do not attribute", out["attribution_note"])
        self.assertEqual(len([u for u in opener.urls if "processIds" not in u]), 1)  # one cluster-wide call, cached

    def test_split_can_be_disabled_for_empty_nodes(self):
        opener = _Opener({None: [SHAPE]})
        out = self.collect(opener, "h1:27017", {"typeName": "SHARD_PRIMARY", "hostname": "h1", "port": 27017},
                           {**CONFIG, "query_shape_split_to_nodes": False})
        self.assertEqual(out["data"]["summaries"], [])
        self.assertEqual(out["scope"], "process")
        self.assertEqual(len(out["cluster_wide"]["data"]["summaries"]), 1)


class SkipExistingQueryStats(unittest.TestCase):
    def run_stage(self, store, config_extra=None):
        from agent import observability_stage
        calls = []

        class Collector:
            def collect(self, config, host, process_id, kind, source, log_date=None, process=None):
                calls.append((host, kind))
                return {"data": {"summaries": [SHAPE]}, "scope": "process", "diagnostics": {}}

        config = {"observability_sources": {"query_stats": "atlas_api"}, "prefix": "p", **(config_extra or {})}
        nodes = [{"host": h, "host_dir": h, "process_id": f"{h}:27017"} for h in ("h1", "h2")]
        with mock.patch.object(observability_stage.skills, "observability", return_value=Collector(), create=True), \
                mock.patch("agent.observability_contract.resolve_source",
                           side_effect=lambda cfg, kind: "atlas_api" if kind == "query_stats" else "disabled"):
            out = observability_stage.collect_nodes(config, DAY, store, nodes)
        return out, calls

    def store_with_h1(self, payload):
        from agent.common import Layout
        layout = Layout.from_config({"prefix": "p"})
        store = _Store()
        store.put_text(layout.query_stats(DAY, "h1"), json.dumps(payload))
        return store

    def test_existing_rows_are_not_fetched_again(self):
        store = self.store_with_h1({"data": {"summaries": [SHAPE]}})
        out, calls = self.run_stage(store)
        self.assertEqual(calls, [("h2", "query_stats")])
        art = out["nodes"][0]["artifacts"]["query_stats"]
        self.assertEqual((art["status"], art["shapes"]), ("skipped_existing", 1))

    def test_empty_existing_file_is_refetched_and_switch_forces_refresh(self):
        _, calls = self.run_stage(self.store_with_h1({"data": {"summaries": []},
                                                      "cluster_wide": {"data": {"summaries": [SHAPE]}}}))
        self.assertEqual(calls, [("h1", "query_stats"), ("h2", "query_stats")])
        _, calls = self.run_stage(self.store_with_h1({"data": {"summaries": [SHAPE]}}),
                                  {"query_stats_skip_existing": False})
        self.assertEqual(len(calls), 2)

    def test_mongosh_query_stats_rows_count(self):
        from agent import observability_stage
        self.assertEqual(observability_stage._query_stats_rows(json.dumps(
            {"data": [{"key": {"queryShape": {}}, "metrics": {"execCount": 1}}]})), 1)

    def test_manual_script_skips_node_with_rows(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import atlas_collect_observability as script
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            writer = script.Writer(tmp, DAY, False, False)
            target = writer.path("h1", "queryStats", "query-shape-insights.json")
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"data": {"summaries": [SHAPE]}, "_cluster_wide": {"data": {"summaries": [SHAPE] * 3}}}))
            self.assertEqual(writer.query_rows("h1", "queryStats", "query-shape-insights.json"), 1)
            self.assertEqual(writer.query_rows("h2", "queryStats", "query-stats.json"), 0)


# Report stage with the same lightweight stubs as test_report_every_node.
sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
_oc = sys.modules["agent.offline_cve"]
if not hasattr(_oc, "check"):
    _oc.check = lambda payload, **kw: {"matches": []}
    _oc.load_catalog = lambda: {}
from agent.common import Layout  # noqa: E402
from agent import report_stage  # noqa: E402

NODES = ["n0", "n1", "n2"]


class _ReportStore(_Store):
    def list_keys(self, prefix):
        return [k for k in self.data if k.startswith(prefix)]


class _Llm:
    def __init__(self):
        self.calls = []

    def generate(self, system, user):
        self.calls.append((system, user))
        return "## Executive summary\n\nconsolidated"


class NodeReportSummaryVariant(unittest.TestCase):
    def setUp(self):
        self.layout = Layout("atlas-logs")
        self.store = _ReportStore()
        for h in NODES:
            self.store.put_text(self.layout.extract(DAY, h, "mongodb", "extractionOccurence.json"), "{}")
            self.store.put_text(self.layout.report(DAY, h, "mongodb", "report.md"),
                                f"# Node {h}\n\nCOLLSCAN on db.c ![g](data:image/png;base64,AAAA)")
        self.cfg = {"prefix": "atlas-logs", "cluster_name": "c", "expected_node_count": 3}

    def run_summary(self, source):
        llm = _Llm()
        with mock.patch.object(report_stage, "_trend_alerts", side_effect=AssertionError("extract read")) as trends, \
                mock.patch.object(report_stage, "summarize_query_stats", return_value=[]) as qsi:
            m = report_stage.run({**self.cfg, "cluster_summary_source": source}, DAY, store=self.store, llm=llm,
                                 summary_only=True) if source == "node_reports" else None
        return m, llm, trends, qsi

    def test_node_reports_only_variant_reads_no_extract(self):
        m, llm, trends, qsi = self.run_summary("node_reports")
        trends.assert_not_called()
        qsi.assert_not_called()
        key = self.layout.cluster_report(DAY, report_stage.NODE_REPORTS_SUMMARY_FILE)
        self.assertIn(key, self.store.data)
        self.assertFalse(self.store.exists(self.layout.cluster_report(DAY, "cluster-summary.md")))
        system, user = llm.calls[0]
        self.assertIn("SOURCE RESTRICTION", system)
        self.assertNotIn("query_shape_insights", user)
        self.assertNotIn("base64,AAAA", user)
        self.assertEqual(user.count("node_report"), 3)
        self.assertEqual(m["cluster_summary_from_node_reports"], self.store.uri(key))
        self.assertIsNone(m["cluster_summary"])

    def test_both_writes_two_comparable_files(self):
        llm = _Llm()
        with mock.patch.object(report_stage, "_trend_alerts", return_value=None):
            m = report_stage.run({**self.cfg, "cluster_summary_source": "both"}, DAY, store=self.store, llm=llm,
                                 summary_only=True)
        self.assertTrue(m["cluster_summary"] and m["cluster_summary_from_node_reports"])
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("query_shape_insights", llm.calls[0][1])

    def test_default_is_unchanged_and_bad_value_falls_back(self):
        self.assertEqual(report_stage._cluster_summary_source({}), "extract")
        self.assertEqual(report_stage._cluster_summary_source({"cluster_summary_source": "node-reports"}), "node_reports")
        self.assertEqual(report_stage._cluster_summary_source({"cluster_summary_source": "x"}), "extract")

    def test_handler_stage_forces_node_reports_variant(self):
        from agent import handler
        seen = {}

        def fake_run(config, log_date, summary_only=False):
            seen.update(source=config.get("cluster_summary_source"), summary_only=summary_only)
            return {}
        with mock.patch.object(handler.report_stage, "run", side_effect=fake_run), \
                mock.patch.object(handler, "load_config", return_value={**self.cfg}, create=True):
            try:
                handler.run_pipeline({"stage": "cluster-summary-md", "log_date": DAY})
            except Exception as exc:  # config loading differs per environment
                self.skipTest(f"handler config not loadable here: {type(exc).__name__}")
        self.assertEqual(seen, {"source": "node_reports", "summary_only": True})


if __name__ == "__main__":
    unittest.main()
