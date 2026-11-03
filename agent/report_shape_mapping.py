"""Append evidence-bound, literal-free shape forms for hashes cited in a node report.

A node report ends with one ``## APPENDIX`` section, always in this order:

* APPENDIX 1 — node metrics graphs (``append_metrics_appendix``), only when metrics exist;
* APPENDIX 2 — Performance Advisor (``append_advisor_appendix``), only when advisor data exists;
* APPENDIX 3 — reported query hashes and redacted query forms (``append_reported_shapes``).

The functions may be called in any order: each inserts its block at its fixed position and is
idempotent. Lists of placeholder values (``$in: ["?", "?", ...]``) are rendered as a single
``"? (N values)"`` string; the count is a lower bound (``≥N``) when the source shape was truncated.

A log query_hash is NOT a Query Shape Insights queryShapeHash. Never join them
by prefix or by namespace alone. This module intentionally performs no I/O.
"""
from __future__ import annotations

import json
import re

_HASH = re.compile(r"(?<![a-fA-F0-9])(?:[a-fA-F0-9]{64}|[a-fA-F0-9]{8})(?![a-fA-F0-9])")
APPENDIX_HEADING = "## APPENDIX"
METRICS_HEADING = "### APPENDIX 1 — Node metrics graphs"
ADVISOR_HEADING = "### APPENDIX 2 — Performance Advisor"
_HEADING = "### APPENDIX 3 — Reported query hashes and redacted query forms"
SHAPES_HEADING = _HEADING
# Fixed order of the sub-appendices inside ``## APPENDIX``.
_ORDER = (METRICS_HEADING, ADVISOR_HEADING, SHAPES_HEADING)
_APPENDIX_RE = re.compile(r"(?m)^## APPENDIX[ \t]*$")
_DATA_URI = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
_MAX_ITEMS = 60
# Arrays whose elements are all placeholders collapse to one "? (N values)" string when they have
# at least this many items. The log extractor keeps the first 10 list items only, so a full list of
# 10 is a lower bound.
_PLACEHOLDER_MIN = 2
_EXTRACTOR_LIST_CAP = 10


def _is_placeholder(value) -> bool:
    """A redacted scalar: every literal is shown as ``?``; never a user value."""
    return not isinstance(value, (dict, list))


def _structure(value, depth=0, list_cap=None):
    """Preserve JSON keys/shape, replace every value; do not echo user literals.

    A list of scalars only (``$in``/``$nin``/``$all`` values, ``_id`` lists, ...) carries no
    structure, only its size: it becomes ``"? (N values)"``, with ``≥N`` when the list reached
    ``list_cap`` (the producer truncated it) or this function's own display cap.
    """
    if depth > 14:
        return "?"
    if isinstance(value, dict):
        # Do not display any field whose name itself suggests secret content.
        return {str(k): _structure(v, depth + 1, list_cap) for k, v in list(value.items())[:80]
                if isinstance(k, str) and len(k) < 100 and not re.search(
                    r"(?i)(password|secret|credential|token|api.?key)", k)}
    if isinstance(value, list):
        if len(value) >= _PLACEHOLDER_MIN and all(_is_placeholder(v) for v in value):
            truncated = list_cap is not None and len(value) >= list_cap
            return f"? ({'≥' if truncated else ''}{len(value)} values)"
        items = [_structure(v, depth + 1, list_cap) for v in value[:12]]
        if len(value) > 12:
            items.append(f"… ({len(value) - 12} more items)")
        return items
    return "?"


# Command options rendered on one compact line above each log query form (and removed from the JSON body).
# The extractor (>= 1.4) keeps their real logged values; older extractions show ``?``.
OPTION_KEYS = ("$db", "$readPreference", "readConcern", "writeConcern", "batchSize", "singleBatch", "limit",
               "skip", "maxTimeMS", "maxAwaitTimeMS", "allowDiskUse", "hint", "cursor", "tailable", "awaitData",
               "noCursorTimeout", "allowPartialResults", "ordered", "readOnce", "apiVersion", "apiStrict",
               "getMoreOptions")
_OPTION_LABEL = {"$readPreference": "readPreference", "getMoreOptions": "getMore"}
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:$\-]{1,128}$")


