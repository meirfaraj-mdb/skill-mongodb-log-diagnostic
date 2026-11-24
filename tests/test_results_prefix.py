"""MONGODB_LOG_DIAG_RESULTS_PREFIX: extracts/reports go to <results bucket>/<results prefix>/..., inputs stay
in <input bucket>/<prefix>/...; the default (unset) keeps the previous single-prefix layout.
Run from repository root: python3 -m unittest tests.test_results_prefix"""
import os
import tempfile
import unittest
from unittest.mock import patch

from agent import handler, providers
from agent.bucket_routing import SplitBucketStore
from agent.common import Layout


class Mem:
    def __init__(self, name):
        self.name, self.scheme, self.items = name, "gs", {}

    def uri(self, key): return f"gs://{self.name}/{key}"
    def exists(self, key): return key in self.items
    def list_keys(self, prefix): return sorted(k for k in self.items if k.startswith(prefix))
    def get_text(self, key): return self.items[key]
    def put_text(self, key, text, content_type):
        self.items[key] = text
        return self.uri(key)
    def upload(self, path, key, content_type, metadata=None):
        with open(path, encoding="utf-8") as f:
            self.items[key] = f.read()
        return self.uri(key)
    def upload_stream(self, fileobj, key, content_type, metadata=None):
        self.items[key] = fileobj.read().decode()
        return self.uri(key)
    def download(self, key, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.items[key])
    def open_read(self, key):
        import io
        return io.BytesIO(self.items[key].encode())


DAY, NODE, LOG = "2026-11-23", "host-0", "mongodb"
L = Layout("atlas-logs")


class SplitRouting(unittest.TestCase):
    def setUp(self):
        self.inp, self.out = Mem("in-bkt"), Mem("out-bkt")
        self.inp.items[L.raw_log(DAY, NODE, LOG)] = "raw"
        self.inp.items[L.query_stats(DAY, NODE)] = "{}"
        self.store = SplitBucketStore(self.inp, self.out, "atlas-logs", "diag/reports-v2")

    def test_results_written_under_results_prefix(self):
        uri = self.store.put_text(L.extract(DAY, NODE, LOG, "extractionOccurence.json"), "{}", "application/json")
        self.assertEqual(uri, f"gs://out-bkt/diag/reports-v2/{DAY}/{NODE}/extracts/{LOG}/extractionOccurence.json")
        self.store.put_text(L.cluster_report(DAY, "cluster-summary.md"), "# s", "text/markdown")
        self.assertIn(f"diag/reports-v2/{DAY}/cluster/reports/cluster-summary.md", self.out.items)
        self.assertFalse([k for k in self.out.items if k.startswith("atlas-logs/")])
        self.assertFalse([k for k in self.inp.items if "/extracts/" in k or "/reports/" in k])

    def test_inputs_stay_in_input_prefix(self):
        self.assertEqual(self.store.uri(L.raw_log(DAY, NODE, LOG)), f"gs://in-bkt/atlas-logs/{DAY}/{NODE}/mongodb/{LOG}.gz")
        self.assertEqual(self.store.get_text(L.query_stats(DAY, NODE)), "{}")
        self.store.put_text(L.index_stats(DAY, NODE), "{}", "application/json")  # supplied/observability input
        self.assertIn(L.index_stats(DAY, NODE), self.inp.items)

    def test_read_back_and_exists_use_logical_keys(self):
        key = L.report(DAY, NODE, LOG, "report.md")
        self.store.put_text(key, "report", "text/markdown")
        self.assertTrue(self.store.exists(key))
        self.assertEqual(self.store.get_text(key), "report")
        self.assertEqual(self.store.open_read(key).read(), b"report")

    def test_listing_merges_both_locations_as_logical_keys(self):
        self.store.put_text(L.extract(DAY, NODE, LOG, "extractionshort.json"), "{}", "application/json")
        keys = self.store.list_keys(L.day(DAY) + "/")
        self.assertEqual(keys, sorted([L.raw_log(DAY, NODE, LOG), L.query_stats(DAY, NODE),
                                       L.extract(DAY, NODE, LOG, "extractionshort.json")]))
        self.assertEqual(self.store.list_keys(f"atlas-logs/{DAY}/{NODE}/extracts/"),
                         [L.extract(DAY, NODE, LOG, "extractionshort.json")])
        self.assertIn(L.extract(DAY, NODE, LOG, "extractionshort.json"), self.store.list_keys(""))

    def test_stale_results_in_wrong_location_are_ignored(self):
        self.inp.items[L.report(DAY, NODE, LOG, "report.md")] = "stale in input bucket"
        self.out.items[L.report(DAY, NODE, LOG, "report.md")] = "stale under old prefix"
        self.assertFalse(self.store.exists(L.report(DAY, NODE, LOG, "report.md")))
        self.assertNotIn(L.report(DAY, NODE, LOG, "report.md"), self.store.list_keys(L.day(DAY) + "/"))

    def test_day_uri_points_to_results_location(self):
        self.assertEqual(self.store.uri(L.day(DAY)), f"gs://out-bkt/diag/reports-v2/{DAY}")
        self.assertEqual(self.store.uri(L.cluster_report(DAY, "")), f"gs://out-bkt/diag/reports-v2/{DAY}/cluster/reports/")

    def test_upload_and_download(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "x.json")
            with open(src, "w") as f:
                f.write("x")
            self.store.upload(src, L.extract(DAY, NODE, LOG, "handoff.md"), "text/markdown")
            dst = os.path.join(d, "y")
            self.store.download(L.extract(DAY, NODE, LOG, "handoff.md"), dst)
            with open(dst, encoding="utf-8") as f:
                self.assertEqual(f.read(), "x")
        self.assertIn(f"diag/reports-v2/{DAY}/{NODE}/extracts/{LOG}/handoff.md", self.out.items)

    def test_default_results_prefix_is_input_prefix(self):
        store = SplitBucketStore(self.inp, self.out, "atlas-logs")
        self.assertEqual(store.results_prefix, "atlas-logs")
        self.assertEqual(store.put_text(L.report(DAY, NODE, LOG, "report.md"), "r", "text/markdown"),
                         f"gs://out-bkt/atlas-logs/{DAY}/{NODE}/reports/{LOG}/report.md")


