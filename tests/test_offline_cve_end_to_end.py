"""update_offline_driver_cves.py -> catalog -> extractor -> agent/offline_cve.py, with real OSV records."""
import importlib.util
import json
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
OSV = ROOT / "tests/fixtures/osv"


def _load(name, path, replace=None):
    source = path.read_text(encoding="utf-8")
    for old, new in (replace or {}).items():
        source = source.replace(old, new)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


# By path: other tests replace agent / agent.skills in sys.modules with stubs.
updater = _load("cve_e2e_updater", ROOT / "scripts/update_offline_driver_cves.py")
matcher = _load("cve_e2e_matcher", ROOT / "agent/offline_cve.py",
                {"from .skills import diagnostic_skill_dir": "diagnostic_skill_dir = None"})
sys.path.insert(0, str(ROOT / "skills/mongodb-log-diagnostic/scripts"))
spec = importlib.util.spec_from_file_location("cve_e2e_driver_compat", ROOT / "skills/mongodb-log-diagnostic/scripts/driver_compatibility.py")
driver_compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver_compat)
sys.path.pop(0)

RECORDS = {p.stem: json.loads(p.read_text()) for p in OSV.glob("*.json")}


def osv_fetch(url, body=None):
    if url.endswith("/query"):
        pkg = body["package"]
        return {"vulns": [{"id": k} for k, r in RECORDS.items()
                          if any(a["package"]["ecosystem"] == pkg["ecosystem"] and a["package"]["name"] == pkg["name"]
                                 for a in r.get("affected", []))]}
    return RECORDS[url.rsplit("/", 1)[1]]


def handshake(name, version, app):
    return {"raw_line": json.dumps({"attr": {"remote": "10.0.0.1:5000", "doc": {
        "application": {"name": app}, "driver": {"name": name, "version": version}}}}), "timestamp": "t"}


def extract(server, *drivers):
    build = {"raw_line": json.dumps({"attr": {"buildInfo": {"version": server}}})}
    return {"driverCompatibility": driver_compat.extract_driver_compatibility([build, *drivers])}


