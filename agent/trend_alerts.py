"""New or sharply increasing problems versus n-1 AND n-8 (cluster summary section).

Deterministic and model-independent. For every node that has BOTH baselines (n-1 = previous log
day, n-8 = one week before, ``extractionshort.json``) the current day is compared with each
baseline and an item is flagged when it is *bad* and either

* **new**: absent from both baselines, or
* **sharp increase**: its current count is at least ``1 + increase_pct/100`` times (default
  +200 %, i.e. 3×) the count in n-1 or in n-8, and grew by at least ``min_abs_increase``
  (default 5) occurrences, so 1 → 3 events is not reported as a trend. New items have no floor.

"Bad" items:

* slow-query groups (namespace + plan_summary + query_hash) that are **unindexed** (COLLSCAN),
  **poorly indexed** (max docs-examined/returned ≥ ``ratio_threshold``, default 100) or use an
  **in-memory sort**; change-stream groups are never "unindexed" (they read the oplog);
* error groups (fingerprint, else category);
* aggregate signals: slow-operation count, COLLSCAN count, error events, distinct error groups,
  error categories, F/E/W severities, slow CPU total, slow median/max duration, incompatible drivers;
* incompatible driver versions not seen in either baseline.

Namespaces under admin/config/local and ``__mdb_internal*`` are skipped, as are error groups and
drivers that only concern platform-managed components. Only identifiers, counts and redacted
templates leave this module (never message text with literals).
"""
from __future__ import annotations

import re

from . import diffing

SECTION_TITLE = "New or sharply increasing issues (vs n-1 and n-8)"
SECTION_HEADING = "## " + SECTION_TITLE
SECTION_RE = re.compile(r"(?im)^#{1,3}\s*New or sharply increasing issues\b.*$")
DEFAULTS = {"increase_pct": 200, "min_abs_increase": 5, "ratio_threshold": 100, "max_items": 40}
_EXCLUDED_PREFIXES = ("admin.", "config.", "local.")
_CS_ALLOWED = {"admin.$cmd.aggregate"}
_PLATFORM_TEXT = re.compile(
    r"\binternal\b|atlas[\s-]+managed|\b(?:mongot|automation[\s-]+agent|monitoring[\s-]+module|mms-automation"
    r"|mongodb-mms|atlas[\s-]+(?:automation|monitoring)|atlas[\s-]+search[\s-]+(?:java[\s-]+)?driver)\b"
    r"|__mdb_internal", re.I)
SIGNALS = {
    "slow.global_stats.count": "slow operations",
    "slow.global_stats.collscan_count": "COLLSCAN (unindexed) slow operations",
    "slow.global_stats.cpuNanos.total": "CPU time of slow operations (ns)",
    "slow.global_stats.duration_ms.median": "median slow-operation duration (ms)",
    "slow.global_stats.duration_ms.max": "max slow-operation duration (ms)",
    "error_scan.total_error_events": "error/warning events",
    "error_scan.unique_error_groups": "distinct error groups",
    "error_scan.repeated_error_groups": "repeated error groups",
    "driverCompatibility.incompatible_count": "incompatible driver connections",
    "summary.severity_counts.F": "fatal-severity log lines",
    "summary.severity_counts.E": "error-severity log lines",
    "summary.severity_counts.W": "warning-severity log lines",
}
# Signals that are not occurrence counts: the absolute floor does not apply.
_NOT_COUNTS = {"slow.global_stats.cpuNanos.total", "slow.global_stats.duration_ms.median",
               "slow.global_stats.duration_ms.max"}


