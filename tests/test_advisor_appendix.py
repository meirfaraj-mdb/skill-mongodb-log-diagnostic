"""Performance Advisor appendix (APPENDIX 2): rendering, safety, placement and report wiring."""
import itertools
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import advisor_appendix as aa  # noqa: E402
from agent.common import Layout  # noqa: E402
from agent.report_shape_mapping import (ADVISOR_HEADING, APPENDIX_HEADING, METRICS_HEADING,  # noqa: E402
                                        SHAPES_HEADING, append_advisor_appendix, append_metrics_appendix,
                                        append_reported_shapes)

DAY = "2026-10-27"


def atlas_advisor():
    """Atlas/Ops Manager API envelope as written by the collectors."""
    return {"schema_version": 2, "source": "atlas_api_advisor", "host": "node:27017",
            "retrievedAt": "2026-10-28T01:00:00Z",
            "window": {"since": "2026-10-27T00:00:00+00:00", "until": "2026-10-28T00:00:00+00:00"},
            "data": [{"suggestedIndexes": [
                {"id": "a1", "namespace": "shop.orders", "index": [{"customerId": 1}, {"createdAt": -1}],
                 "impact": ["s1", "s2"], "weight": 12345.6},
                {"id": "a2", "namespace": "config.system.sessions", "index": [{"x": 1}], "impact": []},
                {"id": "a3", "namespace": "shop.__mdb_internal_search", "index": [{"y": 1}], "impact": []}],
                "shapes": [
                    {"id": "s1", "namespace": "shop.orders", "avgMs": 350.2, "count": 1200, "inefficiencyScore": 4000,
                     "operations": [{"predicates": [{"customerId": "SECRET-LITERAL-1"}], "raw": "find SECRET-RAW",
                                     "stats": {"nScanned": 50000, "nReturned": 3, "ms": 400}}]},
                    {"id": "s2", "namespace": "shop.orders", "avgMs": 20, "count": 10},
                    {"id": "s3", "namespace": "admin.system.users", "avgMs": 1, "count": 1}]}]}


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


class RenderTests(unittest.TestCase):
    def test_atlas_payload_rendered_without_literals_or_system_namespaces(self):
        md = aa.render(atlas_advisor())
        self.assertIn("`{ customerId: 1, createdAt: -1 }`", md)
        self.assertIn("| 1 | `shop.orders` |", md)
        self.assertIn("12,346", md)  # weight
        self.assertIn("16,667", md)  # 50000 / 3 docs examined per returned
        self.assertIn("| `shop.orders` | `s1` | 1,200 | 350.2 |", md)
        self.assertIn("Atlas Performance Advisor API", md)
        self.assertIn("index list not supplied", md)
        for forbidden in ("SECRET", "config.system", "__mdb_internal", "admin.system", "predicates"):
            self.assertNotIn(forbidden, md)

    def test_bare_page_and_indexes_key_form(self):
        page = {"suggestedIndexes": [{"namespace": "db.c", "indexes": [{"key": {"a": 1, "loc": "2dsphere"}}],
                                      "impact": ["x"]}]}
        md = aa.render(page)
        self.assertIn("`{ a: 1, loc: '2dsphere' }`", md)

    def test_unknown_direction_value_is_redacted(self):
        md = aa.render({"suggestedIndexes": [{"namespace": "db.c", "index": [{"a": "secret-value"}]}]})
        self.assertIn("a: '?'", md)
        self.assertNotIn("secret-value", md)

    def test_inventory_marks_existing_same_key(self):
        inventory = {"data": [{"namespace": "shop.orders", "indexes": [
            {"name": "c_1_d_-1", "key": {"customerId": 1, "createdAt": -1}}]}]}
        self.assertIn("yes — compare options", aa.render(atlas_advisor(), inventory))
        other = {"data": [{"namespace": "shop.orders", "indexes": [{"name": "_id_", "key": {"_id": 1}}]}]}
        self.assertIn("| no |", aa.render(atlas_advisor(), other))

    def test_ops_manager_slow_namespaces_and_truncation(self):
        payload = {"source": "ops_manager_advisor", "truncated": True, "data": [{"suggestedIndexes": []}],
                   "slow_namespaces": {"namespaces": [{"namespace": "shop.orders"}, {"namespace": "local.oplog.rs"}]}}
        md = aa.render(payload)
        self.assertIn("Ops Manager Performance Advisor API", md)
        self.assertIn("#### Namespaces with slow queries (1)", md)
        self.assertNotIn("local.oplog", md)
        self.assertIn("truncated", md)

    def test_empty_or_system_only_payload_has_no_appendix(self):
        self.assertIsNone(aa.render({"data": [{"suggestedIndexes": [], "shapes": []}]}))
        self.assertIsNone(aa.render({"data": [{"suggestedIndexes": [{"namespace": "admin.x", "index": [{"a": 1}]}]}]}))
        self.assertIsNone(aa.render({"errors": ["403"]}))
        self.assertIsNone(aa.render("not json"))


