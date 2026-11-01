"""Report priority rules: change-stream COLLSCAN, low-volume slow ops, auth-failure bands."""
import unittest
from pathlib import Path

from agent import priority_rules as pr

RULES_FILE = Path(__file__).resolve().parents[1] / "skills/mongodb-log-diagnostic/references/priority-rules.json"


def rules():
    return pr.load_rules({"priority_rules_file": str(RULES_FILE)})


def slow(ns, count, avg, mx=None, docs=None, qh="ABCD1234", cs=False, plan="IXSCAN { a: 1 }"):
    shape = {"aggregate": "?", "pipeline": [{"$changeStream": {}}]} if cs else {"find": "?", "filter": {"a": "?"}}
    return {"namespace": ns, "plan_summary": "COLLSCAN" if cs else plan, "query_hash": qh, "count": count,
            "duration_ms": {"min": avg, "median": avg, "max": mx or avg, "avg": avg},
            "docs_examined": {"max": docs} if docs is not None else None, "sample_query_shape": shape}


def auth_group(count, msg):
    return {"fingerprint": msg[:6], "count": count, "sample_message": msg, "message_samples": [msg]}


def extraction(ops=(), groups=()):
    return {"metadata": {"time_range": {"first": "2026-10-24T00:00:00Z", "last": "2026-10-24T23:59:00Z"}},
            "slow": {"operations": list(ops), "global_stats": {"change_stream_count": 0}},
            "error_scan": {"groups": list(groups)}}


class RuleEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.rules = rules()

    def test_short_change_stream_is_analysis_only(self):
        d = pr.evaluate(extraction([slow("app.orders", 5000, 1500, mx=4000, docs=20000, cs=True)]), self.rules)
        self.assertEqual(d["slow_operations"][0]["placement"], pr.ANALYSIS_ONLY)
        self.assertIsNone(d["slow_operations"][0]["priority"])

    def test_long_change_stream_is_p3(self):
        for kwargs in ({"mx": 90000}, {"mx": 2000, "docs": 5_000_000}):
            d = pr.evaluate(extraction([slow("app.orders", 3, 1500, cs=True, **kwargs)]), self.rules)
            self.assertEqual((d["slow_operations"][0]["placement"], d["slow_operations"][0]["priority"]),
                             (pr.ISSUES, "P3"))

    def test_cluster_wide_change_stream_on_admin_is_kept(self):
        d = pr.evaluate(extraction([slow("admin.$cmd.aggregate", 3, 1500, mx=90000, cs=True)]), self.rules)
        self.assertEqual(d["slow_operations"][0]["rule"], "change_stream_collscan")
        d = pr.evaluate(extraction([slow("admin.system.users", 3, 1500)]), self.rules)
        self.assertEqual(d["slow_operations"], [])

    def test_low_volume_slow_is_p3_and_kept(self):
        d = pr.evaluate(extraction([slow("app.users", 999, 3000)]), self.rules)  # ~0.83 h
        s = d["slow_operations"][0]
        self.assertEqual((s["placement"], s["priority"]), (pr.ISSUES, "P3"))
        self.assertEqual(s["estimated_total_duration_ms"], 2_997_000)

    def test_slow_thresholds_are_strict(self):
        for count, avg in ((1000, 100), (500, 7200)):  # count == 1k ; total == exactly 1 h
            d = pr.evaluate(extraction([slow("app.users", count, avg)]), self.rules)
            self.assertEqual(d["slow_operations"][0]["placement"], pr.NO_CAP)

    def test_new_low_volume_group_is_watch_item(self):
        op = slow("app.users", 10, 2000)
        diffs = [{"label": "n-1", "slow_operation_groups": [{"key": pr._slow_key(op), "label": "new"}]}]
        d = pr.evaluate(extraction([op]), self.rules, diffs)
        self.assertTrue(d["slow_operations"][0]["watch"])
        self.assertEqual(d["slow_operations"][0]["trend"], {"n-1": "new"})

    def test_auth_bands(self):
        cases = {99: (pr.APPENDIX_ONLY, None), 100: (pr.ISSUES, "P3"), 999: (pr.ISSUES, "P3"),
                 1000: (pr.ISSUES, "P2"), 4999: (pr.ISSUES, "P2"), 5000: (pr.ISSUES, "P1")}
        for count, expected in cases.items():
            groups = [auth_group(count - count // 2, "Authentication failed"),
                      auth_group(count // 2, "Failed to authenticate")]  # summed across groups
            d = pr.evaluate(extraction(groups=groups), self.rules)
            a = d["auth_failures"][0]
            self.assertEqual((a["type"], a["count"]), ("authentication", count))
            self.assertEqual((a["placement"], a["priority"]), expected, count)

    def test_authorization_counted_separately(self):
        groups = [auth_group(50, "Authentication failed"), auth_group(1200, "Checking authorization failed")]
        d = {a["type"]: a for a in pr.evaluate(extraction(groups=groups), self.rules)["auth_failures"]}
        self.assertEqual(d["authentication"]["placement"], pr.APPENDIX_ONLY)
        self.assertEqual(d["authorization"]["priority"], "P2")

    def test_auth_by_code_name(self):
        g = {"fingerprint": "x", "count": 150, "sample_message": "Command failed",
             "operation_details": {"error_code_names": ["Unauthorized"]}}
        self.assertEqual(pr.evaluate(extraction(groups=[g]), self.rules)["auth_failures"][0]["type"], "authorization")


class EnforcementTest(unittest.TestCase):
    def setUp(self):
        self.rules = rules()

    REPORT = "\n".join([
        "## 3. Prioritized findings", "",
        "| Priority | Finding | Evidence | Impact | Recommended resolution | Confidence |",
        "|---|---|---|---|---|---|",
        "| P1 | Authentication failures | 42 events | Login errors | Check users | High |",
        "| P2 | Change stream COLLSCAN on app.orders | 5000 getMore | CPU | Add index | Low |",
        "| P3 | Slow find app.users ABCD1234 | 999 ops [rule: slow_low_volume] | Low | Watch | Medium |",
        "", "## 7. Recommended execution order", "",
        "1. Review authentication failures for the reporting user.",
        "   Validate next day's count.",
        "2. Investigate change stream scans on app.orders.",
        "3. Watch app.users ABCD1234 after the next release.", "",
        "## Evidence appendix", "", "- Authentication failed: 42 events.", ""])

    def decisions(self):
        ops = [slow("app.orders", 5000, 1500, mx=4000, cs=True), slow("app.users", 999, 3000)]
        return pr.evaluate(extraction(ops, [auth_group(42, "Authentication failed")]), self.rules)

    def test_enforce_drops_suppressed_rows_and_actions(self):
        out, removed = pr.enforce(self.REPORT, self.decisions(), self.rules)
        self.assertEqual(removed, 4)
        self.assertNotIn("| P1 | Authentication", out)
        self.assertNotIn("Change stream COLLSCAN", out)
        self.assertNotIn("Validate next day's count", out)
        self.assertIn("| P3 | Slow find app.users", out)
        self.assertIn("3. Watch app.users", out)
        self.assertIn("- Authentication failed: 42 events.", out)  # appendix kept

    def test_long_change_stream_row_is_kept(self):
        d = pr.evaluate(extraction([slow("app.orders", 3, 1500, mx=90000, cs=True)]), self.rules)
        out, _ = pr.enforce(self.REPORT, d, self.rules)
        self.assertIn("Change stream COLLSCAN on app.orders", out)

    def test_no_detected_change_stream_does_not_drop(self):
        out, _ = pr.enforce(self.REPORT, pr.evaluate(extraction(), self.rules), self.rules)
        self.assertIn("Change stream COLLSCAN on app.orders", out)

    def test_audit_flags_wrong_priority(self):
        report = self.REPORT.replace("| P3 | Slow find", "| P1 | Slow find")
        warnings = pr.audit(report, self.decisions(), self.rules)
        self.assertTrue(any("expected P3" in w for w in warnings), warnings)

    def test_prompt_renders_thresholds(self):
        text = pr.render_prompt(self.rules)
        for snippet in ("60,000 ms", "1,000,000", "3,600,000 ms (1 h)", "0-99: not a finding (appendix_only)",
                        "100-999: P3", "1,000-4,999: P2", "5,000 or more: P1"):
            self.assertIn(snippet, text)
        self.assertNotIn("{", text.replace("{rule id}", ""))

    def test_merge_keeps_type_if_any_node_reports_it(self):
        a = pr.evaluate(extraction(groups=[auth_group(50, "Authentication failed")]), self.rules)
        b = pr.evaluate(extraction(groups=[auth_group(300, "Authentication failed")]), self.rules)
        merged = pr.merge_decisions([a, b])
        self.assertEqual(merged["auth_failures"][0]["placement"], pr.ISSUES)
        self.assertEqual(merged["auth_failures"][0]["count"], 350)

    def test_invalid_rules_rejected(self):
        bad = rules()
        bad["rules"]["auth_failures"]["bands"] = [{"lt": 1000, "placement": "issues_and_actions", "priority": "P3"},
                                                  {"lt": 100, "placement": "appendix_only"}]
        with self.assertRaises(ValueError):
            pr.validate_rules(bad)


if __name__ == "__main__":
    unittest.main()
