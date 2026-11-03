"""Per-environment P0 -> Jira and P1 -> Slack notifications built from the cluster reports.

``json``    write an importable file only: ``jira-issues.json`` (Jira REST bulk-create body,
            ``POST /rest/api/2/issue/bulk``) / ``slack-messages.json`` (Slack ``chat.postMessage``
            / incoming-webhook payloads)
``create``  (Jira) create the issues through the Jira REST API
``send``    (Slack) post the message through an incoming webhook or a bot token
==========  ===================================================================================

De-duplication: every finding gets a stable fingerprint (environment + cluster + issue identity). A Jira
issue carries the label ``mldiag-<fingerprint>``; in ``create`` mode a JQL search on that label prevents
a second ticket, and a ledger in the bucket (``<prefix>/notifications/<env>/<cluster>/ledger.json``)
prevents re-exporting the same issue in ``json`` mode and re-sending the same Slack item for a day.

Credentials (Jira token/PAT, Slack webhook/bot token) never come from ``MONGODB_LOG_DIAG_*`` env vars:
they are read from a dedicated secret (``NOTIFICATION_SECRET_ID``) or a local JSON file
(``NOTIFICATION_CONFIG_FILE``), and only when a ``create``/``send`` mode needs them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import date, datetime, timezone

from .common import Layout, logger

JIRA_MODES = ("off", "json", "create")
SLACK_MODES = ("off", "json", "send")
SOURCES = ("auto", "cluster_summary", "node_reports")
PRIORITIES = ("P0", "P1", "P2", "P3")
SECRET_FIELDS = frozenset({"jira_email", "jira_api_token", "jira_pat", "slack_webhook_url", "slack_bot_token"})
# Keys that NOTIFY_ENV_OVERRIDES may override per environment (never credentials).
OVERRIDABLE = frozenset({
    "jira_mode", "jira_priorities", "jira_project_key", "jira_issue_type", "jira_assignee",
    "jira_assignee_field", "jira_labels", "jira_components", "jira_parent_key", "jira_epic_key",
    "jira_epic_link_field", "jira_priority_map", "jira_extra_fields", "jira_dedupe_scope",
    "jira_comment_on_existing", "jira_max_issues", "jira_summary_template", "jira_base_url",
    "jira_deployment", "slack_mode", "slack_priorities", "slack_channel", "slack_mentions",
    "slack_repeat_after_days", "slack_max_findings", "notify_source",
})
SUMMARY_FILE = "cluster-summary.md"
NODE_REPORTS_SUMMARY_FILE = "cluster-summary.from-node-reports.md"
RESULT_FILE = "notifications.json"
JIRA_EXPORT_FILE = "jira-issues.json"
SLACK_EXPORT_FILE = "slack-messages.json"
LABEL_PREFIX = "mldiag-"

# Appended to the cluster-summary prompt only when a notification mode is enabled, so P0/P1 items
# are machine-readable. The parser also accepts headings and bullets when the table is missing.
TABLE_INSTRUCTIONS = (
    "NOTIFICATION TABLE: after the executive summary include the section `## Prioritized findings` with "
    "exactly one Markdown table with the columns | Priority | Key | Finding | Nodes | Evidence | Impact | "
    "Recommended action | Confidence |. One row per independent issue, Priority is exactly P0, P1, P2 or "
    "P3. Key is a short stable kebab-case identifier built only from the issue type and its identifiers "
    "(namespace, query hash, error code/name), for example `collscan-shop.orders-abcdef12` or "
    "`auth-failures-authenticationfailed`; reuse the same Key for the same issue on every day and never "
    "put counts, dates or percentages in it. Cells must not contain line breaks or pipes."
)

_P = re.compile(r"\bP([0-3])\b")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HEX = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{8})(?![0-9a-fA-F])")
_TICKED = re.compile(r"`([^`\n]{2,120})`")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_.$-]*[A-Za-z_$][A-Za-z0-9_.$-]*$")
_CODE = re.compile(r"\b(?:E\d{4,5}|[A-Z][a-z]+(?:[A-Z][a-z]+)+(?:Error|Failed|Key|Exceeded|Timeout|Mismatch))\b")
_CATEGORIES = (
    ("collscan", r"collscan|collection scan|unindexed"), ("in-memory-sort", r"in-memory sort|blocking sort|hassortstage|sort stage"),
    ("scan-ratio", r"examined[- ]to[- ]returned|scan ratio|docs examined|poorly indexed"),
    ("duplicate-key", r"duplicate[- ]key|e11000"), ("auth", r"authenticat|authoriz|unauthori[sz]ed|scram|x\.509"),
    ("index-build", r"index build|createindexes"), ("replication", r"replication|repl lag|oplog|election|rollback|primary step"),
    ("connection", r"connection|pool|socket|network|timeout"), ("tls", r"\btls\b|\bssl\b|certificate"),
    ("cache", r"wiredtiger|cache|eviction|dirty"), ("disk", r"disk|iops|i/o|storage"),
    ("cpu", r"\bcpu\b|steal"), ("memory", r"memory|swap|page fault"), ("latency", r"latency|slow"),
)
_COLUMN_ALIASES = {
    "priority": ("priority", "prio", "severity"), "key": ("key", "issue key", "id"),
    "finding": ("finding", "issue", "title", "problem", "risk"), "nodes": ("nodes", "node", "scope", "hosts"),
    "evidence": ("evidence",), "impact": ("impact", "customer impact"),
    "action": ("recommended action", "recommended resolution", "resolution", "action", "owner/action",
               "recommendation", "next step"),
    "confidence": ("confidence",),
}


# --------------------------------------------------------------------------- configuration
def _csv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            return _csv(json.loads(text))
        except ValueError:
            pass
    return [item.strip() for item in text.split(",") if item.strip()]


def _bool(value, default=False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def environment_name(config: dict) -> str:
    env = str(config.get("environment") or os.environ.get("MONGODB_LOG_DIAG_ENVIRONMENT") or "unspecified")
    env = env.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", env):
        raise ValueError("MONGODB_LOG_DIAG_ENVIRONMENT must be a short slug such as dev, staging or production")
    return env


def settings(config: dict) -> dict:
    """Resolve the notification settings for this run's environment (overrides applied, validated)."""
    env = environment_name(config)
    cfg = dict(config)
    overrides = config.get("notify_env_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("MONGODB_LOG_DIAG_NOTIFY_ENV_OVERRIDES must be a JSON object keyed by environment")
    for key, value in (overrides.get(env) or {}).items():
        if key not in OVERRIDABLE:
            raise ValueError(f"notify_env_overrides.{env}.{key} is not an overridable notification setting")
        cfg[key] = value
    s = {
        "environment": env,
        "cluster": str(config.get("cluster_name") or "MongoDB cluster"),
        "source": str(cfg.get("notify_source") or "auto").strip().lower().replace("-", "_"),
        "jira_mode": str(cfg.get("jira_mode") or "off").strip().lower(),
        "slack_mode": str(cfg.get("slack_mode") or "off").strip().lower(),
        "jira_priorities": _csv(cfg.get("jira_priorities")) or ["P0"],
        "slack_priorities": _csv(cfg.get("slack_priorities")) or ["P1"],
        "jira": {
            "base_url": str(cfg.get("jira_base_url") or "").rstrip("/"),
            "deployment": str(cfg.get("jira_deployment") or "cloud").strip().lower(),
            "project_key": str(cfg.get("jira_project_key") or "").strip(),
            "issue_type": str(cfg.get("jira_issue_type") or "Bug").strip(),
            "assignee": str(cfg.get("jira_assignee") or "").strip(),
            "assignee_field": str(cfg.get("jira_assignee_field") or "").strip(),
            "labels": _csv(cfg.get("jira_labels")),
            "components": _csv(cfg.get("jira_components")),
            "parent_key": str(cfg.get("jira_parent_key") or "").strip(),
            "epic_key": str(cfg.get("jira_epic_key") or "").strip(),
            "epic_link_field": str(cfg.get("jira_epic_link_field") or "").strip(),
            "priority_map": cfg.get("jira_priority_map") or {"P0": "Highest", "P1": "High", "P2": "Medium", "P3": "Low"},
            "extra_fields": cfg.get("jira_extra_fields") or {},
            "dedupe_scope": str(cfg.get("jira_dedupe_scope") or "open").strip().lower(),
            "comment_on_existing": _bool(cfg.get("jira_comment_on_existing")),
            "max_issues": _int(cfg.get("jira_max_issues"), 10),
            "summary_template": str(cfg.get("jira_summary_template") or "[{environment}] {cluster}: {finding}"),
        },
        "slack": {
            "channel": str(cfg.get("slack_channel") or "").strip(),
            "mentions": _csv(cfg.get("slack_mentions")),
            "repeat_after_days": _int(cfg.get("slack_repeat_after_days"), 0),
            "max_findings": _int(cfg.get("slack_max_findings"), 10),
        },
        "timeout": _int(config.get("http_timeout_seconds"), 30),
    }
    _validate(s)
    return s


def _validate(s: dict) -> None:
    if s["jira_mode"] not in JIRA_MODES:
        raise ValueError(f"MONGODB_LOG_DIAG_JIRA_MODE must be one of {'|'.join(JIRA_MODES)}")
    if s["slack_mode"] not in SLACK_MODES:
        raise ValueError(f"MONGODB_LOG_DIAG_SLACK_MODE must be one of {'|'.join(SLACK_MODES)}")
    if s["source"] not in SOURCES:
        raise ValueError(f"MONGODB_LOG_DIAG_NOTIFY_SOURCE must be one of {'|'.join(SOURCES)}")
    for key in ("jira_priorities", "slack_priorities"):
        bad = [p for p in s[key] if p not in PRIORITIES]
        if bad:
            raise ValueError(f"{key} accepts only P0..P3, got {bad}")
    j = s["jira"]
    if s["jira_mode"] != "off":
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,19}", j["project_key"]):
            raise ValueError("MONGODB_LOG_DIAG_JIRA_PROJECT_KEY is required (e.g. OPS) when JIRA_MODE is json/create")
        if j["deployment"] not in {"cloud", "datacenter"}:
            raise ValueError("MONGODB_LOG_DIAG_JIRA_DEPLOYMENT must be cloud or datacenter")
        if j["dedupe_scope"] not in {"open", "all"}:
            raise ValueError("MONGODB_LOG_DIAG_JIRA_DEDUPE_SCOPE must be open or all")
        if not isinstance(j["priority_map"], dict) or not isinstance(j["extra_fields"], dict):
            raise ValueError("JIRA_PRIORITY_MAP and JIRA_EXTRA_FIELDS must be JSON objects")
        if j["epic_key"] and not j["epic_link_field"]:
            raise ValueError("MONGODB_LOG_DIAG_JIRA_EPIC_KEY requires JIRA_EPIC_LINK_FIELD (e.g. customfield_10014); "
                             "for team-managed projects use JIRA_PARENT_KEY instead")
        if bad := [lb for lb in j["labels"] if re.search(r"\s", lb)]:
            raise ValueError(f"Jira labels cannot contain spaces: {bad}")
        j["assignee_field"] = j["assignee_field"] or ("accountId" if j["deployment"] == "cloud" else "name")
        if j["assignee_field"] not in {"accountId", "name"}:
            raise ValueError("MONGODB_LOG_DIAG_JIRA_ASSIGNEE_FIELD must be accountId (Cloud) or name (Data Center)")
    if s["jira_mode"] == "create" and not j["base_url"].startswith("https://"):
        raise ValueError("MONGODB_LOG_DIAG_JIRA_BASE_URL (https://...) is required when JIRA_MODE=create")


