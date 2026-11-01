"""REPORT_MAX_INPUT_CHARS: per-node default 60k chars, 90k with resource investigation; explicit wins."""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.common import Layout  # noqa: E402
from tests.test_metrics_graphs import Store  # noqa: E402
from tests.test_resource_investigation import extract, payload  # noqa: E402

DAY = "2026-10-27"


class InputLimitDefaultTests(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
        sys.modules["agent.offline_cve"].check = lambda p: {"matches": []}
        sys.modules["agent.offline_cve"].load_catalog = lambda: {}
        from agent import report_stage
        self.rs = report_stage
        self.layout = Layout("p")

    def test_resolver(self):
        r = self.rs.resolve_max_input_chars
        self.assertEqual(r(None, False), 60_000)
        self.assertEqual(r(None, True), 90_000)
        self.assertEqual(r("", True), 90_000)
        self.assertEqual(r(0, False), 60_000)  # non-positive -> default
        self.assertEqual(r(250_000, False), 250_000)
        self.assertEqual(r("120000", True), 120_000)
        with self.assertRaises(ValueError):
            r("lots", True)

    def run_node(self, metrics=None, configured=None, config=None):
        L = self.layout
        items = {L.extract(DAY, "node", "mongodb", "extractionOccurence.json"): extract(62)}
        if metrics is not None:
            items[L.observability(DAY, "node", "metrics")] = metrics
        seen = {}

        def generate(system, user):
            seen["user"] = user
            return "# Report\n"
        out = self.rs.report_node(Store(items), L, types.SimpleNamespace(generate=generate), "sys", DAY, "node",
                                  "mongodb", configured, config=config)
        return out, seen["user"]

    def test_default_without_metrics_is_60k(self):
        out, _ = self.run_node()
        self.assertEqual(out["max_input_chars"], 60_000)

    def test_default_with_resource_investigation_is_90k(self):
        out, _ = self.run_node(metrics=payload([20] * 12 + [95, 96] + [20] * 10))
        self.assertIsNotNone(out["resource_investigation"])
        self.assertEqual(out["max_input_chars"], 90_000)

    def test_metrics_with_investigation_disabled_is_60k(self):
        out, _ = self.run_node(metrics=payload([95, 95]), config={"resource_investigation_enabled": False})
        self.assertIsNone(out["resource_investigation"])
        self.assertEqual(out["max_input_chars"], 60_000)

    def test_explicit_value_wins(self):
        out, _ = self.run_node(metrics=payload([95, 95]), configured=600_000)
        self.assertEqual(out["max_input_chars"], 600_000)

    def test_large_input_is_reduced_to_default_budget(self):
        big = {"slow": {"operations": [{"namespace": f"db.c{i}", "query_hash": f"{i:08X}", "count": i,
                                        "pad": "x" * 1500} for i in range(200)]}}
        L = self.layout
        store = Store({L.extract(DAY, "node", "mongodb", "extractionOccurence.json"): big})
        seen = {}

        def generate(system, user):
            seen["user"] = user
            return "# Report\n"
        out = self.rs.report_node(store, L, types.SimpleNamespace(generate=generate), "sys", DAY, "node",
                                  "mongodb", None)
        self.assertEqual(out["max_input_chars"], 60_000)
        self.assertLessEqual(len(seen["user"]), 60_000)

    def test_run_uses_per_node_default_and_env_override(self):
        from agent import providers
        L = self.layout
        items = {}
        for host in ("a", "b"):
            items[L.extract(DAY, host, "mongodb", "extractionOccurence.json")] = {"slow": {"operations": []}}
        items[L.observability(DAY, "b", "metrics")] = payload([20] * 12 + [95, 96] + [20] * 10)
        llm = types.SimpleNamespace(generate=lambda s, u: "## Executive summary\n\nok" if "Cluster `" in u else "# R\n")
        m = self.rs.run({"prefix": "p", "cluster_name": "c", "expected_node_count": 2}, DAY, store=Store(dict(items)),
                        llm=llm)
        self.assertEqual({r["node"]: r["max_input_chars"] for r in m["reports"]}, {"a": 60_000, "b": 90_000})
        with patch.dict(os.environ, {"MONGODB_LOG_DIAG_REPORT_MAX_INPUT_CHARS": "150000"}, clear=True):
            env = providers._env_config()
        self.assertEqual(env["report_max_input_chars"], 150_000)
        m = self.rs.run({"prefix": "p", "cluster_name": "c", "expected_node_count": 2, **env}, DAY,
                        store=Store(dict(items)), llm=llm)
        self.assertEqual({r["max_input_chars"] for r in m["reports"]}, {150_000})
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("report_max_input_chars", providers._env_config())


if __name__ == "__main__":
    unittest.main()
