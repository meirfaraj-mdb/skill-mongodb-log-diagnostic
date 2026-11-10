"""Deterministic write-contention findings for a node report (extracts from parser >= 1.6).

The occurrence extract can be tens of MB; the report prompt only gets a reduced copy (short file,
capped groups, budget sampling), which drops the per-minute ``timeline`` and the hot-document minute
series. This module reads the *full* extract once and produces a small ``concurrency_findings``
document (a few KB) that the budget reducer keeps intact:

* ``hot_documents``     top write targets (one document written repeatedly) with their peak minutes;
* ``incident_windows``  bursts detected on the uncapped minute timeline (slow-operation spikes or
                        write-conflict storms), each compared with the preceding baseline and with the
                        hot documents active in the window, plus a verdict and a priority hint;
* ``coverage``          what the log cannot show (fast writes, capped slow-group samples).

``window_stats`` is also used by ``resource_investigation.correlate`` so a CPU/memory episode is
checked for contention, not only for slow-query occurrences. Pure functions, no I/O.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from statistics import median

DEFAULT_THRESHOLDS = {
    "burst_slow_ratio": 5.0,                  # slow ops/min >= ratio x day median ...
    "burst_min_slow_per_minute": 300,         # ... and at least this many
    "burst_min_write_conflicts_per_minute": 1000,
    "merge_gap_minutes": 2,
    "max_windows": 3,
    "baseline_minutes": 60,
    "contention_min_window_conflicts": 1000,  # verdict hot_document_contention_likely needs both
    "contention_min_target_share": 0.5,
    "hot_target_min_per_minute": 100,         # verdict hot_document_possible (writes without logged conflicts)
    "max_targets": 5,
    "top_minutes": 8,
}
METRICS = ("slow", "slowCpuMs", "slowDurationMs", "writeConcernWaitMs", "writeConflicts", "conflictRecords",
           "lockWaitMs", "flowControlMs", "queueMs", "conflictErrors", "txnCommitted", "txnAborted",
           "temporarilyUnavailable")
_MINUTE_RE = re.compile(r"^(\d\d)H(\d\d)$")
LIKELY = "hot_document_contention_likely"
POSSIBLE = "hot_document_possible"
NOT_CONTENTION = "no_contention_evidence"


def _parse_minutes(occurrence_timestamps) -> dict:
    """``{"YYYYMMDD": [{"HHHMM": {...}}]}`` -> ``{datetime(UTC minute): {...}}``."""
    out: dict = {}
    if not isinstance(occurrence_timestamps, dict):
        return out
    for day, entries in occurrence_timestamps.items():
        if not (isinstance(day, str) and len(day) == 8 and day.isdigit()) or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for minute, values in entry.items():
                m = _MINUTE_RE.match(str(minute))
                if not m or not isinstance(values, dict):
                    continue
                when = datetime(int(day[:4]), int(day[4:6]), int(day[6:]), int(m.group(1)), int(m.group(2)),
                                tzinfo=timezone.utc)
                out[when] = values
    return out


def timeline_minutes(extract) -> dict:
    return _parse_minutes(((extract or {}).get("timeline") or {}).get("occurrence_timestamps"))


def target_minutes(target) -> dict:
    return _parse_minutes((target or {}).get("occurrence_timestamps"))


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _label(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%MZ")


def _sums(minutes: dict, start: datetime, end: datetime) -> tuple[dict, int]:
    sums = dict.fromkeys(METRICS, 0.0)
    count = 0
    for when, values in minutes.items():
        if start <= when <= end:
            count += 1
            for k in METRICS:
                sums[k] += _num(values.get(k))
    return sums, count


def _rounded(d: dict, digits=1) -> dict:
    return {k: (round(v, digits) if isinstance(v, float) else v) for k, v in d.items() if v}


def window_stats(extract, start: datetime, end: datetime, config=None, minutes=None) -> dict | None:
    """Contention evidence for one UTC window (inclusive minutes). None without a parser-1.6 timeline."""
    th = {**DEFAULT_THRESHOLDS, **((config or {}).get("concurrency_thresholds") or {})}
    minutes = minutes if minutes is not None else timeline_minutes(extract)
    if not minutes:
        return None
    start = start.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0)
    span = int((end - start).total_seconds() // 60) + 1
    totals, _ = _sums(minutes, start, end)
    base_start = start - timedelta(minutes=int(th["baseline_minutes"]))
    base, _ = _sums(minutes, base_start, start - timedelta(minutes=1))
    base_span = int(th["baseline_minutes"])
    per_min = {k: v / span for k, v in totals.items()}
    base_per_min = {k: v / base_span for k, v in base.items()}
    ratio = {k: round(per_min[k] / base_per_min[k], 1) for k in ("slow", "slowCpuMs", "writeConcernWaitMs", "queueMs")
             if base_per_min.get(k) and per_min.get(k)}
    peak = max(((w, _num(v.get("slow"))) for w, v in minutes.items() if start <= w <= end), key=lambda x: x[1],
               default=(None, 0))
    conc = (extract or {}).get("concurrency") or {}
    hot = []
    for target in conc.get("hot_write_targets") or []:
        tmin = target_minutes(target)
        inside = {w: v for w, v in tmin.items() if start <= w <= end}
        if not inside:
            continue
        writes = sum(_num(v.get("occur")) for v in inside.values())
        conflicts = sum(_num(v.get("writeConflicts")) for v in inside.values())
        pw, pv = max(inside.items(), key=lambda kv: _num(kv[1].get("occur")))
        hot.append({"namespace": target.get("namespace"), "target_id": target.get("target_id"),
                    "filter_fields": target.get("filter_fields"), "writes": int(writes),
                    "write_conflicts": round(conflicts, 1),
                    "share_of_window_conflicts": round(conflicts / totals["writeConflicts"], 3)
                    if totals["writeConflicts"] else None,
                    "peak_writes_per_minute": int(_num(pv.get("occur"))), "peak_minute": _label(pw),
                    "app_names": (target.get("app_names") or [])[:3],
                    "query_hashes": (target.get("query_hashes") or [])[:3],
                    "minutes_partial": bool(target.get("minutes_partial"))})
    hot.sort(key=lambda h: (-h["write_conflicts"], -h["writes"]))
    hot = hot[: int(th["max_targets"])]
    verdict, reason = NOT_CONTENTION, "no hot write target active in the window"
    if hot:
        top = hot[0]
        share = top["share_of_window_conflicts"] or 0
        if totals["writeConflicts"] >= th["contention_min_window_conflicts"] and share >= th["contention_min_target_share"]:
            verdict = LIKELY
            reason = (f"one document ({top['namespace']} target {top['target_id']}) holds {share:.0%} of the "
                      f"{totals['writeConflicts']:,.0f} write conflicts logged in the window")
        elif top["peak_writes_per_minute"] >= th["hot_target_min_per_minute"]:
            verdict = POSSIBLE
            reason = (f"one document receives up to {top['peak_writes_per_minute']} logged writes per minute; "
                      "conflicts are low or not logged")
        else:
            reason = "hot write targets are active but below the contention thresholds"
    return {"start_utc": _label(start), "end_utc": _label(end), "minutes": span,
            "peak_minute": _label(peak[0]) if peak[0] else None,
            "totals": _rounded(totals), "per_minute_avg": _rounded(per_min),
            "baseline_per_minute_avg": _rounded(base_per_min),
            "baseline_window": f"{_label(base_start)} to {_label(start - timedelta(minutes=1))}",
            "ratio_vs_baseline": ratio, "hot_documents_in_window": hot, "verdict": verdict, "reason": reason,
            "priority_hint": "P1" if verdict == LIKELY else None}


def episode_contention(stats) -> dict | None:
    """Compact per-episode contention evidence for the resource_investigation document (bounded size)."""
    if not stats:
        return None
    out = {"verdict": stats["verdict"], "reason": stats["reason"], "priority_hint": stats["priority_hint"],
           "write_conflicts": stats["totals"].get("writeConflicts", 0),
           "write_conflicts_per_minute": stats["per_minute_avg"].get("writeConflicts", 0),
           "ratio_vs_baseline": stats["ratio_vs_baseline"]}
    if stats["verdict"] != NOT_CONTENTION and stats["hot_documents_in_window"]:
        top = stats["hot_documents_in_window"][0]
        out["top_hot_document"] = {k: top[k] for k in ("namespace", "target_id", "filter_fields", "writes",
                                                         "write_conflicts", "share_of_window_conflicts",
                                                         "peak_writes_per_minute", "peak_minute", "query_hashes")}
    return out


def detect_windows(minutes: dict, config=None) -> list[tuple[datetime, datetime]]:
    """Burst windows on the minute timeline: slow-operation spikes or write-conflict storms."""
    th = {**DEFAULT_THRESHOLDS, **((config or {}).get("concurrency_thresholds") or {})}
    if not minutes:
        return []
    slow_values = [_num(v.get("slow")) for v in minutes.values()]
    med = median(slow_values) if slow_values else 0
    slow_limit = max(th["burst_slow_ratio"] * med, th["burst_min_slow_per_minute"])
    hot = sorted(w for w, v in minutes.items()
                 if _num(v.get("slow")) >= slow_limit
                 or _num(v.get("writeConflicts")) >= th["burst_min_write_conflicts_per_minute"])
    windows: list[list[datetime]] = []
    gap = timedelta(minutes=int(th["merge_gap_minutes"]) + 1)
    for when in hot:
        if windows and when - windows[-1][1] <= gap:
            windows[-1][1] = when
        else:
            windows.append([when, when])

    def weight(w):
        s, _ = _sums(minutes, w[0], w[1])
        return s["slow"] + s["writeConflicts"] / 10
    ranked = sorted(windows, key=weight, reverse=True)[: int(th["max_windows"])]
    return sorted((a, b) for a, b in ranked)


def _hot_documents(conc: dict, th: dict) -> list[dict]:
    out = []
    for target in (conc.get("hot_write_targets") or [])[: int(th["max_targets"])]:
        tmin = target_minutes(target)
        top = sorted(tmin.items(), key=lambda kv: -_num(kv[1].get("occur")))[: int(th["top_minutes"])]
        peak = max((_num(v.get("occur")) for v in tmin.values()), default=0)
        busy = sorted(w for w, v in tmin.items() if peak and _num(v.get("occur")) >= 0.1 * peak)
        cpu = (target.get("cpuNanos") or {}).get("total")
        out.append({k: target.get(k) for k in ("namespace", "target_id", "filter_fields", "count", "write_conflicts",
                                                "records_with_write_conflicts", "share_of_namespace_writes",
                                                "max_per_minute", "peak_minute", "app_names", "query_hashes",
                                                "plan_summaries", "write_kinds", "first_seen", "last_seen",
                                                "count_is_lower_bound", "minutes_partial")}
                   | {"duration_ms": target.get("duration_ms"),
                      "cpu_seconds_total": round(cpu / 1e9, 1) if isinstance(cpu, (int, float)) else None,
                      "busy_from": _label(busy[0]) if busy else None, "busy_to": _label(busy[-1]) if busy else None,
                      "top_minutes": sorted(({"minute": _label(w), "writes": int(_num(v.get("occur"))),
                                              "write_conflicts": _num(v.get("writeConflicts"))} for w, v in top),
                                            key=lambda r: r["minute"])})
    return out


def summarize(extract, config=None) -> dict | None:
    """The ``concurrency_findings`` document, or None when the extract is empty."""
    if not isinstance(extract, dict) or not extract:
        return None
    th = {**DEFAULT_THRESHOLDS, **((config or {}).get("concurrency_thresholds") or {})}
    version = ((extract.get("metadata") or {}).get("parser_version") or extract.get("schema_version"))
    conc = extract.get("concurrency")
    minutes = timeline_minutes(extract)
    if not isinstance(conc, dict) or not minutes:
        return {"status": "not_extracted", "parser_version": version,
                "note": "This extract has no concurrency/timeline section (parser older than 1.6). Write "
                        "conflicts, hot documents and minute-level load were not extracted: say so, do not "
                        "report them as zero."}
    wc = conc.get("write_conflicts") or {}
    txn = conc.get("transactions") or {}
    windows = [window_stats(extract, a, b, config, minutes) for a, b in detect_windows(minutes, config)]
    windows = [w for w in windows if w]
    ops = ((extract.get("slow") or {}).get("operations")) or []
    truncated = [o for o in ops if isinstance(o, dict) and o.get("occurrences_truncated")]
    blind = []
    for w in windows:
        a = datetime.fromisoformat(w["start_utc"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(w["end_utc"].replace("Z", "+00:00")) + timedelta(minutes=1)
        for o in truncated:
            seen = [x for x in (o.get("first_seen"), o.get("last_seen")) if x]
            if len(seen) < 2:
                continue
            f, l = (datetime.fromisoformat(str(x).replace("Z", "+00:00")) for x in seen)
            if f <= b and l >= a and not _sampled_in(o, a, b):
                blind.append({"window": w["start_utc"], "namespace": o.get("namespace"),
                              "query_hash": o.get("query_hash"), "count": o.get("count"),
                              "write_conflicts_total": (o.get("writeConflicts") or {}).get("total")})
    likely = [w for w in windows if w["verdict"] == LIKELY]
    return {
        "status": "ok", "parser_version": version,
        "source": "full extractionOccurence.json: uncapped minute timeline and hot write targets "
                  "(log-side counts: fast writes below the slow threshold are not logged, so values are lower bounds)",
        "thresholds": th,
        "totals": {"write_conflicts": wc.get("total"), "records_with_write_conflicts": wc.get("records_with_write_conflicts"),
                   "by_namespace": (wc.get("by_namespace") or [])[:5], "by_operation": (wc.get("by_operation") or [])[:5],
                   "by_app": (wc.get("by_app") or [])[:5],
                   "lock_wait_ms": round(_num((conc.get("lock_wait") or {}).get("micros_total")) / 1000, 1),
                   "flow_control_ms": round(_num((conc.get("flow_control") or {}).get("micros_total")) / 1000, 1),
                   "queue_time_ms": round(_num(conc.get("queue_time_micros_total")) / 1000, 1),
                   "conflict_errors": conc.get("conflict_errors"),
                   "temporarily_unavailable_errors": conc.get("temporarily_unavailable_errors_total"),
                   "transactions": {"records": txn.get("records"), "termination_causes": txn.get("termination_causes"),
                                    "abort_causes": (txn.get("abort_causes") or [])[:5]}},
        "hot_documents": _hot_documents(conc, th),
        "incident_windows": windows,
        "contention_detected": bool(likely),
        "coverage": {"slow_groups_truncated": len(truncated),
                     "heavy_groups_without_samples_in_windows": blind[:10],
                     "write_target_tracking": conc.get("write_target_tracking")},
        "rules": [
            "A window with verdict hot_document_contention_likely is a P1 finding: one document written by many "
            "concurrent operations. Each write conflict is a retry that burns CPU and holds a write ticket, so "
            "every operation on the node slows down; report it before CPU capacity or individual query causes.",
            "The fix is application/schema side (serialize or queue writes per document, move appended/counter "
            "fields to separate documents, shorter transactions); an index or a larger tier does not remove "
            "the serialization. Never try to reveal the hashed target values.",
            "Use incident_windows for start, peak and end times; slow-group occurrence arrays are samples.",
        ],
    }


def _sampled_in(group: dict, start: datetime, end: datetime) -> bool:
    occ = group.get("occurrence_timestamps")
    if not isinstance(occ, dict):
        return False  # short file / budget-reduced copy: no samples
    for day, entries in occ.items():
        if not (isinstance(day, str) and len(day) == 8 and day.isdigit()):
            continue
        for entry in entries if isinstance(entries, list) else []:
            for key in entry if isinstance(entry, dict) else ():
                try:
                    when = datetime(int(day[:4]), int(day[4:6]), int(day[6:]), int(key[:2]), int(key[3:5]),
                                    tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if start <= when < end:
                    return True
    return False


def render_markdown(findings: dict) -> str:
    """Deterministic fallback section when the model omits a detected contention incident."""
    lines = ["## Write contention (hot document)", "",
             "Deterministic evaluation of the log's minute timeline and write targets; counts are log-side lower bounds.", ""]
    for w in findings.get("incident_windows") or []:
        if w["verdict"] != LIKELY:
            continue
        top = w["hot_documents_in_window"][0]
        ratio = w.get("ratio_vs_baseline") or {}
        lines.append(f"- **[P1] {w['start_utc'][:16]}–{w['end_utc'][11:16]} UTC** — {w['reason']}. "
                     f"`{top['namespace']}` filter {top['filter_fields']} (target `{top['target_id']}`): "
                     f"{top['writes']:,} writes, {top['write_conflicts']:,.0f} write conflicts, peak "
                     f"{top['peak_writes_per_minute']}/min at {top['peak_minute'][11:16]} UTC. Slow operations "
                     f"×{ratio.get('slow', '?')} and slow-operation CPU ×{ratio.get('slowCpuMs', '?')} versus the "
                     "previous hour.")
    lines.extend(["", "Recommended: serialize or queue writes per document in the application, move frequently "
                  "updated counters/arrays out of the shared document, keep transactions on it short; validate "
                  "with a concurrent-write load test and FTDC `metrics.operation.writeConflicts`."])
    return "\n".join(lines).rstrip() + "\n"
