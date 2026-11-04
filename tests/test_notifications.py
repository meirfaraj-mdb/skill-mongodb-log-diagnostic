"""P0 -> Jira / P1 -> Slack notifications: 3 modes each, per-environment config, de-duplication."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import notifications as n  # noqa: E402
from agent.common import Layout  # noqa: E402

DAY = "2026-11-02"
LAYOUT = Layout("atlas-logs")
SUMMARY = """# Cluster summary

## Executive summary

- Two risks.

## Prioritized findings

| Priority | Key | Finding | Nodes | Evidence | Impact | Recommended action | Confidence |
|---|---|---|---|---|---|---|---|
| P0 | collscan-shop.orders-abcdef12 | COLLSCAN on `shop.orders` query hash ABCDEF12 | node-0, node-1 | 1,204 slow ops, ratio 52,000 | p99 4.2 s | Test index candidate with explain | High |
| **P1** | auth-failures | Repeated authentication failures | node-2 | 310 AuthenticationFailed | login errors | Rotate credentials of the failing client | Medium |
| P2 | tls-warn | TLS warnings | node-0 | 12 | none | monitor | Low |
"""


class Store:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def exists(self, key): return key in self.items
    def get_text(self, key): return self.items[key]
    def uri(self, key): return "mock://" + key
    def put_text(self, key, value, content_type):
        self.items[key] = value
        return self.uri(key)


def base_store(summary=SUMMARY):
    return Store({LAYOUT.cluster_report(DAY, "cluster-summary.md"): summary,
                  LAYOUT.cluster_report(DAY, "manifest.json"): json.dumps(
                      {"cluster_summary_source": "extract", "reports": []})})


def cfg(**kw):
    return {"prefix": "atlas-logs", "cluster_name": "Cluster0", "environment": "production",
            "jira_project_key": "OPS", **kw}


class FakeJira:
    def __init__(self, existing=None):
        self.existing, self.created, self.calls = dict(existing or {}), [], []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if "search" in url:
            label = next((v for v in self.existing if v in url), None)
            return 200, {"issues": [{"key": self.existing[label]}] if label else []}
        if url.endswith("/rest/api/2/issue"):
            self.created.append(body["fields"])
            key = f"OPS-{len(self.created)}"
            fp = next(lb for lb in body["fields"]["labels"] if lb.startswith("mldiag-") and "env-" not in lb)
            self.existing[fp] = key
            return 201, {"key": key}
        if url.startswith("https://hooks.slack.com/"):
            self.slack = body
            return 200, "ok"
        return 404, {}


class ParsingTest(unittest.TestCase):
    def test_table_rows(self):
        found = n.parse_findings(SUMMARY, "cluster-summary.md")
        self.assertEqual([f["priority"] for f in found], ["P0", "P1", "P2"])
        self.assertEqual(found[0]["key"], "collscan-shop.orders-abcdef12")
        self.assertIn("Test index candidate", found[0]["action"])

    def test_headings_and_bullets_fallback(self):
        md = "## Executive summary\n\n### P0 — Primary unreachable\n\nElections on node-0.\n\n## Other\n\n- **P1** Slow `shop.items`: 30 ops\n"
        found = n.parse_findings(md, "x")
        self.assertEqual([(f["priority"], f["finding"]) for f in found],
                         [("P0", "Primary unreachable"), ("P1", "Slow `shop.items`")])
        self.assertIn("Elections", found[0]["evidence"])

    def test_fingerprint_ignores_counts_and_priority_but_not_environment(self):
        a = {"finding": "COLLSCAN on `shop.orders` hash ABCDEF12", "evidence": "1204 ops"}
        b = {"finding": "Collection scan COLLSCAN on `shop.orders` hash abcdef12 grew", "evidence": "999 ops"}
        self.assertEqual(n.fingerprint(a, "production", "C"), n.fingerprint(b, "production", "C"))
        self.assertNotEqual(n.fingerprint(a, "production", "C"), n.fingerprint(a, "staging", "C"))


class JiraModesTest(unittest.TestCase):
    def test_off_is_default_and_writes_nothing(self):
        store = base_store()
        out = n.run({"prefix": "atlas-logs", "cluster_name": "C"}, DAY, store=store)
        self.assertEqual(out["status"], "disabled")
        self.assertFalse(any(k.endswith("notifications.json") for k in store.items))

    def test_json_mode_exports_once_then_ledger_dedupes(self):
        store = base_store()
        out = n.run(cfg(jira_mode="json", jira_assignee="5b10ac8d82e05b22cc7d4ef5", jira_labels="mongodb,prod"),
                    DAY, store=store)
        body = json.loads(store.items[LAYOUT.cluster_report(DAY, n.JIRA_EXPORT_FILE)])
        self.assertEqual(len(body["issueUpdates"]), 1)  # P0 only
        fields = body["issueUpdates"][0]["fields"]
        self.assertEqual(fields["project"], {"key": "OPS"})
        self.assertEqual(fields["assignee"], {"accountId": "5b10ac8d82e05b22cc7d4ef5"})
        self.assertEqual(fields["priority"], {"name": "Highest"})
        self.assertTrue(fields["summary"].startswith("[PRODUCTION] Cluster0: COLLSCAN"))
        self.assertIn("mldiag-env-production", fields["labels"])
        self.assertEqual(out["slack_mode"], "off")
        store.items[LAYOUT.cluster_report("2026-11-03", "cluster-summary.md")] = SUMMARY.replace("1,204", "1,500")
        out2 = n.run(cfg(jira_mode="json"), "2026-11-03", store=store)
        self.assertEqual(out2["jira"]["exported"], [])
        self.assertEqual(len(out2["jira"]["existing"]), 1)

    def test_create_mode_dedupes_with_jql(self):
        store, http = base_store(), FakeJira()
        conf = cfg(jira_mode="create", jira_base_url="https://acme.atlassian.net")
        secrets = {"jira_email": "bot@acme.com", "jira_api_token": "t"}
        out = n.run(conf, DAY, store=store, http=http, secrets=secrets)
        self.assertEqual(len(out["jira"]["created"]), 1)
        self.assertEqual(out["jira"]["created"][0]["url"], "https://acme.atlassian.net/browse/OPS-1")
        out2 = n.run(conf, DAY, store=base_store(), http=http, secrets=secrets)  # fresh ledger: JQL still dedupes
        self.assertEqual(out2["jira"]["created"], [])
        self.assertEqual(out2["jira"]["existing"][0]["key"], "OPS-1")
        self.assertEqual(len(http.created), 1)
        self.assertTrue(any("statusCategory" in u for _, u in http.calls))

    def test_search_failure_never_creates(self):
        store = base_store()
        http = lambda *a: (500, {})  # noqa: E731
        out = n.run(cfg(jira_mode="create", jira_base_url="https://j.example.com", jira_deployment="datacenter"),
                    DAY, store=store, http=http, secrets={"jira_pat": "x"})
        self.assertEqual(out["jira"]["created"], [])
        self.assertEqual(out["status"], "partial")

    def test_create_requires_credentials_and_project(self):
        with self.assertRaises(ValueError):
            n.settings({"jira_mode": "json", "cluster_name": "C"})
        out = n.run(cfg(jira_mode="create", jira_base_url="https://j.example.com"), DAY, store=base_store(),
                    http=FakeJira(), secrets={})
        self.assertIn("jira_email", out["jira"]["errors"][0]["error"])


class SlackModesTest(unittest.TestCase):
    def test_json_mode_payload(self):
        store = base_store()
        n.run(cfg(slack_mode="json", slack_channel="#mongo-alerts", slack_mentions="<!subteam^S1>"), DAY, store=store)
        msg = json.loads(store.items[LAYOUT.cluster_report(DAY, n.SLACK_EXPORT_FILE)])["messages"][0]
        self.assertEqual(msg["channel"], "#mongo-alerts")
        text = json.dumps(msg)
        self.assertIn("Repeated authentication failures", text)
        self.assertNotIn("COLLSCAN", text)  # P0 goes to Jira, not Slack, by default
        self.assertIn("<!subteam^S1>", text)

    def test_send_mode_once_per_day_and_repeat_window(self):
        store, http = base_store(), FakeJira()
        conf = cfg(slack_mode="send", slack_repeat_after_days=3)
        secrets = {"slack_webhook_url": "https://hooks.slack.com/services/T/B/X"}
        self.assertTrue(n.run(conf, DAY, store=store, http=http, secrets=secrets)["slack"]["sent"])
        again = n.run(conf, DAY, store=store, http=http, secrets=secrets)["slack"]
        self.assertFalse(again["sent"])
        self.assertEqual(again["suppressed"][0]["reason"], "already_sent_for_day")
        store.items[LAYOUT.cluster_report("2026-11-03", "cluster-summary.md")] = SUMMARY
        later = n.run(conf, "2026-11-03", store=store, http=http, secrets=secrets)["slack"]
        self.assertEqual(later["suppressed"][0]["reason"], "sent_within_3_days")

    def test_environment_overrides(self):
        conf = cfg(environment="dev", slack_mode="send",
                   notify_env_overrides={"dev": {"slack_mode": "off", "jira_mode": "json"}})
        s = n.settings(conf)
        self.assertEqual((s["slack_mode"], s["jira_mode"]), ("off", "json"))
        with self.assertRaises(ValueError):
            n.settings(cfg(notify_env_overrides={"production": {"jira_api_token": "x"}}))


class NodeReportFallbackTest(unittest.TestCase):
    def test_node_reports_used_when_summary_has_no_priorities(self):
        store = base_store("# Summary\n\n## Executive summary\n\nNothing prioritized.\n")
        store.items[LAYOUT.cluster_report(DAY, "manifest.json")] = json.dumps(
            {"reports": [{"node": "node-0", "log_name": "mongodb"}, {"node": "node-1", "log_name": "mongodb"}]})
        row = ("| Priority | Finding | Evidence | Impact | Recommended resolution | Confidence |\n|---|---|---|---|---|---|\n"
               "| P0 | Duplicate key DuplicateKey on `shop.users` | 40 | writes fail | dedupe | High |\n")
        for node in ("node-0", "node-1"):
            store.items[LAYOUT.report(DAY, node, "mongodb", "report.md")] = row
        out = n.run(cfg(jira_mode="json"), DAY, store=store)
        self.assertEqual(len(out["findings"]), 1)  # merged across nodes
        self.assertEqual(out["findings"][0]["nodes"], "node-0, node-1")


if __name__ == "__main__":
    unittest.main()
