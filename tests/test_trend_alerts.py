"""Cluster summary section: new or sharply increasing problems versus n-1 AND n-8."""
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import trend_alerts as ta  # noqa: E402
from agent.common import Layout  # noqa: E402

DAY, N1, N8 = "2026-10-27", "2026-10-26", "2026-10-20"


def ext(slow, errors, collscan=0, incompatible=None, first="2026-10-27T00:00:00Z", last="2026-10-27T23:59:00Z"):
    return {"metadata": {"parser_version": "1", "slow_threshold_ms": 100, "time_range": {"first": first, "last": last}},
            "slow": {"global_stats": {"count": sum(o["count"] for o in slow), "collscan_count": collscan},
                     "operations": slow},
            "error_scan": {"total_error_events": sum(e["count"] for e in errors), "unique_error_groups": len(errors),
                           "groups": errors},
            "summary": {"severity_counts": {"E": sum(e["count"] for e in errors)}, "category_counts": {}},
            "driverCompatibility": {"incompatible_count": len(incompatible or []),
                                    "incompatible_drivers": incompatible or []}}


def op(ns, plan, h, count, ratio=1, sort=0, change_stream=False, median=50):
    return {"namespace": ns, "plan_summary": plan, "query_hash": h, "count": count,
            "collscan_count": count if "COLLSCAN" in plan else 0, "sort_stage_count": sort,
            "examined_returned_ratio_max": ratio, "duration_ms": {"median": median},
            "sample_query_shape": {"pipeline": [{"$changeStream": {}}]} if change_stream else {"find": "?"}}


def err(fp, count, cat="network", msg='Connection refused from 10.0.0.12:27017 user "bob"', components=("NETWORK",)):
    return {"fingerprint": fp, "category": cat, "count": count, "severities": ["E"], "components": list(components),
            "sample_message": msg}


def keyed(alerts):
    return {(i["kind"], i.get("query_hash") or i.get("fingerprint") or i.get("signal") or i.get("driver")): i
            for i in alerts["items"]}


