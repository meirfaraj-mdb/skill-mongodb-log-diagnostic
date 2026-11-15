"""Cluster scoping: a selected cluster downloads/observes only its own nodes (prefix unchanged);
an empty cluster_name lists the project clusters and stores each under <prefix>/<cluster>/."""
import io
import os
import tempfile
import unittest
from unittest import mock

from agent import download_stage, handler, observability_stage, skills
from agent.providers import LocalStore, normalize_config

atlas = skills.atlas_logs()
DAY = "2026-11-10"
BASE = {"atlas_public_key": "p", "atlas_private_key": "s", "group_id": "g" * 24, "timezone": "UTC",
        "api_version": "2025-03-12", "prefix": "atlas-logs", "bucket": "local", "input_mode": "atlas_api"}


def proc(host, rs, type_name="REPLICA_SECONDARY", alias=None, port=27017):
    return {"id": f"{host}:{port}", "hostname": host, "userAlias": alias or host, "port": port,
            "replicaSetName": rs, "typeName": type_name}


# Project with 3 clusters: replica set "prod", sharded "prod-eu" (internal atlas-* host names,
# cluster-name alias), and "analytics". "prod" is a prefix of "prod-eu": must never match it.
PROCESSES = (
    [proc(f"prod-shard-00-0{i}.ab1cd.mongodb.net", "atlas-p1-shard-0",
          "REPLICA_PRIMARY" if i == 0 else "REPLICA_SECONDARY") for i in range(3)]
    + [proc(f"atlas-xy9-shard-0{s}-0{i}.zz9.mongodb.net", f"atlas-xy9-shard-{s}", "SHARD_SECONDARY",
            alias=f"prod-eu-shard-0{s}-0{i}.zz9.mongodb.net") for s in range(2) for i in range(2)]
    + [proc("atlas-xy9-config-00-00.zz9.mongodb.net", "atlas-xy9-config-0", "SHARD_CONFIG_PRIMARY",
            alias="prod-eu-config-00-00.zz9.mongodb.net")]
    + [proc("atlas-xy9-shard-00-00.zz9.mongodb.net", None, "SHARD_MONGOS",
            alias="prod-eu-shard-00-00.zz9.mongodb.net", port=27016)]
    + [proc(f"analytics-shard-00-0{i}.qq7.mongodb.net", "atlas-an1-shard-0") for i in range(2)]
    + [proc("analytics-shard-00-09.qq7.mongodb.net", "atlas-an1-shard-0", "NO_DATA")]
)
CLUSTERS = {
    "prod": {"name": "prod", "connectionStrings": {"standard":
             "mongodb://prod-shard-00-00.ab1cd.mongodb.net:27017,prod-shard-00-01.ab1cd.mongodb.net:27017,"
             "prod-shard-00-02.ab1cd.mongodb.net:27017/?ssl=true&authSource=admin&replicaSet=atlas-p1-shard-0"}},
    "prod-eu": {"name": "prod-eu", "connectionStrings": {"standard":
                "mongodb://prod-eu-shard-00-00.zz9.mongodb.net:27016,prod-eu-shard-00-01.zz9.mongodb.net:27016"
                "/?ssl=true&authSource=admin"}},
    "analytics": {"name": "analytics", "connectionStrings": {}},
    "paused-one": {"name": "paused-one", "paused": True},
}


class FakeAtlas:
    max_retries = 1

    def __init__(self, config=None):
        self.downloads = []

    def _retry_delay(self, attempt):
        return 0

    def get_json(self, path, query=None):
        if path.endswith("/processes"):
            return {"results": PROCESSES}
        if path.endswith("/clusters"):
            return {"results": list(CLUSTERS.values())}
        name = path.rsplit("/", 1)[-1]
        return CLUSTERS[name]

    def download_log(self, group, host, log, start, end):
        self.downloads.append((host, log))
        return io.BytesIO(b"gzip")


def selected(cluster, **extra):
    return sorted(p["hostname"] + ":" + str(p["port"])
                  for p in atlas.target_hosts({**BASE, "cluster_name": cluster, **extra}, FakeAtlas()))


class TargetHostsTest(unittest.TestCase):
    def test_replica_set_selects_only_its_nodes(self):
        self.assertEqual(selected("prod"), [f"prod-shard-00-0{i}.ab1cd.mongodb.net:27017" for i in range(3)])

    def test_sharded_cluster_with_internal_host_names(self):
        hosts = selected("prod-eu")
        self.assertEqual(len(hosts), 6)  # 4 shard members + config + mongos
        self.assertTrue(all(h.startswith("atlas-xy9-") for h in hosts))

    def test_name_prefix_of_another_cluster_never_matches(self):
        self.assertFalse(any("xy9" in h for h in selected("prod")))
        self.assertFalse(any("prod-shard" in h for h in selected("prod-eu")))

    def test_name_only_match_and_no_data_ignored(self):
        self.assertEqual(selected("analytics"), [f"analytics-shard-00-0{i}.qq7.mongodb.net:27017" for i in range(2)])

    def test_host_selector_only_narrows_the_cluster(self):
        self.assertEqual(selected("prod", host_selector="00-02"), ["prod-shard-00-02.ab1cd.mongodb.net:27017"])
        with self.assertRaises(atlas.NoMatchingHosts):
            selected("prod", host_selector="analytics")

    def test_explicit_hostnames_override(self):
        self.assertEqual(selected("prod", hostnames=["analytics-shard-00-00.qq7.mongodb.net"]),
                         ["analytics-shard-00-00.qq7.mongodb.net:27017"])

    def test_list_clusters_skips_paused(self):
        self.assertEqual(atlas.list_clusters(FakeAtlas(), "g"), ["prod", "prod-eu", "analytics"])


