"""MongoDB server CVE: NVD updater -> catalog -> agent/offline_cve.check_server; report section placement."""
import json
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/nvd/mongodb_server_sample.json"


def _load(name, path, replace=None):
    source = path.read_text(encoding="utf-8")
    for old, new in (replace or {}).items():
        source = source.replace(old, new)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


updater = _load("server_cve_updater", ROOT / "scripts/update_offline_mongodb_server_cves.py")
matcher = _load("server_cve_matcher", ROOT / "agent/offline_cve.py",
                {"from .skills import diagnostic_skill_dir": "diagnostic_skill_dir = None"})
section = _load("server_cve_section", ROOT / "agent/report_cve_section.py")


def records():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["vulnerabilities"]


class UpdaterTests(unittest.TestCase):
    def test_ranges_platform_and_unrepresentable(self):
        cat, stats = updater.refresh(updater.empty_catalog(), records(), today="2026-11-22")
        by = {e["cve"]: e for e in cat["entries"]}
        self.assertIn({"introduced": "8.0.0", "fixed": "8.0.37"}, by["CVE-2026-118383"]["affected"])
        self.assertNotIn("reviewed", by["CVE-2026-118383"])
        self.assertIn("linux", by["CVE-2024-8207"]["platform_note"])
        self.assertEqual([u["cve"] for u in stats["unrepresentable"]], ["CVE-2014-8180"])
        self.assertEqual(cat["$schema"], updater.SCHEMA)

    def test_refresh_rebuilds_from_source_and_reports_diff(self):
        cat, _ = updater.refresh(updater.empty_catalog(), records(), today="2026-11-22")
        cat["entries"][0]["summary"] = "hand-edited"
        again, stats = updater.refresh(cat, records(), today="2026-11-23")
        self.assertEqual((stats["added"], stats["changed_ranges"], stats["removed"]), ([], [], []))
        self.assertNotIn("hand-edited", [e["summary"] for e in again["entries"]])
        changed = records()
        for r in changed:
            for cfg in r["cve"].get("configurations", []):
                for node in cfg["nodes"]:
                    for m in node["cpeMatch"]:
                        if m.get("versionEndExcluding") == "8.0.37":
                            m["versionEndExcluding"] = "8.0.38"
        again, stats = updater.refresh(cat, changed, today="2026-11-23")
        self.assertEqual(stats["changed_ranges"], ["CVE-2026-118383"])
        by = {e["cve"]: e for e in again["entries"]}
        self.assertIn({"introduced": "8.0.0", "fixed": "8.0.38"}, by["CVE-2026-118383"]["affected"])

    def test_cna_fallback_when_nvd_not_analysed(self):
        rec = {"cve": {"id": "CVE-2026-99999", "vulnStatus": "Received", "descriptions": [{"lang": "en", "value": "x"}],
                       "affected": [{"affectedData": [{"product": "MongoDB Server", "versions": [
                           {"version": "8.0.0", "lessThan": "8.0.20", "status": "affected"}]}]}]}}
        cat, stats = updater.refresh(updater.empty_catalog(), [rec], today="2026-11-22")
        self.assertEqual(cat["entries"][0]["range_source"], "cna")
        self.assertEqual(cat["entries"][0]["affected"], [{"introduced": "8.0.0", "fixed": "8.0.20"}])

    def test_pagination_refuses_partial(self):
        pages = [{"totalResults": 3, "resultsPerPage": 2, "vulnerabilities": records()[:2]},
                 {"totalResults": 3, "resultsPerPage": 2, "vulnerabilities": []}]
        with self.assertRaisesRegex(ValueError, "partial"):
            updater.fetch_all(fetch=lambda url: pages.pop(0), sleep=lambda s: None)


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.catalog, _ = updater.refresh(updater.empty_catalog(), records(), today="2026-11-22")

    def check(self, version, **kw):
        return matcher.check_server({"mongodbServer": {"version": version, "source": "log buildInfo"}}, self.catalog, **kw)

    def test_match_and_fixed_in(self):
        r = self.check("8.0.4")
        m = next(x for x in r["matches"] if x["cve"] == "CVE-2026-118383")
        self.assertEqual(m["fixed_in"], "8.0.37")
        self.assertNotIn("review_status", m)
        self.assertEqual(self.check("8.0.37")["status"], "no_match_in_snapshot")

    def test_unknown_and_major_minor(self):
        self.assertEqual(matcher.check_server({}, self.catalog)["status"], "server_version_unknown")
        self.assertEqual(self.check("8.0")["status"], "server_version_incomplete")
        r = matcher.check_server({}, self.catalog, fallback_version="7.0.12")
        self.assertEqual(r["observed_server"]["source"], "configuration (mongodb_server_version)")


