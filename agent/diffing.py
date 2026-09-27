"""Deterministic comparison of extractions (current vs n-1 / n-8).

Implements the matching rules from references/analysis-prompt.md
("Optional historical comparison"):
  * errors matched by fingerprint (fallback: category)
  * slow ops matched by namespace + plan_summary + query_hash
  * slow_operations is an alias of slow.operations -- never summed
  * labels new / resolved / increased / decreased / stable / not comparable
The LLM receives this JSON so the comparison table uses exact numbers.
"""
from __future__ import annotations

from typing import Any


def _get(d: Any, *path, default=None):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _change(cur, base) -> dict:
    cur, base = _num(cur), _num(base)
    out = {"current": cur, "baseline": base}
    if cur is None or base is None:
        out["label"] = "not comparable"
        return out
    out["delta"] = round(cur - base, 4)
    out["pct"] = round((cur - base) / base * 100, 2) if base else None
    out["label"] = "stable" if cur == base else ("increased" if cur > base else "decreased")
    return out


def _slow_key(op: dict) -> str:
    return " | ".join(str(op.get(k) or "-") for k in ("namespace", "plan_summary", "query_hash"))


def _error_map(payload: dict) -> dict[str, dict]:
    groups = _get(payload, "error_scan", "groups", default=[]) or []
    return {str(g.get("fingerprint") or g.get("category") or "unknown"): g for g in groups}


def _slow_map(payload: dict) -> dict[str, dict]:
    ops = _get(payload, "slow", "operations")
    if ops is None:
        ops = payload.get("slow_operations", []) or []
    return {_slow_key(op): op for op in ops}


def _drivers(payload: dict) -> set[str]:
    out = set()
    dc = payload.get("driverCompatibility") or {}
    for bucket in ("distinct_compatible_drivers", "incompatible_drivers"):
        items = dc.get(bucket) or []
        if isinstance(items, dict):  # grouped by driver name
            for name, versions in items.items():
                for v in (versions if isinstance(versions, list) else [versions]):
                    ver = v.get("version") if isinstance(v, dict) else v
                    out.add(f"{name}@{ver}")
        else:
            for item in items:
                if isinstance(item, dict):
                    out.add(f"{item.get('driver') or item.get('name')}@{item.get('version')}")
    return out


def _group_delta(cur: dict[str, dict], base: dict[str, dict], limit: int = 40) -> list[dict]:
    rows = []
    for key in set(cur) | set(base):
        c, b = cur.get(key), base.get(key)
        cc, bc = (c or {}).get("count"), (b or {}).get("count")
        if c and not b:
            label = "new"
        elif b and not c:
            label = "resolved"  # absent in current extraction; not proof of a fix
        else:
            label = _change(cc, bc)["label"]
        row = {"key": key, "current_count": cc, "baseline_count": bc, "label": label}
        if c and b and _get(c, "duration_ms", "median") is not None:
            row["median_ms"] = {"current": _get(c, "duration_ms", "median"), "baseline": _get(b, "duration_ms", "median")}
        rows.append(row)
    rows.sort(key=lambda r: -max(r["current_count"] or 0, r["baseline_count"] or 0))
    return rows[:limit]


def compare(current: dict, baseline: dict, label: str, baseline_date: str) -> dict:
    cur_md, base_md = current.get("metadata", {}), baseline.get("metadata", {})
    limitations = []
    if cur_md.get("parser_version") != base_md.get("parser_version"):
        limitations.append(f"parser_version differs ({cur_md.get('parser_version')} vs {base_md.get('parser_version')})")
    if cur_md.get("slow_threshold_ms") != base_md.get("slow_threshold_ms"):
        limitations.append("slow_threshold_ms differs; slow counts are not directly comparable")
    if not _get(base_md, "time_range", "first"):
        limitations.append("baseline has no usable time range")

    signals = {
        "quality.lines_seen": _change(_get(current, "quality", "lines_seen"), _get(baseline, "quality", "lines_seen")),
        "quality.parsed_records": _change(_get(current, "quality", "parsed_records"), _get(baseline, "quality", "parsed_records")),
        "quality.skipped_records": _change(_get(current, "quality", "skipped_records"), _get(baseline, "quality", "skipped_records")),
        "quality.skipped_ratio": _change(_get(current, "quality", "skipped_ratio"), _get(baseline, "quality", "skipped_ratio")),
        "error_scan.total_error_events": _change(_get(current, "error_scan", "total_error_events"), _get(baseline, "error_scan", "total_error_events")),
        "error_scan.unique_error_groups": _change(_get(current, "error_scan", "unique_error_groups"), _get(baseline, "error_scan", "unique_error_groups")),
        "error_scan.repeated_error_groups": _change(_get(current, "error_scan", "repeated_error_groups"), _get(baseline, "error_scan", "repeated_error_groups")),
        "slow.global_stats.count": _change(_get(current, "slow", "global_stats", "count"), _get(baseline, "slow", "global_stats", "count")),
        "slow.global_stats.collscan_count": _change(_get(current, "slow", "global_stats", "collscan_count"), _get(baseline, "slow", "global_stats", "collscan_count")),
        "slow.global_stats.change_stream_count": _change(_get(current, "slow", "global_stats", "change_stream_count"), _get(baseline, "slow", "global_stats", "change_stream_count")),
        "slow.global_stats.duration_ms.median": _change(_get(current, "slow", "global_stats", "duration_ms", "median"), _get(baseline, "slow", "global_stats", "duration_ms", "median")),
        "slow.global_stats.duration_ms.max": _change(_get(current, "slow", "global_stats", "duration_ms", "max"), _get(baseline, "slow", "global_stats", "duration_ms", "max")),
        "slow.global_stats.cpuNanos.total": _change(_get(current, "slow", "global_stats", "cpuNanos", "total"), _get(baseline, "slow", "global_stats", "cpuNanos", "total")),
        "driverCompatibility.incompatible_count": _change(_get(current, "driverCompatibility", "incompatible_count"), _get(baseline, "driverCompatibility", "incompatible_count")),
    }
    for sev in sorted(set(_get(current, "summary", "severity_counts", default={}) or {}) | set(_get(baseline, "summary", "severity_counts", default={}) or {})):
        signals[f"summary.severity_counts.{sev}"] = _change(
            _get(current, "summary", "severity_counts", sev, default=0), _get(baseline, "summary", "severity_counts", sev, default=0))
    for cat in sorted(set(_get(current, "summary", "category_counts", default={}) or {}) | set(_get(baseline, "summary", "category_counts", default={}) or {})):
        signals[f"summary.category_counts.{cat}"] = _change(
            _get(current, "summary", "category_counts", cat, default=0), _get(baseline, "summary", "category_counts", cat, default=0))

    cur_drv, base_drv = _drivers(current), _drivers(baseline)
    return {
        "label": label,
        "baseline_date": baseline_date,
        "current_time_range": cur_md.get("time_range"),
        "baseline_time_range": base_md.get("time_range"),
        "comparability_limitations": limitations,
        "signals": signals,
        "error_groups": _group_delta(_error_map(current), _error_map(baseline)),
        "slow_operation_groups": _group_delta(_slow_map(current), _slow_map(baseline)),
        "slow_match_note": "Groups without query_hash are matched approximately (namespace + plan_summary).",
        "driver_versions": {"new": sorted(cur_drv - base_drv), "no_longer_seen": sorted(base_drv - cur_drv)},
        "note": "A group absent from one extraction is labelled new/resolved only relative to that extraction; it is corroborating context, not proof of remediation or causality.",
    }
