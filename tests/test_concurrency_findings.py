"""Write-contention findings, report wiring/fallback, budget protection, priority rule and embedded graph."""
import json
import re
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import concurrency_findings as cf  # noqa: E402
from agent import priority_rules as pr  # noqa: E402
from agent import report_budget  # noqa: E402
from agent.common import Layout  # noqa: E402
from tests.test_metrics_graphs import Store  # noqa: E402

DAY = "2026-03-10"


def synthetic_extract(burst=True):
    """Day 14:00-16:00: 100 slow ops/min; burst 15:05-15:12 with one hot document."""
    minutes, target = [], []
    for h in (14, 15):
        for m in range(60):
            hot = burst and h == 15 and 5 <= m <= 12
            minutes.append({f"{h:02d}H{m:02d}": {"slow": 2000 if hot else 100, "slowCpuMs": 9000 if hot else 400,
                                                 "writeConflicts": 30000 if hot else 2}})
            if hot:
                target.append({f"{h:02d}H{m:02d}": {"occur": 400, "writeConflicts": 29500}})
    return {"schema_version": "1.7",
            "timeline": {"occurrence_timestamps": {"20260310": minutes}},
            "concurrency": {"write_conflicts": {"total": 240000, "by_operation": [["update", 240000]]},
                            "hot_write_targets": [{"namespace": "appdb.items", "target_id": "0123456789abcdef",
                                                   "filter_fields": ["id"], "count": 3200, "write_conflicts": 236000,
                                                   "app_names": ["app-service-1"], "query_hashes": ["QH000001"],
                                                   "occurrence_timestamps": {"20260310": target}}] if burst else []},
            "slow": {"operations": [{"namespace": "appdb.items", "query_hash": "QH000001", "count": 90000,
                                     "occurrences_truncated": True, "first_seen": "2026-03-10T14:00:00Z",
                                     "last_seen": "2026-03-10T15:59:00Z",
                                     "occurrence_timestamps": {"20260310": [{"14:00:01.000": {"duration_ms": 200}}]}}]}}


class FindingsTests(unittest.TestCase):
    def test_burst_window_and_verdict(self):
        f = cf.summarize(synthetic_extract())
        self.assertTrue(f["contention_detected"])
        (w,) = f["incident_windows"]
        self.assertEqual((w["start_utc"], w["end_utc"]), ("2026-03-10T15:05Z", "2026-03-10T15:12Z"))
        self.assertEqual(w["verdict"], cf.LIKELY)
        self.assertEqual(w["priority_hint"], "P1")
        top = w["hot_documents_in_window"][0]
        self.assertEqual((top["target_id"], top["writes"], top["peak_writes_per_minute"]), ("0123456789abcdef", 3200, 400))
        self.assertGreater(w["ratio_vs_baseline"]["slow"], 10)
        self.assertEqual(f["coverage"]["heavy_groups_without_samples_in_windows"][0]["query_hash"], "QH000001")
        self.assertLess(len(json.dumps(f)), 20_000)

    def test_no_burst_no_contention(self):
        f = cf.summarize(synthetic_extract(burst=False))
        self.assertEqual((f["status"], f["contention_detected"], f["incident_windows"]), ("ok", False, []))

    def test_old_parser_is_not_extracted_not_zero(self):
        f = cf.summarize({"schema_version": "1.5", "slow": {"operations": []}})
        self.assertEqual(f["status"], "not_extracted")

    def test_thresholds_configurable(self):
        f = cf.summarize(synthetic_extract(), {"concurrency_thresholds": {"contention_min_target_share": 1.0}})
        self.assertEqual(f["incident_windows"][0]["verdict"], cf.POSSIBLE)

    def test_episode_contention_is_compact(self):
        a, b = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc), datetime(2026, 3, 10, 15, 20, tzinfo=timezone.utc)
        e = cf.episode_contention(cf.window_stats(synthetic_extract(), a, b))
        self.assertEqual((e["verdict"], e["top_hot_document"]["target_id"]), (cf.LIKELY, "0123456789abcdef"))

    def test_render_markdown(self):
        md = cf.render_markdown(cf.summarize(synthetic_extract()))
        self.assertTrue(md.startswith("## Write contention (hot document)"))
        self.assertIn("[P1] 2026-03-10T15:05–15:12 UTC", md)


class ResourceEpisodeTests(unittest.TestCase):
    def test_cpu_episode_carries_contention_and_unsampled_groups(self):
        import tests.test_resource_investigation as tri
        from agent import resource_investigation as ri
        cpu = [30] * 288
        for i in range(181, 184):  # 15:05-15:15 at PT5M
            cpu[i] = 96
        metrics = json.loads(json.dumps(tri.payload(cpu)).replace("2026-10-27", DAY).replace("2026-10-28", "2026-03-11"))
        a = ri.analyze(metrics, synthetic_extract(), {})
        ep = a["cpu"]["peak_episodes_over_90"][0]
        self.assertEqual(ep["contention"]["verdict"], cf.LIKELY)
        self.assertEqual(ep["correlated"]["unsampled_active_groups"][0]["query_hash"], "QH000001")
        md = ri.render_markdown(a)
        self.assertIn("**Write contention (P1)**", md)
        self.assertIn("none of its retained samples falls in it", md)


