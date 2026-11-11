"""Hourly/daily metrics (old log days): peak rules on MAX_* + average spikes; workload correlation
with opcounters and document metrics."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import resource_investigation as ri  # noqa: E402

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
GB_KB = 1024 * 1024


def m(name, units, values, minutes):
    return {"name": name, "units": units, "dataPoints": [
        {"timestamp": (T0 + timedelta(minutes=minutes * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "value": v}
        for i, v in enumerate(values)]}


def payload(granularity, minutes, cpu, cpu_max=None, mem_used=None, mem_max=None, total_kb=16 * GB_KB,
            queue=None, queue_max=None, extra=None):
    ms = [m("SYSTEM_NORMALIZED_CPU_USER", "PERCENT", [v * 0.8 for v in cpu], minutes),
          m("SYSTEM_NORMALIZED_CPU_KERNEL", "PERCENT", [v * 0.2 for v in cpu], minutes)]
    if cpu_max is not None:
        ms += [m("MAX_SYSTEM_NORMALIZED_CPU_USER", "PERCENT", [v * 0.8 for v in cpu_max], minutes),
               m("MAX_SYSTEM_NORMALIZED_CPU_KERNEL", "PERCENT", [v * 0.2 for v in cpu_max], minutes)]
    if mem_used is not None:
        ms += [m("SYSTEM_MEMORY_USED", "KILOBYTES", [u * total_kb / 100 for u in mem_used], minutes),
               m("SYSTEM_MEMORY_AVAILABLE", "KILOBYTES", [(100 - u) * total_kb / 100 for u in mem_used], minutes)]
    if mem_max is not None:
        ms.append(m("MAX_SYSTEM_MEMORY_USED", "KILOBYTES", [u * total_kb / 100 for u in mem_max], minutes))
    for name, values in (extra or {}).items():
        ms.append(m(name, "SCALAR_PER_SECOND", values, minutes))
    out = {"window": {"since": T0.isoformat(), "granularity": granularity}, "data": [{"measurements": ms}]}
    if queue is not None:
        dm = [m("DISK_QUEUE_DEPTH", "SCALAR", queue, minutes)]
        if queue_max is not None:
            dm.append(m("MAX_DISK_QUEUE_DEPTH", "SCALAR", queue_max, minutes))
        out["disk_measurements"] = [{"partitionName": "data", "data": [{"measurements": dm}]}]
    return out


H = 24  # hourly samples in a day


class GranularityTests(unittest.TestCase):
    def test_parse_iso_durations(self):
        self.assertEqual([ri.granularity_seconds(g) for g in ("PT10S", "PT1M", "PT5M", "PT1H", "P1D")],
                         [10, 60, 300, 3600, 86400])
        self.assertEqual(ri.granularity_seconds("bogus"), 300)

    def test_minute_data_keeps_average_basis(self):
        res = ri.analyze(payload("PT1M", 1, [50] * 30, cpu_max=[99] * 30), {}, {})
        self.assertFalse(res["coarse_granularity"])
        self.assertEqual(res["cpu"]["status"], "not_triggered")
        self.assertEqual(res["cpu"]["basis"], "average")
        self.assertNotIn("average_spikes", res["cpu"])


class CoarseCpuTests(unittest.TestCase):
    def test_hourly_peak_on_max_with_average_spike_is_sustained(self):
        avg = [20] * H
        avg[10] = 55  # average spike: > 2x median and +15 points
        mx = [40] * H
        mx[10], mx[3] = 98, 95  # 03h: max-only short peak
        res = ri.analyze(payload("PT1H", 60, avg, cpu_max=mx), {}, {})
        cpu = res["cpu"]
        self.assertTrue(res["coarse_granularity"])
        self.assertEqual(cpu["basis"], "max")
        self.assertEqual(cpu["status"], "triggered")  # averages alone (max 55) would not trigger
        peaks = {e["start_utc"][11:13]: e for e in cpu["peak_episodes_over_90"]}
        self.assertEqual(set(peaks), {"03", "10"})
        self.assertEqual(peaks["10"]["confidence"], "sustained_load")
        self.assertTrue(peaks["10"]["average_spike"])
        self.assertEqual(peaks["03"]["confidence"], "short_peak")
        self.assertEqual(cpu["average_max_percent"], 55)
        self.assertEqual(peaks["10"]["duration_minutes"], 60.0)

    def test_average_spike_below_trigger_is_reported(self):
        avg = [10] * H
        avg[14] = 40
        res = ri.analyze(payload("PT1H", 60, avg, cpu_max=[50] * H), {}, {})
        self.assertEqual(res["cpu"]["status"], "not_triggered")
        self.assertEqual([e["start_utc"][11:13] for e in res["cpu"]["average_spikes"]], ["14"])

    def test_without_max_series_falls_back_to_average_with_note(self):
        res = ri.analyze(payload("PT1H", 60, [95] + [20] * 23), {}, {})
        self.assertEqual(res["cpu"]["basis"], "average")
        self.assertEqual(res["cpu"]["status"], "triggered")
        self.assertTrue(any("MAX_SYSTEM_NORMALIZED_CPU_*" in n for n in res["notes"]))

    def test_sustained_rule_stays_on_average(self):
        res = ri.analyze(payload("PT1H", 60, [75] * 2 + [20] * 22, cpu_max=[80] * H), {}, {})
        eps = res["cpu"]["sustained_episodes_over_70_for_20min"]
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["duration_minutes"], 120.0)


class CoarseMemoryAndDiskTests(unittest.TestCase):
    def test_memory_uses_max_used_over_total(self):
        used, mx = [50] * H, [55] * H
        mx[8] = 90
        res = ri.analyze(payload("PT1H", 60, [10] * H, mem_used=used, mem_max=mx), {}, {})
        mem = res["memory"]
        self.assertEqual(mem["basis"], "max")
        self.assertEqual(mem["status"], "triggered")
        ep = mem["episodes_over_75"][0]
        self.assertEqual(ep["start_utc"][11:13], "08")
        self.assertAlmostEqual(ep["peak"], 90.0, places=1)
        self.assertEqual(ep["confidence"], "short_peak")
        self.assertAlmostEqual(mem["average_max_percent"], 50.0)

    def test_disk_single_hour_confirmed_by_average_spike(self):
        queue, qmax = [1] * H, [4] * H
        queue[5], qmax[5] = 6, 30   # max over 10 + average spike: pressure with one sample
        qmax[20] = 25               # max only, average flat: not enough alone
        part = ri.analyze(payload("PT1H", 60, [10] * H, queue=queue, queue_max=qmax), {}, {})["disk"]["partitions"][0]
        self.assertEqual(part["basis"], "max")
        self.assertTrue(part["pressure"])
        self.assertEqual(part["samples_over_threshold"], 2)
        self.assertEqual(part["samples_confirmed_by_average"], 1)
        conf = {e["start_utc"][11:13]: e["confidence"] for e in part["episodes"]}
        self.assertEqual(conf, {"05": "sustained_load", "20": "short_peak"})

    def test_disk_max_only_peak_is_not_pressure(self):
        qmax = [4] * H
        qmax[20] = 25
        part = ri.analyze(payload("PT1H", 60, [10] * H, queue=[1] * H, queue_max=qmax), {}, {})["disk"]["partitions"][0]
        self.assertFalse(part["pressure"])
        self.assertEqual(part["episodes"], [])


class WorkloadTests(unittest.TestCase):
    def test_volume_increase_names_counters(self):
        cpu = [20] * 12 + [95, 96] + [20] * 10
        inserts = [100] * 12 + [900, 950] + [100] * 10
        queries = [50] * 24
        res = ri.analyze(payload("PT5M", 5, cpu, extra={"OPCOUNTER_INSERT": inserts, "OPCOUNTER_QUERY": queries,
                                                         "DOCUMENT_METRICS_INSERTED": inserts}), {}, {})
        ep = res["cpu"]["peak_episodes_over_90"][0]
        w = ep["workload"]
        self.assertEqual(w["verdict"], "volume_increase")
        self.assertEqual({r["metric"] for r in w["increased"]}, {"OPCOUNTER_INSERT", "DOCUMENT_METRICS_INSERTED"})
        self.assertEqual(res["workload"]["metrics"]["OPCOUNTER_INSERT"]["max"], 950)
        md = ri.render_markdown(res)
        self.assertIn("workload volume increased", md)
        self.assertIn("op insert", md)

    def test_no_volume_change_and_docs_returned_per_read(self):
        cpu = [20] * 12 + [95, 96] + [20] * 10
        queries = [100] * 24
        returned = [200] * 12 + [5000, 5200] + [200] * 10
        res = ri.analyze(payload("PT5M", 5, cpu, extra={"OPCOUNTER_QUERY": queries,
                                                         "DOCUMENT_METRICS_RETURNED": returned}), {}, {})
        w = res["cpu"]["peak_episodes_over_90"][0]["workload"]
        self.assertEqual(w["verdict"], "volume_increase")  # documents returned rose, operations did not
        self.assertEqual([r["metric"] for r in w["increased"]], ["DOCUMENT_METRICS_RETURNED"])
        self.assertEqual(w["docs_returned_per_read_op"], {"window_mean": 51.0, "day_median": 2.0})
        flat = ri.analyze(payload("PT5M", 5, cpu, extra={"OPCOUNTER_QUERY": queries}), {}, {})
        self.assertEqual(flat["cpu"]["peak_episodes_over_90"][0]["workload"]["verdict"], "no_volume_change")
        self.assertIn("per-operation cost", ri.render_markdown(flat))

    def test_no_workload_metrics(self):
        res = ri.analyze(payload("PT5M", 5, [95, 96]), {}, {})
        self.assertEqual(res["cpu"]["peak_episodes_over_90"][0]["workload"], {"verdict": "no_data"})
        self.assertEqual(res["workload"]["status"], "no_data")

    def test_metrics_are_requested(self):
        for name in ri.WORKLOAD_METRICS + ri.MAX_MEMORY:
            self.assertIn(name, ri.INVESTIGATION_METRICS)
        from agent.metrics_catalog import plan_measurement_requests
        plan = plan_measurement_requests({}, extra=ri.INVESTIGATION_METRICS)
        # Pre-existing graph-only names may be unsupported; none of the investigation names is.
        self.assertFalse(set(plan["unsupported"]) & set(ri.INVESTIGATION_METRICS))
        self.assertTrue(all(n in plan["requested"] for n in ri.WORKLOAD_METRICS + ri.MAX_MEMORY))
        self.assertTrue(all(len(b) <= 10 for b in plan["batches"]))


class RenderTests(unittest.TestCase):
    def test_coarse_markdown_mentions_basis_and_confidence(self):
        avg = [20] * H
        avg[10] = 55
        mx = [40] * H
        mx[10] = 98
        md = ri.render_markdown(ri.analyze(payload("PT1H", 60, avg, cpu_max=mx), {}, {}))
        self.assertIn("peak rules evaluated on the MAX_* series", md)
        self.assertIn("max basis", md)
        self.assertIn("average spike confirms sustained load", md)


if __name__ == "__main__":
    unittest.main()
