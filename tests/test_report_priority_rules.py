"""End to end: the model writes P0 for 1,316 authentication failures; the report stage rewrites it to P2
(auth_failures band 1,000-4,999) in the node reports and in the cluster summary that feeds Jira/Slack."""
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
_oc = sys.modules["agent.offline_cve"]
_oc.check = getattr(_oc, "check", lambda payload: {"matches": []})
_oc.load_catalog = getattr(_oc, "load_catalog", lambda: {})
_oc.check_server = getattr(_oc, "check_server",
                           lambda p, **k: {"matches": [], "match_count": 0, "status": "no_match_in_snapshot"})

from agent import report_stage  # noqa: E402
from agent.common import Layout  # noqa: E402
from tests.test_report_every_node import DAY, NODES, Store  # noqa: E402

AUTH_GROUP = {"fingerprint": "authfail", "count": 1316, "sample_message": "Authentication failed",
              "message_samples": ["Authentication failed"],
              "operation_details": {"error_codes": ["18"], "error_code_names": ["AuthenticationFailed"]}}
TABLE = ("## Prioritized findings\n\n"
         "| Priority | Key | Finding | Nodes | Evidence | Impact | Recommended action | Confidence |\n"
         "|---|---|---|---|---|---|---|---|\n"
         "| P0 | auth-failures-authenticationfailed | Authentication failures | all | 1,316 AuthenticationFailed "
         "| Logins fail | Check users | High |\n")


class Llm:
    def __init__(self):
        self.calls = []

    def generate(self, system, user):
        self.calls.append((system, user))
        if "Cluster `" in user:
            return "## Executive summary\n\n- P0: Authentication failures on every node.\n\n" + TABLE
        return ("# Report\n\n## 3. Prioritized findings\n\n"
                "| Priority | Finding | Evidence | Impact | Recommended resolution | Confidence |\n"
                "|---|---|---|---|---|---|\n"
                "| P0 | Authentication failures | 1,316 AuthenticationFailed (code 18) | Logins fail | Check users | High |\n")


def store_with_auth_failures():
    layout = Layout("atlas-logs")
    extract = {"metadata": {"time_range": {"first": f"{DAY}T00:00:00Z", "last": f"{DAY}T23:59:00Z"}},
               "slow": {"operations": []}, "error_scan": {"groups": [AUTH_GROUP]}}
    return layout, Store({layout.extract(DAY, h, "mongodb", "extractionOccurence.json"): extract for h in NODES})


class ReportPriorityRules(unittest.TestCase):
    cfg = {"prefix": "atlas-logs", "cluster_name": "c", "expected_node_count": 3}

    def test_p0_for_1316_auth_failures_becomes_p2_everywhere(self):
        layout, store = store_with_auth_failures()
        llm = Llm()
        manifest = report_stage.run(self.cfg, DAY, store=store, llm=llm)
        node_system, node_user = llm.calls[0]
        self.assertIn("Report priority rules", node_system)
        self.assertIn("1,000-4,999: P2", node_system)
        self.assertIn('<document label="priority_decisions"', node_user)
        for host in NODES:
            md = store.get_text(layout.report(DAY, host, "mongodb", "report.md"))
            self.assertIn("| P2 | Authentication failures", md)
            self.assertNotIn("| P0 |", md)
            decisions = json.loads(store.get_text(layout.report(DAY, host, "mongodb", "priority-decisions.json")))
            self.assertEqual(decisions["auth_failures"][0]["priority"], "P2")
            self.assertEqual(len(decisions["enforcement"]["corrected"]), 1)
        summary = store.get_text(layout.cluster_report(DAY, "cluster-summary.md"))
        self.assertIn("| P2 | auth-failures-authenticationfailed", summary)
        self.assertIn("- P2: Authentication failures", summary)
        self.assertNotIn("P0", summary)
        self.assertEqual(manifest["priority_rules_version"], "2026-11-10.1")
        self.assertNotIn("priority_decisions", manifest["reports"][0])

    def test_summary_only_rerun_reloads_node_decisions(self):
        layout, store = store_with_auth_failures()
        report_stage.run(self.cfg, DAY, store=store, llm=Llm())
        store.items.pop(layout.cluster_report(DAY, "cluster-summary.md"))
        report_stage.run(self.cfg, DAY, store=store, llm=Llm(), summary_only=True)
        summary = store.get_text(layout.cluster_report(DAY, "cluster-summary.md"))
        self.assertIn("| P2 | auth-failures-authenticationfailed", summary)

    def test_node_reports_summary_variant_is_corrected(self):
        layout, store = store_with_auth_failures()
        report_stage.run({**self.cfg, "cluster_summary_source": "node_reports"}, DAY, store=store, llm=Llm())
        summary = store.get_text(layout.cluster_report(DAY, report_stage.NODE_REPORTS_SUMMARY_FILE))
        self.assertIn("| P2 | auth-failures-authenticationfailed", summary)
        self.assertNotIn("| P0 |", summary)

    def test_jira_export_gets_no_p0(self):
        from agent import notifications
        layout, store = store_with_auth_failures()
        report_stage.run(self.cfg, DAY, store=store, llm=Llm())
        found = notifications.parse_findings(store.get_text(layout.cluster_report(DAY, "cluster-summary.md")),
                                             "cluster_summary")
        self.assertEqual([f["priority"] for f in found], ["P2"])


if __name__ == "__main__":
    unittest.main()