class UpdaterWithRealOsvRecords(unittest.TestCase):
    def setUp(self):
        self.catalog, self.stats = updater.refresh({"$schema": updater.SCHEMA, "entries": []}, osv_fetch, "2026-11-01")
        self.by = {(e["driver"], e["cve"]): e for e in self.catalog["entries"]}

    def test_introduced_zero_is_kept(self):
        # Previously dropped: VERSION needed two parts, so "0" (all earlier releases) was rejected.
        self.assertEqual(self.by[("pymongo", "CVE-2024-5629")]["affected"], [{"introduced": "0", "fixed": "4.6.3"}])
        self.assertEqual(self.by[("mongo-go-driver", "CVE-2021-20329")]["affected"], [{"introduced": "0", "fixed": "1.5.1"}])

    def test_all_java_artifacts_and_severity(self):
        entry = self.by[("mongo-java-driver", "CVE-2021-20328")]
        self.assertEqual({p["name"] for p in entry["packages"]}, {
            "org.mongodb:mongo-java-driver", "org.mongodb:mongodb-driver", "org.mongodb:mongodb-driver-legacy",
            "org.mongodb:mongodb-driver-sync"})
        self.assertEqual(len(entry["affected"]), 5)
        self.assertEqual(entry["severity"], "MODERATE")
        self.assertTrue(entry["cvss"].startswith("CVSS:3.1/"))

    def test_one_advisory_for_several_drivers_and_swift(self):
        drivers = {d for d, c in self.by if c == "CVE-2021-32050"}
        self.assertEqual(drivers, {"mongo-nodejs-driver", "mongo-php-library", "mongo-swift-driver"})

    def test_unrelated_dependency_not_imported(self):
        self.assertNotIn("CVE-2024-3651", {c for _, c in self.by})  # idna, not a driver

    def test_last_affected_and_versions_list(self):
        rec = {"id": "GHSA-x", "aliases": ["CVE-2030-0001"], "affected": [
            {"package": {"ecosystem": "PyPI", "name": "pymongo"},
             "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "3.0"}, {"last_affected": "3.2.1"}]}]},
            {"package": {"ecosystem": "npm", "name": "mongodb"},
             "ranges": [{"type": "GIT", "repo": "r", "events": [{"introduced": "0"}, {"fixed": "abc"}]}],
             "versions": ["6.1.0", "6.1.1"]}]}
        ranges, _, _ = updater._intervals(rec, "PyPI", "pymongo")
        self.assertEqual(ranges, [{"introduced": "3.0", "last_affected": "3.2.1"}])
        ranges, versions, notes = updater._intervals(rec, "npm", "mongodb")
        self.assertEqual((ranges, versions), ([], ["6.1.0", "6.1.1"]))
        self.assertIn("GIT range ignored", notes)

    def test_prerelease_bounds_are_approximated_not_dropped(self):
        rec = {"id": "GHSA-y", "affected": [{"package": {"ecosystem": "npm", "name": "mongodb"},
               "ranges": [{"type": "SEMVER", "events": [{"introduced": "6.0.0-beta.0"}, {"fixed": "6.2.0"}]}]}]}
        ranges, _, _ = updater._intervals(rec, "npm", "mongodb")
        self.assertEqual(ranges, [{"introduced": "6.0.0", "fixed": "6.2.0", "approximate": True}])

    def test_advisory_without_cve_is_kept_by_ghsa_id(self):
        RECORDS["GHSA-nocv-e000-0000"] = {"id": "GHSA-nocv-e000-0000", "summary": "no cve", "affected": [
            {"package": {"ecosystem": "RubyGems", "name": "mongo"},
             "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "2.0.0"}, {"fixed": "2.1.0"}]}]}]}
        try:
            catalog, _ = updater.refresh({"$schema": updater.SCHEMA, "entries": []}, osv_fetch, "2026-11-01")
        finally:
            del RECORDS["GHSA-nocv-e000-0000"]
        entry = next(e for e in catalog["entries"] if e["driver"] == "mongo-ruby-driver")
        self.assertIsNone(entry["cve"])
        self.assertEqual(entry["advisory_ids"], ["GHSA-nocv-e000-0000"])

    def test_every_catalog_key_is_reachable_by_the_matcher(self):
        reachable = set(matcher.OBSERVED_ALIASES.values()) | set(matcher.STACK_COMPONENTS.values())
        self.assertEqual({d for d, _, _ in updater.PACKAGES} - reachable, set())


class MatcherWithExtractorOutput(unittest.TestCase):
    def setUp(self):
        self.catalog, _ = updater.refresh({"$schema": updater.SCHEMA, "entries": []}, osv_fetch, "2026-11-01")

    def matches(self, ext, include_unreviewed=True):
        return matcher.check(ext, self.catalog, include_unreviewed=include_unreviewed)

    def test_drivers_found_even_when_server_not_in_compat_matrix(self):
        # 8.2 is not in DRIVER_MATRIX: the compatibility buckets are empty, observed_drivers is not.
        ext = extract("8.2.1", handshake("nodejs", "4.10.0", "orders"), handshake("PyMongo", "4.6.2", "etl"),
                      handshake("mongo-go-driver", "1.4.0", "svc"))
        self.assertEqual(ext["driverCompatibility"]["distinct_compatible_drivers"], {})
        got = {(m["catalog_driver"], m["cve"]) for m in self.matches(ext)["matches"]}
        self.assertEqual(got, {("mongo-nodejs-driver", "CVE-2021-32050"), ("pymongo", "CVE-2024-5629"),
                               ("mongo-go-driver", "CVE-2021-20329")})

    def test_fixed_versions_do_not_match(self):
        ext = extract("8.0.4", handshake("nodejs", "6.9.0", "a"), handshake("PyMongo", "4.6.3", "b"),
                      handshake("mongo-go-driver", "1.5.1", "c"), handshake("mongo-rust-driver", "2.8.2", "d"))
        self.assertEqual(self.matches(ext)["matches"], [])

    def test_php_stack_uses_phplib_version(self):
        ext = extract("8.0.4", handshake("mongoc / ext-mongodb:PHP / PHPLIB", "1.25.2 / 1.17.2 / 1.9.1", "web"))
        m = self.matches(ext)["matches"]
        self.assertEqual([(x["catalog_driver"], x["matched_version"], x["cve"]) for x in m],
                         [("mongo-php-library", "1.9.1", "CVE-2021-32050")])
        ext = extract("8.0.4", handshake("mongoc / ext-mongodb:PHP / PHPLIB", "1.25.2 / 1.9.1 / 1.17.0", "web"))
        self.assertEqual(self.matches(ext)["matches"], [])  # 1.9.1 is the extension, not PHPLIB

    def test_java_kotlin_variant_and_legacy_php_key(self):
        ext = extract("8.0.4", handshake("mongo-java-driver|sync", "4.1.1", "billing"))
        self.assertEqual([m["cve"] for m in self.matches(ext)["matches"]], ["CVE-2021-20328"])
        drv = {"driver": "mongo-java-driver", "version": "5.1.0", "raw_names": ["mongo-java-driver|kotlin"]}
        self.assertIn(("mongo-kotlin-driver", "5.1.0", "kotlin"), matcher.catalog_targets(drv))
        legacy = {"$schema": "x", "entries": [{"driver": "mongo-php-driver", "cve": "CVE-2021-32050", "reviewed": True,
                                               "source": "s", "affected": [{"introduced": "1.0.0", "fixed": "1.9.2"}]}]}
        ext = extract("8.0.4", handshake("mongoc / ext-mongodb:PHP / PHPLIB", "1.25.2 / 1.17.2 / 1.9.1", "web"))
        self.assertEqual(len(matcher.check(ext, legacy)["matches"]), 1)

    def test_unreviewed_hidden_by_default_and_labelled_when_enabled(self):
        ext = extract("8.2.1", handshake("PyMongo", "4.6.2", "etl"))
        hidden = self.matches(ext, include_unreviewed=False)
        self.assertEqual((hidden["matches"], hidden["unreviewed_candidates_not_reported"]), ([], 1))
        shown = self.matches(ext)["matches"][0]
        self.assertEqual((shown["review_status"], shown["severity"]), ("unreviewed_candidate", "MODERATE"))

    def test_range_semantics(self):
        a = matcher._affected
        self.assertTrue(a("4.6.2", [{"introduced": "0", "fixed": "4.6.3"}]))
        self.assertFalse(a("4.6.3", [{"introduced": "0", "fixed": "4.6.3"}]))
        self.assertTrue(a("3.2.1", [{"introduced": "3.0", "last_affected": "3.2.1"}]))
        self.assertFalse(a("3.2.2", [{"introduced": "3.0", "last_affected": "3.2.1"}]))
        self.assertTrue(a("9.0.0", [{"introduced": "2.0.0", "unfixed": True}]))
        self.assertFalse(a("9.0.0", [{"introduced": "2.0.0"}]))  # malformed: fail closed
        self.assertTrue(a("6.1.1", [], ["6.1.0", "6.1.1"]))
        self.assertTrue(a("4.6.2-cloud", [{"introduced": "0", "fixed": "4.6.3"}]))

    def test_shipped_catalog_has_recovered_entries(self):
        shipped = json.loads((ROOT / "skills/mongodb-log-diagnostic/references/offline-driver-cves.json").read_text())
        keys = {(e["driver"], e["cve"]) for e in shipped["entries"]}
        self.assertIn(("pymongo", "CVE-2024-5629"), keys)
        self.assertIn(("mongo-go-driver", "CVE-2021-20329"), keys)
        self.assertNotIn("mongo-php-driver", {d for d, _ in keys})


if __name__ == "__main__":
    unittest.main()