class NodeAlertTests(unittest.TestCase):
    def setUp(self):
        self.cur = ext([op("shop.orders", "COLLSCAN", "AAAA1111", 40),
                        op("shop.items", "IXSCAN { a: 1 }", "BBBB2222", 300, ratio=5000),
                        op("shop.items", "IXSCAN { b: 1 }", "CCCC3333", 12, sort=12),
                        op("shop.feed", "COLLSCAN", "DDDD4444", 900, change_stream=True),
                        op("admin.system.users", "COLLSCAN", "EEEE5555", 50),
                        op("shop.good", "IXSCAN { c: 1 }", "FFFF6666", 5000),
                        op("shop.lat", "COLLSCAN", "GGGG7777", 10, median=900)],
                       [err("aaa111", 30), err("bbb222", 4), err("ccc333", 100, msg="mongot internal failure"),
                        err("ddd444", 60)],
                       collscan=990, incompatible=[{"driver": "pymongo", "version": "3.2"}])
        self.n1 = ext([op("shop.items", "IXSCAN { a: 1 }", "BBBB2222", 30, ratio=4000),
                       op("shop.items", "IXSCAN { b: 1 }", "CCCC3333", 10, sort=10),
                       op("shop.feed", "COLLSCAN", "DDDD4444", 100, change_stream=True),
                       op("shop.lat", "COLLSCAN", "GGGG7777", 10, median=100)],
                      [err("bbb222", 1), err("ddd444", 25)], collscan=100)
        self.n8 = ext([op("shop.items", "IXSCAN { a: 1 }", "BBBB2222", 250, ratio=4000),
                       op("shop.lat", "COLLSCAN", "GGGG7777", 9, median=120)],
                      [err("bbb222", 2), err("ddd444", 10)], collscan=90)
        self.alerts = ta.node_alerts("node-0", "mongodb", self.cur, {"n-1": (N1, self.n1), "n-8": (N8, self.n8)})
        self.items = keyed(self.alerts)

    def test_new_unindexed_query(self):
        item = self.items[("slow_query", "AAAA1111")]
        self.assertEqual(item["trigger"], "new")
        self.assertIn("unindexed (COLLSCAN)", item["problems"])

    def test_poorly_indexed_query_tripled_vs_one_baseline(self):
        item = self.items[("slow_query", "BBBB2222")]
        self.assertEqual(item["trigger"], "increase")
        self.assertEqual(item["vs"], ["n-1"])  # 300 vs 30 (+900%); vs n-8 only +20%
        self.assertEqual(item["pct"], {"n-1": 900, "n-8": 20})
        self.assertTrue(item["problems"][0].startswith("poorly indexed"))

    def test_latency_tripled_for_a_bad_query_with_stable_count(self):
        item = self.items[("slow_query", "GGGG7777")]
        self.assertEqual(item["metric"], "median duration (ms)")

    def test_not_reported(self):
        for key in (("slow_query", "CCCC3333"),  # sort, but stable
                    ("slow_query", "DDDD4444"),  # change stream: not an index problem
                    ("slow_query", "EEEE5555"),  # system namespace
                    ("slow_query", "FFFF6666"),  # not bad
                    ("error_group", "bbb222"),   # 4 vs 1: +300% but only +3 occurrences (< 5)
                    ("error_group", "ccc333")):  # platform-managed component
            self.assertNotIn(key, self.items)

    def test_errors_signals_drivers(self):
        self.assertEqual(self.items[("error_group", "aaa111")]["trigger"], "new")
        self.assertEqual(self.items[("error_group", "ddd444")]["vs"], ["n-8"])  # 60 vs 10 (+500%), vs 25 (+140%)
        self.assertEqual(self.items[("signal", "slow.global_stats.collscan_count")]["vs"], ["n-1", "n-8"])
        self.assertEqual(self.items[("driver", "pymongo@3.2")]["trigger"], "new")

    def test_threshold_is_configurable(self):
        bases = {"n-1": (N1, self.n1), "n-8": (N8, self.n8)}
        strict = keyed(ta.node_alerts("node-0", "mongodb", self.cur, bases, {"trend_increase_pct": 1000}))
        self.assertNotIn(("slow_query", "BBBB2222"), strict)  # +900% < +1000%
        self.assertIn(("slow_query", "AAAA1111"), strict)     # new is always listed
        no_floor = keyed(ta.node_alerts("node-0", "mongodb", self.cur, bases, {"trend_min_abs_increase": 0}))
        self.assertIn(("error_group", "bbb222"), no_floor)    # 4 vs 1 (+300%) once the floor is removed

    def test_requires_both_baselines(self):
        alerts = ta.node_alerts("node-1", "mongodb", self.cur, {"n-1": (N1, self.n1), "n-8": (N8, None)})
        self.assertEqual(alerts["status"], "not_compared")
        self.assertEqual(alerts["missing_baselines"], ["n-8"])
        self.assertIsNone(ta.render_markdown(ta.cluster_alerts([alerts])))

    def test_markdown_is_redacted(self):
        md = ta.render_markdown(ta.cluster_alerts([self.alerts]))
        self.assertTrue(md.startswith(ta.SECTION_HEADING))
        self.assertIn("| `node-0` | `shop.orders` | `AAAA1111` | unindexed (COLLSCAN) | 40 | absent | absent | **new**", md)
        self.assertIn("**+900% vs n-1** (+20% vs n-8)", md)
        for forbidden in ("10.0.0.12", "bob", "mongot", "admin.system"):
            self.assertNotIn(forbidden, md)

    def test_short_baseline_is_flagged(self):
        short = dict(self.n1, metadata={**self.n1["metadata"], "time_range": {"first": "2026-10-26T00:00:00Z",
                                                                               "last": "2026-10-26T03:00:00Z"}})
        alerts = ta.node_alerts("n", "mongodb", self.cur, {"n-1": (N1, short), "n-8": (N8, self.n8)})
        self.assertTrue(any("increases may be overstated" in x for x in alerts["comparability_limitations"]))

    def test_place_section(self):
        section = ta.SECTION_HEADING + "\n\nTABLE\n"
        md = "# C\n\n## Executive summary\n\ntext\n\n## Priorities\n\n1. x\n"
        out = ta.place_section(md, section)
        self.assertLess(out.index("## Executive summary"), out.index(ta.SECTION_HEADING))
        self.assertLess(out.index("TABLE"), out.index("## Priorities"))
        model_copy = md.replace("## Priorities", "## New or sharply increasing issues\n\nmodel text\n\n## Priorities")
        out2 = ta.place_section(model_copy, section)
        self.assertNotIn("model text", out2)
        self.assertEqual(out2.count("New or sharply increasing issues"), 1)


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