class PlacementTests(unittest.TestCase):
    extraction = {"slow": {"operations": [{"query_hash": "ABCDEF12", "namespace": "db.c",
                                            "sample_query_shape": {"x": 1}}]}}

    def test_fixed_order_for_every_call_order(self):
        steps = {"metrics": lambda md: append_metrics_appendix(md, "graph body"),
                 "advisor": lambda md: append_advisor_appendix(md, "advisor body"),
                 "shapes": lambda md: append_reported_shapes(md, self.extraction)}
        results = set()
        for order in itertools.permutations(steps):
            md = "# Report\n\nSlow hash ABCDEF12 found.\n"
            for name in order:
                md = steps[name](md)
            results.add(md)
            self.assertEqual(md.count(APPENDIX_HEADING + "\n"), 1)
            self.assertLess(md.index(METRICS_HEADING), md.index("graph body"))
            self.assertLess(md.index("graph body"), md.index(ADVISOR_HEADING))
            self.assertLess(md.index(ADVISOR_HEADING), md.index("advisor body"))
            self.assertLess(md.index("advisor body"), md.index(SHAPES_HEADING))
        self.assertEqual(len(results), 1)
        self.assertIn("APPENDIX 2", ADVISOR_HEADING)
        self.assertIn("APPENDIX 3", SHAPES_HEADING)

    def test_advisor_without_metrics_and_idempotent(self):
        md = append_reported_shapes("# Report\n\nSlow hash ABCDEF12 found.\n", self.extraction)
        md = append_advisor_appendix(md, "advisor body")
        self.assertIn(APPENDIX_HEADING + "\n\n" + ADVISOR_HEADING, md)
        self.assertLess(md.index(ADVISOR_HEADING), md.index(SHAPES_HEADING))
        self.assertEqual(append_advisor_appendix(md, "again"), md)
        self.assertEqual(append_metrics_appendix(append_advisor_appendix("# R\n", "a"), "m"),
                         append_advisor_appendix(append_metrics_appendix("# R\n", "m"), "a"))


class ReportWiringTests(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
        sys.modules["agent.offline_cve"].check = lambda payload: {"matches": []}
        from agent import report_stage
        self.rs = report_stage
        self.layout = Layout("p")

    def store(self, advisor=None):
        items = {self.layout.extract(DAY, "node", "mongodb", "extractionOccurence.json"): {"slow": {"operations": []}}}
        if advisor is not None:
            items[self.layout.observability(DAY, "node", "advisor")] = advisor
        return Store(items)

    def node(self, store):
        llm = types.SimpleNamespace(generate=lambda system, user: "# Report\n\nFindings.")
        return self.rs.report_node(store, self.layout, llm, "sys", DAY, "node", "mongodb", None)["report_md"]

    def test_advisor_appendix_present_only_with_advisor_data(self):
        md = self.node(self.store(atlas_advisor()))
        self.assertIn(ADVISOR_HEADING, md)
        self.assertLess(md.index(ADVISOR_HEADING), md.index(SHAPES_HEADING))
        self.assertNotIn(ADVISOR_HEADING, self.node(self.store()))
        self.assertNotIn(ADVISOR_HEADING, self.node(self.store({"data": [{"suggestedIndexes": []}]})))
        self.assertNotIn(ADVISOR_HEADING, self.node(self.store("{not json")))

    def test_advisor_appendix_can_be_disabled(self):
        llm = types.SimpleNamespace(generate=lambda system, user: "# Report\n")
        md = self.rs.report_node(self.store(atlas_advisor()), self.layout, llm, "sys", DAY, "node", "mongodb", None,
                                 config={"advisor_appendix_enabled": False})["report_md"]
        self.assertNotIn(ADVISOR_HEADING, md)

    def test_fallback_report_has_advisor_appendix(self):
        result = self.rs.write_fallback_report(self.store(atlas_advisor()), self.layout, DAY, "node", "mongodb",
                                               TimeoutError("x"))
        self.assertIn("Partial report", result["report_md"])
        self.assertIn(ADVISOR_HEADING, result["report_md"])


if __name__ == "__main__":
    unittest.main()
