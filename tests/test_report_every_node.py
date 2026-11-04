"""Every node with an extract gets a report.md, even when its model-based report fails."""
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _name in ("offline_cve",):
    sys.modules.setdefault("agent." + _name, types.ModuleType("agent." + _name))
_oc = sys.modules["agent.offline_cve"]
_oc.check = lambda payload: {"matches": []}
_oc.load_catalog = lambda: {}

from agent.common import Layout  # noqa: E402
from agent import report_stage  # noqa: E402

DAY = "2026-10-16"
NODES = ["atlas-x-shard-00-00", "atlas-x-shard-00-01", "atlas-x-shard-00-02"]


class Store:
    def __init__(self, items):
        self.items = dict(items)

    def exists(self, key): return key in self.items
    def get_text(self, key):
        v = self.items[key]
        return v if isinstance(v, str) else json.dumps(v)
    def list_keys(self, prefix): return [k for k in self.items if k.startswith(prefix)]
    def uri(self, key): return "mock://" + key
    def put_text(self, key, value, content_type):
        self.items[key] = value
        return self.uri(key)


def store_for(third_extract=None):
    layout = Layout("atlas-logs")
    items = {}
    for i, host in enumerate(NODES):
        items[layout.extract(DAY, host, "mongodb", "extractionOccurence.json")] = (
            third_extract if (i == 2 and third_extract is not None) else {"slow": {"operations": []}})
    return layout, Store(items)


class Llm:
    def __init__(self, fail_for=None, cve_for=None):
        self.fail_for, self.cve_for = fail_for, cve_for

    def generate(self, system, user):
        if "Cluster `" in user:
            return "## Executive summary\n\nok"
        if self.fail_for and f"`{self.fail_for}`" in user:
            raise TimeoutError("Bedrock read timeout")
        if self.cve_for and f"`{self.cve_for}`" in user:
            return "# Report\n\nDriver affected by CVE-2024-99999."
        return "# Report\n\nFindings."


class EveryNodeGetsReport(unittest.TestCase):
    cfg = {"prefix": "atlas-logs", "cluster_name": "c", "expected_node_count": 3}

    def reports(self, store, layout):
        return [h for h in NODES if store.exists(layout.report(DAY, h, "mongodb", "report.md"))]

    def test_model_failure_on_third_node_still_writes_report_and_summary(self):
        layout, store = store_for()
        m = report_stage.run(self.cfg, DAY, store=store, llm=Llm(fail_for=NODES[2]))
        self.assertEqual(self.reports(store, layout), NODES)
        md = store.get_text(layout.report(DAY, NODES[2], "mongodb", "report.md"))
        self.assertIn("Partial report", md)
        self.assertTrue(m["cluster_summary"])
        self.assertEqual(len(m["reports"]), 3)
        self.assertEqual(m["failed"][0]["node"], NODES[2])

    def test_unvetted_cve_is_redacted_not_fatal(self):
        layout, store = store_for()
        report_stage.run(self.cfg, DAY, store=store, llm=Llm(cve_for=NODES[2]))
        md = store.get_text(layout.report(DAY, NODES[2], "mongodb", "report.md"))
        self.assertNotIn("CVE-2024-99999", md)
        self.assertIn("[unverified CVE reference removed]", md)
        self.assertNotIn("Partial report", md)

    def test_oversized_extract_is_reduced_further_instead_of_failing(self):
        big = {"slow": {"operations": [{"namespace": f"db.c{i}", "query_hash": f"H{i:06d}",
                                        "pad": "x" * 2000} for i in range(400)]}}
        layout, store = store_for(third_extract=big)
        report_stage.run({**self.cfg, "report_max_input_chars": 120_000}, DAY, store=store, llm=Llm())
        md = store.get_text(layout.report(DAY, NODES[2], "mongodb", "report.md"))
        self.assertNotIn("Partial report", md)

    def test_missing_extract_folder_is_named(self):
        layout, store = store_for()
        del store.items[layout.extract(DAY, NODES[2], "mongodb", "extractionOccurence.json")]
        store.items[layout.observability(DAY, NODES[2], "metrics")] = {}
        nodes = report_stage.discover_nodes(store, layout, DAY)
        self.assertEqual(report_stage.hosts_without_extract(store, layout, DAY, nodes), [NODES[2]])


if __name__ == "__main__":
    unittest.main()
