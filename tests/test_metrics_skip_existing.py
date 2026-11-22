"""No-network tests: a stored, complete metrics.json is reused instead of calling Atlas again."""
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import observability_stage  # noqa: E402
from agent.common import Layout  # noqa: E402
from agent.observability_contract import artifact_key  # noqa: E402

DAY = "2026-11-10"
AFTER_DAY = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
DURING_DAY = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
CONFIG = {"group_id": "g", "prefix": "p", "timezone": "UTC", "observability_sources": {"metrics": "atlas_api"}}


def _collector():
    spec = importlib.util.spec_from_file_location("metrics_skip_collector",
                                                  ROOT / "skills/mongodb-observability/scripts/observability.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLLECTOR = _collector()


def _fake_pages(config, url, source):
    if url.endswith("/disks"):
        return [{"results": []}], False
    return [{"measurements": [{"name": "CONNECTIONS", "dataPoints": [{"value": 1}]}]}], False


def real_metrics(config=CONFIG, now=AFTER_DAY, pages=_fake_pages):
    """What the real collector stores for DAY (Atlas mocked)."""
    with mock.patch.object(COLLECTOR, "_api_pages", side_effect=pages):
        return COLLECTOR.collect_api(config, "h1", "h1:27017", "metrics", "atlas_api", log_date=DAY, now=now)


class _Store:
    def __init__(self):
        self.data = {}

    def put_text(self, key, text, content_type=None):
        self.data[key] = text
        return "mem://" + key

    def exists(self, key):
        return key in self.data

    def get_text(self, key):
        return self.data[key]

    def uri(self, key):
        return "mem://" + key


class MetricsSkipExisting(unittest.TestCase):
    def key(self, host="h1"):
        return artifact_key(Layout.from_config(CONFIG), DAY, host, "metrics")

    def store_with(self, payload):
        store = _Store()
        store.put_text(self.key(), payload if isinstance(payload, str) else json.dumps(payload))
        return store

    def run_stage(self, store, config=CONFIG, hosts=("h1", "h2")):
        calls = []

        class Collector:
            def collect(self, cfg, host, process_id, kind, source, log_date=None, process=None):
                calls.append((host, kind))
                return real_metrics(cfg)

        nodes = [{"host": h, "host_dir": h, "process_id": f"{h}:27017"} for h in hosts]
        with mock.patch.object(observability_stage.skills, "observability", return_value=Collector(), create=True), \
                mock.patch("agent.observability_contract.resolve_source",
                           side_effect=lambda cfg, kind: "atlas_api" if kind == "metrics" else "disabled"):
            out = observability_stage.collect_nodes(config, DAY, store, nodes)
        return out, calls

    def test_complete_day_is_not_fetched_again(self):
        store = self.store_with(real_metrics())
        before = store.get_text(self.key())
        out, calls = self.run_stage(store)
        self.assertEqual(calls, [("h2", "metrics")])
        art = out["nodes"][0]["artifacts"]["metrics"]
        self.assertEqual(art["status"], "skipped_existing")
        self.assertTrue(art["window"]["complete_day"])
        self.assertEqual(store.get_text(self.key()), before)  # untouched
        self.assertEqual(out["nodes"][1]["artifacts"]["metrics"]["status"], "collected")
        self.assertEqual(out["status"], "completed")

    def test_second_run_makes_no_api_call(self):
        store = _Store()
        _, first = self.run_stage(store)
        _, second = self.run_stage(store)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])

    def test_day_in_progress_is_refreshed(self):
        stored = real_metrics(now=DURING_DAY)
        self.assertFalse(stored["window"]["complete_day"])
        out, calls = self.run_stage(self.store_with(stored))
        self.assertIn(("h1", "metrics"), calls)
        art = out["nodes"][0]["artifacts"]["metrics"]
        self.assertEqual((art["status"], art["refetch_reason"]), ("collected", "incomplete_day"))

    def assert_refetched(self, payload, reason, config=CONFIG):
        out, calls = self.run_stage(self.store_with(payload), config)
        self.assertIn(("h1", "metrics"), calls, reason)
        self.assertEqual(out["nodes"][0]["artifacts"]["metrics"].get("refetch_reason"), reason)

    def test_unusable_files_are_refreshed(self):
        good = real_metrics()
        self.assert_refetched("{not json", "unreadable")
        self.assert_refetched({**good, "schema_version": 1}, "old_or_unknown_format")
        self.assert_refetched({**good, "data": []}, "no_data")
        self.assert_refetched({**good, "source": "ops_manager_metrics"}, "different_source")

    def test_config_changes_are_refreshed(self):
        good = real_metrics()
        self.assert_refetched(good, "different_granularity", {**CONFIG, "metrics_granularity": "PT1M"})
        self.assert_refetched(good, "different_timezone", {**CONFIG, "timezone": "Asia/Jerusalem"})
        narrow = real_metrics({**CONFIG, "metrics_graph_groups": ["CACHE"], "resource_investigation_enabled": False})
        self.assert_refetched(narrow, "missing_measurements")
        # A wider stored plan covers a narrower config.
        out, calls = self.run_stage(self.store_with(good), {**CONFIG, "metrics_graph_groups": ["CACHE"]}, hosts=("h1",))
        self.assertEqual(calls, [])

    def test_coarse_fallback_is_reused_for_same_requested_granularity(self):
        stored = real_metrics()  # two days later: PT5M fell back to PT1H
        self.assertEqual((stored["window"]["granularity"], stored["window"]["requested_granularity"]),
                         ("PT1H", "PT5M"))
        _, calls = self.run_stage(self.store_with(stored), hosts=("h1",))
        self.assertEqual(calls, [])

    def test_transient_errors_refresh_but_permanent_ones_do_not(self):
        good = real_metrics()
        self.assert_refetched({**good, "errors": [{"metrics": ["X"], "status": 429, "error_type": "HTTPError"}]},
                              "transient_errors")
        self.assert_refetched({**good, "errors": [{"metrics": ["X"], "status": 503, "error_type": "HTTPError"}]},
                              "transient_errors")
        self.assert_refetched({**good, "disk_errors": [{"step": "list_disks", "status": None,
                                                        "error_type": "URLError"}]}, "transient_errors")
        permanent = {**good, "errors": [{"metrics": ["X"], "status": 404, "error_type": "HTTPError"}],
                     "disk_errors": [{"step": "disk_measurements", "partitionName": "data", "status": None,
                                      "metrics": ["MAX_DISK_QUEUE_DEPTH"], "error_type": "NotReturned"}]}
        _, calls = self.run_stage(self.store_with(permanent), hosts=("h1",))
        self.assertEqual(calls, [])

    def test_disk_plan_is_recorded_and_checked(self):
        good = real_metrics()
        self.assertIn("DISK_QUEUE_DEPTH", good["measurement_plan"]["disk"])
        missing = json.loads(json.dumps(good))
        missing["measurement_plan"]["disk"] = []
        self.assert_refetched(missing, "missing_disk_measurements")

    def test_switch_and_cli_force_refresh(self):
        _, calls = self.run_stage(self.store_with(real_metrics()), {**CONFIG, "metrics_skip_existing": False})
        self.assertEqual(len(calls), 2)
        from agent import handler
        seen = {}
        with mock.patch.object(handler.observability_stage, "run",
                               side_effect=lambda cfg, day: seen.update(cfg) or {"status": "completed"}):
            handler.run_pipeline({"stage": "observability", "log_date": DAY, "refresh_observability": True},
                                 config={**CONFIG, "cloud": "local", "cluster_name": "c"})
        self.assertFalse(seen["metrics_skip_existing"])
        self.assertFalse(seen["query_stats_skip_existing"])

    def test_env_switch(self):
        from agent.providers import _env_config
        with mock.patch.dict("os.environ", {"MONGODB_LOG_DIAG_METRICS_SKIP_EXISTING": "false"}):
            self.assertIs(_env_config()["metrics_skip_existing"], False)


if __name__ == "__main__":
    unittest.main()
