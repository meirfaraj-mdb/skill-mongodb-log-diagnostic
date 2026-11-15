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

Evaluation basis depends on the metrics granularity:

* seconds / minutes (PT10S, PT1M, PT5M ...): every rule uses the average series; MAX_* series
  (MAX_SYSTEM_NORMALIZED_CPU_*, MAX_SYSTEM_MEMORY_USED, MAX_DISK_QUEUE_DEPTH) are context only.
* hours / days (PT1H, P1D: log days older than the fine-grained retention): an hourly average hides
  short peaks, so the peak rules (CPU > 90%, memory > 75%, disk queue > 10) are evaluated on the MAX_*
  series (the peak inside each sample). The average series is still used: the sustained-CPU rule
  stays on it, and an *average spike* (sample >= ``avg_spike_ratio`` x the day median and at least
  ``avg_spike_min_delta_*`` above it) marks load sustained over the sample. Each max-basis episode
  carries ``confidence``: ``sustained_load`` (average spike or average over the threshold in the same
  window) or ``short_peak`` (max only). Average spikes outside any episode are listed separately.
  Without MAX_* data the average series is used, with a note.

Workload (opcounters + document metrics): every episode carries ``workload`` comparing OPCOUNTER_*
(incl. OPCOUNTER_REPL_* applied on secondaries) and DOCUMENT_METRICS_* in the window with the day
median. ``volume_increase`` means more operations/documents explain the resource rise;
``no_volume_change`` points to a higher per-operation cost (scans, query targeting, cache) or contention.

Every episode also carries ``contention`` (``concurrency_findings.episode_contention``): the
write-conflict / hot-document evidence of the same window from the uncapped minute timeline
(parser >= 1.6). A hot document makes CPU high *because of* conflict retries, so the report must
check it before blaming CPU capacity or individual queries.
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
# Fine granularity: MAX_* series are context only. Hourly/daily granularity: peak rules use them.
MAX_CPU_COMPONENTS = ["MAX_" + n for n in CPU_COMPONENTS]
MAX_MEMORY = ["MAX_SYSTEM_MEMORY_USED", "MAX_SYSTEM_MEMORY_AVAILABLE"]
# Workload context of every episode: operation rates and document rates.
OPCOUNTER_METRICS = ["OPCOUNTER_QUERY", "OPCOUNTER_GETMORE", "OPCOUNTER_INSERT", "OPCOUNTER_UPDATE",
                     "OPCOUNTER_DELETE", "OPCOUNTER_CMD", "OPCOUNTER_TTL_DELETED",
                     "OPCOUNTER_REPL_INSERT", "OPCOUNTER_REPL_UPDATE", "OPCOUNTER_REPL_DELETE", "OPCOUNTER_REPL_CMD"]
DOCUMENT_METRICS = ["DOCUMENT_METRICS_RETURNED", "DOCUMENT_METRICS_INSERTED", "DOCUMENT_METRICS_UPDATED",
                    "DOCUMENT_METRICS_DELETED"]
WORKLOAD_METRICS = OPCOUNTER_METRICS + DOCUMENT_METRICS
# Extra process measurements requested (besides the graph groups) so the rules can be evaluated.
INVESTIGATION_METRICS = list(dict.fromkeys(
    CPU_COMPONENTS + MAX_CPU_COMPONENTS + ["SYSTEM_MEMORY_USED", "SYSTEM_MEMORY_AVAILABLE", "SYSTEM_MEMORY_FREE"]
    + MAX_MEMORY + CPU_CONTEXT + MEMORY_CONTEXT + DISK_CONTEXT_PROCESS + WORKLOAD_METRICS))
DISK_METRICS = ["DISK_QUEUE_DEPTH", "MAX_DISK_QUEUE_DEPTH", "DISK_PARTITION_IOPS_TOTAL",
                "DISK_PARTITION_LATENCY_READ", "DISK_PARTITION_LATENCY_WRITE", "MAX_DISK_PARTITION_IOPS_TOTAL",
                "MAX_DISK_PARTITION_LATENCY_READ", "MAX_DISK_PARTITION_LATENCY_WRITE"]