def _option_scalar(value) -> str:
    """Defence in depth: only short plain settings are echoed; anything else is ``?``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and _SAFE_TEXT.match(value):
        return value
    return "?"


def _option_text(key: str, value) -> str | None:
    if isinstance(value, dict):
        primary = {"$readPreference": "mode", "readConcern": "level"}.get(key)
        rest = []
        head = None
        for k, v in list(value.items())[:12]:
            if k == primary:
                head = _option_scalar(v)
            elif v == "?":
                continue  # opaque cluster-time payloads etc.
            elif isinstance(v, list):
                rest.append(f"{k}=[" + ", ".join(
                    ";".join(f"{a}:{_option_scalar(b)}" for a, b in item.items()) if isinstance(item, dict)
                    else _option_scalar(item) for item in v[:5]) + "]")
            elif isinstance(v, dict):
                rest.append(f"{k}={{" + ", ".join(f"{a}:{_option_scalar(b)}" for a, b in list(v.items())[:6]) + "}")
            else:
                rest.append(f"{k}={_option_scalar(v)}")
        if head is None and not rest:
            return None  # e.g. aggregate `cursor: {}`
        return " ".join(x for x in (head, f"({', '.join(rest)})" if head and rest else ", ".join(rest)) if x)
    if isinstance(value, list):
        return "[" + ", ".join(_option_scalar(v) for v in value[:6]) + "]"
    return _option_scalar(value)


def split_options(form: dict) -> tuple[str | None, dict]:
    """Return (one-line option summary, form without the option keys) for a top-level command form."""
    parts, body = [], dict(form)
    for key in OPTION_KEYS:
        if key in body:
            text = _option_text(key, body.pop(key))
            if text is not None:
                parts.append(f"`{_OPTION_LABEL.get(key, key)}: {text.replace('`', '')}`")
    return (" · ".join(parts) if parts else None), body


_ONE_LINE_MAX = 120


def _render_form(value) -> str:
    """Compact JSON: one line when short, otherwise indented."""
    one = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return one if len(one) <= _ONE_LINE_MAX else json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)


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


def _is_cluster_scope(stats_json) -> bool:
    try:
        data = json.loads(stats_json) if isinstance(stats_json, str) else stats_json
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("scope") == "cluster"


def append_reported_shapes(report_md: str, extraction: dict, query_stats_json=None) -> str:
    """Only enumerate exact hashes already mentioned in the generated report.

    Log hashes resolve to redacted sample_query_shape from the SAME node's
    extraction. True queryShapeHash resolves only to the SAME node's stats.
    Never infer equivalence between the two schemes. For missing forms, report
    the absence rather than inventing a query. Idempotent across reruns.
    """
    if _HEADING in report_md:
        return report_md
    # Hashes cited in the report text; embedded base64 images (metrics graphs) can contain
    # 8-hex-character runs and are never scanned.
    mentioned = {m.group(0).upper() for m in _HASH.finditer(_DATA_URI.sub("", report_md))}
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
    scope_label = " (cluster-wide, not node-attributed)" if _is_cluster_scope(query_stats_json) else ""
    lines = [_HEADING, "",
             "Hashes are included only if cited in this node report and found in this node's supplied evidence. "
             "Log `query_hash` and Query Shape Insights `queryShapeHash` are different identifiers; "
             "no relationship is assumed. Query literal values are replaced with `?`; command options "
             "(`$db`, read preference/concern, write concern, batchSize, singleBatch, limit, maxTimeMS, ...) "
             "are shown once on the **Options** line with their logged values. Use representative "
             "parameters and `explain(\"executionStats\")` to validate a plan.", ""]
    if not log_rows and not insight_rows:
        lines.append("No cited query hash could be resolved to a supplied redacted query form for this node.")
    for h, ns, form in sorted(set((h, ns, json.dumps(form, sort_keys=True, default=str)) for h, ns, form in log_rows))[:_MAX_ITEMS]:
        # Only structured extracted shapes are emitted; strings may contain unredacted literals.
        parsed = json.loads(form)
        lines.extend([f"#### Log query hash `{h}` — `{ns.replace('`', '')[:120]}`", ""])
        parsed = _json_shape(parsed)
        if parsed is not None:
            options, body = split_options(parsed)
            if options:
                lines.extend([f"**Options:** {options}", ""])
            rendered = _render_form(_structure(body, list_cap=_EXTRACTOR_LIST_CAP))
            lines.extend(["```json", rendered, "```", ""] if len(rendered) <= 6000 else
                         ["Redacted structural form exceeds the display limit.", ""])
        else:
            lines.extend(["Redacted structural form unavailable in the supplied extraction.", ""])
    for h, form in sorted(insight_rows)[:_MAX_ITEMS]:
        rendered = _render_form(_structure(form))
        lines.extend([f"#### Query Shape Insights `queryShapeHash` `{h}`{scope_label}", ""])
        lines.extend(["```json", rendered, "```", ""] if len(rendered) <= 6000 else
                     ["Redacted structural form exceeds the display limit.", ""])
    if log_rows and not insight_rows:
        lines.extend(["No cited 64-character Query Shape Insights `queryShapeHash` could be resolved "
                      "from this node's query-statistics file; do not relabel the 8-character log hashes.", ""])
    return _insert_ordered(report_md, SHAPES_HEADING, "\n".join(lines).rstrip())


def append_metrics_appendix(report_md: str, body_md: str) -> str:
    """Insert APPENDIX 1 (one-page node metric graphs) first in the APPENDIX section; idempotent."""
    if METRICS_HEADING in report_md:
        return report_md
    return _insert_ordered(report_md, METRICS_HEADING, METRICS_HEADING + "\n\n" + body_md.strip())


def append_advisor_appendix(report_md: str, body_md: str) -> str:
    """Insert APPENDIX 2 (Performance Advisor) between metrics and query forms; idempotent."""
    if ADVISOR_HEADING in report_md:
        return report_md
    return _insert_ordered(report_md, ADVISOR_HEADING, ADVISOR_HEADING + "\n\n" + body_md.strip())


def _insert_ordered(report_md: str, heading: str, block: str) -> str:
    """Place ``block`` (starting with ``heading``) at its fixed position in ``_ORDER``.

    Inserted ahead of the first later sub-appendix already present; otherwise after the last
    earlier one (at the next ``### APPENDIX`` heading or the end of the section).
    """
    rank = _ORDER.index(heading)
    match = _APPENDIX_RE.search(report_md)
    if match:
        tail = report_md[match.end():]
        later = [tail.index(h) for h in _ORDER[rank + 1:] if h in tail]
        if later:
            return _insert_block(report_md, block, before_pos=min(later))
        earlier = [h for h in _ORDER[:rank] if h in tail]
        if earlier:
            return _insert_block(report_md, block, after=max(earlier, key=tail.index))
    return _insert_block(report_md, block)


def _insert_block(report_md: str, block: str, before: str | None = None, after: str | None = None,
                  before_pos: int | None = None) -> str:
    """Place ``block`` inside the single ``## APPENDIX`` section (created if missing).

    ``before``/``before_pos``: insert ahead of that sub-heading (or offset in the section) when
    present; ``after``: insert after the block that starts with that sub-heading (i.e. at the next
    ``### APPENDIX`` heading or the end).
    """
    match = _APPENDIX_RE.search(report_md)
    if not match:
        return report_md.rstrip() + "\n\n" + APPENDIX_HEADING + "\n\n" + block.strip() + "\n"
    head, tail = report_md[:match.end()], report_md[match.end():]
    position = before_pos
    if position is None and before and before in tail:
        position = tail.index(before)
    elif position is None and after and after in tail:
        start = tail.index(after) + len(after)
        nxt = re.search(r"(?m)^### APPENDIX ", tail[start:])
        position = start + nxt.start() if nxt else None
    if position is None:
        return head + "\n\n" + (tail.strip() + "\n\n" if tail.strip() else "") + block.strip() + "\n"
    first, rest = tail[:position].strip(), tail[position:].strip()
    parts = [p for p in (first, block.strip(), rest) if p]
    return head + "\n\n" + "\n\n".join(parts) + "\n"
