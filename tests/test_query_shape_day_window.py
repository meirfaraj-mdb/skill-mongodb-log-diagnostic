"""Query Shape Insights / API windows must match the log day and the 7-day retention."""
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from agent.observability_contract import (QueryShapeWindowUnavailable, log_day_window,
                                          query_shape_window)
from agent.skills import observability as load_observability

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
TZ = "Asia/Jerusalem"
CONFIG = {"group_id": "g" * 24, "cluster_name": "c0", "atlas_public_key": "pub",
          "atlas_private_key": "priv", "timezone": TZ}


class _Opener:
    def __init__(self):
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        return io.BytesIO(json.dumps({"summaries": []}).encode())


class QueryShapeWindowTest(unittest.TestCase):
    def test_previous_day_is_full_local_day(self):
        since, until, complete = query_shape_window("2026-10-24", TZ, NOW)
        self.assertEqual(since.isoformat(), "2026-10-23T21:00:00+00:00")
        self.assertEqual(until.isoformat(), "2026-10-24T21:00:00+00:00")
        self.assertTrue(complete)

    def test_oldest_day_inside_retention(self):
        since, _, complete = query_shape_window("2026-10-19", TZ, NOW)
        self.assertGreater(since, NOW - timedelta(days=7))
        self.assertTrue(complete)

    def test_day_outside_retention_is_rejected(self):
        with self.assertRaises(QueryShapeWindowUnavailable):
            query_shape_window("2026-10-18", TZ, NOW)

    def test_today_is_partial(self):
        _, until, complete = query_shape_window("2026-10-25", TZ, NOW)
        self.assertEqual(until, NOW)
        self.assertFalse(complete)

    def test_future_day_is_rejected(self):
        with self.assertRaises(QueryShapeWindowUnavailable):
            query_shape_window("2026-10-26", TZ, NOW)

    def test_dst_day_length(self):
        since, until = log_day_window("2026-10-25", "Europe/London")
        self.assertEqual(until - since, timedelta(hours=25))

    def test_collector_sends_log_day_window(self):
        obs = load_observability()
        opener = _Opener()
        with mock.patch.object(obs, "_atlas_opener", return_value=opener):
            result = obs.collect_api(CONFIG, "h1", "h1:27017", "query_stats", "atlas_api",
                                     log_date="2026-10-24", now=NOW)
        query = parse_qs(urlsplit(opener.urls[0]).query)
        since, until = log_day_window("2026-10-24", TZ)
        self.assertEqual(query["since"], [str(int(since.timestamp() * 1000))])
        self.assertEqual(query["until"], [str(int(until.timestamp() * 1000))])
        self.assertEqual(query["processIds"], ["h1:27017"])
        self.assertEqual(result["temporal_basis"], "log_day_window")
        self.assertTrue(result["window"]["complete_day"])
        self.assertEqual(result["window"]["log_date"], "2026-10-24")

    def test_collector_requires_log_date(self):
        obs = load_observability()
        with self.assertRaises(ValueError):
            obs.collect_query_shapes(CONFIG, "h1", "h1:27017")

    def test_stage_marks_out_of_retention_and_writes_nothing(self):
        from agent import observability_stage

        class Store:
            def __init__(self):
                self.puts = []

            def put_text(self, key, text, content_type):
                self.puts.append(key)
                return "mem://" + key

        class Collector:
            def collect(self, config, host, process_id, kind, source, log_date=None):
                raise QueryShapeWindowUnavailable("too old")

        store = Store()
        config = {"prefix": "p", "deployment_type": "atlas"}
        with mock.patch.object(observability_stage.skills, "observability", return_value=Collector()):
            out = observability_stage.collect_nodes(config, "2026-10-01", store,
                                                    [{"host": "h1", "host_dir": "h1", "process_id": "h1:27017"}])
        artifact = out["nodes"][0]["artifacts"]["query_stats"]
        self.assertEqual(artifact["status"], "out_of_retention")
        self.assertNotIn("p/2026-10-01/h1/queryStats/query-stats.json", store.puts)
        self.assertEqual(out["status"], "completed")


if __name__ == "__main__":
    unittest.main()