DEFAULT_THRESHOLDS = {"cpu_peak_percent": 90.0, "cpu_sustained_percent": 70.0, "cpu_sustained_minutes": 20,
                      "memory_percent": 75.0, "disk_queue_depth": 10.0, "disk_min_points": 3,
                      "connection_memory_mb": 1.0, "correlation_margin_minutes": 5, "top_groups": 10,
                      # Hourly/daily granularity: spike of the average series versus the day median.
                      "avg_spike_ratio": 2.0, "avg_spike_min_delta_percent": 15.0, "avg_spike_min_delta_queue": 3.0,
                      # Workload (opcounters / document metrics) change versus the day median.
                      "workload_increase_ratio": 2.0, "workload_min_abs_increase": 5.0}
SECTION_HEADING = "## High resource usage investigation"
SECTION_RE = re.compile(r"(?im)^#{1,3}\s*High resource usage investigation\b")
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")
# Samples of one hour or more are "coarse": averages hide peaks shorter than the sample.
COARSE_STEP_SECONDS = 3600
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


def granularity_seconds(value, default=300) -> int:
    """Seconds of an ISO-8601 granularity (PT10S, PT1M, PT5M, PT1H, P1D ...)."""
    m = _DURATION.match(str(value or "").strip().upper())
    if not m or not any(m.groups()):
        return default
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    total = int(days * 86400 + hours * 3600 + minutes * 60 + seconds)
    return total if total > 0 else default


def _step_seconds(payload) -> int:
    window = (payload or {}).get("window") or {}
    return granularity_seconds(window.get("granularity"))


def _median(values):
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def average_spikes(points, ratio, min_delta) -> set:
    """Timestamps where the average series jumps well above its day median (load sustained over
    the sample, which an hourly average only shows when the rise lasted a large part of the hour)."""
    base = _median(points.values())
    if base is None:
        return set()
    return {t for t, v in points.items() if v - base >= min_delta and (base <= 0 or v >= base * ratio)}


def _in(points, start, end):
    return [v for t, v in points.items() if start <= t < end]


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


