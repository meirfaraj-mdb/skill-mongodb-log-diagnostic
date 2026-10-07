"""Append evidence-bound, literal-free shape forms for hashes cited in a node report.

A log query_hash is NOT a Query Shape Insights queryShapeHash. Never join them
by prefix or by namespace alone. This module intentionally performs no I/O.
"""
from __future__ import annotations

import json
import re

_HASH = re.compile(r"(?<![a-fA-F0-9])(?:[a-fA-F0-9]{64}|[a-fA-F0-9]{8})(?![a-fA-F0-9])")
_HEADING = "## Reported query hashes and redacted query forms"
_MAX_ITEMS = 60


def _structure(value, depth=0):
    """Preserve JSON keys/shape, replace every value; do not echo user literals."""
    if depth > 14:
        return "?"
    if isinstance(value, dict):
        # Do not display any field whose name itself suggests secret content.
        return {str(k): _structure(v, depth + 1) for k, v in list(value.items())[:80]
                if isinstance(k, str) and len(k) < 100 and not re.search(
                    r"(?i)(password|secret|credential|token|api.?key)", k)}
    if isinstance(value, list):
        return [_structure(v, depth + 1) for v in value[:12]]
    return "?"


def _json_shape(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _entries(obj, depth=0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("queryShapeHash"), str):
            yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _entries(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:5000]:
            yield from _entries(v, depth + 1)


def _node_stats(stats_json):
    if not stats_json:
        return {}
    try:
        data = json.loads(stats_json) if isinstance(stats_json, str) else stats_json
    except (TypeError, ValueError):
        return {}
    mapping = {}
    for record in _entries(data):
        key = record.get("key") if isinstance(record.get("key"), dict) else {}
        hash_value = record.get("queryShapeHash") or key.get("queryShapeHash")
        form = key.get("queryShape") or record.get("queryShape")
        if isinstance(hash_value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", hash_value) and _json_shape(form) is not None:
            mapping[hash_value.upper()] = _json_shape(form)
    return mapping


def append_reported_shapes(report_md: str, extraction: dict, query_stats_json=None) -> str:
    """Only enumerate exact hashes already mentioned in the generated report.

    Log hashes resolve to redacted sample_query_shape from the SAME node's
    extraction. True queryShapeHash resolves only to the SAME node's stats.
    Never infer equivalence between the two schemes. For missing forms, report
    the absence rather than inventing a query. Idempotent across reruns.
    """
    if _HEADING in report_md:
        return report_md
    mentioned = {m.group(0).upper() for m in _HASH.finditer(report_md)}
    groups = (extraction.get("slow") or {}).get("operations") or extraction.get("slow_operations") or []
    log_rows = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        hash_value = group.get("query_hash")
        if isinstance(hash_value, str) and hash_value.upper() in mentioned and re.fullmatch(r"[a-fA-F0-9]{8}", hash_value):
            log_rows.append((hash_value.upper(), str(group.get("namespace") or "unknown"), group.get("sample_query_shape")))
    insight_rows = [(hash_value, form) for hash_value, form in _node_stats(query_stats_json).items()
                    if hash_value in mentioned]
    lines = ["", _HEADING, "",
             "Hashes are included only if cited in this node report and found in this node's supplied evidence. "
             "Log `query_hash` and Query Shape Insights `queryShapeHash` are different identifiers; "
             "no relationship is assumed. Values below are replaced with `?`; use representative "
             "parameters and `explain(\"executionStats\")` to validate a plan.", ""]
    if not log_rows and not insight_rows:
        lines.append("No cited query hash could be resolved to a supplied redacted query form for this node.")
    for h, ns, form in sorted(set((h, ns, json.dumps(form, sort_keys=True, default=str)) for h, ns, form in log_rows))[:_MAX_ITEMS]:
        # Only structured extracted shapes are emitted; strings may contain unredacted literals.
        parsed = json.loads(form)
        lines.extend([f"### Log query hash `{h}` — `{ns.replace('`', '')[:120]}`", ""])
        parsed = _json_shape(parsed)
        if parsed is not None:
            rendered = json.dumps(_structure(parsed), sort_keys=True, indent=2)
            lines.extend(["```json", rendered, "```", ""] if len(rendered) <= 6000 else
                         ["Redacted structural form exceeds the display limit.", ""])
        else:
            lines.extend(["Redacted structural form unavailable in the supplied extraction.", ""])
    for h, form in sorted(insight_rows)[:_MAX_ITEMS]:
        rendered = json.dumps(_structure(form), sort_keys=True, indent=2)
        lines.extend([f"### Query Shape Insights `queryShapeHash` `{h}`", ""])
        lines.extend(["```json", rendered, "```", ""] if len(rendered) <= 6000 else
                     ["Redacted structural form exceeds the display limit.", ""])
    if log_rows and not insight_rows:
        lines.extend(["No cited 64-character Query Shape Insights `queryShapeHash` could be resolved "
                      "from this node's query-statistics file; do not relabel the 8-character log hashes.", ""])
    return report_md.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n"
