"""Deterministic, literal-free Markdown for a node's Performance Advisor data (report APPENDIX 2).

Input: the node's ``advisor/advisor.json`` as written by the agent collector
(``skills/mongodb-observability/scripts/observability.py``) or by the manual collectors
(``scripts/atlas_collect_observability.py`` / ``scripts/opsmanager_collect_observability.py``)::

    {"schema_version": 2, "source": "atlas_api_advisor" | "ops_manager_advisor", "host": ...,
     "retrievedAt": ..., "window": {...}, "truncated": bool,
     "data": [ {"suggestedIndexes": [...], "shapes": [...]} , ... ],      # API pages
     "slow_namespaces": {"namespaces": [{"namespace": ...}]}}             # Ops Manager only

A bare API page (``{"suggestedIndexes": [...], "shapes": [...]}``) is accepted too. Suggested
index keys are read from ``index: [{field: 1}, ...]`` (Atlas / Ops Manager API) or from
``indexes: [{key: {...}}]``.

Safety: only namespaces, index field names, index directions/types and numeric statistics are
rendered. Example predicates, ``raw`` operations and any other value are never printed. Namespaces
under admin/config/local and ``__mdb_internal*`` are left out. No I/O in this module.
"""
from __future__ import annotations

import math
import re

MAX_SUGGESTIONS = 30
MAX_SHAPES = 20
MAX_NAMESPACES = 30
_SECRET_KEY = re.compile(r"(?i)password|secret|credential|token|api.?key")
_INDEX_TYPES = {"text", "2d", "2dsphere", "hashed", "geoHaystack", "wildcard"}
_EXCLUDED_PREFIXES = ("admin.", "config.", "local.")
_EXCLUDED_MARKERS = ("__mdb_internal",)
_SOURCES = {"atlas_api_advisor": "Atlas Performance Advisor API",
            "ops_manager_advisor": "Ops Manager Performance Advisor API"}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _excluded(namespace) -> bool:
    ns = str(namespace or "")
    return not ns or any(m in ns for m in _EXCLUDED_MARKERS) or ns.startswith(_EXCLUDED_PREFIXES)


def _md(text, limit=120) -> str:
    """Inline-code-safe, table-safe text."""
    return str(text).replace("`", "'").replace("|", "/").replace("\n", " ")[:limit]