def _episode(run, step, tz_name, metric, unit="%", basis="average"):
    start, end = run[0][0], run[-1][0] + timedelta(seconds=step)
    peak_t, peak = max(run, key=lambda p: p[1])
    return {"metric": metric, "basis": basis, "start_utc": start.isoformat(), "end_utc": end.isoformat(),
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
    """Slow-operation and error groups with occurrences inside [start, end] (UTC).

    ``unsampled_active_groups`` lists capped groups (``occurrences_truncated``) whose first/last
    seen span covers the window although none of their retained samples falls in it: they may
    have run in the window (extracts before parser 1.7 kept only the first 2,000 records)."""
    slow, errors, unsampled = [], [], []
    for g in _slow_groups(extract):
        ns = str(g.get("namespace") or "")
        if _SYSTEM_NS.search(ns):
            continue
        hits = [(t, d) for t, d in _occurrence_times(g.get("occurrence_timestamps")) if start <= t <= end]
        if not hits:
            first, last = _ts(g.get("first_seen")), _ts(g.get("last_seen"))
            if g.get("occurrences_truncated") and first and last and first <= end and last >= start:
                unsampled.append({"namespace": ns, "query_hash": g.get("query_hash"),
                                  "plan_summary": g.get("plan_summary"), "count": g.get("count"),
                                  "write_conflicts_total": (g.get("writeConflicts") or {}).get("total")})
            continue
        def total(field, *aliases):
            vals = [_num(next((d.get(f) for f in (field, *aliases) if d.get(f) is not None), None)) for _, d in hits]
            vals = [v for v in vals if v is not None]
            return round(sum(vals), 2) if vals else None
        reads = [_bytes_read(d.get("storage")) for _, d in hits]
        reads = [r for r in reads if r is not None]
        slow.append({"namespace": ns, "query_hash": g.get("query_hash"), "plan_summary": g.get("plan_summary"),
                     "app_names": (g.get("app_names") or [])[:5], "occurrences_in_window": len(hits),
                     "first_in_window_utc": min(t for t, _ in hits).isoformat(),
                     "last_in_window_utc": max(t for t, _ in hits).isoformat(),
                     "duration_ms_total": total("duration_ms"), "cpu_nanos_total": total("cpuNanos", "cpu_nanos"),
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
            "error_occurrences_in_window": sum(r["occurrences_in_window"] for r in errors),
            **({"unsampled_active_groups": sorted(unsampled, key=lambda r: -(r["count"] or 0))[:top]}
               if unsampled else {})}


# ----------------------------------------------------------------------------- rules
def _cpu_total(series, components=None):
    total: dict = {}
    for name in components or CPU_COMPONENTS:
        for when, value in ((series.get(name) or {}).get("points") or {}).items():
            total[when] = total.get(when, 0.0) + value
    return total


def _memory_percent(series):
    used = (series.get("SYSTEM_MEMORY_USED") or {}).get("points") or {}
    avail = (series.get("SYSTEM_MEMORY_AVAILABLE") or {}).get("points") or {}
    return {t: 100.0 * u / (u + avail[t]) for t, u in used.items() if t in avail and (u + avail[t]) > 0}


def _memory_max_percent(series):
    """Peak memory used per sample: MAX_SYSTEM_MEMORY_USED / total RAM (total from the average
    series, used + available, which is stable; the day median fills missing samples)."""
    max_used = (series.get("MAX_SYSTEM_MEMORY_USED") or {}).get("points") or {}
    used = (series.get("SYSTEM_MEMORY_USED") or {}).get("points") or {}
    avail = (series.get("SYSTEM_MEMORY_AVAILABLE") or {}).get("points") or {}
    totals = {t: u + avail[t] for t, u in used.items() if t in avail and u + avail[t] > 0}
    fallback = _median(totals.values())
    if not max_used or not fallback:
        return {}
    return {t: min(100.0, 100.0 * u / (totals.get(t) or fallback)) for t, u in max_used.items()}


def _annotate_max_episode(ep, avg_points, spikes, sustained_threshold):
    """Max-basis episode (hourly/daily data): does the average series confirm sustained load?"""
    start, end = ep["_start"], ep["_end"]
    vals = _in(avg_points, start, end)
    spike = any(start <= t < end for t in spikes)
    ep["average_series_in_window"] = ({"max": round(max(vals), 2), "mean": round(sum(vals) / len(vals), 2)}
                                      if vals else None)
    ep["average_spike"] = spike
    ep["confidence"] = ("sustained_load" if spike or (vals and max(vals) > sustained_threshold)
                        else "short_peak" if vals else "unknown_no_average_series")
    return ep


def _spike_episodes(avg_points, spikes, covered, step, tz_name, metric, unit):
    """Average spikes that are not inside an already reported episode."""
    free = {t: v for t, v in avg_points.items() if t in spikes
            and not any(a <= t < b for a, b in covered)}
    return [_episode(r, step, tz_name, metric + "_average_spike", unit=unit, basis="average_spike")
            for r in _runs(free, lambda v: True, step)]


def workload_summary(series) -> dict:
    """Day view of opcounters and document metrics (median, max, time of max)."""
    out = {}
    for name in WORKLOAD_METRICS:
        s = series.get(name) or {}
        pts = s.get("points") or {}
        if not pts:
            continue
        peak_t = max(pts, key=pts.get)
        out[name] = {"median": round(_median(pts.values()), 2), "max": round(pts[peak_t], 2),
                     "max_at_utc": peak_t.isoformat(), "units": s.get("units")}
    return {"status": "available" if out else "no_data", "metrics": out}


def _read_ops(series, start=None, end=None):
    total = {}
    for name in ("OPCOUNTER_QUERY", "OPCOUNTER_GETMORE"):
        for t, v in ((series.get(name) or {}).get("points") or {}).items():
            if start is None or start <= t < end:
                total[t] = total.get(t, 0.0) + v
    return total


def workload_in_window(series, start, end, th) -> dict:
    """Opcounters / document metrics in [start, end) versus the day median.

    ``increased``: metrics at >= ``workload_increase_ratio`` x the median and at least
    ``workload_min_abs_increase`` above it. ``verdict``: volume_increase | no_volume_change | no_data."""
    ratio_th, abs_th = float(th["workload_increase_ratio"]), float(th["workload_min_abs_increase"])
    increased, rows = [], []
    for name in WORKLOAD_METRICS:
        pts = (series.get(name) or {}).get("points") or {}
        vals = _in(pts, start, end)
        if not vals:
            continue
        base = _median(pts.values()) or 0.0
        mean, peak = sum(vals) / len(vals), max(vals)
        row = {"metric": name, "window_mean": round(mean, 2), "window_max": round(peak, 2),
               "day_median": round(base, 2), "ratio": round(mean / base, 2) if base > 0 else None}
        rows.append(row)
        if mean - base >= abs_th and (base <= 0 or mean >= base * ratio_th):
            increased.append(row)
    if not rows:
        return {"verdict": "no_data"}
    increased.sort(key=lambda r: -(float("inf") if r["ratio"] is None else r["ratio"]))
    busiest = sorted(rows, key=lambda r: -r["window_mean"])[:3]
    out = {"verdict": "volume_increase" if increased else "no_volume_change",
           "increased": increased, "busiest": [r for r in busiest if r not in increased]}
    # Documents returned per read operation: a rise means larger result sets / less selective reads.
    returned = (series.get("DOCUMENT_METRICS_RETURNED") or {}).get("points") or {}
    reads_all, reads_win = _read_ops(series), _read_ops(series, start, end)
    per_op = lambda pts, reads: [pts[t] / reads[t] for t in pts if reads.get(t)]
    day, win = per_op(returned, reads_all), per_op({t: v for t, v in returned.items() if start <= t < end}, reads_win)
    if day and win:
        out["docs_returned_per_read_op"] = {"window_mean": round(sum(win) / len(win), 2),
                                            "day_median": round(_median(day), 2)}
    return out


def analyze(metrics_payload, extract, config=None) -> dict:
    config = config or {}
    th = {**DEFAULT_THRESHOLDS, **(config.get("resource_thresholds") or {})}
    tz_name = config.get("timezone") or "UTC"
    step = _step_seconds(metrics_payload)
    margin = timedelta(minutes=float(th["correlation_margin_minutes"]))
    series = series_of((metrics_payload or {}).get("data"))
    top = int(th["top_groups"])
    window = (metrics_payload or {}).get("window") or {}
    coarse = step >= COARSE_STEP_SECONDS
    result = {"thresholds": th, "granularity_seconds": step, "timezone": tz_name,
              "metrics_window": {k: window.get(k) for k in ("since", "until", "granularity", "requested_granularity")
                                 if k in window},
              "coarse_granularity": coarse,
              "evaluation_basis": ("max series for peak rules, average series for sustained rule and spikes"
                                   if coarse else "average series"),
              "notes": []}
    if coarse:
        result["notes"].append(
            f"Metrics granularity is {window.get('granularity') or str(step) + ' s'} (hourly/daily data, typical "
            "for log days older than the fine-grained retention). Averages hide peaks shorter than one sample, so "
            "the peak rules are evaluated on the MAX_* series (peak inside each sample). An average spike "
            f"(>= {th['avg_spike_ratio']:g}x the day median) in the same window means the load lasted a large part "
            "of the sample (confidence sustained_load); a max-only episode is a short_peak. The sustained-CPU rule "
            "uses the average series (one sample covers the whole hour).")
    result["workload"] = workload_summary(series)

    from . import concurrency_findings as cf
    timeline = cf.timeline_minutes(extract or {})

    def finish(episode, ctx_names):
        start, end = episode.pop("_start"), episode.pop("_end")
        episode["context_max_in_window"] = {n: round(v, 2) for n in ctx_names
                                            if (v := _window_max(series, n, start, end)) is not None}
        episode["correlated"] = correlate(extract or {}, start - margin, end + margin, top)
        if timeline:
            episode["contention"] = cf.episode_contention(
                cf.window_stats(extract, start - margin, end + margin, config, timeline))
        episode["workload"] = workload_in_window(series, start, end, th)
        return episode

    pct_delta = float(th["avg_spike_min_delta_percent"])

    # A - CPU
    cpu = _cpu_total(series)
    # Sum of per-component maxima (as the Atlas "Max Normalized System CPU" chart): an upper bound.
    cpu_max = {t: min(100.0, v) for t, v in _cpu_total(series, MAX_CPU_COMPONENTS).items()}
    use_max = coarse and bool(cpu_max)
    if not cpu and not use_max:
        result["cpu"] = {"status": "no_data", "rule": "normalized system CPU metrics missing"}
    else:
        peak_series = cpu_max if use_max else cpu
        basis = "max" if use_max else "average"
        spikes = average_spikes(cpu, float(th["avg_spike_ratio"]), pct_delta) if coarse and cpu else set()
        peaks = []
        for r in _runs(peak_series, lambda v: v > th["cpu_peak_percent"], step):
            ep = _episode(r, step, tz_name, "normalized_system_cpu", basis=basis)
            if use_max:
                _annotate_max_episode(ep, cpu, spikes, th["cpu_sustained_percent"])
            peaks.append(ep)
        sustained = []
        for r in _runs(cpu, lambda v: v > th["cpu_sustained_percent"], step):
            ep = _episode(r, step, tz_name, "normalized_system_cpu")
            if ep["duration_minutes"] > th["cpu_sustained_minutes"]:
                sustained.append(ep)
        covered = [(ep["_start"], ep["_end"]) for ep in peaks + sustained]
        spike_eps = _spike_episodes(cpu, spikes, covered, step, tz_name, "normalized_system_cpu", "%") \
            if coarse else []
        peaks, sustained, spike_eps = ([finish(ep, CPU_CONTEXT) for ep in group]
                                       for group in (peaks, sustained, spike_eps))
        for ep in peaks + sustained + spike_eps:
            s0, e0 = _ts(ep["start_utc"]), _ts(ep["end_utc"])
            vals = [v for t, v in cpu_max.items() if s0 <= t < e0]
            ep["max_series_peak_percent"] = round(max(vals), 2) if vals else None
        result["cpu"] = {"status": "triggered" if peaks or sustained else "not_triggered",
                         "evaluated_on": ("max (MAX_SYSTEM_NORMALIZED_CPU_*, sum of per-component maxima = upper "
                                          "bound) for the peak rule; average for the sustained rule") if use_max
                                         else "average (SYSTEM_NORMALIZED_CPU_*)",
                         "basis": basis,
                         "max_percent": round(max(peak_series.values()), 2),
                         "max_at_utc": max(peak_series, key=peak_series.get).isoformat(),
                         "average_max_percent": round(max(cpu.values()), 2) if cpu else None,
                         "average_median_percent": round(_median(cpu.values()), 2) if cpu else None,
                         "max_series_max_percent": round(max(cpu_max.values()), 2) if cpu_max else None,
                         "peak_episodes_over_%g" % th["cpu_peak_percent"]: peaks,
                         "sustained_episodes_over_%g_for_%gmin" % (th["cpu_sustained_percent"],
                                                                   th["cpu_sustained_minutes"]): sustained}
        if coarse:
            result["cpu"]["average_spikes"] = spike_eps
            if not use_max:
                result["notes"].append("CPU: MAX_SYSTEM_NORMALIZED_CPU_* not collected; peak rule evaluated on "
                                       "hourly averages, short peaks may be hidden.")
    # B - Memory
    mem = _memory_percent(series)
    mem_max = _memory_max_percent(series) if coarse else {}
    use_max = bool(mem_max)
    if not mem:
        result["memory"] = {"status": "no_data", "rule": "SYSTEM_MEMORY_USED/AVAILABLE missing"}
    else:
        peak_series = mem_max if use_max else mem
        spikes = average_spikes(mem, float(th["avg_spike_ratio"]), pct_delta) if coarse else set()
        episodes, raw = [], []
        for r in _runs(peak_series, lambda v: v > th["memory_percent"], step):
            ep = _episode(r, step, tz_name, "system_memory_used_percent", basis="max" if use_max else "average")
            if use_max:
                _annotate_max_episode(ep, mem, spikes, th["memory_percent"])
            raw.append(ep)
        spike_eps = _spike_episodes(mem, spikes, [(e["_start"], e["_end"]) for e in raw], step, tz_name,
                                    "system_memory_used_percent", "%") if coarse else []
        spike_eps = [finish(ep, MEMORY_CONTEXT) for ep in spike_eps]
        for ep in raw:
            ep = finish(ep, MEMORY_CONTEXT)
            peak_t = _ts(ep["peak_at_utc"])
            used = _value_at(series, "MAX_SYSTEM_MEMORY_USED" if use_max else "SYSTEM_MEMORY_USED", peak_t)
            avg_used, avail = (_value_at(series, "SYSTEM_MEMORY_USED", peak_t),
                               _value_at(series, "SYSTEM_MEMORY_AVAILABLE", peak_t))
            conns = _value_at(series, "CONNECTIONS", peak_t)
            total_mb = (avg_used + avail) / 1024 if avg_used is not None and avail is not None else None
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
                            "basis": "max" if use_max else "average",
                            "max_percent": round(max(peak_series.values()), 2),
                            "max_at_utc": max(peak_series, key=peak_series.get).isoformat(),
                            "average_max_percent": round(max(mem.values()), 2),
                            "average_median_percent": round(_median(mem.values()), 2),
                            "method": ("MAX_SYSTEM_MEMORY_USED / (SYSTEM_MEMORY_USED + SYSTEM_MEMORY_AVAILABLE) "
                                       "per sample; approximate") if use_max else
                                      "SYSTEM_MEMORY_USED / (SYSTEM_MEMORY_USED + SYSTEM_MEMORY_AVAILABLE); approximate",
                            "episodes_over_%g" % th["memory_percent"]: episodes}
        if coarse:
            result["memory"]["average_spikes"] = spike_eps
            if not use_max:
                result["notes"].append("Memory: MAX_SYSTEM_MEMORY_USED not collected; evaluated on hourly "
                                       "averages, short peaks may be hidden.")
    # C - Disk
    disks = (metrics_payload or {}).get("disk_measurements") or []
    partitions = []
    for disk in disks if isinstance(disks, list) else []:
        s = series_of(disk.get("data"))
        queue = (s.get("DISK_QUEUE_DEPTH") or {}).get("points") or {}
        if not queue:
            continue
        qmax = (s.get("MAX_DISK_QUEUE_DEPTH") or {}).get("points") or {}
        use_max = coarse and bool(qmax)
        evaluated = qmax if use_max else queue
        over = [t for t, v in evaluated.items() if v > th["disk_queue_depth"]]
        spikes = average_spikes(queue, float(th["avg_spike_ratio"]), float(th["avg_spike_min_delta_queue"])) \
            if coarse else set()
        entry = {"partition": disk.get("partitionName"),
                 "evaluated_on": "max (MAX_DISK_QUEUE_DEPTH)" if use_max else "average (DISK_QUEUE_DEPTH)",
                 "basis": "max" if use_max else "average",
                 "max_queue_depth": round(max(evaluated.values()), 2),
                 "max_at_utc": max(evaluated, key=evaluated.get).isoformat(),
                 "average_max_queue_depth": round(max(queue.values()), 2),
                 "max_series_max_queue_depth": round(max(qmax.values()), 2) if qmax else None,
                 "samples_over_threshold": len(over),
                 "pressure": len(over) >= int(th["disk_min_points"]), "episodes": []}
        if use_max:
            # One hourly max over the threshold is enough when the average confirms it (spike, or the
            # hourly average itself over the threshold): the queue stayed high for a large part of the hour.
            confirmed = [t for t in over if t in spikes or queue.get(t, 0) > th["disk_queue_depth"]]
            entry["samples_confirmed_by_average"] = len(confirmed)
            entry["pressure"] = entry["pressure"] or bool(confirmed)
        if coarse and not use_max:
            result["notes"].append(f"Disk {disk.get('partitionName')}: MAX_DISK_QUEUE_DEPTH not collected; "
                                   "evaluated on hourly averages, short peaks may be hidden.")
        merged = {**series, **{k: v for k, v in s.items()}}
        runs = _runs(evaluated, lambda v: v > th["disk_queue_depth"], step) if entry["pressure"] else []
        drafts = []
        for r in runs:
            ep = _episode(r, step, tz_name, "disk_queue_depth", unit="", basis="max" if use_max else "average")
            if use_max:
                _annotate_max_episode(ep, queue, spikes, th["disk_queue_depth"])
            drafts.append(ep)
        spike_drafts = _spike_episodes(queue, spikes, [(e["_start"], e["_end"]) for e in drafts], step, tz_name,
                                       "disk_queue_depth", "") if coarse else []
        for target, group in (("episodes", drafts), ("average_spikes", spike_drafts)):
            if coarse:
                entry.setdefault(target, [])
            for ep in group:
                start, end = ep["_start"], ep["_end"]
                ctx = {n: round(v, 2) for n in [m for m in DISK_METRICS if m != "DISK_QUEUE_DEPTH"] + DISK_CONTEXT_PROCESS
                       if (v := _window_max(merged, n, start, end)) is not None}
                ep = finish(ep, [])
                ep["context_max_in_window"] = ctx
                ep["correlated"]["slow_operations"].sort(
                    key=lambda r: ((r["storage_bytes_read_total"] or 0) / 1e6 + (r["docs_examined_total"] or 0) / 1e3),
                    reverse=True)
                entry[target].append(ep)
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
    for r in corr.get("unsampled_active_groups", [])[:3]:
        lines.append(f"  - Capped group `{r['namespace']}` hash `{r.get('query_hash')}` ({r['count']} records) was active "
                     "around this window but none of its retained samples falls in it")
    if not lines:
        lines.append("  - No slow-operation or error occurrence in the extract falls inside this window; "
                     "source not identified from the available evidence.")
    return lines


def _fmt_contention(ep):
    c = ep.get("contention") or {}
    if c.get("verdict") == "hot_document_contention_likely":
        return [f"  - **Write contention (P1)**: {c['reason']}; check the application write pattern "
                "before CPU capacity or individual queries."]
    if c.get("verdict") == "hot_document_possible":
        return [f"  - Possible hot document: {c['reason']}."]
    return []


_WORKLOAD_LABEL = {"volume_increase": "workload volume increased", "no_volume_change": "no workload volume change"}


def _short(name):
    return name.replace("OPCOUNTER_", "op ").replace("DOCUMENT_METRICS_", "docs ").lower()


def _fmt_workload(ep):
    w = ep.get("workload") or {}
    if w.get("verdict") in (None, "no_data"):
        return []
    rows = w.get("increased") or w.get("busiest") or []
    detail = ", ".join(f"{_short(r['metric'])} {r['window_mean']:g}/s (day median {r['day_median']:g}"
                       + (f", x{r['ratio']:g}" if r.get("ratio") else "") + ")" for r in rows[:4])
    line = f"  - Workload: {_WORKLOAD_LABEL[w['verdict']]}" + (f" — {detail}" if detail else "")
    if w["verdict"] == "no_volume_change":
        line += "; the rise is per-operation cost (scans, targeting, cache) or contention, not more traffic"
    per = w.get("docs_returned_per_read_op")
    if per:
        line += f"; docs returned per read op {per['window_mean']:g} (day median {per['day_median']:g})"
    return [line]


def _fmt_basis(ep):
    if ep.get("basis") != "max":
        return ""
    avg = ep.get("average_series_in_window") or {}
    conf = {"sustained_load": "average spike confirms sustained load",
            "short_peak": "max only: short peak, average stays normal"}.get(ep.get("confidence"), "no average series")
    return f" [max basis; average max {avg.get('max', 'n/a')}; {conf}]"


def render_markdown(analysis) -> str:
    th = analysis["thresholds"]
    out = [SECTION_HEADING, "",
           "Deterministic evaluation of the node metrics; causes are plausible correlations with log "
           "occurrences in the same window, not confirmed root causes.", ""]
    if analysis.get("coarse_granularity"):
        out.extend([f"Granularity {analysis.get('metrics_window', {}).get('granularity') or analysis['granularity_seconds']} "
                    "(hourly/daily): peak rules evaluated on the MAX_* series; average spikes versus the day median "
                    "separate sustained load from short peaks.", ""])
    for key, title in (("cpu", "A — CPU"), ("memory", "B — Memory"), ("disk", "C — Disk")):
        sec = analysis.get(key) or {}
        out.extend([f"### {title}", ""])
        if sec.get("status") == "no_data":
            out.extend([f"Not evaluated: {sec.get('rule')}.", ""])
            continue
        if key == "disk":
            for part in sec["partitions"]:
                mx = part.get("max_series_max_queue_depth")
                if part.get("basis") == "max":
                    head = (f"- Partition `{part['partition']}`: max queue depth {part['max_queue_depth']} (max series), "
                            f"average series max {part.get('average_max_queue_depth')}, ")
                else:
                    head = (f"- Partition `{part['partition']}`: max avg queue depth {part['max_queue_depth']}"
                            + (f" (max series peak {mx})" if mx is not None else "") + ", ")
                out.append(head +
                           f"{part['samples_over_threshold']} sample(s) over {th['disk_queue_depth']:g} — "
                           + ("**pressure**" if part["pressure"] else "no pressure"))
                for ep in part["episodes"]:
                    out.append(f"  - {ep['start_utc'][:16]}–{ep['end_utc'][11:16]} UTC, peak {ep['peak']}{_fmt_basis(ep)}")
                    out.extend("  " + x for x in _fmt_contention(ep) + _fmt_workload(ep) + _fmt_corr(ep["correlated"]))
                for ep in part.get("average_spikes") or []:
                    out.append(f"  - Average spike {ep['start_utc'][:16]}–{ep['end_utc'][11:16]} UTC, avg queue "
                               f"{ep['peak']} (below the trigger but well above the day median)")
                    out.extend("  " + x for x in _fmt_workload(ep))
            out.append("")
            continue
        mx = sec.get("max_series_max_percent")
        if sec.get("basis") == "max":
            out.append(f"Max {sec['max_percent']}% (max series) at {sec['max_at_utc'][:16]} UTC, average series max "
                       f"{sec.get('average_max_percent')}% / median {sec.get('average_median_percent')}% — "
                       + ("**threshold exceeded**" if sec["status"] == "triggered" else "threshold not reached") + ".")
        else:
            out.append(f"Max {sec['max_percent']}% (average series) at {sec['max_at_utc'][:16]} UTC"
                       + (f", max series peak {mx}%" if mx is not None else "") + " — "
                       + ("**threshold exceeded**" if sec["status"] == "triggered" else "threshold not reached") + ".")
        out.append("")
        for label, episodes in sec.items():
            if not isinstance(episodes, list):
                continue
            for ep in episodes:
                out.append(f"- {label.replace('_', ' ')}: {ep['start_utc'][:16]}–{ep['end_utc'][11:16]} UTC "
                           f"({ep.get('start_local') or ''}), {ep['duration_minutes']} min, peak {ep['peak']}%, "
                           + (f"mean of max {ep['average']}%" if ep.get("basis") == "max" else f"avg {ep['average']}%")
                           + _fmt_basis(ep))
                if ep.get("at_peak"):
                    a = ep["at_peak"]
                    out.append(f"  - At peak: {a.get('connections')} connections ≈ {a.get('connection_memory_estimate_mb')} MB "
                               f"({a.get('connection_memory_share_percent')}% of RAM), {a.get('cursors_open')} open cursors, "
                               f"global lock queue {a.get('global_lock_queue_total')}")
                out.extend(_fmt_contention(ep))
                out.extend(_fmt_workload(ep))
                out.extend(_fmt_corr(ep["correlated"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
