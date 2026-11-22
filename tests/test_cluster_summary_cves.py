"""Cluster summary CVE handling: CVEs from the node reports are kept, anything else is redacted.

Regression: a cluster summary that repeated the server CVE match from the node reports
(CVE-2026-118383, MongoDB 8.0.34) failed with "Cluster summary contains a CVE without
customer-use verification".
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("agent.offline_cve", types.ModuleType("agent.offline_cve"))
_oc = sys.modules["agent.offline_cve"]
for _attr, _value in (("check", lambda payload: {"matches": []}), ("load_catalog", lambda: {}),
                      ("check_server", lambda p, **k: {"matches": [], "match_count": 0,
                                                        "status": "no_match_in_snapshot"})):
    if not hasattr(_oc, _attr):
        setattr(_oc, _attr, _value)

from agent import report_stage  # noqa: E402

NODE_REPORTS = [
    {"node": "n0", "log_name": "mongodb",
     "report_md": "## 10. MongoDB server version and CVE review\n\n| [CVE-2026-118383](u) | CRITICAL |"},
    {"node": "n1", "log_name": "mongodb", "report_md": "# Report\n\nNo CVE here."},
]


class Llm:
    def __init__(self, text):
        self.text = text

    def generate(self, system, user):
        return self.text


class ClusterSummaryCves(unittest.TestCase):
    def finish(self, text):
        return report_stage._finish_cluster_summary(
            Llm(text), report_stage.CLUSTER_PROMPT, "Cluster `c`", None,
            allowed_cves=report_stage._report_cves(NODE_REPORTS))

    def test_cve_from_node_reports_is_kept(self):
        out = self.finish("## Executive summary\n\n- MongoDB 8.0.34 matches cve-2026-118383 (CRITICAL).")
        self.assertIn("cve-2026-118383", out)
        self.assertNotIn("removed", out)

    def test_cve_not_in_any_node_report_is_redacted_not_fatal(self):
        out = self.finish("## Executive summary\n\n- CVE-2026-118383 and CVE-2099-12345.")
        self.assertIn("CVE-2026-118383", out)
        self.assertNotIn("CVE-2099-12345", out)
        self.assertIn("[unverified CVE reference removed]", out)

    def test_no_allowed_list_redacts_everything(self):
        self.assertNotIn("CVE-2026-118383",
                         report_stage._validate_customer_summary("CVE-2026-118383"))

    def test_prompt_allows_node_report_cves(self):
        self.assertIn("cite only CVE IDs that appear in the node reports", report_stage.CLUSTER_PROMPT)


if __name__ == "__main__":
    unittest.main()