class ShippedCatalogTests(unittest.TestCase):
    def test_no_review_field_in_catalogs(self):
        refs = ROOT / "skills/mongodb-log-diagnostic/references"
        for name in ("offline-mongodb-server-cves.json", "offline-driver-cves.json"):
            entries = json.loads((refs / name).read_text(encoding="utf-8"))["entries"]
            self.assertTrue(entries)
            self.assertFalse([e.get("cve") for e in entries if "reviewed" in e], name)


class StatusTests(unittest.TestCase):
    """Regression: 8.0.34 is inside CVE-2026-118383 (fixed 8.0.37); the report said 'No match'."""
    def setUp(self):
        self.catalog, _ = updater.refresh(updater.empty_catalog(), records(), today="2026-11-22")
        self.ext = {"mongodbServer": {"version": "8.0.34", "source": "log buildInfo"}}

    def test_server_matches_shown_by_default(self):
        r = matcher.check_server(self.ext, self.catalog)
        self.assertEqual((r["status"], r["highest_fixed_in"]), ("matches_found", "8.0.37"))
        self.assertIn("CVE-2026-118383", [m["cve"] for m in r["matches"]])

    def test_guard_rewrites_false_no_match(self):
        r = matcher.check_server(self.ext, self.catalog)
        md = ("# R\n\n## 10. MongoDB server version and CVE review\n\nThe observed MongoDB server version is 8.0.34.\n"
              "No match was found in the offline MongoDB server CVE snapshot refreshed 2026-11-22.\n\n"
              "## 11. Driver compatibility and CVE review\n\nNo match in the offline CVE snapshot refreshed 2026-11-22.\n")
        drivers = {"status": "not_covered_by_snapshot", "matches": [], "match_count": 0,
                   "catalog": {"refreshed_at": "2026-11-22"},
                   "drivers_not_covered_by_snapshot": ["mongoc 1.24.3"]}
        out, fixes = section.enforce_cve_status(md, r, drivers)
        self.assertEqual(len(fixes), 2)
        self.assertNotIn("No match", out)
        self.assertIn("CVE-2026-118383", out)
        self.assertIn("8.0.37", out)
        self.assertIn("mongoc 1.24.3", out)
        self.assertLess(out.index("CVE-2026-118383"), out.index("## 11."))

    def test_guard_keeps_true_no_match(self):
        r = matcher.check_server({"mongodbServer": {"version": "8.0.37"}}, self.catalog)
        self.assertEqual(r["status"], "no_match_in_snapshot")
        md = "## 10. MongoDB server version and CVE review\n\nNo match in the offline MongoDB server CVE snapshot refreshed 2026-11-22.\n"
        self.assertEqual(section.enforce_cve_status(md, r, None), (md, []))


class SectionTests(unittest.TestCase):
    def test_server_cve_section_is_not_relocated(self):
        md = ("# R\n\n## 10. MongoDB server version and CVE review\n\n### CVE status\n\nCVE-2026-118383 8.0.4\n\n"
              "## 11. Driver compatibility and CVE review\n\nNo match in the offline CVE snapshot.\n")
        out, moved = section.relocate_standalone_cve_status(md)
        self.assertEqual((out, moved), (md, 0))


if __name__ == "__main__":
    unittest.main()