class PriorityRuleTests(unittest.TestCase):
    def test_rule_loads_and_decides(self):
        rules = pr.load_rules()
        pr.validate_rules(rules)
        self.assertIn("Rule hot_document_contention:", pr.render_prompt(rules))
        d = pr.evaluate(synthetic_extract(), rules, concurrency=cf.summarize(synthetic_extract()))
        self.assertEqual([x["priority"] for x in d["hot_document_contention"]], ["P1"])
        self.assertEqual(pr.evaluate(synthetic_extract(), rules)["hot_document_contention"], [])


class BudgetTests(unittest.TestCase):
    def test_findings_kept_while_current_shrinks(self):
        findings = json.dumps(cf.summarize(synthetic_extract()), separators=(",", ":"))
        current = json.dumps({"slow": {"operations": [{"namespace": f"db.c{i}", "pad": "x" * 500} for i in range(400)]}})
        user = ("header\n\n" + f'<document label="current">\n{current}\n</document>\n\n'
                f'<document label="concurrency_findings">\n{findings}\n</document>')
        out, receipt = report_budget.fit_prompt(user, 40_000)
        self.assertLessEqual(len(out), 40_000)
        self.assertNotIn("concurrency_findings", receipt.get("levels", {}))
        self.assertIn(findings, out)


class ReportWiringTests(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
        sys.modules["agent.offline_cve"].check = lambda p, **k: {"matches": []}
        sys.modules["agent.offline_cve"].load_catalog = lambda: {}
        from agent import report_stage
        self.rs = report_stage

    def run_node(self, answer, config=None, extract=None):
        L = Layout("p")
        store = Store({L.extract(DAY, "node", "mongodb", "extractionOccurence.json"): extract or synthetic_extract()})
        seen = {}

        def generate(system, user):
            seen["user"] = user
            return answer
        out = self.rs.report_node(store, L, types.SimpleNamespace(generate=generate), "sys", DAY, "node", "mongodb",
                                  None, config=config)
        return out, seen["user"], store, L

    def test_document_instructions_fallback_and_graph(self):
        out, user, store, L = self.run_node("# R\n\n## Executive summary\n\nok\n")
        self.assertIn('<document label="concurrency_findings"', user)
        self.assertIn("## Write contention (hot document)", user)  # instruction header
        md = out["report_md"]
        self.assertTrue(out["contention_detected"])
        self.assertIn("## Write contention (hot document)", md)
        self.assertTrue(store.exists(L.report(DAY, "node", "mongodb", "concurrency-findings.json")))
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib not installed")
        self.assertRegex(md, r"### Write contention timeline \(log\)\n\n!\[Write contention timeline\]\(data:image/png;base64,")
        section = md.split("## Write contention (hot document)")[1]
        self.assertIn("data:image/png;base64,", section)

    def test_model_section_kept_and_graph_inside_it(self):
        answer = "# R\n\n## Write contention (hot document)\n\nmodel text\n\n## Findings\n\nx\n"
        out, _, _, _ = self.run_node(answer)
        md = out["report_md"]
        self.assertEqual(md.count("## Write contention (hot document)"), 1)
        if "data:image/png" in md:
            self.assertLess(md.index("### Write contention timeline"), md.index("## Findings"))

    def test_graph_disabled(self):
        out, _, _, _ = self.run_node("# R\n", config={"concurrency_graphs_enabled": False})
        self.assertNotIn("data:image/png", out["report_md"])
        self.assertIn("## Write contention (hot document)", out["report_md"])

    def test_findings_disabled(self):
        out, user, _, _ = self.run_node("# R\n", config={"concurrency_findings_enabled": False})
        self.assertNotIn("concurrency_findings", user)
        self.assertNotIn("Write contention", out["report_md"])

    def test_graph_import_error_is_a_note(self):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name.startswith("matplotlib"):
                raise ImportError(name)
            return real(name, *a, **k)
        builtins.__import__ = fake
        try:
            out, _, _, _ = self.run_node("# R\n")
        finally:
            builtins.__import__ = real
        self.assertIn("Graph not available: matplotlib is not installed", out["report_md"])

    def test_system_prompt_loads_resource_rules(self):
        self.assertIn("=== references/resource-investigation-rules.md ===", self.rs.build_system_prompt())


if __name__ == "__main__":
    unittest.main()
