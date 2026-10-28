"""High resource usage investigation (node report section) from per-node Atlas metrics.

Deterministic rule evaluation over ``metrics.json`` + correlation with the occurrence
timestamps of ``extractionOccurence.json``.  The result is supplied to the model as the
``resource_investigation`` document and rendered as a fallback section when the model omits it.

Rules (thresholds configurable with ``resource_thresholds``):

A - CPU     normalized system CPU (sum of SYSTEM_NORMALIZED_CPU_* except GUEST, which Linux
            already counts in USER) > 90%, or > 70% for more than 20 consecutive minutes.
B - Memory  system memory used > 75% (used / (used + available)); context from connections
            (~1 MB each), open cursors, global lock queue and errors in the same window.
C - Disk    DISK_QUEUE_DEPTH > 10 on at least ``disk_min_points`` samples (pressure).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

CPU_COMPONENTS = ["SYSTEM_NORMALIZED_CPU_USER", "SYSTEM_NORMALIZED_CPU_KERNEL", "SYSTEM_NORMALIZED_CPU_NICE",
                  "SYSTEM_NORMALIZED_CPU_IOWAIT", "SYSTEM_NORMALIZED_CPU_IRQ", "SYSTEM_NORMALIZED_CPU_SOFTIRQ",
                  "SYSTEM_NORMALIZED_CPU_STEAL"]
MEMORY_CONTEXT = ["CONNECTIONS", "CURSORS_TOTAL_OPEN", "GLOBAL_LOCK_CURRENT_QUEUE_TOTAL",
                  "GLOBAL_LOCK_CURRENT_QUEUE_READERS", "GLOBAL_LOCK_CURRENT_QUEUE_WRITERS", "MEMORY_RESIDENT",
                  "CACHE_USED_BYTES", "CACHE_DIRTY_BYTES", "SWAP_USAGE_USED"]
CPU_CONTEXT = ["PROCESS_NORMALIZED_CPU_USER", "PROCESS_NORMALIZED_CPU_KERNEL", "SYSTEM_NORMALIZED_CPU_IOWAIT",
               "SYSTEM_NORMALIZED_CPU_STEAL", "CONNECTIONS", "GLOBAL_LOCK_CURRENT_QUEUE_TOTAL",
               "TICKETS_AVAILABLE_READS", "TICKETS_AVAILABLE_WRITE"]
DISK_CONTEXT_PROCESS = ["GLOBAL_LOCK_CURRENT_QUEUE_TOTAL", "CACHE_DIRTY_BYTES", "SYSTEM_NORMALIZED_CPU_IOWAIT",
                        "EXTRA_INFO_PAGE_FAULTS", "TICKETS_AVAILABLE_READS", "TICKETS_AVAILABLE_WRITE"]
# Extra process measurements requested (besides the graph groups) so the rules can be evaluated.
INVESTIGATION_METRICS = list(dict.fromkeys(
    CPU_COMPONENTS + ["SYSTEM_MEMORY_USED", "SYSTEM_MEMORY_AVAILABLE", "SYSTEM_MEMORY_FREE"]
    + CPU_CONTEXT + MEMORY_CONTEXT + DISK_CONTEXT_PROCESS))
DISK_METRICS = ["DISK_QUEUE_DEPTH", "DISK_PARTITION_IOPS_TOTAL", "DISK_PARTITION_LATENCY_READ",
                "DISK_PARTITION_LATENCY_WRITE"]
DEFAULT_THRESHOLDS = {"cpu_peak_percent": 90.0, "cpu_sustained_percent": 70.0, "cpu_sustained_minutes": 20,
                      "memory_percent": 75.0, "disk_queue_depth": 10.0, "disk_min_points": 3,
                      "connection_memory_mb": 1.0, "correlation_margin_minutes": 5, "top_groups": 10}
SECTION_HEADING = "## High resource usage investigation"
SECTION_RE = re.compile(r"(?im)^#{1,3}\s*High resource usage investigation\b")
_GRANULARITY = {"PT1M": 60, "PT5M": 300, "PT1H": 3600, "P1D": 86400}
_SYSTEM_NS = re.compile(r"^(admin|local|config)\.|__mdb_internal")


# ----------------------------------------------------------------------------- metrics helpers
def _ts(value):
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _walk(obj, depth=0):
    if depth > 10:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str) and isinstance(obj.get("dataPoints"), list):
            yield obj
            return
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value, depth + 1)


def series_of(obj) -> dict:
    """``{name: {"units": u, "points": {datetime: float}}}`` from any measurements envelope."""
    out: dict = {}
    for item in _walk(obj):
        entry = out.setdefault(item["name"], {"units": item.get("units") or "SCALAR", "points": {}})
        for point in item["dataPoints"]:
            if not isinstance(point, dict):
                continue
            when, value = _ts(point.get("timestamp")), point.get("value")
            if when is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
                entry["points"][when] = float(value)
    return out


def metrics_present(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(s["points"] for name, s in series_of(payload.get("data")).items())


def _step_seconds(payload) -> int:
    window = payload.get("window") or {}
    return _GRANULARITY.get(str(window.get("granularity") or "").upper(), 300)


def _runs(points, predicate, step):
    """Group consecutive samples satisfying ``predicate`` (gap <= 1.5 steps)."""
    runs, current = [], []
    for when, value in sorted(points.items()):
        if predicate(value):
            if current and (when - current[-1][0]).total_seconds() > 1.5 * step:
                runs.append(current)
                current = []
            current.append((when, value))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _value_at(series, name, when):
    pts = (series.get(name) or {}).get("points") or {}
    if not pts:
        return None
    if when in pts:
        return pts[when]
    nearest = min(pts, key=lambda t: abs((t - when).total_seconds()))
    return pts[nearest] if abs((nearest - when).total_seconds()) <= 3600 else None


def _window_max(series, name, start, end):
    pts = (series.get(name) or {}).get("points") or {}
    vals = [v for t, v in pts.items() if start <= t <= end]
    return max(vals) if vals else None


def _local(when, tz_name):
    try:
        from zoneinfo import ZoneInfo
        return when.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return None


def _episode(run, step, tz_name, metric, unit="%"):
    start, end = run[0][0], run[-1][0] + timedelta(seconds=step)
    peak_t, peak = max(run, key=lambda p: p[1])
    return {"metric": metric, "start_utc": start.isoformat(), "end_utc": end.isoformat(),
            "start_local": _local(start, tz_name), "end_local": _local(end, tz_name),
            "duration_minutes": round((end - start).total_seconds() / 60, 1), "samples": len(run),
            "peak": round(peak, 2), "peak_at_utc": peak_t.isoformat(), "unit": unit,
            "average": round(sum(v for _, v in run) / len(run), 2), "_start": start, "_end": end}


# ----------------------------------------------------------------------------- extract helpers
def _occurrence_times(occ):
    """Yield (datetime, detail) from both compact occurrence formats (UTC)."""
    if not isinstance(occ, dict):
        return
    for day, entries in occ.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key, detail in entry.items():
                m = re.fullmatch(r"(\d{2})[H:](\d{2})(?::(\d{2})(?:\.(\d{1,2}))?)?", str(key))
                if not m:
                    continue
                try:
                    when = datetime.strptime(f"{day}{m.group(1)}{m.group(2)}{m.group(3) or '00'}",
                                             "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                yield when, detail if isinstance(detail, dict) else {}


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("total", "sum", "value"):
            if isinstance(value.get(key), (int, float)):
                return float(value[key])
    return None


def _bytes_read(storage):
    if isinstance(storage, dict):
        data = storage.get("data") if isinstance(storage.get("data"), dict) else storage
        return _num(data.get("bytesRead"))
    return None


def _slow_groups(extract):
    groups = (extract.get("slow") or {}).get("operations") or extract.get("slow_operations") or []
    return [g for g in groups if isinstance(g, dict)]


def _error_groups(extract):
    groups = (extract.get("error_scan") or {}).get("groups") or []
    return [g for g in groups if isinstance(g, dict)]


def correlate(extract, start, end, top=10):
    """Slow-operation and error groups with occurrences inside [start, end] (UTC)."""
    slow, errors = [], []
    for g in _slow_groups(extract):
        ns = str(g.get("namespace") or "")
        if _SYSTEM_NS.search(ns):
            continue
        hits = [(t, d) for t, d in _occurrence_times(g.get("occurrence_timestamps")) if start <= t <= end]
        if not hits:
            continue
        def total(field):
            vals = [_num(d.get(field)) for _, d in hits]
            vals = [v for v in vals if v is not None]
            return round(sum(vals), 2) if vals else None
        reads = [_bytes_read(d.get("storage")) for _, d in hits]
        reads = [r for r in reads if r is not None]
        slow.append({"namespace": ns, "query_hash": g.get("query_hash"), "plan_summary": g.get("plan_summary"),
                     "app_names": (g.get("app_names") or [])[:5], "occurrences_in_window": len(hits),
                     "first_in_window_utc": min(t for t, _ in hits).isoformat(),
                     "last_in_window_utc": max(t for t, _ in hits).isoformat(),
                     "duration_ms_total": total("duration_ms"), "cpu_nanos_total": total("cpu_nanos"),
                     "docs_examined_total": total("docs_examined"), "keys_examined_total": total("keys_examined"),
                     "n_returned_total": total("n_returned"),
                     "storage_bytes_read_total": round(sum(reads), 0) if reads else None,
                     "collscan_group": bool(g.get("collscan_count")), "sort_stage_group": bool(g.get("sort_stage_count"))})
    for g in _error_groups(extract):
        hits = [t for t, d in _occurrence_times(g.get("occurrence_timestamps")) if start <= t <= end
                for _ in range(int(_num(d.get("occur")) or 1))]
        if hits:
            errors.append({"fingerprint": str(g.get("fingerprint") or "")[:160],
                           "sample_message": str(g.get("sample_message") or "")[:240],
                           "components": (g.get("components") or [])[:5], "severities": g.get("severities"),
                           "occurrences_in_window": len(hits)})
    slow.sort(key=lambda r: ((r["cpu_nanos_total"] or 0) / 1e6 + (r["duration_ms_total"] or 0)), reverse=True)
    errors.sort(key=lambda r: r["occurrences_in_window"], reverse=True)
    return {"slow_operations": slow[:top], "slow_groups_in_window": len(slow),
            "slow_occurrences_in_window": sum(r["occurrences_in_window"] for r in slow),
            "errors": errors[:top], "error_groups_in_window": len(errors),
            "error_occurrences_in_window": sum(r["occurrences_in_window"] for r in errors)}


# ----------------------------------------------------------------------------- rules
def _cpu_total(series):
    total: dict = {}
    for name in CPU_COMPONENTS:
        for when, value in ((series.get(name) or {}).get("points") or {}).items():
            total[when] = total.get(when, 0.0) + value
    return total


def _memory_percent(series):
    used = (series.get("SYSTEM_MEMORY_USED") or {}).get("points") or {}
    avail = (series.get("SYSTEM_MEMORY_AVAILABLE") or {}).get("points") or {}
    return {t: 100.0 * u / (u + avail[t]) for t, u in used.items() if t in avail and (u + avail[t]) > 0}


def analyze(metrics_payload, extract, config=None) -> dict:
    config = config or {}
    th = {**DEFAULT_THRESHOLDS, **(config.get("resource_thresholds") or {})}
    tz_name = config.get("timezone") or "UTC"
    step = _step_seconds(metrics_payload)
    margin = timedelta(minutes=float(th["correlation_margin_minutes"]))
    series = series_of((metrics_payload or {}).get("data"))
    top = int(th["top_groups"])
    window = (metrics_payload or {}).get("window") or {}
    result = {"thresholds": th, "granularity_seconds": step, "timezone": tz_name,
              "metrics_window": {k: window.get(k) for k in ("since", "until", "granularity") if k in window},
              "notes": []}
    if step >= 3600:
        result["notes"].append("Metrics are hourly averages: short peaks may be hidden and each sample "
                               "covers one hour of the sustained-CPU rule.")

    def finish(episode, ctx_names):
        start, end = episode.pop("_start"), episode.pop("_end")
        episode["context_max_in_window"] = {n: round(v, 2) for n in ctx_names
                                            if (v := _window_max(series, n, start, end)) is not None}
        episode["correlated"] = correlate(extract or {}, start - margin, end + margin, top)
        return episode

    # A - CPU
    cpu = _cpu_total(series)
    if not cpu:
        result["cpu"] = {"status": "no_data", "rule": "normalized system CPU metrics missing"}
    else:
        peaks = [finish(_episode(r, step, tz_name, "normalized_system_cpu"), CPU_CONTEXT)
                 for r in _runs(cpu, lambda v: v > th["cpu_peak_percent"], step)]
        sustained = []
        for r in _runs(cpu, lambda v: v > th["cpu_sustained_percent"], step):
            ep = _episode(r, step, tz_name, "normalized_system_cpu")
            if ep["duration_minutes"] > th["cpu_sustained_minutes"]:
                sustained.append(finish(ep, CPU_CONTEXT))
        result["cpu"] = {"status": "triggered" if peaks or sustained else "not_triggered",
                         "max_percent": round(max(cpu.values()), 2),
                         "max_at_utc": max(cpu, key=cpu.get).isoformat(),
                         "peak_episodes_over_%g" % th["cpu_peak_percent"]: peaks,
                         "sustained_episodes_over_%g_for_%gmin" % (th["cpu_sustained_percent"],
                                                                   th["cpu_sustained_minutes"]): sustained}
    # B - Memory
    mem = _memory_percent(series)
    if not mem:
        result["memory"] = {"status": "no_data", "rule": "SYSTEM_MEMORY_USED/AVAILABLE missing"}
    else:
        episodes = []
        for r in _runs(mem, lambda v: v > th["memory_percent"], step):
            ep = finish(_episode(r, step, tz_name, "system_memory_used_percent"), MEMORY_CONTEXT)
            peak_t = _ts(ep["peak_at_utc"])
            used, avail = (_value_at(series, "SYSTEM_MEMORY_USED", peak_t),
                           _value_at(series, "SYSTEM_MEMORY_AVAILABLE", peak_t))
            conns = _value_at(series, "CONNECTIONS", peak_t)
            total_mb = (used + avail) / 1024 if used is not None and avail is not None else None
            conn_mb = conns * th["connection_memory_mb"] if conns is not None else None
            ep["at_peak"] = {"system_memory_used_mb": round(used / 1024, 1) if used is not None else None,
                             "system_memory_total_estimate_mb": round(total_mb, 1) if total_mb else None,
                             "connections": conns, "connection_memory_estimate_mb": conn_mb,
                             "connection_memory_share_percent": round(100 * conn_mb / total_mb, 1)
                             if conn_mb is not None and total_mb else None,
                             "cursors_open": _value_at(series, "CURSORS_TOTAL_OPEN", peak_t),
                             "global_lock_queue_total": _value_at(series, "GLOBAL_LOCK_CURRENT_QUEUE_TOTAL", peak_t)}
            episodes.append(ep)
        result["memory"] = {"status": "triggered" if episodes else "not_triggered",
                            "max_percent": round(max(mem.values()), 2),
                            "max_at_utc": max(mem, key=mem.get).isoformat(),
                            "method": "SYSTEM_MEMORY_USED / (SYSTEM_MEMORY_USED + SYSTEM_MEMORY_AVAILABLE); approximate",
                            "episodes_over_%g" % th["memory_percent"]: episodes}
    # C - Disk
    disks = (metrics_payload or {}).get("disk_measurements") or []
    partitions = []
    for disk in disks if isinstance(disks, list) else []:
        s = series_of(disk.get("data"))
        queue = (s.get("DISK_QUEUE_DEPTH") or {}).get("points") or {}
        if not queue:
            continue
        over = [t for t, v in queue.items() if v > th["disk_queue_depth"]]
        entry = {"partition": disk.get("partitionName"), "max_queue_depth": round(max(queue.values()), 2),
                 "max_at_utc": max(queue, key=queue.get).isoformat(), "samples_over_threshold": len(over),
                 "pressure": len(over) >= int(th["disk_min_points"]), "episodes": []}
        if entry["pressure"]:
            merged = {**series, **{k: v for k, v in s.items()}}
            for r in _runs(queue, lambda v: v > th["disk_queue_depth"], step):
                ep = _episode(r, step, tz_name, "disk_queue_depth", unit="")
                start, end = ep["_start"], ep["_end"]
                ctx = {n: round(v, 2) for n in DISK_METRICS[1:] + DISK_CONTEXT_PROCESS
                       if (v := _window_max(merged, n, start, end)) is not None}
                ep = finish(ep, [])
                ep["context_max_in_window"] = ctx
                ep["correlated"]["slow_operations"].sort(
                    key=lambda r: ((r["storage_bytes_read_total"] or 0) / 1e6 + (r["docs_examined_total"] or 0) / 1e3),
                    reverse=True)
                entry["episodes"].append(ep)
        partitions.append(entry)
    if not partitions:
        result["disk"] = {"status": "no_data", "rule": "DISK_QUEUE_DEPTH not collected for this node"}
    else:
        result["disk"] = {"status": "triggered" if any(p["pressure"] for p in partitions) else "not_triggered",
                          "partitions": partitions}
    result["triggered"] = [k for k in ("cpu", "memory", "disk") if result[k]["status"] == "triggered"]
    return result


def episode_windows(analysis, margin_minutes=None):
    """[(start, end)] of every triggered episode (with correlation margin), for occurrence trimming."""
    margin = timedelta(minutes=float(margin_minutes if margin_minutes is not None else
                                     (analysis.get("thresholds") or {}).get("correlation_margin_minutes", 5)))
    out = []

    def visit(obj):
        if isinstance(obj, dict):
            if "start_utc" in obj and "end_utc" in obj:
                out.append((_ts(obj["start_utc"]) - margin, _ts(obj["end_utc"]) + margin))
            for v in obj.values():
                visit(v)
        elif isinstance(obj, list):
            for v in obj:
                visit(v)
    visit(analysis)
    return out


def trim_occurrences(extract, windows, keep_outside, max_groups=None):
    """Copy of the occurrence extract keeping every occurrence inside ``windows`` and at most
    ``keep_outside`` other occurrences per group; optionally cap groups (window groups first)."""
    p = json.loads(json.dumps(extract))
    trimmed_total = 0

    def in_windows(t):
        return any(a <= t <= b for a, b in windows)

    def trim_group(g):
        nonlocal trimmed_total
        occ = g.get("occurrence_timestamps")
        if not isinstance(occ, dict):
            return False
        kept, outside, dropped, hit = {}, 0, 0, False
        for day, entries in occ.items():
            for entry in entries if isinstance(entries, list) else []:
                times = list(_occurrence_times({day: [entry]}))
                inside = any(in_windows(t) for t, _ in times)
                if inside or outside < keep_outside:
                    kept.setdefault(day, []).append(entry)
                    outside += 0 if inside else 1
                    hit = hit or inside
                else:
                    dropped += 1
        g["occurrence_timestamps"] = kept
        if dropped:
            g["occurrences_trimmed"] = dropped
            trimmed_total += dropped
        return hit

    def cap(groups):
        flags = [trim_group(g) if isinstance(g, dict) else False for g in groups]
        if max_groups is None or len(groups) <= max_groups:
            return groups
        first = [g for g, f in zip(groups, flags) if f]
        rest = [g for g, f in zip(groups, flags) if not f]
        return (first + rest)[:max(max_groups, len(first))]

    if isinstance((p.get("slow") or {}).get("operations"), list):
        p["slow"]["operations"] = cap(p["slow"]["operations"])
    if isinstance((p.get("error_scan") or {}).get("groups"), list):
        p["error_scan"]["groups"] = cap(p["error_scan"]["groups"])
    if isinstance(p.get("repeated_errors"), list):
        p["repeated_errors"] = cap(p["repeated_errors"])
    notes = p.setdefault("quality", {}).setdefault("notes", [])
    if "slow_operations" in p and "slow" in p:
        p.pop("slow_operations")
        notes.append("Pipeline note: `slow_operations` alias removed to fit context; use `slow.operations`.")
    if max_groups is not None:
        p.pop("trends", None)
        notes.append(f"Pipeline note: groups capped at {max_groups} (resource-episode groups kept first), trends removed.")
    notes.append(f"Pipeline note: occurrence_timestamps kept in full inside high-resource episode windows; "
                 f"{trimmed_total} occurrence entries outside those windows removed (at most {keep_outside} "
                 "kept per group). `occurrences_trimmed` gives the removed count per group; group counts are unchanged.")
    return p


# ----------------------------------------------------------------------------- fallback markdown
def _fmt_corr(corr):
    lines = []
    for r in corr.get("slow_operations", [])[:5]:
        lines.append(f"  - Slow `{r['namespace']}` hash `{r.get('query_hash')}` ({r.get('plan_summary')}): "
                     f"{r['occurrences_in_window']} occurrence(s) {r['first_in_window_utc'][11:16]}–"
                     f"{r['last_in_window_utc'][11:16]} UTC, total {r.get('duration_ms_total')} ms, "
                     f"docs examined {r.get('docs_examined_total')}")
    for r in corr.get("errors", [])[:3]:
        lines.append(f"  - Error/warning `{r['fingerprint'][:80]}`: {r['occurrences_in_window']} occurrence(s)")
    if not lines:
        lines.append("  - No slow-operation or error occurrence in the extract falls inside this window; "
                     "source not identified from the available evidence.")
    return lines


def render_markdown(analysis) -> str:
    th = analysis["thresholds"]
    out = [SECTION_HEADING, "",
           "Deterministic evaluation of the node metrics; causes are plausible correlations with log "
           "occurrences in the same window, not confirmed root causes.", ""]
    for key, title in (("cpu", "A — CPU"), ("memory", "B — Memory"), ("disk", "C — Disk")):
        sec = analysis.get(key) or {}
        out.extend([f"### {title}", ""])
        if sec.get("status") == "no_data":
            out.extend([f"Not evaluated: {sec.get('rule')}.", ""])
            continue
        if key == "disk":
            for part in sec["partitions"]:
                out.append(f"- Partition `{part['partition']}`: max queue depth {part['max_queue_depth']}, "
                           f"{part['samples_over_threshold']} sample(s) over {th['disk_queue_depth']:g} — "
                           + ("**pressure**" if part["pressure"] else "no pressure"))
                for ep in part["episodes"]:
                    out.append(f"  - {ep['start_utc'][:16]}–{ep['end_utc'][11:16]} UTC, peak {ep['peak']}")
                    out.extend("  " + x for x in _fmt_corr(ep["correlated"]))
            out.append("")
            continue
        out.append(f"Max {sec['max_percent']}% at {sec['max_at_utc'][:16]} UTC — "
                   + ("**threshold exceeded**" if sec["status"] == "triggered" else "threshold not reached") + ".")
        out.append("")
        for label, episodes in sec.items():
            if not isinstance(episodes, list):
                continue
            for ep in episodes:
                out.append(f"- {label.replace('_', ' ')}: {ep['start_utc'][:16]}–{ep['end_utc'][11:16]} UTC "
                           f"({ep.get('start_local') or ''}), {ep['duration_minutes']} min, peak {ep['peak']}%, "
                           f"avg {ep['average']}%")
                if ep.get("at_peak"):
                    a = ep["at_peak"]
                    out.append(f"  - At peak: {a.get('connections')} connections ≈ {a.get('connection_memory_estimate_mb')} MB "
                               f"({a.get('connection_memory_share_percent')}% of RAM), {a.get('cursors_open')} open cursors, "
                               f"global lock queue {a.get('global_lock_queue_total')}")
                out.extend(_fmt_corr(ep["correlated"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