class ClusterSummaryTests(unittest.TestCase):
    def setUp(self):
        for name in ("offline_cve",):
            sys.modules.setdefault("agent." + name, types.ModuleType("agent." + name))
        sys.modules["agent.offline_cve"].check = lambda p: {"matches": []}
        sys.modules["agent.offline_cve"].load_catalog = lambda: {}
        from agent import report_stage
        self.rs = report_stage
        self.L = Layout("p")

    def run_cluster(self, with_n8=True, model_writes_section=False):
        L, items = self.L, {}
        cur = ext([op("shop.orders", "COLLSCAN", "AAAA1111", 40)], [err("aaa111", 30)])
        for n in ("n0", "n1", "n2"):
            items[L.extract(DAY, n, "mongodb", "extractionOccurence.json")] = cur
            items[L.extract(N1, n, "mongodb", "extractionshort.json")] = ext([], [])
            if with_n8:
                items[L.extract(N8, n, "mongodb", "extractionshort.json")] = ext([], [])
        store, seen = Store(items), {}

        def generate(system, user):
            if "Cluster `" in user:
                seen.update(system=system, user=user)
                extra = "## New or sharply increasing issues\n\ninvented\n\n" if model_writes_section else ""
                return "# Cluster\n\n## Executive summary\n\nNew COLLSCAN.\n\n" + extra + "## Priorities\n\n1. x\n"
            return "# Report\n"
        manifest = self.rs.run({"prefix": "p", "cluster_name": "c"}, DAY, store=store,
                               llm=types.SimpleNamespace(generate=generate))
        return manifest, store, seen

    def test_section_inserted_after_executive_summary(self):
        manifest, store, seen = self.run_cluster(model_writes_section=True)
        md = store.items[self.L.cluster_report(DAY, "cluster-summary.md")]
        self.assertLess(md.index("## Executive summary"), md.index(ta.SECTION_HEADING))
        self.assertLess(md.index(ta.SECTION_HEADING), md.index("## Priorities"))
        self.assertEqual(md.count("New or sharply increasing issues"), 1)
        self.assertNotIn("invented", md)
        self.assertIn("`AAAA1111`", md)
        self.assertIn("trend_alerts", seen["user"])
        self.assertIn("+200%", seen["system"])
        self.assertTrue(manifest["trend_alerts"].endswith("cluster/reports/trend-alerts.json"))
        alerts = json.loads(store.items[self.L.cluster_report(DAY, "trend-alerts.json")])
        self.assertEqual(len(alerts["nodes_compared"]), 3)

    def test_no_section_without_both_baselines(self):
        manifest, store, seen = self.run_cluster(with_n8=False)
        md = store.items[self.L.cluster_report(DAY, "cluster-summary.md")]
        self.assertNotIn(ta.SECTION_HEADING, md)
        self.assertNotIn("trend_alerts", seen["user"])
        self.assertEqual(seen["system"], self.rs.CLUSTER_PROMPT)


if __name__ == "__main__":
    unittest.main()
