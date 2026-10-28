"""No-network tests: metric selection on the Atlas API, one-page graphs and report appendices."""
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import metrics_catalog as catalog  # noqa: E402
from agent import metrics_graphs as graphs  # noqa: E402
from agent.common import Layout  # noqa: E402
from agent.report_shape_mapping import (APPENDIX_HEADING, METRICS_HEADING, _HEADING,  # noqa: E402
                                        append_metrics_appendix, append_reported_shapes)

spec = importlib.util.spec_from_file_location("collector_metrics", ROOT / "skills/mongodb-observability/scripts/observability.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

USER_SELECTION = ["CACHE", "CONNECTIONS_AND_CURSORS", "STORAGE", "DOCUMENT_ACTIVITY", "PAGE_FAULTS", "MEMORY",
                  "NETWORK", "OPCOUNTERS_PRIMARY", "OPERATIONS", "OP_EXECUTION_TIMES", "OPLOG", "QUERY_EXECUTION",
                  "READ_WRITE_TICKETS", "OPERATION_THROTTLING", "QUERY_SPILL_SORT", "FTS_PROCESS",
                  "SYSTEM_NETWORK", "SYSTEM_MEMORY"]


def measurement(name, units, values, start="2026-10-27T00:00:00Z"):
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return {"name": name, "units": units, "dataPoints": [
        {"timestamp": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "value": v}
        for i, v in enumerate(values)]}


class Store:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.binary = {}

    def exists(self, key): return key in self.items or key in self.binary
    def get_text(self, key): return json.dumps(self.items[key]) if not isinstance(self.items[key], str) else self.items[key]
    def list_keys(self, prefix): return [k for k in list(self.items) + list(self.binary) if k.startswith(prefix)]
    def uri(self, key): return "mock://" + key

    def put_text(self, key, value, content_type):
        self.items[key] = value
        return self.uri(key)

    def upload(self, path, key, content_type, metadata=None):
        self.binary[key] = (Path(path).read_bytes(), content_type)
        return self.uri(key)


class CatalogTests(unittest.TestCase):
    def test_default_selection_matches_requested_groups(self):
        self.assertEqual(catalog.DEFAULT_SELECTED_GROUPS, USER_SELECTION)
        self.assertEqual([g for g, _ in catalog.selected_groups()], USER_SELECTION)

    def test_plan_only_documented_names_batched_by_ten(self):
        plan = catalog.plan_measurement_requests()
        self.assertTrue(all(len(b) <= catalog.MAX_M_PER_REQUEST for b in plan["batches"]))
        flat = [m for b in plan["batches"] for m in b]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertTrue(set(flat) <= catalog.ATLAS_PROCESS_MEASUREMENTS)
        self.assertIn("FTS_MEMORY_RESIDENT", flat)
        self.assertNotIn("FTS_PROCESS_RESIDENT_MEMORY", flat)
        self.assertIn("DB_INDEX_SIZE_TOTAL", plan["unsupported"])
        # Unselected groups (e.g. SYSTEM_CPU / ASSERTIONS) are never requested.
        self.assertNotIn("SYSTEM_CPU_USER", flat)
        self.assertNotIn("ASSERT_REGULAR", flat)

    def test_group_override(self):
        plan = catalog.plan_measurement_requests({"metrics_graph_groups": ["CACHE", "NOT_A_GROUP"]})
        self.assertEqual(plan["requested"], catalog.GROUP_DEFINITIONS["CACHE"])
        cfg = catalog.graph_config({"metrics_graph_config": {
            "group_of_metrics": {"MINE": {"list_of_metrics": ["CONNECTIONS"]}}, "metrics": ["MINE"]}})
        self.assertEqual(cfg["metrics"], ["MINE"])


class CollectorTests(unittest.TestCase):
    cfg = {"group_id": "g", "api_version": "2025-03-12"}

    def test_measurement_urls_carry_selected_m_and_window(self):
        with patch.object(collector, "_api_pages", return_value=([{"measurements": []}], False)) as api:
            result = collector.collect_api(self.cfg, "node", "node:27017", "metrics", "atlas_api")
        plan = catalog.plan_measurement_requests()
        self.assertEqual(api.call_count, len(plan["batches"]))
        seen = []
        for call in api.call_args_list:
            url = call.args[1]
            self.assertIn("/processes/node%3A27017/measurements?", url)
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["granularity"], ["PT5M"])
            self.assertTrue(query["start"][0].endswith("Z") and query["end"][0].endswith("Z"))
            self.assertLessEqual(len(query["m"]), 10)
            seen += query["m"]
        self.assertEqual(seen, plan["requested"])
        self.assertEqual(result["measurement_plan"]["unsupported"], plan["unsupported"])
        self.assertEqual(result["window"]["granularity"], "PT5M")

    def test_rejected_batch_retried_per_metric(self):
        def fake(config, url, source):
            names = parse_qs(urlsplit(url).query)["m"]
            if len(names) > 1 or names == ["CONNECTIONS"]:
                raise HTTPError(url, 400, "bad", {}, io.BytesIO(b""))
            return [{"measurements": [{"name": names[0], "dataPoints": []}]}], False
        cfg = dict(self.cfg, metrics_graph_groups=["CONNECTIONS_AND_CURSORS"])
        with patch.object(collector, "_api_pages", side_effect=fake):
            result = collector.collect_api(cfg, "node", "node:27017", "metrics", "atlas_api")
        self.assertEqual(len(result["data"]), 2)
        self.assertEqual(result["errors"], [{"metrics": ["CONNECTIONS"], "status": 400, "error_type": "HTTPError"}])

    def test_invalid_granularity(self):
        with self.assertRaises(ValueError):
            collector.collect_api(dict(self.cfg, metrics_granularity="PT2M"), "n", "n:1", "metrics", "atlas_api")