class SameBucketDistinctPrefix(unittest.TestCase):
    def test_one_bucket_two_prefixes(self):
        bkt = Mem("one")
        bkt.items[L.raw_log(DAY, NODE, LOG)] = "raw"
        store = SplitBucketStore(bkt, bkt, "atlas-logs", "atlas-logs/results")  # nested on purpose
        store.put_text(L.report(DAY, NODE, LOG, "report.md"), "r", "text/markdown")
        self.assertIn(f"atlas-logs/results/{DAY}/{NODE}/reports/{LOG}/report.md", bkt.items)
        keys = store.list_keys("atlas-logs/")
        self.assertEqual(keys, sorted([L.raw_log(DAY, NODE, LOG), L.report(DAY, NODE, LOG, "report.md")]))
        self.assertFalse([k for k in keys if k.startswith("atlas-logs/results/")])


ENV = {"MONGODB_LOG_DIAG_INPUT_MODE": "existing_bucket", "MONGODB_LOG_DIAG_BUCKET": "gs://out-bkt",
       "MONGODB_LOG_DIAG_INPUT_BUCKET": "in-bkt", "MONGODB_LOG_DIAG_PREFIX": "/atlas-logs/"}


class Config(unittest.TestCase):
    def cfg(self, **extra):
        with patch.dict(os.environ, {**ENV, **extra}, clear=False):
            return providers.normalize_config({}, "gcp")

    def test_defaults_to_input_prefix(self):
        self.assertEqual(self.cfg()["results_prefix"], "atlas-logs")
        self.assertEqual(self.cfg(MONGODB_LOG_DIAG_RESULTS_PREFIX="  ")["results_prefix"], "atlas-logs")

    def test_custom_and_normalized(self):
        cfg = self.cfg(MONGODB_LOG_DIAG_RESULTS_PREFIX="/diag/reports/")
        self.assertEqual((cfg["prefix"], cfg["results_prefix"]), ("atlas-logs", "diag/reports"))

    def test_rejects_unsafe_values(self):
        for bad in ("../x", "a/../b", "gs://b/x", "a//b", "a b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.cfg(MONGODB_LOG_DIAG_RESULTS_PREFIX=bad)

    def test_get_store_routing(self):
        stores = {}
        def single(config):
            return stores.setdefault(config["bucket"], Mem(config["bucket"]))
        with patch.object(providers, "_get_single_store", side_effect=single):
            same = providers.get_store({"bucket": "b", "input_bucket": "b", "prefix": "p", "results_prefix": "p"})
            self.assertNotIsInstance(getattr(same, "_delegate", same), SplitBucketStore)
            one = providers.get_store({"bucket": "b", "input_bucket": "b", "prefix": "p", "results_prefix": "r"})
            self.assertEqual(one.put_text(Layout("p").report(DAY, NODE, LOG, "report.md"), "x", "text/markdown"),
                             f"gs://b/r/{DAY}/{NODE}/reports/{LOG}/report.md")
            two = providers.get_store({"bucket": "out", "input_bucket": "in", "prefix": "p", "results_prefix": "r"})
            self.assertEqual(two.uri(Layout("p").raw_log(DAY, NODE, LOG)), f"gs://in/p/{DAY}/{NODE}/mongodb/{LOG}.gz")
            self.assertEqual(two.uri(Layout("p").extract(DAY, NODE, LOG, "a.json")),
                             f"gs://out/r/{DAY}/{NODE}/extracts/{LOG}/a.json")


class AllClusters(unittest.TestCase):
    def test_cluster_folder_added_to_both_prefixes(self):
        with patch.object(handler.skills, "atlas_logs") as atlas:
            atlas.return_value.safe_filename.side_effect = lambda n: n.replace(" ", "_")
            cfg = handler.cluster_config({"prefix": "atlas-logs", "results_prefix": "diag"}, "Cluster 0")
            legacy = handler.cluster_config({"prefix": "atlas-logs"}, "Cluster 0")
        self.assertEqual((cfg["prefix"], cfg["results_prefix"]), ("atlas-logs/Cluster_0", "diag/Cluster_0"))
        self.assertEqual(legacy["results_prefix"], "atlas-logs/Cluster_0")


class ReportStageEndToEnd(unittest.TestCase):
    """report_stage.run() unchanged: it reads extracts and writes reports through logical keys."""
    def test_report_stage_uses_results_prefix(self):
        import json
        from agent import report_stage
        from tests.test_report_every_node import DAY as RDAY, NODES, Llm, store_for
        _, legacy = store_for()  # extracts laid out under the input prefix "atlas-logs"
        inp, out = Mem("in-bkt"), Mem("out-bkt")
        for key, value in legacy.items.items():
            text = value if isinstance(value, str) else json.dumps(value)
            out.items["diag/" + key[len("atlas-logs/"):]] = text  # extracts already in the results prefix
        store = SplitBucketStore(inp, out, "atlas-logs", "diag")
        cfg = {"prefix": "atlas-logs", "results_prefix": "diag", "cluster_name": "c", "expected_node_count": 3}
        manifest = report_stage.run(cfg, RDAY, store=store, llm=Llm())
        self.assertFalse(manifest["failed"])
        for host in NODES:
            self.assertIn(f"diag/{RDAY}/{host}/reports/mongodb/report.md", out.items)
        self.assertIn(f"diag/{RDAY}/cluster/reports/cluster-summary.md", out.items)
        self.assertEqual(manifest["storage"], f"gs://out-bkt/diag/{RDAY}")
        self.assertFalse(inp.items)
        self.assertFalse([k for k in out.items if k.startswith("atlas-logs/")])


if __name__ == "__main__":
    unittest.main()
