"""No-network tests for the high resource usage investigation (CPU / memory / disk rules)."""
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import resource_investigation as ri  # noqa: E402
from agent.common import Layout  # noqa: E402
from tests.test_metrics_graphs import Store, measurement  # noqa: E402

T0 = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)


def payload(cpu, mem_used=None, mem_avail=None, conns=None, disk=None):
    ms = [measurement("SYSTEM_NORMALIZED_CPU_USER", "PERCENT", [v * 0.8 for v in cpu]),
          measurement("SYSTEM_NORMALIZED_CPU_KERNEL", "PERCENT", [v * 0.2 for v in cpu]),
          measurement("SYSTEM_NORMALIZED_CPU_GUEST", "PERCENT", [50 for _ in cpu])]  # ignored (in USER)
    if mem_used:
        ms += [measurement("SYSTEM_MEMORY_USED", "KILOBYTES", mem_used),
               measurement("SYSTEM_MEMORY_AVAILABLE", "KILOBYTES", mem_avail)]
    if conns:
        ms.append(measurement("CONNECTIONS", "SCALAR", conns))
    out = {"window": {"since": T0.isoformat(), "until": (T0 + timedelta(days=1)).isoformat(), "granularity": "PT5M"},
           "data": [{"measurements": ms}]}
    if disk is not None:
        out["disk_measurements"] = [{"partitionName": "data", "data": [{"measurements": [
            measurement("DISK_QUEUE_DEPTH", "SCALAR", disk)]}]}]
    return out


def extract(at_minute):
    t = T0 + timedelta(minutes=at_minute)
    return {"slow": {"operations": [
        {"namespace": "shop.orders", "query_hash": "ABCD1234", "plan_summary": "COLLSCAN", "collscan_count": 3,
         "occurrence_timestamps": {t.strftime("%Y%m%d"): [
             {t.strftime("%H:%M:%S") + ".10": {"duration_ms": 4000, "cpu_nanos": 3e9, "docs_examined": 900000,
                                                "n_returned": 2, "storage": {"data": {"bytesRead": 5e8}}}}]}},
        {"namespace": "admin.$cmd", "query_hash": "SYS", "occurrence_timestamps": {t.strftime("%Y%m%d"): [
            {t.strftime("%H:%M:%S") + ".00": {"duration_ms": 9999}}]}}]},
        "error_scan": {"groups": [{"fingerprint": "connection refused", "occurrence_timestamps": {
            t.strftime("%Y%m%d"): [{t.strftime("%HH%M"): {"occur": 4}}]}}]}}


class RuleTests(unittest.TestCase):
    def test_cpu_peak_over_90_correlates_slow_ops_and_errors(self):
        cpu = [20] * 12 + [95, 96] + [20] * 10
        res = ri.analyze(payload(cpu), extract(62), {"timezone": "Asia/Jerusalem"})
        self.assertEqual(res["cpu"]["status"], "triggered")
        peak = res["cpu"]["peak_episodes_over_90"]
        self.assertEqual(len(peak), 1)
        self.assertEqual(peak[0]["start_utc"], "2026-10-27T01:00:00+00:00")
        corr = peak[0]["correlated"]
        self.assertEqual([r["query_hash"] for r in corr["slow_operations"]], ["ABCD1234"])  # admin ns excluded
        self.assertEqual(corr["slow_operations"][0]["storage_bytes_read_total"], 5e8)
        self.assertEqual(corr["error_occurrences_in_window"], 4)
        self.assertEqual(res["cpu"]["sustained_episodes_over_70_for_20min"], [])

    def test_cpu_sustained_70_needs_more_than_20_minutes(self):
        short = ri.analyze(payload([20] + [75] * 4 + [20]), {}, {})  # 20 min -> not triggered
        self.assertEqual(short["cpu"]["status"], "not_triggered")
        long = ri.analyze(payload([20] + [75] * 5 + [20]), {}, {})  # 25 min
        eps = long["cpu"]["sustained_episodes_over_70_for_20min"]
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["duration_minutes"], 25.0)
        self.assertEqual(eps[0]["correlated"]["slow_operations"], [])

    def test_memory_over_75_reports_connections_share(self):
        total = 16 * 1024 * 1024  # 16 GB in KB
        used = [0.5 * total, 0.8 * total, 0.5 * total]
        res = ri.analyze(payload([10, 10, 10], used, [total - u for u in used], conns=[100, 3000, 100]), {}, {})
        self.assertEqual(res["memory"]["status"], "triggered")
        at = res["memory"]["episodes_over_75"][0]["at_peak"]
        self.assertEqual(at["connections"], 3000)
        self.assertEqual(at["connection_memory_estimate_mb"], 3000)
        self.assertAlmostEqual(at["connection_memory_share_percent"], 18.3, places=1)

    def test_disk_pressure_needs_several_samples_over_10(self):
        once = ri.analyze(payload([10] * 6, disk=[1, 15, 1, 1, 1, 1]), {}, {})
        self.assertEqual(once["disk"]["status"], "not_triggered")
        many = ri.analyze(payload([10] * 6, disk=[1, 15, 12, 1, 20, 1]), {}, {})
        part = many["disk"]["partitions"][0]
        self.assertTrue(part["pressure"])
        self.assertEqual(part["samples_over_threshold"], 3)
        self.assertEqual(len(part["episodes"]), 2)
        self.assertEqual(ri.analyze(payload([10]), {}, {})["disk"]["status"], "no_data")

    def test_thresholds_override(self):
        res = ri.analyze(payload([85, 85]), {}, {"resource_thresholds": {"cpu_peak_percent": 80}})
        self.assertEqual(len(res["cpu"]["peak_episodes_over_80"]), 1)

    def test_trim_keeps_episode_occurrences(self):
        ext = extract(62)
        day = "20261027"
        ext["slow"]["operations"][0]["occurrence_timestamps"][day] += [
            {f"0{h}:00:00.00": {"duration_ms": 1}} for h in range(3, 9)]
        trimmed = ri.trim_occurrences(ext, [(T0 + timedelta(minutes=55), T0 + timedelta(minutes=75))], 2)
        g = trimmed["slow"]["operations"][0]
        self.assertEqual(len(g["occurrence_timestamps"][day]), 3)  # in-window one + 2 outside
        self.assertEqual(g["occurrences_trimmed"], 4)
        self.assertEqual(len(ext["slow"]["operations"][0]["occurrence_timestamps"][day]), 7)  # copy


class ReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
        sys.modules["agent.offline_cve"].check = lambda payload: {"matches": []}
        from agent import report_stage
        self.rs = report_stage
        self.layout = Layout("p")

    def run_node(self, store, answer, max_chars=600000):
        seen = {}

        def generate(system, user):
            seen["user"] = user
            return answer
        out = self.rs.report_node(store, self.layout, types.SimpleNamespace(generate=generate), "sys",
                                  "2026-10-27", "node", "mongodb", max_chars)
        return out, seen["user"]

    def test_metrics_present_uses_occurrence_extract_and_adds_section(self):
        L = self.layout
        big_short = {"slow": {"operations": []}, "note": "SHORT"}
        store = Store({
            L.extract("2026-10-27", "node", "mongodb", "extractionOccurence.json"): extract(62),
            L.extract("2026-10-27", "node", "mongodb", "extractionshort.json"): big_short,
            L.observability("2026-10-27", "node", "metrics"): payload([20] * 12 + [95, 96] + [20] * 10,
                                                                     conns=[100] * 24),
        })
        out, user = self.run_node(store, "# Report\n\nFindings.")
        self.assertIn("resource_investigation", user)
        self.assertIn("High resource usage investigation", user)  # instructions sent to the model
        self.assertIn("occurrence_timestamps", user)
        self.assertNotIn("SHORT", user)
        md = out["report_md"]
        self.assertIn(ri.SECTION_HEADING, md)  # deterministic fallback (model omitted it)
        self.assertLess(md.index(ri.SECTION_HEADING), md.index("## APPENDIX"))
        self.assertIn("data:image/png;base64,", md)
        self.assertEqual(out["resource_investigation"], ["cpu"])
        self.assertIn("resource_investigation", out["uploads"])

    def test_model_section_is_kept_not_duplicated(self):
        L = self.layout
        store = Store({L.extract("2026-10-27", "node", "mongodb", "extractionOccurence.json"): extract(62),
                       L.observability("2026-10-27", "node", "metrics"): payload([95, 95])})
        out, _ = self.run_node(store, "# Report\n\n## High resource usage investigation\n\nModel text.\n")
        self.assertEqual(out["report_md"].count("High resource usage investigation"), 1)

    def test_oversized_occurrence_extract_is_trimmed_not_switched_to_short(self):
        L = self.layout
        ext = extract(62)
        ext["slow"]["operations"][0]["occurrence_timestamps"]["20261027"] += [
            {f"{h:02d}:{m:02d}:00.00": {"duration_ms": 1, "slow_message": "x" * 300}}
            for h in range(5, 23) for m in range(60)]
        store = Store({L.extract("2026-10-27", "node", "mongodb", "extractionOccurence.json"): ext,
                       L.extract("2026-10-27", "node", "mongodb", "extractionshort.json"): {"note": "SHORT"},
                       L.observability("2026-10-27", "node", "metrics"): payload([20] * 12 + [95] + [20] * 5)})
        full = len(json.dumps(ext))
        out, user = self.run_node(store, "# R", max_chars=full // 2)
        self.assertLess(len(user), full // 2)
        self.assertNotIn("SHORT", user)
        self.assertIn("extractionOccurence.json (occurrences outside resource episodes capped", out["current_file"])
        self.assertIn("01:02:00.10", user)  # in-episode occurrence kept

    def test_summary_input_strips_embedded_images(self):
        md = "# R\n\n![graph](data:image/png;base64,iVBORw0KGgo=)\n"
        self.assertEqual(self.rs._without_embedded_images(md), "# R\n\n[embedded image omitted: graph]\n")


if __name__ == "__main__":
    unittest.main()