def _walk(obj, depth=0):
    """Yield every dict that carries advisor arrays (pages), at any depth (bounded)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("suggestedIndexes"), list) or isinstance(obj.get("shapes"), list):
            yield obj
            return
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:2000]:
            yield from _walk(value, depth + 1)


def _direction(value):
    if _num(value) in (1, -1):
        return int(value)
    if isinstance(value, str) and value in _INDEX_TYPES:
        return value
    return "?"


def _index_key(suggestion: dict) -> list[tuple[str, object]] | None:
    """Ordered [(field, direction)] from either suggestion format; None when unusable."""
    fields = []
    if isinstance(suggestion.get("index"), list):  # Atlas / Ops Manager: [{"a": 1}, {"b": -1}]
        for item in suggestion["index"]:
            if isinstance(item, dict):
                fields.extend(item.items())
    elif isinstance(suggestion.get("key"), dict):
        fields = list(suggestion["key"].items())
    out = [(str(k), _direction(v)) for k, v in fields
           if isinstance(k, str) and len(k) < 200 and not _SECRET_KEY.search(k)]
    return out or None


def _suggestions(page: dict):
    for suggestion in page.get("suggestedIndexes") or []:
        if not isinstance(suggestion, dict):
            continue
        keys = []
        if isinstance(suggestion.get("indexes"), list):  # [{"key": {...}}]
            keys = [_index_key(c) for c in suggestion["indexes"] if isinstance(c, dict)]
        else:
            keys = [_index_key(suggestion)]
        for key in keys:
            if key:
                yield suggestion, key


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.4g}"


def _key_text(key) -> str:
    return "{ " + ", ".join(f"{_md(f, 60)}: {d if isinstance(d, int) else repr(d)}" for f, d in key) + " }"


def _shape_ratio(shape: dict):
    """Max docs-examined / returned over the sampled operations (numbers only)."""
    best = None
    for op in shape.get("operations") or []:
        stats = op.get("stats") if isinstance(op, dict) else None
        if not isinstance(stats, dict):
            continue
        scanned, returned = _num(stats.get("nScanned")), _num(stats.get("nReturned"))
        if scanned is None:
            continue
        ratio = scanned / max(returned or 0, 1)
        best = ratio if best is None else max(best, ratio)
    return best


def _inventory(index_definitions) -> dict | None:
    """{namespace: [ordered key lists]} from indexDefinitions/index-definitions.json, or None."""
    if not index_definitions:
        return None
    out = {}

    def visit(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            if isinstance(obj.get("namespace"), str) and isinstance(obj.get("indexes"), list):
                for idx in obj["indexes"]:
                    if isinstance(idx, dict) and isinstance(idx.get("key"), dict):
                        out.setdefault(obj["namespace"], []).append(
                            [(str(k), _direction(v)) for k, v in idx["key"].items()])
                return
            for v in obj.values():
                visit(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj[:5000]:
                visit(v, depth + 1)
    visit(index_definitions)
    return out or None


def collect(payload) -> dict:
    """Bounded structured view of the advisor payload (also used by tests and callers)."""
    if not isinstance(payload, (dict, list)):
        return {"suggestions": [], "shapes": [], "slow_namespaces": [], "excluded": 0}
    seen, suggestions, shapes, excluded = set(), [], {}, 0
    for page in _walk(payload):
        for suggestion, key in _suggestions(page):
            ns = suggestion.get("namespace")
            if _excluded(ns):
                excluded += 1
                continue
            ident = (ns, tuple(key))
            if ident in seen:
                continue
            seen.add(ident)
            impact = suggestion.get("impact")
            suggestions.append({"namespace": str(ns), "key": key,
                                "impact": [str(x) for x in impact if isinstance(x, (str, int))]
                                if isinstance(impact, list) else [],
                                "weight": _num(suggestion.get("weight"))})
        for shape in page.get("shapes") or []:
            if not isinstance(shape, dict) or _excluded(shape.get("namespace")):
                continue
            sid = str(shape.get("id") or f"#{len(shapes) + 1}")
            shapes.setdefault(sid, {"id": sid, "namespace": str(shape.get("namespace")),
                                    "count": _num(shape.get("count")), "avg_ms": _num(shape.get("avgMs")),
                                    "inefficiency": _num(shape.get("inefficiencyScore")),
                                    "ratio_max": _shape_ratio(shape)})
    slow_ns = []
    raw_ns = payload.get("slow_namespaces") if isinstance(payload, dict) else None
    if isinstance(raw_ns, dict):
        raw_ns = raw_ns.get("namespaces")
    for item in raw_ns or []:
        ns = item.get("namespace") if isinstance(item, dict) else item
        if isinstance(ns, str) and not _excluded(ns) and ns not in slow_ns:
            slow_ns.append(ns)
    suggestions.sort(key=lambda s: (-(s["weight"] or 0), -len(s["impact"]), s["namespace"]))
    shape_rows = sorted(shapes.values(), key=lambda r: -((r["count"] or 0) * (r["avg_ms"] or 0)))
    return {"suggestions": suggestions, "shapes": shape_rows, "slow_namespaces": slow_ns, "excluded": excluded}


def available(payload) -> bool:
    """True when the payload carries at least one customer suggestion, shape or slow namespace."""
    view = collect(payload)
    return bool(view["suggestions"] or view["shapes"] or view["slow_namespaces"])


def render(payload, index_definitions=None) -> str | None:
    """Markdown body for APPENDIX 2 (heading added by the caller); None when nothing to show."""
    view = collect(payload)
    if not (view["suggestions"] or view["shapes"] or view["slow_namespaces"]):
        return None
    meta = payload if isinstance(payload, dict) else {}
    window = meta.get("window") if isinstance(meta.get("window"), dict) else {}
    source = _SOURCES.get(str(meta.get("source")), "Performance Advisor")
    span = ""
    if window.get("since") and window.get("until"):
        span = (f", window {str(window['since'])[:16].replace('T', ' ')} → "
                f"{str(window['until'])[:16].replace('T', ' ')} UTC")
    lines = [f"Source: {source}{span}"
             + (f", retrieved {str(meta['retrievedAt'])[:16].replace('T', ' ')} UTC" if meta.get("retrievedAt") else "")
             + ". Suggestions are candidates, not verified gaps: confirm with "
             "`explain(\"executionStats\")` and the existing index list before creating an index. "
             "Query values are never shown.", ""]
    if meta.get("truncated"):
        lines += ["> The advisor response was truncated (page limit); the lists below may be incomplete.", ""]
    inventory = _inventory(index_definitions)
    shape_ids = {s["id"] for s in view["shapes"]}
    if view["suggestions"]:
        rows = view["suggestions"][:MAX_SUGGESTIONS]
        lines += [f"#### Suggested indexes ({len(view['suggestions'])})", "",
                  "| # | Namespace | Suggested index key | Query shapes impacted | Weight | Same key in index list |",
                  "|---|---|---|---|---|---|"]
        for i, s in enumerate(rows, 1):
            if inventory is None:
                same = "index list not supplied"
            else:
                same = "yes — compare options" if s["key"] in inventory.get(s["namespace"], []) else "no"
            weight = _fmt(s["weight"])
            lines.append(f"| {i} | `{_md(s['namespace'])}` | `{_key_text(s['key'])}` | "
                         f"{len(s['impact']) or '—'} | {weight} | {same} |")
        if len(view["suggestions"]) > MAX_SUGGESTIONS:
            lines.append(f"\n{len(view['suggestions']) - MAX_SUGGESTIONS} more suggestion(s) not shown.")
        lines.append("")
    if view["shapes"]:
        index_of = {}
        for i, s in enumerate(view["suggestions"][:MAX_SUGGESTIONS], 1):
            for sid in s["impact"]:
                index_of.setdefault(sid, []).append(str(i))
        lines += [f"#### Query shapes behind the suggestions (top {min(MAX_SHAPES, len(view['shapes']))} "
                  "by executions × average duration)", "",
                  "| Namespace | Shape id | Executions | Avg ms | Max docs examined / returned | "
                  "Inefficiency score | Suggestion # |",
                  "|---|---|---|---|---|---|---|"]
        for r in view["shapes"][:MAX_SHAPES]:
            lines.append(f"| `{_md(r['namespace'])}` | `{_md(r['id'], 40)}` | {_fmt(r['count'])} | "
                         f"{_fmt(r['avg_ms'])} | {_fmt(r['ratio_max'])} | "
                         f"{_fmt(r['inefficiency'])} | {', '.join(index_of.get(r['id'], [])) or '—'} |")
        lines.append("")
    elif view["suggestions"] and any(s["impact"] for s in view["suggestions"]) and not shape_ids:
        lines += ["Shape details were not included in the advisor response.", ""]
    if view["slow_namespaces"]:
        ns = view["slow_namespaces"]
        lines += [f"#### Namespaces with slow queries ({len(ns)})", "",
                  ", ".join(f"`{_md(n)}`" for n in ns[:MAX_NAMESPACES])
                  + (f" … and {len(ns) - MAX_NAMESPACES} more" if len(ns) > MAX_NAMESPACES else ""), ""]
    return "\n".join(lines).rstrip()
