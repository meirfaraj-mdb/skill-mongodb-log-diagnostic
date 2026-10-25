"""Deterministic report priority rules (see references/priority-rules.json).

Flow per node report:
  1. evaluate()      -> priority_decisions document from the extraction (+ n-1/n-8 diffs)
  2. render_prompt() -> rule text added to the system prompt
  3. enforce()       -> remove findings/actions that the rules suppress
  4. audit()         -> non-blocking warnings when a table row disagrees with a decision
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .skills import diagnostic_skill_dir

RULES_REL = "references/priority-rules.json"
PRIORITIES = ("P0", "P1", "P2", "P3")
ISSUES = "issues_and_actions"
ANALYSIS_ONLY = "query_analysis_only"
APPENDIX_ONLY = "appendix_only"
NO_CAP = "no_cap"
PLACEMENTS = {ISSUES, ANALYSIS_ONLY, APPENDIX_ONLY, NO_CAP}

_P_TOKEN = re.compile(r"\bP([0-3])\b")
_CHANGE_STREAM_TEXT = re.compile(r"change[\s-]?streams?|\$changeStream", re.I)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_ACTION_HEADING = re.compile(r"finding|issue|action|execution|next|priorit|executive|summary|watch|risk", re.I)
_MAX_SLOW_DECISIONS = 60


# --------------------------------------------------------------------------- loading
def rules_path(config: dict | None = None) -> Path:
    override = (config or {}).get("priority_rules_file") or os.environ.get("MONGODB_LOG_DIAG_PRIORITY_RULES_FILE")
    return Path(override) if override else diagnostic_skill_dir() / RULES_REL


def validate_rules(rules: dict) -> None:
    body = rules.get("rules")
    if not isinstance(body, dict):
        raise ValueError("priority rules: missing 'rules' object")
    cs = body.get("change_stream_collscan", {})
    if cs.get("enabled") and cs.get("long_priority") not in PRIORITIES:
        raise ValueError("priority rules: change_stream_collscan.long_priority must be P0-P3")
    slow = body.get("slow_low_volume", {})
    if slow.get("enabled") and slow.get("priority") not in PRIORITIES:
        raise ValueError("priority rules: slow_low_volume.priority must be P0-P3")
    auth = body.get("auth_failures", {})
    if auth.get("enabled"):
        bands = auth.get("bands") or []
        if not bands or "lt" in bands[-1]:
            raise ValueError("priority rules: auth_failures.bands must end with an open-ended band")
        limits = [b["lt"] for b in bands if "lt" in b]
        if limits != sorted(limits):
            raise ValueError("priority rules: auth_failures.bands must be in ascending 'lt' order")
        for band in bands:
            if band.get("placement") not in PLACEMENTS:
                raise ValueError(f"priority rules: invalid placement {band.get('placement')!r}")
            if band["placement"] == ISSUES and band.get("priority") not in PRIORITIES:
                raise ValueError("priority rules: issues_and_actions bands need a priority P0-P3")
        for name, spec in (auth.get("types") or {}).items():
            for pattern in spec.get("message_patterns", []) + [spec.get("report_keywords", "")]:
                re.compile(pattern)


def load_rules(config: dict | None = None) -> dict:
    path = rules_path(config)
    rules = json.loads(path.read_text(encoding="utf-8"))
    validate_rules(rules)
    return rules


# --------------------------------------------------------------------------- helpers
def _get(d, *path, default=None):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _slow_key(op: dict) -> str:
    # Same key as agent/diffing.py so n-1/n-8 labels can be attached.
    return " | ".join(str(op.get(k) or "-") for k in ("namespace", "plan_summary", "query_hash"))


def _excluded(namespace, rules, change_stream: bool = False) -> bool:
    ns = str(namespace or "")
    if any(m in ns for m in rules.get("excluded_namespace_markers", [])):
        return True
    # Cluster-wide change streams are logged on admin.$cmd.aggregate; keep them.
    if change_stream and ns in set(rules.get("change_stream_allowed_namespaces", [])):
        return False
    return any(ns.startswith(p) for p in rules.get("excluded_namespace_prefixes", []))


def _contains_key(value, key, depth=0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_contains_key(v, key, depth + 1) for v in value)
    if isinstance(value, str) and depth == 0:
        return key in value
    return False


def is_change_stream_group(op: dict) -> bool:
    """Slow groups carry no change-stream flag; the redacted shape keeps the $changeStream stage key."""
    return (_contains_key(op.get("sample_query_shape"), "$changeStream")
            or "$changeStream" in str(op.get("sample_message") or ""))


def _coverage(extraction: dict) -> dict:
    first = _get(extraction, "metadata", "time_range", "first")
    last = _get(extraction, "metadata", "time_range", "last")
    hours = None
    try:
        if first and last:
            delta = (datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                     - datetime.fromisoformat(str(first).replace("Z", "+00:00")))
            hours = round(delta.total_seconds() / 3600, 2)
    except ValueError:
        pass
    return {"first": first, "last": last, "hours": hours}


def _trend_map(diffs) -> dict:
    trends: dict[str, dict] = {}
    for diff in diffs or []:
        for row in diff.get("slow_operation_groups", []) or []:
            trends.setdefault(row.get("key"), {})[diff.get("label")] = row.get("label")
    return trends


# --------------------------------------------------------------------------- evaluation
def _slow_decision(op: dict, rules: dict, trends: dict) -> dict | None:
    body = rules["rules"]
    count = _num(op.get("count")) or 0
    dur_max = _num(_get(op, "duration_ms", "max"))
    dur_avg = _num(_get(op, "duration_ms", "avg"))
    docs_max = _num(_get(op, "docs_examined", "max"))
    est_total = round(dur_avg * count, 1) if dur_avg is not None else None
    decision = {"namespace": op.get("namespace"), "plan_summary": op.get("plan_summary"),
                "query_hash": op.get("query_hash"), "count": count, "duration_ms_max": dur_max,
                "docs_examined_max": docs_max, "estimated_total_duration_ms": est_total}
    trend = trends.get(_slow_key(op))
    if trend:
        decision["trend"] = trend
        if any(label in {"new", "increased"} for label in trend.values()):
            decision["watch"] = True

    cs = body.get("change_stream_collscan", {})
    if cs.get("enabled") and is_change_stream_group(op):
        long_scan = ((dur_max is not None and dur_max >= cs["long_scan_duration_ms_gte"])
                     or (docs_max is not None and docs_max >= cs["long_scan_docs_examined_gte"]))
        if long_scan:
            return {**decision, "rule": "change_stream_collscan", "priority": cs["long_priority"],
                    "placement": ISSUES,
                    "reason": "Change-stream oplog scan (expected, not index-fixable) with a very long scan; "
                              "check restart/failover resume impact."}
        return {**decision, "rule": "change_stream_collscan", "priority": None, "placement": ANALYSIS_ONLY,
                "reason": "Change-stream oplog scan below the long-scan thresholds: expected behaviour."}

    slow = body.get("slow_low_volume", {})
    if not slow.get("enabled"):
        return None
    if est_total is None:
        return {**decision, "rule": "slow_low_volume", "priority": None, "placement": NO_CAP,
                "reason": "Duration statistics missing; the low-volume rule cannot be evaluated."}
    if count < slow["count_lt"] and est_total < slow["total_duration_ms_lt"]:
        return {**decision, "rule": "slow_low_volume", "priority": slow["priority"], "placement": ISSUES,
                "reason": "Low volume and low total slow time: probably not critical, kept visible as a "
                          "watch item (new feature or growth)."}
    return {**decision, "rule": "slow_low_volume", "priority": None, "placement": NO_CAP,
            "reason": "At or above the low-volume thresholds: prioritize from evidence."}


def _group_text(group: dict) -> str:
    parts = [group.get("sample_message"), _get(group, "sample_log", "message")]
    parts += list(group.get("message_samples") or [])
    return "\n".join(str(p) for p in parts if p)


def _group_codes(group: dict) -> tuple[set[str], set[str]]:
    codes = {str(c) for c in (_get(group, "operation_details", "error_codes") or [])}
    names = {str(c) for c in (_get(group, "operation_details", "error_code_names") or [])}
    if _get(group, "sample_log", "error_code") is not None:
        codes.add(str(_get(group, "sample_log", "error_code")))
    if _get(group, "sample_log", "error_code_name"):
        names.add(str(_get(group, "sample_log", "error_code_name")))
    return codes, names


def classify_auth_group(group: dict, types: dict) -> str | None:
    text = _group_text(group)
    codes, names = _group_codes(group)
    for name, spec in types.items():
        if any(re.search(p, text, re.I) for p in spec.get("message_patterns", [])):
            return name
        if names & set(spec.get("error_code_names", [])) or codes & set(spec.get("error_codes", [])):
            return name
    return None


def _band(count: int, bands: list[dict]) -> dict:
    for band in bands:
        if "lt" not in band or count < band["lt"]:
            return band
    return bands[-1]


def _auth_decisions(extraction: dict, rules: dict) -> list[dict]:
    auth = rules["rules"].get("auth_failures", {})
    if not auth.get("enabled"):
        return []
    types = auth.get("types", {})
    totals = {name: {"count": 0, "groups": 0, "first_seen": None, "last_seen": None} for name in types}
    for group in _get(extraction, "error_scan", "groups", default=[]) or []:
        if not isinstance(group, dict):
            continue
        kind = classify_auth_group(group, types)
        if not kind:
            continue
        t = totals[kind]
        t["count"] += _num(group.get("count")) or 0
        t["groups"] += 1
        for field, pick in (("first_seen", min), ("last_seen", max)):
            if group.get(field):
                t[field] = group[field] if t[field] is None else pick(t[field], group[field])
    decisions = []
    for name, t in totals.items():
        if not t["count"]:
            continue
        band = _band(t["count"], auth["bands"])
        decisions.append({"rule": "auth_failures", "type": name, "label": types[name].get("label", name),
                          **t, "priority": band.get("priority"), "placement": band["placement"],
                          "reason": f"{t['count']} events in the log day -> "
                                    f"{band.get('priority') or 'not a finding'} ({band['placement']})."})
    return decisions


def evaluate(extraction: dict, rules: dict, diffs=None) -> dict:
    trends = _trend_map(diffs)
    ops = _get(extraction, "slow", "operations")
    if ops is None:
        ops = extraction.get("slow_operations") or []
    slow = []
    for op in ops:
        if not isinstance(op, dict) or _excluded(op.get("namespace"), rules, is_change_stream_group(op)):
            continue
        decision = _slow_decision(op, rules, trends)
        if decision:
            slow.append(decision)
    slow.sort(key=lambda d: (d["placement"] != ISSUES, -(d["count"] or 0)))
    notes = []
    cs_global = _num(_get(extraction, "slow", "global_stats", "change_stream_count")) or 0
    if cs_global and not any(d["rule"] == "change_stream_collscan" for d in slow):
        notes.append(f"slow.global_stats.change_stream_count={cs_global} but no slow group exposes a "
                     "$changeStream shape; treat change-stream evidence as aggregate only.")
    if len(slow) > _MAX_SLOW_DECISIONS:
        notes.append(f"{len(slow) - _MAX_SLOW_DECISIONS} lower-volume slow decisions omitted for size.")
    coverage = _coverage(extraction)
    if coverage["hours"] is not None and coverage["hours"] < 20:
        notes.append(f"Log covers {coverage['hours']} h, not a full day; thresholds are applied to raw counts.")
    return {"rules_version": rules.get("version"), "basis": "per node, per log day",
            "log_coverage": coverage, "slow_operations": slow[:_MAX_SLOW_DECISIONS],
            "auth_failures": _auth_decisions(extraction, rules), "notes": notes}


# --------------------------------------------------------------------------- prompt
class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fmt(text: str, values: dict) -> str:
    try:
        return text.format_map(_Safe(values))
    except (ValueError, IndexError, KeyError):
        return text


def _band_text(bands: list[dict]) -> str:
    out, low = [], 0
    for band in bands:
        span = f"{low:,}-{band['lt'] - 1:,}" if "lt" in band else f"{low:,} or more"
        result = band.get("priority") or "not a finding"
        out.append(f"{span}: {result} ({band['placement']})")
        low = band.get("lt", low)
    return "; ".join(out)


def render_prompt(rules: dict) -> str:
    body = rules["rules"]
    lines = [
        f"=== Report priority rules (references/priority-rules.json, version {rules.get('version')}) ===",
        "These rules OVERRIDE conflicting prioritization in analysis-prompt.md, including 'Prioritize P0, P1, and P2 findings'.",
        "A <document label=\"priority_decisions\"> lists the decision for each matched slow group and auth-failure type. "
        "Use its priority and placement exactly; do not raise or lower them.",
        "Placement meanings:",
        f"- {ISSUES}: one row in the Prioritized findings table with exactly that priority, and one matching action in the "
        "Recommended execution order. P3 rows are required and P3 actions go last as watch-list items.",
        f"- {ANALYSIS_ONLY}: no findings-table row and no action; mention briefly in Query and latency analysis as expected behaviour.",
        f"- {APPENDIX_ONLY}: not a finding; leave it out of the executive summary, findings table, action plan and top-three "
        "actions. At most one line with the count in the Evidence appendix.",
        f"- {NO_CAP}: the rule does not constrain the item; prioritize from evidence as usual.",
        "Add '[rule: <rule id>]' at the end of the Evidence cell of every row driven by a decision.",
    ]
    for rule_id, rule in body.items():
        if not rule.get("enabled"):
            continue
        values = dict(rule)
        if "total_duration_ms_lt" in rule:
            values["total_duration_hours"] = rule["total_duration_ms_lt"] / 3_600_000
        lines.append(f"Rule {rule_id}:")
        lines += [f"- {_fmt(g, values)}" for g in rule.get("guidance", [])]
        if rule_id == "auth_failures":
            labels = ", ".join(f"'{t.get('label', n)}'" for n, t in rule.get("types", {}).items())
            lines.append(f"- Bands per type ({labels}): {_band_text(rule['bands'])}.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- enforcement / audit
def merge_decisions(decision_docs: list[dict]) -> dict:
    """Cluster view: an auth type is suppressed only when it is suppressed on every node that saw it."""
    merged = {"rules_version": None, "slow_operations": [], "auth_failures": []}
    by_type: dict[str, list[dict]] = {}
    for doc in decision_docs:
        if not doc:
            continue
        merged["rules_version"] = merged["rules_version"] or doc.get("rules_version")
        merged["slow_operations"] += doc.get("slow_operations", [])
        for d in doc.get("auth_failures", []):
            by_type.setdefault(d["type"], []).append(d)
    for kind, items in by_type.items():
        suppressed = all(d["placement"] == APPENDIX_ONLY for d in items)
        merged["auth_failures"].append({**items[0], "placement": APPENDIX_ONLY if suppressed else ISSUES,
                                        "count": sum(d["count"] for d in items)})
    return merged


def _suppression_matchers(decisions: dict, rules: dict):
    types = rules["rules"].get("auth_failures", {}).get("types", {})
    suppressed = {d["type"] for d in decisions.get("auth_failures", []) if d["placement"] == APPENDIX_ONLY}
    kept = {d["type"] for d in decisions.get("auth_failures", []) if d["placement"] != APPENDIX_ONLY}
    sup_re = [re.compile(types[t]["report_keywords"], re.I) for t in suppressed if t in types]
    kept_re = [re.compile(types[t]["report_keywords"], re.I) for t in kept if t in types]
    cs_decisions = [d for d in decisions.get("slow_operations", []) if d["rule"] == "change_stream_collscan"]
    long_cs = {str(d["namespace"]) for d in cs_decisions if d["placement"] == ISSUES and d.get("namespace")}
    # Without a detected change-stream group we cannot tell short from long scans: rely on the prompt.
    enforce_cs = bool(cs_decisions)

    def should_drop(text: str) -> bool:
        if any(r.search(text) for r in sup_re) and not any(r.search(text) for r in kept_re):
            return True
        if enforce_cs and _CHANGE_STREAM_TEXT.search(text) and not any(ns in text for ns in long_cs):
            return True
        return False
    return should_drop


def enforce(report_md: str, decisions: dict, rules: dict) -> tuple[str, int]:
    """Drop findings-table rows and action items that the rules suppress.

    Only table rows that carry a P0-P3 token, and list items under finding/action/summary
    headings, are candidates; narrative sections and the evidence appendix are untouched.
    """
    should_drop = _suppression_matchers(decisions, rules)
    out, removed, heading, skipping_item = [], 0, "", False
    for line in report_md.splitlines():
        m = _HEADING.match(line)
        if m:
            heading, skipping_item = m.group(1), False
            out.append(line)
            continue
        stripped = line.lstrip()
        if stripped.startswith("|") and _P_TOKEN.search(line) and should_drop(line):
            removed += 1
            continue
        if _LIST_ITEM.match(line):
            skipping_item = bool(_ACTION_HEADING.search(heading)) and should_drop(line)
            if skipping_item:
                removed += 1
                continue
        elif skipping_item and line.strip() and line[:1].isspace():
            continue  # continuation of a dropped list item
        else:
            skipping_item = False
        out.append(line)
    text = "\n".join(out)
    return (text + "\n" if report_md.endswith("\n") else text), removed


def audit(report_md: str, decisions: dict, rules: dict) -> list[str]:
    """Return warnings when a findings-table row disagrees with a fixed-priority decision."""
    rows = [l for l in report_md.splitlines() if l.lstrip().startswith("|") and _P_TOKEN.search(l)]
    warnings = []
    for d in decisions.get("slow_operations", []):
        if d["placement"] != ISSUES or not d.get("priority"):
            continue
        anchor = d.get("query_hash") or None
        hits = [r for r in rows if anchor and anchor in r]
        if anchor and not hits:
            warnings.append(f"{d['rule']}: no findings row for {d.get('namespace')} hash {anchor} (expected {d['priority']})")
        for r in hits:
            found = {f"P{x}" for x in _P_TOKEN.findall(r)}
            if d["priority"] not in found:
                warnings.append(f"{d['rule']}: row for hash {anchor} has {sorted(found)}, expected {d['priority']}")
    types = rules["rules"].get("auth_failures", {}).get("types", {})
    for d in decisions.get("auth_failures", []):
        if d["placement"] != ISSUES or d["type"] not in types:
            continue
        pattern = re.compile(types[d["type"]]["report_keywords"], re.I)
        hits = [r for r in rows if pattern.search(r)]
        if not hits:
            warnings.append(f"auth_failures: no findings row for {d['label']} (expected {d['priority']})")
        for r in hits:
            found = {f"P{x}" for x in _P_TOKEN.findall(r)}
            if d["priority"] not in found:
                warnings.append(f"auth_failures: {d['label']} row has {sorted(found)}, expected {d['priority']}")
    return warnings