def settings(config: dict | None) -> dict:
    config = config or {}
    out = dict(DEFAULTS)
    for key, conf_key in (("increase_pct", "trend_increase_pct"), ("min_abs_increase", "trend_min_abs_increase"),
                          ("ratio_threshold", "trend_ratio_threshold")):
        value = config.get(conf_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _excluded_ns(ns, change_stream=False) -> bool:
    ns = str(ns or "")
    if "__mdb_internal" in ns:
        return True
    if change_stream and ns in _CS_ALLOWED:
        return False
    return ns.startswith(_EXCLUDED_PREFIXES)


def _contains_key(value, key, depth=0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_contains_key(v, key, depth + 1) for v in value)
    return False


def _is_change_stream(op: dict) -> bool:
    return _contains_key(op.get("sample_query_shape"), "$changeStream") or \
        "$changeStream" in str(op.get("sample_message") or "")


def _stat(d, key, field):
    v = d.get(key)
    return _num(v.get(field)) if isinstance(v, dict) else None


def _slow_problems(op: dict, ratio_threshold) -> list[str]:
    problems = []
    plan = str(op.get("plan_summary") or "").upper()
    if not _is_change_stream(op) and ((_num(op.get("collscan_count")) or 0) > 0 or "COLLSCAN" in plan):
        problems.append("unindexed (COLLSCAN)")
    ratio = _num(op.get("examined_returned_ratio_max"))
    if ratio is not None and ratio >= ratio_threshold and "COLLSCAN" not in plan:
        problems.append(f"poorly indexed (docs examined/returned up to {ratio:g})")
    if (_num(op.get("sort_stage_count")) or 0) > 0:
        problems.append("in-memory sort")
    return problems


def _ratio(cur, base):
    if base in (None, 0) or cur is None:
        return None
    return cur / base


def _assess(cur, bases: dict, cfg, counts=True) -> dict | None:
    """Return the trigger for one item, or None. ``bases``: label -> baseline value or None (absent)."""
    factor = 1 + cfg["increase_pct"] / 100.0
    floor = cfg["min_abs_increase"] if counts else 0
    if cur is None or cur <= 0:
        return None
    present = {k: v for k, v in bases.items() if v is not None}
    if not present or all((v or 0) == 0 for v in present.values()):
        return {"trigger": "new", "vs": sorted(bases)}
    hits = []
    for label, base in sorted(present.items()):
        if base == 0:
            hits.append(label)
        elif cur >= factor * base and cur - base >= floor:
            hits.append(label)
    if not hits:
        return None
    pct = {label: (round((cur - base) / base * 100) if base else None) for label, base in present.items()}
    return {"trigger": "increase", "vs": hits, "pct": pct}


def _redacted_template(text) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    t = re.sub(r"(['\"]).*?\1", r"\1?\1", text)
    t = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<ip>", t)
    t = re.sub(r"[0-9a-fA-F]{8,}", "#", t)
    t = re.sub(r"\b\d+(?:\.\d+)?\b", "#", t)
    t = re.sub(r"\{.*\}", "{…}", t)
    return re.sub(r"\s+", " ", t).strip()[:140]


def _incompatible(payload) -> set[str]:
    dc = (payload or {}).get("driverCompatibility") or {}
    items = dc.get("incompatible_drivers") or []
    out = set()
    if isinstance(items, dict):
        for name, versions in items.items():
            for v in (versions if isinstance(versions, list) else [versions]):
                out.add(f"{name}@{v.get('version') if isinstance(v, dict) else v}")
    else:
        for item in items:
            if isinstance(item, dict):
                out.add(f"{item.get('driver') or item.get('name')}@{item.get('version')}")
    return {d for d in out if not _PLATFORM_TEXT.search(d)}


def _hours(payload) -> float | None:
    from datetime import datetime
    tr = ((payload or {}).get("metadata") or {}).get("time_range") or {}
    try:
        a = datetime.fromisoformat(str(tr.get("first")).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(tr.get("last")).replace("Z", "+00:00"))
        return round((b - a).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def node_alerts(node: str, log_name: str, current: dict, baselines: dict, config=None) -> dict:
    """``baselines``: {"n-1": (date, payload|None), "n-8": (date, payload|None)}."""
    cfg = settings(config)
    missing = [label for label in ("n-1", "n-8") if not (baselines.get(label) or (None, None))[1]]
    out = {"node": node, "log_name": log_name,
           "baseline_dates": {k: v[0] for k, v in baselines.items() if v}, "status": "compared"}
    if missing:
        out.update(status="not_compared", missing_baselines=missing)
        return out
    payloads = {label: baselines[label][1] for label in ("n-1", "n-8")}
    limitations = []
    for label, base in payloads.items():
        cmp_ = diffing.compare(current, base, label, baselines[label][0])
        limitations += [f"{label}: {x}" for x in cmp_["comparability_limitations"]]
    cur_h = _hours(current)
    for label, base in payloads.items():
        h = _hours(base)
        if cur_h and h is not None and h < 0.5 * cur_h:
            limitations.append(f"{label} log covers {h} h vs {cur_h} h for the current day; increases may be overstated")
    cap_note = []
    for label, base in payloads.items():
        if len(diffing._slow_map(base)) >= 50:
            cap_note.append(label)
    if cap_note:
        limitations.append("slow-operation list capped at 50 groups in " + ", ".join(cap_note) +
                           "; a 'new' low-volume group may have existed below that cap")
    items = []
    # Slow-query groups
    cur_slow = diffing._slow_map(current)
    base_slow = {label: diffing._slow_map(p) for label, p in payloads.items()}
    for key, op in cur_slow.items():
        cs = _is_change_stream(op)
        if _excluded_ns(op.get("namespace"), cs):
            continue
        problems = _slow_problems(op, cfg["ratio_threshold"])
        if not problems:
            continue
        counts = {label: _num((m.get(key) or {}).get("count")) if key in m else None for label, m in base_slow.items()}
        hit = _assess(_num(op.get("count")), counts, cfg)
        median = {"current": _stat(op, "duration_ms", "median"),
                  **{label: _stat(m[key], "duration_ms", "median") for label, m in base_slow.items() if key in m}}
        if hit is None:  # count stable but latency of a bad query tripled
            lat = _assess(median["current"], {l: median.get(l) for l in base_slow if key in base_slow[l]}, cfg, counts=False)
            if lat and lat["trigger"] == "increase":
                hit = {**lat, "metric": "median duration (ms)"}
        if hit is None:
            continue
        items.append({"kind": "slow_query", "node": node, "namespace": op.get("namespace"),
                      "plan_summary": str(op.get("plan_summary") or "")[:80], "query_hash": op.get("query_hash"),
                      "problems": problems, "current": op.get("count"), "baselines": counts,
                      "median_ms": median, **hit})
    # Error groups
    cur_err = diffing._error_map(current)
    base_err = {label: diffing._error_map(p) for label, p in payloads.items()}
    for key, group in cur_err.items():
        text = " ".join(str(x) for x in (group.get("category"), group.get("components"), group.get("sample_message")))
        if _PLATFORM_TEXT.search(text):
            continue
        ns = ((group.get("operation_details") or {}).get("namespaces") or [None])
        if ns and all(_excluded_ns(n) for n in ns if n) and any(ns):
            continue
        counts = {label: _num((m.get(key) or {}).get("count")) if key in m else None for label, m in base_err.items()}
        hit = _assess(_num(group.get("count")), counts, cfg)
        if hit is None:
            continue
        items.append({"kind": "error_group", "node": node, "fingerprint": key,
                      "category": group.get("category"), "severities": group.get("severities"),
                      "template": _redacted_template(group.get("sample_message")),
                      "current": group.get("count"), "baselines": counts, **hit})
    # Aggregate signals and error categories
    cmp_by = {label: diffing.compare(current, p, label, baselines[label][0])["signals"] for label, p in payloads.items()}
    names = dict(SIGNALS)
    for label in cmp_by:
        for sig in cmp_by[label]:
            if sig.startswith("summary.category_counts.") and not _PLATFORM_TEXT.search(sig):
                names.setdefault(sig, "log category " + sig.rsplit(".", 1)[1])
    for sig, label_text in names.items():
        cur = next((cmp_by[l][sig]["current"] for l in cmp_by if sig in cmp_by[l]), None)
        if cur is None:
            continue
        bases = {l: cmp_by[l][sig]["baseline"] if sig in cmp_by[l] else None for l in cmp_by}
        hit = _assess(_num(cur), bases, cfg, counts=sig not in _NOT_COUNTS)
        if hit is None:
            continue
        items.append({"kind": "signal", "node": node, "signal": sig, "label": label_text,
                      "current": cur, "baselines": bases, **hit})
    # New incompatible driver versions (absent from both baselines)
    new_drivers = sorted(_incompatible(current) - _incompatible(payloads["n-1"]) - _incompatible(payloads["n-8"]))
    for drv in new_drivers[:10]:
        items.append({"kind": "driver", "node": node, "driver": drv, "trigger": "new", "vs": ["n-1", "n-8"]})
    rank = {"slow_query": 0, "error_group": 1, "driver": 2, "signal": 3}
    items.sort(key=lambda i: (rank[i["kind"]], i["trigger"] != "new", -(_num(i.get("current")) or 0)))
    out.update(items=items[:cfg["max_items"]], items_total=len(items), comparability_limitations=limitations)
    return out


def cluster_alerts(per_node: list[dict], config=None) -> dict:
    cfg = settings(config)
    compared = [n for n in per_node if n["status"] == "compared"]
    return {"status": "available" if compared else "unavailable",
            "thresholds": {"increase_pct": cfg["increase_pct"], "min_abs_increase": cfg["min_abs_increase"],
                           "poorly_indexed_ratio": cfg["ratio_threshold"]},
            "nodes_compared": [f"{n['node']}/{n['log_name']}" for n in compared],
            "nodes_not_compared": [{"node": f"{n['node']}/{n['log_name']}", "missing": n.get("missing_baselines")}
                                   for n in per_node if n["status"] != "compared"],
            "nodes": per_node}


def _fmt(v):
    if v is None:
        return "absent"
    if isinstance(v, float):
        return f"{v:,.4g}" if abs(v) < 1e6 else f"{v:,.0f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def _change(item) -> str:
    bases = item.get("baselines") or {}
    if item["trigger"] == "new":
        if bases and all(v == 0 for v in bases.values()):
            return "**new** (0 in n-1 and n-8)"
        return "**new** (absent from n-1 and n-8)"
    pct = item.get("pct") or {}
    parts = [f"+{pct[l]}% vs {l}" for l in item["vs"] if pct.get(l) is not None]
    parts += [f"new vs {l} (was 0)" for l in item["vs"] if pct.get(l) is None]
    context = [f"{'+' if pct[l] >= 0 else ''}{pct[l]}% vs {l}" for l in sorted(pct)
               if l not in item["vs"] and pct[l] is not None]
    context += [f"absent from {l}" for l, v in sorted(bases.items()) if v is None]
    text = "**" + "; ".join(parts) + "**"
    if context:
        text += " (" + "; ".join(context) + ")"
    return text + (f", {item['metric']}" if item.get("metric") else "")


def _cell(text, limit=120):
    return str(text).replace("|", "/").replace("`", "'").replace("\n", " ")[:limit]


def render_markdown(alerts: dict) -> str | None:
    """The cluster-summary section, or None when no node has both baselines."""
    if alerts.get("status") != "available":
        return None
    t = alerts["thresholds"]
    lines = [SECTION_HEADING, "",
             f"Compared with the previous day (n-1) and one week earlier (n-8) on: "
             f"{', '.join('`' + _cell(n, 160) + '`' for n in alerts['nodes_compared'])}. "
             f"Listed: problems absent from both baselines (**new**) and problems at least "
             f"+{t['increase_pct']:g}% ({1 + t['increase_pct'] / 100:g}×) above n-1 or n-8 with at least "
             f"{t['min_abs_increase']:g} more occurrences. Problem queries are COLLSCAN (unindexed), docs "
             f"examined/returned ≥ {t['poorly_indexed_ratio']:g} (poorly indexed) or in-memory sorts.", ""]
    if alerts["nodes_not_compared"]:
        lines += ["Not compared (a baseline is missing): " + ", ".join(
            f"`{_cell(n['node'], 160)}` (missing {', '.join(n['missing'] or [])})" for n in alerts["nodes_not_compared"]) + ".", ""]
    rows = [i for n in alerts["nodes"] if n["status"] == "compared" for i in n.get("items", [])]
    queries = [i for i in rows if i["kind"] == "slow_query"]
    errors = [i for i in rows if i["kind"] == "error_group"]
    others = [i for i in rows if i["kind"] in ("signal", "driver")]
    if not rows:
        lines += ["No new or sharply increasing problem was detected on the compared nodes.", ""]
    if queries:
        lines += ["### Unindexed or poorly indexed queries", "",
                  "| Node | Namespace | Query hash | Problem | Today | n-1 | n-8 | Change |",
                  "|---|---|---|---|---|---|---|---|"]
        for i in queries:
            lines.append(f"| `{_cell(i['node'], 80)}` | `{_cell(i['namespace'])}` | `{_cell(i.get('query_hash') or '—', 20)}` | "
                         f"{_cell(', '.join(i['problems']), 160)} | {_fmt(i['current'])} | {_fmt(i['baselines'].get('n-1'))} | "
                         f"{_fmt(i['baselines'].get('n-8'))} | {_change(i)} |")
        lines.append("")
    if errors:
        lines += ["### Errors and warnings", "",
                  "| Node | Category | Fingerprint | Redacted message | Today | n-1 | n-8 | Change |",
                  "|---|---|---|---|---|---|---|---|"]
        for i in errors:
            lines.append(f"| `{_cell(i['node'], 80)}` | {_cell(i.get('category') or '—', 60)} | `{_cell(i['fingerprint'], 40)}` | "
                         f"{_cell(i.get('template') or '—', 140)} | {_fmt(i['current'])} | {_fmt(i['baselines'].get('n-1'))} | "
                         f"{_fmt(i['baselines'].get('n-8'))} | {_change(i)} |")
        lines.append("")
    if others:
        lines += ["### Other signals", "", "Totals cover every log line of the node.", "",
                  "| Node | Signal | Today | n-1 | n-8 | Change |", "|---|---|---|---|---|---|"]
        for i in others:
            if i["kind"] == "driver":
                lines.append(f"| `{_cell(i['node'], 80)}` | incompatible driver `{_cell(i['driver'], 80)}` | seen | absent | absent | **new** |")
            else:
                lines.append(f"| `{_cell(i['node'], 80)}` | {_cell(i['label'], 80)} | {_fmt(i['current'])} | "
                             f"{_fmt(i['baselines'].get('n-1'))} | {_fmt(i['baselines'].get('n-8'))} | {_change(i)} |")
        lines.append("")
    hidden = sum(n.get("items_total", 0) - len(n.get("items", [])) for n in alerts["nodes"] if n["status"] == "compared")
    if hidden:
        lines += [f"{hidden} lower-ranked item(s) not shown (see `trend-alerts.json`).", ""]
    limits = sorted({f"`{_cell(n['node'], 80)}`: {_cell(x, 200)}" for n in alerts["nodes"]
                     if n["status"] == "compared" for x in n.get("comparability_limitations", [])})
    if limits:
        lines += ["Comparability limits: " + "; ".join(limits) + ".", ""]
    lines.append("A new or increased item shows a change versus the baselines; it is not proof of cause or impact. "
                 "Confirm an index gap with `explain(\"executionStats\")` and the existing index list.")
    return "\n".join(lines).rstrip() + "\n"


def model_view(alerts: dict) -> dict:
    """Bounded copy for the cluster model (no message templates beyond the redacted ones)."""
    view = {k: alerts[k] for k in ("status", "thresholds", "nodes_compared", "nodes_not_compared")}
    view["items"] = [{k: v for k, v in i.items() if k not in ("median_ms",)}
                     for n in alerts["nodes"] if n["status"] == "compared" for i in n.get("items", [])][:80]
    return view


def place_section(summary_md: str, section_md: str) -> str:
    """Replace a model-written copy of the section, or insert it right after the executive summary."""
    lines = summary_md.splitlines()
    start = next((i for i, l in enumerate(lines) if SECTION_RE.match(l)), None)
    if start is not None:
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        end = next((j for j in range(start + 1, len(lines))
                    if re.match(r"^#{1,%d}\s" % level, lines[j])), len(lines))
        del lines[start:end]
    text = "\n".join(lines)
    m = re.search(r"(?im)^#{1,2}\s*Executive summary\b.*$", text)
    if m:
        nxt = re.search(r"(?m)^#{1,2}\s", text[m.end():])
        pos = m.end() + nxt.start() if nxt else len(text)
        return text[:pos].rstrip() + "\n\n" + section_md.strip() + "\n\n" + text[pos:].lstrip()
    return text.rstrip() + "\n\n" + section_md.strip() + "\n"
