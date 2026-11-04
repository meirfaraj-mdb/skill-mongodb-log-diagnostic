"""python3 -m unittest discover -s tests -p 'test_report_budget.py'  (from the repository root)"""
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.report_budget import fit_prompt, document_sizes  # noqa: E402


def _doc(label, body):
    return f'<document label="{label}" node="n1">\n{json.dumps(body, separators=(",", ":"))}\n</document>'


def _occ(n):
    return {"20261031": [{f"{h:02d}:{m:02d}:{s:02d}": {"duration_ms": 1200, "cpu_nanos": 9e8, "docs_examined": 50000}}
                         for h in range(24) for m in range(60) for s in (0, 30)][:n]}


def busy_node(groups=120, occurrences=400, episodes=250):
    random.seed(1)
    ops = [{"namespace": f"db.c{i}", "query_hash": f"{i:08X}", "plan_summary": "COLLSCAN", "count": 999,
            "app_names": ["app"] * 20, "sample": "x" * 3000, "occurrence_timestamps": _occ(occurrences)}
           for i in range(groups)]
    current = {"metadata": {"parser_version": 3}, "summary": {"severity_counts": {"E": 5}},
               "slow": {"global_stats": {"count": 99999}, "operations": ops}, "slow_operations": ops,
               "error_scan": {"total_error_events": 5, "groups": [{"fingerprint": f"e{i}", "sample_message": "y" * 2000,
                                                                     "occurrence_timestamps": _occ(occurrences)}
                                                                    for i in range(groups)]},
               "repeated_errors": [{"fingerprint": f"r{i}", "msg": "z" * 500} for i in range(2000)], "trends": [1] * 5000}
    corr = {"slow_operations": [{"namespace": f"db.c{i}", "query_hash": "H"} for i in range(10)],
            "errors": [{"fingerprint": "e", "sample_message": "m" * 240} for _ in range(10)]}
    analysis = {"thresholds": {}, "triggered": ["cpu"], "cpu": {"status": "triggered", "max_percent": 99,
                "peak_episodes_over_90": [{"start_utc": f"2026-10-31T{i // 12:02d}:{(i % 12) * 5:02d}:00+00:00",
                                           "end_utc": "x", "peak": random.random() * 100, "correlated": corr}
                                          for i in range(episodes)]}}
    docs = [_doc("current", current), _doc("n-1", current), _doc("n-8", current),
            _doc("precomputed_diff", {"comparisons": [{"error_groups": [{"k": i} for i in range(40)]}]}),
            _doc("offline_driver_cves", {"matches": []}), _doc("resource_investigation", analysis),
            _doc("index_stats", {"collections": [{"namespace": f"a.b{i}", "indexes": [{"name": "x" * 100}] * 30}
                                                 for i in range(3000)]}),
            _doc("query_stats", {"data": [{"queryShapeHash": f"{i}", "key": {"queryShape": {"f": "?" * 500}}}
                                          for i in range(5000)]})]
    return "Generate the report for node `n1`.\n\nResource rules here.\n\n" + "\n\n".join(docs)


class FitPromptTest(unittest.TestCase):
    def test_under_limit_is_unchanged(self):
        user = "header\n\n" + _doc("current", {"a": 1})
        self.assertEqual(fit_prompt(user, 10_000)[0], user)

    def test_busy_node_fits_and_stays_valid_json(self):
        user = busy_node()
        self.assertGreater(len(user), 2_000_000)
        for limit in (600_000, 250_000, 120_000):
            out, receipt = fit_prompt(user, limit)
            self.assertLessEqual(len(out), limit)
            self.assertTrue(out.startswith("Generate the report for node `n1`."))
            labels = [label for label, _ in document_sizes(out)]
            self.assertIn("current", labels)
            self.assertIn("resource_investigation", labels)
            for body in __import__("re").findall(r"<document [^>]*>\n(.*?)\n</document>", out, __import__("re").S):
                json.loads(body)  # every document is still valid JSON
            cur = json.loads(__import__("re").search(r'label="current"[^>]*>\n(.*?)\n</document>', out,
                                                     __import__("re").S).group(1))
            self.assertNotIn("slow_operations", cur)            # duplicate alias removed
            self.assertEqual(cur["slow"]["global_stats"]["count"], 99999)  # aggregates untouched
            self.assertIn("_pipeline_budget", cur)

    def test_impossible_limit_raises_with_breakdown(self):
        with self.assertRaises(RuntimeError) as ctx:
            fit_prompt(busy_node(groups=5, occurrences=5, episodes=5), 500)
        self.assertIn("largest documents", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