class SelectedClusterStagesTest(unittest.TestCase):
    def test_download_only_cluster_nodes_prefix_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, client = LocalStore(tmp), FakeAtlas()
            out = download_stage.run({**BASE, "cluster_name": "prod"}, DAY, store=store, client=client)
            self.assertEqual(len(client.downloads), 3)
            self.assertTrue(all(h.startswith("prod-shard-") for h, _ in client.downloads))
            for entry in out["logs"]:
                self.assertRegex(entry["key"], rf"^atlas-logs/{DAY}/prod-shard-00-0\d\.ab1cd\.mongodb\.net/mongodb/")

    def test_observability_nodes_are_cluster_nodes(self):
        with mock.patch.object(atlas, "AtlasClient", FakeAtlas):
            nodes = observability_stage._discover_atlas_nodes({**BASE, "cluster_name": "prod-eu"})
        self.assertEqual(len(nodes), 5)  # mongos shares a shard host: folded into that node
        self.assertTrue(all(n["host"].startswith("atlas-xy9-") for n in nodes))


class AllClustersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalStore(self.tmp.name)
        self.client = FakeAtlas()
        patches = [mock.patch.object(atlas, "AtlasClient", lambda config=None: self.client),
                   mock.patch("agent.providers.get_store", lambda config: self.store)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_empty_cluster_downloads_each_cluster_under_its_folder(self):
        out = handler.run_pipeline({"stage": "download", "log_date": DAY}, config={**BASE, "cluster_name": ""})
        self.assertEqual(list(out["clusters"]), ["prod", "prod-eu", "analytics"])
        keys = self.store.list_keys("atlas-logs/")
        self.assertEqual(len(keys), 3 + 6 + 2)
        for key in keys:
            cluster, day, host = key.split("/")[1:4]
            self.assertEqual(day, DAY)
            self.assertTrue(selected(cluster) and any(h.startswith(host) for h in selected(cluster)), key)
        self.assertEqual({e["cluster"] for e in out["download"]["logs"]}, {"prod", "prod-eu", "analytics"})
        self.assertEqual(out["clusters"]["prod"]["prefix"], "atlas-logs/prod")

    def test_cluster_without_hosts_is_skipped_not_fatal(self):
        CLUSTERS["empty"] = {"name": "empty", "connectionStrings": {}}
        self.addCleanup(CLUSTERS.pop, "empty")
        out = handler.run_pipeline({"stage": "download", "log_date": DAY}, config={**BASE, "cluster_name": ""})
        self.assertEqual(out["clusters"]["empty"]["status"], "skipped_no_hosts")
        self.assertNotIn("failed_clusters", out)

    def test_extract_logs_are_routed_to_their_cluster(self):
        seen = []
        with mock.patch.object(handler.extract_stage, "run",
                               side_effect=lambda config, log_date, logs=None, skip_existing=True:
                               seen.append((config["prefix"], config["cluster_name"], logs)) or {}):
            self.store.put_text(f"atlas-logs/analytics/{DAY}/h/mongodb/mongodb.gz", "x", "application/gzip")
            handler.run_pipeline({"stage": "extract", "log_date": DAY,
                                  "logs": [{"cluster": "analytics", "host_dir": "h", "log_name": "mongodb"}]},
                                 config={**BASE, "cluster_name": ""})
        self.assertEqual([(p, c) for p, c, _ in seen], [("atlas-logs/analytics", "analytics")])

    def test_report_skips_clusters_without_data(self):
        seen = []
        self.store.put_text(f"atlas-logs/prod/{DAY}/n/extracts/mongodb/x.json", "{}", "application/json")
        with mock.patch.object(handler.report_stage, "run",
                               side_effect=lambda config, log_date, summary_only=False: seen.append(config["prefix"]) or {}):
            out = handler.run_pipeline({"stage": "report", "log_date": DAY}, config={**BASE, "cluster_name": ""})
        self.assertEqual(seen, ["atlas-logs/prod"])
        self.assertEqual(out["clusters"]["analytics"]["status"], "skipped_no_data")

    def test_selected_cluster_keeps_flat_prefix(self):
        out = handler.run_pipeline({"stage": "download", "log_date": DAY}, config={**BASE, "cluster_name": "prod"})
        self.assertNotIn("clusters", out)
        self.assertTrue(all(k.startswith(f"atlas-logs/{DAY}/prod-shard-") for k in self.store.list_keys("atlas-logs/")))


class ConfigTest(unittest.TestCase):
    def test_empty_cluster_name_is_valid_in_atlas_api_mode(self):
        env = {"MONGODB_LOG_DIAG_BUCKET": "b", "MONGODB_LOG_DIAG_GROUP_ID": "g" * 24,
               "MONGODB_LOG_DIAG_TIMEZONE": "UTC", "MONGODB_LOG_DIAG_API_VERSION": "2025-03-12",
               "MONGODB_LOG_DIAG_CLUSTER_NAME": "", "MONGODB_LOG_DIAG_INPUT_MODE": "atlas_api"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = normalize_config({"atlas_public_key": "p", "atlas_private_key": "s"}, "local")
        self.assertEqual(cfg["cluster_name"], "")
        self.assertTrue(handler._all_clusters_mode(cfg))


if __name__ == "__main__":
    unittest.main()