def enabled(config: dict) -> bool:
    try:
        s = settings(config)
    except ValueError:
        return True  # surface the configuration error in the notify step
    return s["jira_mode"] != "off" or s["slack_mode"] != "off"


def load_secrets(config: dict, cloud: str | None = None) -> dict:
    """Notification credentials only; separate from the Atlas secret (which holds Atlas keys only)."""
    path = os.environ.get("NOTIFICATION_CONFIG_FILE")
    secret_id = os.environ.get("NOTIFICATION_SECRET_ID")
    if path:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    elif secret_id:
        from . import skills
        cloud = cloud or config.get("cloud")
        if cloud == "aws":
            raw = skills.aws_storage().load_secret(secret_id)
        elif cloud == "gcp":
            raw = skills.gcp_storage().load_secret(secret_id)
        else:
            raise RuntimeError("local cloud requires NOTIFICATION_CONFIG_FILE")
    else:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Notification secret must be a JSON object")
    if unexpected := set(raw) - SECRET_FIELDS:
        raise ValueError("Notification secret may contain only " + ", ".join(sorted(SECRET_FIELDS)) +
                         f"; unexpected: {sorted(unexpected)}")
    return {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}


# --------------------------------------------------------------------------- parsing
def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", text)]


def _column_map(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(header):
        name = re.sub(r"[*_`]", "", raw).strip().lower()
        for field, aliases in _COLUMN_ALIASES.items():
            if field not in mapping and name in aliases:
                mapping[field] = idx
    return mapping


def _plain(text: str, limit: int = 1500) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    text = re.sub(r"<br\s*/?>", "; ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_findings(markdown: str, origin: str, node: str | None = None) -> list[dict]:
    """Extract prioritized findings from Markdown tables, ``P0``-headings, or ``P0`` bullets."""
    lines = (markdown or "").splitlines()
    findings: list[dict] = []
    seen_rows: set[int] = set()
    i = 0
    while i < len(lines) - 1:
        if "|" in lines[i] and _TABLE_SEP.match(lines[i + 1]):
            cols = _column_map(_cells(lines[i]))
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                if "priority" in cols and "finding" in cols:
                    cells = _cells(lines[j])
                    get = lambda f: cells[cols[f]] if f in cols and cols[f] < len(cells) else ""  # noqa: E731
                    m = _P.search(get("priority"))
                    if m and _plain(get("finding")):
                        findings.append(_finding(f"P{m.group(1)}", get("finding"), origin, node,
                                                 key=get("key"), nodes=get("nodes"), evidence=get("evidence"),
                                                 impact=get("impact"), action=get("action"),
                                                 confidence=get("confidence")))
                    seen_rows.add(j)
                j += 1
            i = j
            continue
        i += 1
    if findings:
        return findings
    # Fallback: "### P0 — Title" sections or "- **P0** Title: text" bullets.
    current = None
    for line in lines:
        h = _HEADING.match(line)
        if h:
            m = _P.search(h.group(2))
            if m and len(h.group(1)) >= 2:
                title = _P.sub("", h.group(2), count=1).strip(" -—:·|*")
                current = _finding(f"P{m.group(1)}", title or h.group(2), origin, node)
                findings.append(current)
            else:
                current = None
            continue
        bullet = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if bullet and current is None:
            m = re.match(r"^\W*P([0-3])\b\W*(.*)$", bullet.group(1))
            if m and m.group(2).strip():
                title, _, rest = m.group(2).partition(":")
                findings.append(_finding(f"P{m.group(1)}", title, origin, node, evidence=rest))
            continue
        if current is not None and line.strip():
            current["evidence"] = _plain((current["evidence"] + " " + line).strip())
    return findings


def _finding(priority, finding, origin, node, key="", nodes="", evidence="", impact="", action="", confidence=""):
    item = {"priority": priority, "finding": _plain(finding, 300), "key": _plain(key, 120),
            "nodes": _plain(nodes, 300) or (node or ""), "evidence": _plain(evidence), "impact": _plain(impact),
            "action": _plain(action), "confidence": _plain(confidence, 40), "origin": origin}
    return item


def fingerprint(item: dict, environment: str, cluster: str) -> str:
    """Stable identity of an issue for one environment/cluster (priority, counts and dates excluded)."""
    if item.get("key"):
        identity = "key:" + re.sub(r"[^a-z0-9.$_-]+", "-", item["key"].lower()).strip("-")
    else:
        text = " ".join(item.get(k, "") for k in ("finding", "evidence"))
        anchors = {h.lower() for h in _HEX.findall(text)}
        anchors |= {t for t in _TICKED.findall(text) if _NAMESPACE.match(t)}
        anchors |= set(_CODE.findall(text))
        low = item.get("finding", "").lower()
        category = next((name for name, rx in _CATEGORIES if re.search(rx, low)), "other")
        if anchors:
            identity = f"anchors:{category}:" + ",".join(sorted(anchors))
        else:
            words = re.sub(r"[^a-z ]+", " ", low)
            words = " ".join(w for w in words.split() if len(w) > 2)
            identity = f"text:{category}:{words}"
    raw = f"{environment}|{cluster.lower()}|{identity}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def collect_findings(store, layout: Layout, log_date: str, s: dict) -> tuple[list[dict], list[str]]:
    """Findings for this run and the files they came from (cluster summary first, node reports fallback)."""
    manifest = _read_json(store, layout.cluster_report(log_date, "manifest.json")) or {}
    summary_source = str(manifest.get("cluster_summary_source") or "extract")
    order = [NODE_REPORTS_SUMMARY_FILE, SUMMARY_FILE] if summary_source == "node_reports" else \
        [SUMMARY_FILE, NODE_REPORTS_SUMMARY_FILE]
    findings, used = [], []
    if s["source"] in ("auto", "cluster_summary"):
        for name in order:
            key = layout.cluster_report(log_date, name)
            if store.exists(key):
                found = parse_findings(store.get_text(key), name)
                if found:
                    findings, used = found, [name]
                    break
    if not findings and s["source"] in ("auto", "node_reports"):
        for report in manifest.get("reports") or []:
            node, log = report.get("node"), report.get("log_name")
            if not node or not log:
                continue
            key = layout.report(log_date, node, log, "report.md")
            if store.exists(key):
                found = parse_findings(store.get_text(key), f"{node}/{log}/report.md", node=node)
                if found:
                    findings.extend(found)
                    used.append(f"{node}/{log}/report.md")
    # Merge the same issue seen on several nodes; keep the highest priority.
    merged: dict[str, dict] = {}
    for item in findings:
        item["fingerprint"] = fingerprint(item, s["environment"], s["cluster"])
        prev = merged.get(item["fingerprint"])
        if prev is None:
            merged[item["fingerprint"]] = item
            continue
        if PRIORITIES.index(item["priority"]) < PRIORITIES.index(prev["priority"]):
            item["nodes"] = ", ".join(x for x in (item["nodes"], prev["nodes"]) if x)
            merged[item["fingerprint"]] = item
        elif item["nodes"] and item["nodes"] not in prev["nodes"]:
            prev["nodes"] = ", ".join(x for x in (prev["nodes"], item["nodes"]) if x)
    return list(merged.values()), used


# --------------------------------------------------------------------------- storage helpers
def _read_json(store, key):
    if not store.exists(key):
        return None
    try:
        return json.loads(store.get_text(key))
    except ValueError:
        return None


def ledger_key(layout: Layout, s: dict) -> str:
    cluster = re.sub(r"[^a-z0-9_-]+", "-", s["cluster"].lower()).strip("-") or "cluster"
    return f"{layout.prefix}/notifications/{s['environment']}/{cluster}/ledger.json"


# --------------------------------------------------------------------------- HTTP
def _http(method: str, url: str, headers: dict, body=None, timeout: int = 30) -> tuple[int, dict | str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/json", **headers,
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https enforced by config
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as err:
        text, status = err.read().decode("utf-8", "replace"), err.code
    try:
        return status, json.loads(text) if text.strip().startswith(("{", "[")) else text
    except ValueError:
        return status, text


# --------------------------------------------------------------------------- Jira
def _jira_description(item: dict, s: dict, log_date: str, links: dict) -> str:
    rows = [("Priority", item["priority"]), ("Environment", s["environment"]), ("Cluster", s["cluster"]),
            ("Log day (UTC)", log_date), ("Nodes", item["nodes"] or "see report"),
            ("Evidence", item["evidence"]), ("Impact", item["impact"]),
            ("Recommended action", item["action"]), ("Confidence", item["confidence"])]
    text = "\n".join(f"*{k}:* {v}" for k, v in rows if v)
    text += "\n\nh3. Reports\n" + "\n".join(f"* {k}: {{{{{v}}}}}" for k, v in links.items() if v)
    text += (f"\n\n----\nGenerated by the MongoDB log diagnostic pipeline from {item['origin']}. "
             "Index or configuration changes are candidates: validate with explain(\"executionStats\") and the "
             f"current index inventory before applying. Fingerprint: {item['fingerprint']}")
    return text[:30000]


def jira_fields(item: dict, s: dict, log_date: str, links: dict) -> dict:
    j = s["jira"]
    finding = item["finding"]
    try:
        summary = j["summary_template"].format(environment=s["environment"].upper(), cluster=s["cluster"],
                                               finding=finding, priority=item["priority"], date=log_date)
    except (KeyError, IndexError, ValueError):
        summary = f"[{s['environment'].upper()}] {s['cluster']}: {finding}"
    labels = list(dict.fromkeys([*j["labels"], "mongodb-log-diagnostic", f"{LABEL_PREFIX}env-{s['environment']}",
                                 LABEL_PREFIX + item["fingerprint"]]))
    fields = {"project": {"key": j["project_key"]}, "issuetype": {"name": j["issue_type"]},
              "summary": re.sub(r"\s+", " ", summary)[:250],
              "description": _jira_description(item, s, log_date, links), "labels": labels}
    if name := (j["priority_map"] or {}).get(item["priority"]):
        fields["priority"] = {"name": str(name)}
    if j["assignee"]:
        fields["assignee"] = {j["assignee_field"]: j["assignee"]}
    if j["components"]:
        fields["components"] = [{"name": c} for c in j["components"]]
    if j["parent_key"]:
        fields["parent"] = {"key": j["parent_key"]}
    if j["epic_key"]:
        fields[j["epic_link_field"]] = j["epic_key"]
    for key, value in j["extra_fields"].items():
        fields.setdefault(key, value)
    return fields


class JiraClient:
    def __init__(self, s: dict, secrets: dict, http=None):
        self.base = s["jira"]["base_url"]
        self.cloud = s["jira"]["deployment"] == "cloud"
        self.timeout = s["timeout"]
        self.http = http or _http
        if secrets.get("jira_pat"):
            self.headers = {"Authorization": "Bearer " + secrets["jira_pat"]}
        elif secrets.get("jira_email") and secrets.get("jira_api_token"):
            token = b64encode(f"{secrets['jira_email']}:{secrets['jira_api_token']}".encode()).decode()
            self.headers = {"Authorization": "Basic " + token}
        else:
            raise RuntimeError("JIRA_MODE=create needs jira_email+jira_api_token (Cloud) or jira_pat (Data Center) "
                               "in the notification secret")

    def find(self, project: str, fp: str, scope: str) -> str | None:
        jql = f'project = "{project}" AND labels = "{LABEL_PREFIX}{fp}"'
        if scope == "open":
            jql += " AND statusCategory != Done"
        query = urllib.parse.urlencode({"jql": jql, "maxResults": 1, "fields": "key,status"})
        paths = (["/rest/api/3/search/jql", "/rest/api/2/search"] if self.cloud else ["/rest/api/2/search"])
        for path in paths:
            status, body = self.http("GET", f"{self.base}{path}?{query}", self.headers, None, self.timeout)
            if status in (404, 410) and path != paths[-1]:
                continue
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError(f"Jira search failed status={status}")
            issues = body.get("issues") or []
            return issues[0].get("key") if issues else None
        return None

    def create(self, fields: dict) -> str:
        status, body = self.http("POST", f"{self.base}/rest/api/2/issue", self.headers, {"fields": fields}, self.timeout)
        if status not in (200, 201) or not isinstance(body, dict) or not body.get("key"):
            detail = json.dumps(body)[:500] if isinstance(body, dict) else str(body)[:300]
            raise RuntimeError(f"Jira create failed status={status} detail={detail}")
        return body["key"]

    def comment(self, key: str, text: str) -> None:
        self.http("POST", f"{self.base}/rest/api/2/issue/{key}/comment", self.headers, {"body": text[:30000]},
                  self.timeout)


def _jira(items, s, log_date, links, ledger, store, layout, secrets, http) -> dict:
    mode, j = s["jira_mode"], s["jira"]
    book = ledger.setdefault("jira", {})
    out = {"mode": mode, "priorities": s["jira_priorities"], "candidates": len(items), "created": [],
           "existing": [], "exported": [], "skipped": [], "errors": []}
    if mode == "off":
        return out
    if len(items) > j["max_issues"]:
        out["skipped"] += [{"fingerprint": i["fingerprint"], "reason": "max_issues"} for i in items[j["max_issues"]:]]
        items = items[:j["max_issues"]]
    today = datetime.now(timezone.utc).date().isoformat()
    if mode == "json":
        updates = []
        for item in items:
            entry = book.get(item["fingerprint"])
            if entry:
                entry["last_seen"] = log_date
                out["existing"].append({"fingerprint": item["fingerprint"], "key": entry.get("key"),
                                        "first_seen": entry.get("first_seen"), "source": "ledger"})
                continue
            updates.append({"fields": jira_fields(item, s, log_date, links)})
            book[item["fingerprint"]] = {"key": None, "first_seen": log_date, "last_seen": log_date,
                                         "exported_at": today, "finding": item["finding"][:200]}
            out["exported"].append(item["fingerprint"])
        if updates:
            out["file"] = store.put_text(layout.cluster_report(log_date, JIRA_EXPORT_FILE),
                                         json.dumps({"issueUpdates": updates}, indent=2), "application/json")
        return out
    client = JiraClient(s, secrets, http)
    for item in items:
        fp = item["fingerprint"]
        try:
            existing = client.find(j["project_key"], fp, j["dedupe_scope"])
        except Exception as exc:  # never create blindly when de-duplication is unavailable
            out["errors"].append({"fingerprint": fp, "error": str(exc)[:300]})
            continue
        if existing:
            book.setdefault(fp, {"first_seen": log_date})
            book[fp].update({"key": existing, "last_seen": log_date})
            out["existing"].append({"fingerprint": fp, "key": existing, "source": "jira"})
            if j["comment_on_existing"] and book[fp].get("commented_for") != log_date:
                try:
                    client.comment(existing, f"Still observed on {log_date} ({s['environment']}, {s['cluster']}). "
                                             f"Evidence: {item['evidence'][:1500]}")
                    book[fp]["commented_for"] = log_date
                except Exception as exc:
                    out["errors"].append({"fingerprint": fp, "error": "comment: " + str(exc)[:200]})
            continue
        try:
            key = client.create(jira_fields(item, s, log_date, links))
        except Exception as exc:
            out["errors"].append({"fingerprint": fp, "error": str(exc)[:500]})
            continue
        book[fp] = {"key": key, "first_seen": log_date, "last_seen": log_date, "created_at": today,
                    "finding": item["finding"][:200]}
        out["created"].append({"fingerprint": fp, "key": key, "url": f"{j['base_url']}/browse/{key}"})
    return out


# --------------------------------------------------------------------------- Slack
def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(items: list[dict], s: dict, log_date: str, links: dict) -> dict:
    env = s["environment"].upper()
    title = f"[{env}] {len(items)} {'/'.join(s['slack_priorities'])} finding(s) — {s['cluster']} — {log_date}"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title[:150]}}]
    if s["slack"]["mentions"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": " ".join(s["slack"]["mentions"])}})
    for item in items[: s["slack"]["max_findings"]]:
        lines = [f"*{item['priority']} — {_esc(item['finding'])}*"]
        for label, key in (("Nodes", "nodes"), ("Impact", "impact"), ("Action", "action"), ("Confidence", "confidence")):
            if item.get(key):
                lines.append(f"• {label}: {_esc(item[key][:400])}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    if len(items) > s["slack"]["max_findings"]:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"+{len(items) - s['slack']['max_findings']} more in the cluster summary"}]})
    refs = " · ".join(f"{k}: `{_esc(v)}`" for k, v in links.items() if v)
    if refs:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": refs[:2900]}]})
    payload = {"text": title, "blocks": blocks[:50], "unfurl_links": False}
    if s["slack"]["channel"]:
        payload["channel"] = s["slack"]["channel"]
    return payload


def _slack(items, s, log_date, links, ledger, store, layout, secrets, http) -> dict:
    mode = s["slack_mode"]
    book = ledger.setdefault("slack", {})
    out = {"mode": mode, "priorities": s["slack_priorities"], "candidates": len(items), "sent": False,
           "suppressed": [], "errors": []}
    if mode == "off" or not items:
        return out
    fresh = []
    repeat = s["slack"]["repeat_after_days"]
    for item in items:
        last = (book.get(item["fingerprint"]) or {}).get("last_sent")
        if last == log_date:
            out["suppressed"].append({"fingerprint": item["fingerprint"], "reason": "already_sent_for_day"})
        elif last and repeat > 0 and _days(last, log_date) < repeat:
            out["suppressed"].append({"fingerprint": item["fingerprint"], "reason": f"sent_within_{repeat}_days"})
        else:
            fresh.append(item)
    out["items"] = [i["fingerprint"] for i in fresh]
    if not fresh:
        return out
    payload = slack_payload(fresh, s, log_date, links)
    if mode == "json":
        out["file"] = store.put_text(layout.cluster_report(log_date, SLACK_EXPORT_FILE),
                                     json.dumps({"messages": [payload]}, indent=2), "application/json")
    else:
        http = http or _http
        if secrets.get("slack_webhook_url"):
            url = secrets["slack_webhook_url"]
            if not url.startswith("https://hooks.slack.com/"):
                raise RuntimeError("slack_webhook_url must be an https://hooks.slack.com/ URL")
            body = {k: v for k, v in payload.items() if k != "channel"}  # webhooks are bound to a channel
            status, resp = http("POST", url, {}, body, s["timeout"])
            ok = status == 200
        elif secrets.get("slack_bot_token"):
            if not payload.get("channel"):
                raise RuntimeError("MONGODB_LOG_DIAG_SLACK_CHANNEL is required with slack_bot_token")
            status, resp = http("POST", "https://slack.com/api/chat.postMessage",
                                {"Authorization": "Bearer " + secrets["slack_bot_token"]}, payload, s["timeout"])
            ok = status == 200 and isinstance(resp, dict) and resp.get("ok")
        else:
            raise RuntimeError("SLACK_MODE=send needs slack_webhook_url or slack_bot_token in the notification secret")
        if not ok:
            detail = resp.get("error") if isinstance(resp, dict) else str(resp)[:200]
            out["errors"].append({"error": f"Slack post failed status={status} detail={detail}"})
            return out
        out["sent"] = True
    for item in fresh:
        book[item["fingerprint"]] = {"last_sent": log_date, "mode": mode, "finding": item["finding"][:200]}
    return out


def _days(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except ValueError:
        return 10**6


# --------------------------------------------------------------------------- entry point
def run(config: dict, log_date: str, store=None, http=None, secrets: dict | None = None) -> dict:
    """Generate the configured notifications for one day; returns (and stores) ``notifications.json``."""
    s = settings(config)
    result = {"environment": s["environment"], "cluster": s["cluster"], "log_date": log_date,
              "jira_mode": s["jira_mode"], "slack_mode": s["slack_mode"],
              "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if s["jira_mode"] == "off" and s["slack_mode"] == "off":
        result["status"] = "disabled"
        return result
    from .providers import get_store
    layout = Layout.from_config(config)
    store = store or get_store(config)
    findings, used = collect_findings(store, layout, log_date, s)
    if secrets is None and ("create" == s["jira_mode"] or "send" == s["slack_mode"]):
        secrets = load_secrets(config)
    secrets = secrets or {}
    links = {"cluster summary": store.uri(layout.cluster_report(log_date, used[0])) if used and
             not used[0].endswith("report.md") else None,
             "cluster reports": store.uri(layout.cluster_report(log_date, ""))}
    trend_key = layout.cluster_report(log_date, "trend-alerts.json")
    if store.exists(trend_key):
        links["trend alerts"] = store.uri(trend_key)
    lkey = ledger_key(layout, s)
    ledger = _read_json(store, lkey) or {"environment": s["environment"], "cluster": s["cluster"]}
    by = lambda prios: sorted([f for f in findings if f["priority"] in prios],  # noqa: E731
                              key=lambda f: (PRIORITIES.index(f["priority"]), f["finding"]))
    result.update({"sources": used, "findings": [{k: f[k] for k in ("priority", "fingerprint", "finding", "nodes")}
                                                 for f in findings if f["priority"] in
                                                 set(s["jira_priorities"]) | set(s["slack_priorities"])]})
    errors = []
    for name, func, prios in (("jira", _jira, s["jira_priorities"]), ("slack", _slack, s["slack_priorities"])):
        try:
            result[name] = func(by(prios), s, log_date, links, ledger, store, layout, secrets, http)
        except Exception as exc:
            logger.exception("Notification channel %s failed", name)
            result[name] = {"mode": s[f"{name}_mode"], "errors": [{"error": str(exc)[:500]}]}
        errors += result[name].get("errors") or []
    ledger["updated_at"] = result["generated_at"]
    store.put_text(lkey, json.dumps(ledger, indent=2, sort_keys=True), "application/json")
    result["ledger"] = store.uri(lkey)
    result["status"] = "partial" if errors else "ok"
    result["uri"] = store.put_text(layout.cluster_report(log_date, RESULT_FILE), json.dumps(result, indent=2),
                                   "application/json")
    logger.info("Notifications env=%s jira=%s created=%d existing=%d exported=%d slack=%s sent=%s errors=%d",
                s["environment"], s["jira_mode"], len(result["jira"].get("created", [])),
                len(result["jira"].get("existing", [])), len(result["jira"].get("exported", [])),
                s["slack_mode"], result["slack"].get("sent"), len(errors))
    return result