class GraphAndAppendixTests(unittest.TestCase):
    def payload(self):
        return {"window": {"since": "2026-10-27T00:00:00+00:00", "until": "2026-10-28T00:00:00+00:00",
                           "granularity": "PT5M"},
                "measurement_plan": catalog.plan_measurement_requests(),
                "data": [{"measurements": [
                    measurement("CONNECTIONS", "SCALAR", [10, 12, None, 15]),
                    measurement("CACHE_USED_BYTES", "BYTES", [1e9, 1.1e9, 1.2e9, 1.3e9]),
                    measurement("FTS_MEMORY_RESIDENT", "MEGABYTES", [100, 110, 120, 130]),
                    measurement("OPERATION_THROTTLING_REJECTED_OPERATIONS", "SCALAR_PER_SECOND", [0, 0, 0, 0]),
                ]}]}

    def test_series_and_panels_alias_and_empty_groups(self):
        series = graphs.load_series(self.payload())
        self.assertEqual(len(series["CONNECTIONS"]["points"]), 3)
        panels, empty = graphs.plan_panels(series)
        self.assertEqual([p["group"] for p in panels], ["CACHE", "CONNECTIONS_AND_CURSORS", "FTS_PROCESS"])
        self.assertIn("OPERATION_THROTTLING", empty)
        fts = next(p for p in panels if p["group"] == "FTS_PROCESS")
        self.assertEqual(fts["lines"][0]["name"], "FTS_PROCESS_RESIDENT_MEMORY")

    def test_appendix_order_and_idempotence(self):
        extraction = {"slow": {"operations": [{"query_hash": "ABCDEF12", "namespace": "db.c",
                                                "sample_query_shape": {"x": 1}}]}}
        md = append_reported_shapes("# Report\n\nSlow hash ABCDEF12 found.\n", extraction)
        md = append_metrics_appendix(md, "graph body")
        self.assertEqual(md.count(APPENDIX_HEADING + "\n"), 1)
        self.assertLess(md.index(APPENDIX_HEADING), md.index(_HEADING))
        self.assertLess(md.index(_HEADING), md.index(METRICS_HEADING))
        self.assertIn("#### Log query hash `ABCDEF12`", md)
        self.assertEqual(append_metrics_appendix(md, "again"), md)
        self.assertEqual(append_reported_shapes(md, extraction), md)

    def test_metrics_appendix_without_query_shapes_creates_appendix(self):
        md = append_metrics_appendix("# Report\n", "body")
        self.assertIn(APPENDIX_HEADING + "\n\n" + METRICS_HEADING, md)

    def test_report_node_renders_one_page_png_and_svg(self):
        for name in ("diffing", "offline_cve"):
            sys.modules.setdefault("agent." + name, types.ModuleType("agent." + name))
        sys.modules["agent.offline_cve"].check = lambda payload: {"matches": []}
        from agent import report_stage
        layout = Layout("p")
        store = Store({
            layout.extract("2026-10-27", "node", "mongodb", "extractionOccurence.json"): {"slow": {"operations": []}},
            layout.observability("2026-10-27", "node", "metrics"): self.payload(),
        })
        llm = types.SimpleNamespace(generate=lambda system, user: "# Report\n\nFindings.")
        result = report_stage.report_node(store, layout, llm, "sys", "2026-10-27", "node", "mongodb", 600000)
        md = result["report_md"]
        self.assertLess(md.index(_HEADING), md.index(METRICS_HEADING))
        self.assertIn("![Per-node metric graphs: CACHE, CONNECTIONS_AND_CURSORS, FTS_PROCESS](metrics-appendix.png)", md)
        self.assertIn("not offered by the measurements API", md)
        png_key = layout.report("2026-10-27", "node", "mongodb", "metrics-appendix.png")
        data, ctype = store.binary[png_key]
        self.assertEqual(ctype, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertIn("metrics_graph", result["uploads"])
        # Exactly one page: A4 portrait aspect ratio.
        import struct
        width, height = struct.unpack(">II", data[16:24])
        self.assertAlmostEqual(height / width, 11.69 / 8.27, places=2)

    def test_report_node_without_metrics_states_absence(self):
        sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
        sys.modules["agent.offline_cve"].check = lambda payload: {"matches": []}
        from agent import report_stage
        layout = Layout("p")
        store = Store({layout.extract("2026-10-27", "node", "mongodb", "extractionOccurence.json"): {}})
        llm = types.SimpleNamespace(generate=lambda system, user: "# Report")
        md = report_stage.report_node(store, layout, llm, "sys", "2026-10-27", "node", "mongodb", 600000)["report_md"]
        self.assertIn(METRICS_HEADING, md)
        self.assertIn("No per-node metrics file was supplied", md)
        md2 = report_stage.report_node(store, layout, llm, "sys", "2026-10-27", "node", "mongodb", 600000,
                                       config={"metrics_graphs_enabled": False})["report_md"]
        self.assertNotIn(METRICS_HEADING, md2)


if __name__ == "__main__":
    unittest.main()
