#!/usr/bin/env python3
"""Create a structured MongoDB log handoff with bundled analyzers."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

try:
    from ftdc_decoder import decode_ftdc_bytes, emit_source_debug
    from driver_compatibility import extract_driver_compatibility
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ftdc_decoder import decode_ftdc_bytes, emit_source_debug
    from driver_compatibility import extract_driver_compatibility

VERSION = "1.3"
LEGACY_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<sev>[DIWFE])\s+(?P<component>\S+)(?:\s+(?P<id>\d+))?\s+(?:\[(?P<ctx>[^\]]+)\])?\s*(?P<msg>.*)$"
)
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")

CATEGORY_PATTERNS = {
    "fatal_assertion": re.compile(r"assert|invariant|panic|crash|fassert|corrupt|fatal", re.I),
    "availability_replication": re.compile(r"election|heartbeat|rollback|step.?down|sync.?source|replication lag|majority|flow.?control", re.I),
    "storage_resource": re.compile(r"wiredtiger|checkpoint|journal|cache|disk|filesystem|out of memory|memory pressure|resource", re.I),
    "connection_network": re.compile(r"handshake|authentication|auth failed|tls|timeout|connection|socket|network", re.I),
    "sharding_topology": re.compile(r"balancer|chunk|migration|stale config|mongos|topology|router", re.I),
}



FTDC_RESOURCE_TOKENS = {
    "cpu", "mem", "memory", "cache", "disk", "filesystem", "file_system", "io", "iops",
}
FTDC_EXCLUDED_TOKENS = {
    "config", "collection", "collections", "collectionstats", "collstats",
    "database", "databases", "dbstats", "indexstats", "stats", "statistics",
    "network", "connection", "connections", "socket", "query", "queries",
    "operation", "operations",
}
FTDC_TIME_KEYS = {"t", "ts", "timestamp", "time", "wall", "date", "start"}


def _ftdc_paths(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(item for item in path.rglob("*") if item.is_file() and not item.name.endswith(".lock"))
    return sorted(set(files))


def _ftdc_timestamp(record: dict[str, Any]) -> str | None:
    for key, value in record.items():
        if str(key).lower() in FTDC_TIME_KEYS:
            parsed = parse_time(value)
            if parsed:
                return parsed
    for value in record.values():
        if isinstance(value, dict):
            parsed = _ftdc_timestamp(value)
            if parsed:
                return parsed
    return None


def _numeric_metrics(value: Any, prefix: str = "") -> dict[str, float]:
    metrics: dict[str, float] = {}
    if isinstance(value, bool):
        return metrics
    if isinstance(value, (int, float)):
        if prefix:
            metrics[prefix] = float(value)
        return metrics
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            metrics.update(_numeric_metrics(child, child_prefix))
    elif isinstance(value, list):
        for index, child in enumerate(value[:20]):
            metrics.update(_numeric_metrics(child,f"{prefix}[{index}]"))
    return metrics


def _is_resource_metric(name: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", name.lower().replace("-", "_")))
    if tokens & FTDC_EXCLUDED_TOKENS:
        return False
    return bool(tokens & FTDC_RESOURCE_TOKENS)


def _epoch(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def analyze_ftdc(paths: list[Path] | None, events: list[dict[str, Any]], window_minutes: int, debug_source_json: bool = False) -> dict[str, Any]:
    log_timestamps = sorted(str(event["timestamp"]) for event in events if event.get("timestamp"))
    log_epochs = [_epoch(timestamp) for timestamp in log_timestamps]
    log_epochs = [value for value in log_epochs if value is not None]
    log_time_range = {
        "first": log_timestamps[0] if log_timestamps else None,
        "last": log_timestamps[-1] if log_timestamps else None,
    }
    base = {
        "files": [],
        "records": 0,
        "resource_samples": 0,
        "resource_metrics": [],
        "contention_windows": [],
        "find_correlations": [],
        "correlation_window_minutes": window_minutes,
        "log_time_range": log_time_range,
        "ftdc_time_range": {"first": None, "last": None},
        "timeline_status": "matched" if log_epochs else "unavailable",
        "filtered_outside_log_timeline": 0,
        "filtered_without_timestamp": 0,
    }
    if not paths:
        return {"status": "not_provided", **base}

    files = _ftdc_paths(paths)
    records: list[dict[str, Any]] = []
    file_status = []
    for path in files:
        step_started = time.perf_counter()
        try:
            opener = gzip.open if path.name.lower().endswith(".gz") else open
            with opener(path, "rb") as handle:
                data = handle.read()
            decoded_result = decode_ftdc_bytes(data)
            decoded = decoded_result["records"]
            if debug_source_json:
                emit_source_debug(path, {**decoded_result, "data": data})
            records.extend(decoded)
            file_status.append({
                "file": path.name,
                "status": "parsed" if decoded else "no_records",
                "records": len(decoded),
                "decoder_errors": decoded_result.get("errors", []),
            })
        except (OSError, ValueError) as exc:
            file_status.append({"file": path.name, "status": "error", "error": str(exc)[:200], "records": 0})
        elapsed = round(time.perf_counter() - step_started, 3)
        file_status[-1]["duration_seconds"] = elapsed
        print(f"Loading FTDC {path.name}: {elapsed:.3f}s", file=sys.stderr)

    samples = []
    in_timeline_by_file: dict[str, int] = collections.defaultdict(int)
    filtered_outside = 0
    filtered_without_timestamp = 0
    log_start = min(log_epochs) if log_epochs else None
    log_end = max(log_epochs) if log_epochs else None
    for record in records:
        timestamp = _ftdc_timestamp(record)
        record_epoch = _epoch(timestamp)
        if record_epoch is None:
            filtered_without_timestamp += 1
            continue
        if log_start is None or log_end is None:
            continue
        if record_epoch < log_start or record_epoch > log_end:
            filtered_outside += 1
            continue
        metrics = {name: value for name, value in _numeric_metrics(record).items() if _is_resource_metric(name)}
        if not metrics:
            continue
        samples.append({"timestamp": timestamp, "metrics": metrics})
    contention_windows = []
    for sample in samples:
        active = {name: value for name, value in sample["metrics"].items() if value != 0}
        if active:
            contention_windows.append({"timestamp": sample["timestamp"], "signal_count": len(active), "signals": dict(list(sorted(active.items()))[:30])})

    find_events = [
        event for event in events
        if str(event.get("operation_name") or "").lower() == "find"
        and (event.get("duration_ms") is not None or category_for(event) == "inefficient_query")
    ]
    correlations = []
    window_seconds = max(window_minutes, 0) * 60
    for event in find_events:
        event_epoch = _epoch(event.get("timestamp"))
        if event_epoch is None:
            continue
        nearby = [sample for sample in contention_windows if abs((_epoch(sample["timestamp"]) or event_epoch) - event_epoch) <= window_seconds]
        if nearby:
            correlations.append({
                "find_issue": {
                    "timestamp": event.get("timestamp"),
                    "namespace": event.get("namespace"),
                    "query_hash": event.get("query_hash"),
                    "duration_ms": event.get("duration_ms"),
                    "plan_summary": event.get("plan_summary"),
                },
                "ftdc_samples": nearby[:20],
                "interpretation": "Temporal overlap within the log timeline only; validate with workload and host metrics before inferring causality.",
            })

    sample_timestamps = [sample["timestamp"] for sample in samples]
    status = "no_records"
    if records and not log_epochs:
        status = "no_log_timeline"
    elif records and not samples:
        status = "no_records_in_log_timeline"
    elif records:
        status = "parsed"
    base.update({
        "status": status,
        "files": file_status,
        "records": len(records),
        "resource_samples": len(samples),
        "resource_metrics": samples[:200],
        "contention_windows": contention_windows[:200],
        "find_correlations": correlations[:200],
        "ftdc_time_range": {"first": min(sample_timestamps) if sample_timestamps else None, "last": max(sample_timestamps) if sample_timestamps else None},
        "filtered_outside_log_timeline": filtered_outside,
        "filtered_without_timestamp": filtered_without_timestamp,
    })
    return base


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--slow-ms", type=float, default=1000.0)
    p.add_argument("--occurrence-details", dest="include_occurrence_details", action="store_true", default=True, help="Include detailed error and slow-operation occurrence timestamps (default)")
    p.add_argument("--no-occurrence-details", dest="include_occurrence_details", action="store_false", help="Omit detailed error and slow-operation occurrence timestamps")
    p.add_argument("--bucket-minutes", type=int, default=60)
    p.add_argument("--ftdc", action="append", type=Path, help="Optional FTDC file or diagnostic.data directory; repeat for multiple paths")
    p.add_argument("--ftdc-window-minutes", type=int, default=5, help="Correlation window around find issues")
    p.add_argument("--debug-ftdc-json", action="store_true", help="Print the loaded FTDC source JSON or decoded source records to stderr")
    return p.parse_args()


def read_lines(path: Path) -> Iterable[str]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        yield from fh


def parse_time(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("$date")
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if abs(float(value)) >= 100_000_000_000 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        match = ISO_RE.search(text)
        return match.group(0) if match else None


def scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


SEVERITY_ALIASES = {"DEBUG": "D", "INFO": "I", "WARNING": "W", "WARN": "W", "ERROR": "E", "FATAL": "F"}
ERROR_PATTERN = re.compile(r"\b(error|failed|failure|exception|fatal|panic|assert|corrupt)\b", re.I)


def normalize_severity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).upper()
    return SEVERITY_ALIASES.get(text, text) or None


def is_error_event(event: dict[str, Any]) -> bool:
    return event.get("severity") in {"E", "F"} or bool(ERROR_PATTERN.search(str(event.get("message") or "")))


def fingerprint(text: str) -> str:
    normalized = text.lower()
    normalized = re.sub(r"(dup(?:licate)? key\s*:\s*)\{.*?\}", r"\1{#}", normalized, flags=re.I)
    normalized = re.sub(r"([\"]).*?\1", r"\1?\1", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "#", normalized)
    normalized = re.sub(r"[0-9a-f]{8,}", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


SENSITIVE_QUERY_KEYS = {"lsid", "$clusterTime", "signature", "clientMetadata", "remote"}


def query_shape(value: Any, depth: int = 0) -> Any:
    """Keep command structure and field names while replacing literal values."""
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        shaped = {}
        for key, item in value.items():
            if str(key) not in SENSITIVE_QUERY_KEYS:
                shaped[str(key)] = query_shape(item, depth + 1)
        return shaped
    if isinstance(value, list):
        return [query_shape(item, depth + 1) for item in value[:10]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return "?"
    return "?"


def redact_message(value: str) -> str:
    value = re.sub(r"mongodb(?:\+srv)?://\S+", "<redacted-uri>", value, flags=re.I)
    value = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<redacted-id>", value, flags=re.I)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "<redacted-address>", value)
    value = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "<redacted-email>", value)
    value = re.sub(r"((?:password|passwd|token|secret|key)=)\S+", r"\1<redacted>", value, flags=re.I)
    value = re.sub(r"(dup(?:licate)? key\s*:\s*\{).*?(\})", r"\1<redacted>\2", value, flags=re.I)
    return value[:300]


def infer_error_metadata(message: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if re.search(r"index\s+build|index\s+creation|create\s+indexes?|createIndexes|building\s+index|unique\s+index", message, re.I):
        metadata["operation_name"] = "createIndexes"
    codes = re.findall(r"\bE\d{4,5}\b", message, flags=re.I)
    if codes:
        metadata["error_code"] = codes[0].upper()
    if re.search(r"duplicate\s+key", message, re.I):
        metadata["error_code_name"] = "DuplicateKey"
    collection = re.search(r"\bcollection\s*:\s*([A-Za-z0-9_.$-]+)", message, re.I)
    if collection:
        metadata["namespace"] = collection.group(1)
    index = re.search(r"\bindex\s*:\s*[\"']?([^,\s\"']+)", message, re.I)
    if index:
        metadata["index_name"] = index.group(1)
    duplicate = re.search(r"dup(?:licate)?\s+key\s*:\s*\{([^}]*)\}", message, re.I)
    if duplicate:
        metadata["duplicate_key_fields"] = sorted(set(re.findall(r"([A-Za-z0-9_.$-]+)\s*:", duplicate.group(1))))[:20]
    return metadata


def numeric_summary(group: list[dict[str, Any]], field: str) -> dict[str, float] | None:
    values = []
    for event in group:
        if event.get(field) is not None:
            values.append(float(event[field]))
    if not values:
        return None
    return {"min": min(values), "median": median(values), "max": max(values), "avg": round(mean(values), 2)}


def repeated_operation_details(group: list[dict[str, Any]]) -> dict[str, Any] | None:
    shapes = []
    seen_shapes = set()
    for event in group:
        shape = event.get("query_shape")
        if shape is None:
            continue
        encoded = json.dumps(shape, sort_keys=True)
        if encoded not in seen_shapes:
            seen_shapes.add(encoded)
            shapes.append(shape)
    details: dict[str, Any] = {
        "namespaces": sorted({str(e["namespace"]) for e in group if e.get("namespace")})[:20],
        "operations": sorted({str(e["operation_name"]) for e in group if e.get("operation_name")} | {str(next(iter(shape))) for shape in shapes if isinstance(shape, dict) and shape}),
        "query_hashes": sorted({str(e["query_hash"]) for e in group if e.get("query_hash")})[:20],
        "error_codes": sorted({str(e["error_code"]) for e in group if e.get("error_code")})[:20],
        "error_code_names": sorted({str(e["error_code_name"]) for e in group if e.get("error_code_name")})[:20],
        "index_names": sorted({str(e["index_name"]) for e in group if e.get("index_name")} | {str(name) for e in group for name in e.get("index_names", [])})[:20],
        "duplicate_key_fields": sorted({str(field) for e in group for field in e.get("duplicate_key_fields", [])})[:20],
        "plan_summaries": sorted({str(e["plan_summary"]) for e in group if e.get("plan_summary")})[:20],
        "app_names": sorted({str(e["app_name"]) for e in group if e.get("app_name")})[:20],
        "query_shapes": shapes[:5],
        "duration_ms": numeric_summary(group, "duration_ms"),
        "keys_examined": numeric_summary(group, "keys_examined"),
        "docs_examined": numeric_summary(group, "docs_examined"),
        "n_returned": numeric_summary(group, "n_returned"),
        "queue_time_us": numeric_summary(group, "queue_time_us"),
    }
    if not any(details.values()):
        return None
    return details


def redact_original_log(raw_line: str | None) -> str | None:
    """Preserve the first source record while removing query and secret values."""
    if not raw_line:
        return None
    try:
        original = json.loads(raw_line)

        def scrub(value: Any, key: str = "") -> Any:
            key_lower = key.lower()
            if key in SENSITIVE_QUERY_KEYS or any(token in key_lower for token in ("password", "passwd", "token", "secret", "credential")):
                return "<redacted>"
            if key_lower in {"command", "query", "filter", "updates", "update", "insert", "documents", "deletes", "q", "u", "keyvalue", "keys", "indexspec", "keypattern"}:
                return query_shape(value)
            if key_lower in {"msg", "message"}:
                return redact_message(str(value))
            if isinstance(value, dict):
                return {str(child_key): scrub(child_value, str(child_key)) for child_key, child_value in value.items()}
            if isinstance(value, list):
                return [scrub(item, key) for item in value[:20]]
            if isinstance(value, str):
                return redact_message(value)
            return value

        return json.dumps(scrub(original), sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        return redact_message(raw_line)


def sample_log(event: dict[str, Any]) -> dict[str, Any]:
    """Return the first occurrence as safe source-line and structured evidence."""
    return {
        "raw_line": redact_original_log(event.get("raw_line")),
        "source_file": event.get("source_file"),
        "timestamp": event.get("timestamp"),
        "severity": event.get("severity"),
        "component": event.get("component"),
        "event_id": event.get("event_id"),
        "message": redact_message(str(event.get("message") or "")),
        "namespace": event.get("namespace"),
        "operation_name": event.get("operation_name"),
        "error_code": event.get("error_code"),
        "error_code_name": event.get("error_code_name"),
        "index_name": event.get("index_name"),
        "index_names": event.get("index_names", []),
        "duplicate_key_fields": event.get("duplicate_key_fields", []),
        "query_hash": event.get("query_hash"),
        "plan_summary": event.get("plan_summary"),
        "app_name": event.get("app_name"),
        "query_shape": event.get("query_shape"),
        "duration_ms": event.get("duration_ms"),
        "keys_examined": event.get("keys_examined"),
        "docs_examined": event.get("docs_examined"),
        "n_returned": event.get("n_returned"),
    }


def parse_line(line: str) -> tuple[dict[str, Any] | None, str | None]:
    text = line.strip()
    if not text:
        return None, "blank"
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            return None, "non_object_json"
        attr = obj.get("attr") if isinstance(obj.get("attr"), dict) else {}
        msg = str(obj.get("msg") or obj.get("message") or "")
        inferred = infer_error_metadata(msg)
        command = attr.get("command") if isinstance(attr.get("command"), dict) else {}
        command_names = [str(key) for key in command if str(key) not in {"$db", "lsid", "$clusterTime"}]
        index_entries = command.get("indexes") if isinstance(command.get("indexes"), list) else []
        event = {
            "timestamp": parse_time(obj.get("t")),
            "severity": normalize_severity(obj.get("s")),
            "component": scalar(obj.get("c")),
            "event_id": scalar(obj.get("id")),
            "context": scalar(obj.get("ctx")),
            "message": msg,
            "fingerprint": fingerprint(msg),
            "namespace": scalar(attr.get("ns")) or inferred.get("namespace"),
            "duration_ms": scalar(attr.get("durationMillis")),
            "keys_examined": scalar(attr.get("keysExamined")),
            "docs_examined": scalar(attr.get("docsExamined")),
            "n_returned": scalar(attr.get("nreturned")),
            "plan_summary": scalar(attr.get("planSummary")),
            "query_hash": scalar(attr.get("queryHash") or attr.get("queryShapeHash")),
            "has_sort_stage": bool(attr.get("hasSortStage", False)),
            "queue_time_us": scalar(attr.get("queues", {}).get("execution", {}).get("totalTimeQueuedMicros")) if isinstance(attr.get("queues"), dict) else None,
            "app_name": scalar(attr.get("appName")),
            "query_shape": query_shape(attr.get("command")) if isinstance(attr.get("command"), dict) else None,
            "operation_name": scalar(attr.get("commandName") or attr.get("operation") or attr.get("op")) or (command_names[0] if command_names else inferred.get("operation_name")),
            "error_code": scalar(attr.get("code")) or inferred.get("error_code"),
            "error_code_name": scalar(attr.get("codeName")) or inferred.get("error_code_name"),
            "index_name": scalar(attr.get("indexName")) or inferred.get("index_name"),
            "index_names": [str(item.get("name")) for item in index_entries if isinstance(item, dict) and item.get("name")][:20],
            "duplicate_key_fields": inferred.get("duplicate_key_fields", []),
            "raw_line": text,
        }
        return event, None
    except json.JSONDecodeError:
        match = LEGACY_RE.match(text)
        if not match:
            return None, "unparsed"
        gd = match.groupdict()
        message = gd.get("msg") or ""
        event = {
            "timestamp": parse_time(gd.get("ts")),
            "severity": normalize_severity(gd.get("sev")),
            "component": gd.get("component"),
            "event_id": gd.get("id"),
            "context": gd.get("ctx"),
            "message": message,
            "fingerprint": fingerprint(message),
            "namespace": None,
            "duration_ms": None,
            "keys_examined": None,
            "docs_examined": None,
            "n_returned": None,
            "plan_summary": None,
            "query_hash": None,
            "has_sort_stage": False,
            "queue_time_us": None,
            "app_name": None,
            "query_shape": None,
            "operation_name": infer_error_metadata(message).get("operation_name"),
            "error_code": infer_error_metadata(message).get("error_code"),
            "error_code_name": infer_error_metadata(message).get("error_code_name"),
            "index_name": infer_error_metadata(message).get("index_name"),
            "index_names": [],
            "duplicate_key_fields": infer_error_metadata(message).get("duplicate_key_fields", []),
            "raw_line": text,
        }
        return event, None



def category_for(event: dict[str, Any]) -> str | None:
    msg = event["message"]
    sev = event.get("severity") or ""
    if sev in {"F", "E"} or CATEGORY_PATTERNS["fatal_assertion"].search(msg):
        return "fatal_assertion"
    for category in ("availability_replication", "storage_resource", "connection_network", "sharding_topology"):
        if CATEGORY_PATTERNS[category].search(msg):
            return category
    duration = event.get("duration_ms")
    plan = str(event.get("plan_summary") or "")
    examined = max(float(event.get("keys_examined") or 0), float(event.get("docs_examined") or 0))
    returned = float(event.get("n_returned") or 0)
    if (duration is not None and float(duration) >= 1000) or "COLLSCAN" in plan.upper() or (returned > 0 and examined / returned >= 100):
        return "inefficient_query"
    if sev == "W":
        return "warning_pattern"
    return None


def bucket(timestamp: str | None, minutes: int) -> str | None:
    if not timestamp:
        return None
    try:
        value = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        minute = (value.minute // minutes) * minutes
        return value.replace(minute=minute, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _compact_timestamp(timestamp: Any) -> tuple[str, str] | None:
    """Return UTC YYYYMMDD and HH:MM:SS.cc keys for occurrence maps."""
    if not timestamp:
        return None
    try:
        value = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc)
    centiseconds = value.microsecond // 10000
    return value.strftime("%Y%m%d"), f"{value:%H:%M:%S}.{centiseconds:02d}"


def _occurrence_timestamps(events: list[dict[str, Any]]) -> dict[str, list[dict[str, dict[str, int]]]]:
    """Bucket error occurrences by UTC date and hour/minute with counts."""
    grouped: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for event in events:
        compact = _compact_timestamp(event.get("timestamp"))
        if compact:
            time_value = compact[1]
            minute = f"{time_value[:2]}H{time_value[3:5]}"
            grouped[compact[0]][minute] += 1
    return {
        date: [{minute: {"occur": count}} for minute, count in sorted(minutes.items())]
        for date, minutes in sorted(grouped.items())
    }


def _slow_message_after_plan_summary(message: Any) -> str | None:
    text = redact_message(str(message or ""))
    index = text.find("planSummary")
    return text[index:] if index >= 0 else None


def _slow_occurrences(events: list[dict[str, Any]]) -> dict[str, list[dict[str, dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, dict[str, Any]]]] = collections.defaultdict(list)
    for event in events:
        compact = _compact_timestamp(event.get("timestamp"))
        if not compact:
            continue
        stats: dict[str, Any] = {}
        for key in ("duration_ms", "docs_examined", "keys_examined", "n_returned", "has_sort_stage", "namespace"):
            if event.get(key) is not None:
                stats[key] = event.get(key)
        slow_message = _slow_message_after_plan_summary(event.get("message"))
        if slow_message:
            stats["slow_message"] = slow_message
        grouped[compact[0]].append({compact[1]: stats})
    return {date: values for date, values in sorted(grouped.items())}


def summarize(events: list[dict[str, Any]], slow_ms: float, bucket_minutes: int, include_occurrence_details: bool = True) -> dict[str, Any]:
    severity = collections.Counter(e.get("severity") for e in events if e.get("severity"))
    components = collections.Counter(e.get("component") for e in events if e.get("component"))
    messages = collections.Counter(e["fingerprint"] for e in events if e.get("fingerprint"))
    categories = collections.Counter()
    buckets: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    slow = []
    for event in events:
        duration = event.get("duration_ms")
        is_slow = duration is not None and float(duration) >= slow_ms
        category = category_for(event)
        if category:
            categories[category] += 1
        b = bucket(event.get("timestamp"), bucket_minutes)
        if b:
            if is_error_event(event):
                buckets[b]["errors"] += 1
            if event.get("severity") == "W":
                buckets[b]["warnings"] += 1
            if is_slow:
                buckets[b]["slow_operations"] += 1
            if category:
                buckets[b][category] += 1
        if is_slow:
            slow.append(event)
    top_messages = [{"fingerprint": k, "count": v} for k, v in messages.most_common(20)]
    error_groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in events:
        if is_error_event(event):
            error_groups[event["fingerprint"]].append(event)
    error_group_summaries = []
    for error_fingerprint, group in sorted(error_groups.items(), key=lambda item: (-len(item[1]), str(item[0]))):
        timestamps = [e["timestamp"] for e in group if e.get("timestamp")]
        sample = group[0]
        error_summary = {
            "fingerprint": error_fingerprint,
            "count": len(group),
            "severities": sorted({str(e["severity"]) for e in group if e.get("severity")}),
            "first_seen": min(timestamps) if timestamps else None,
            "last_seen": max(timestamps) if timestamps else None,
            "components": sorted({str(e["component"]) for e in group if e.get("component")}),
            "event_ids": sorted({str(e["event_id"]) for e in group if e.get("event_id")})[:20],
            "message_samples": list(dict.fromkeys(redact_message(str(e.get("message") or "")) for e in group))[:5],
            "sample_message": redact_message(str(sample.get("message") or "")),
            "sample_log": sample_log(sample),
            "operation_details": repeated_operation_details(group),
        }
        if include_occurrence_details:
            error_summary["occurrence_timestamps"] = _occurrence_timestamps(group)
        error_group_summaries.append(error_summary)
    repeated_errors = [group for group in error_group_summaries if group["count"] >= 2]
    by_category: dict[str, dict[str, Any]] = {}
    for category in categories:
        matching = [e for e in events if category_for(e) == category]
        timestamps = [e["timestamp"] for e in matching if e.get("timestamp")]
        by_category[category] = {
            "count": len(matching),
            "first_seen": min(timestamps) if timestamps else None,
            "last_seen": max(timestamps) if timestamps else None,
            "components": sorted({str(e["component"]) for e in matching if e.get("component")}),
            "namespaces": sorted({str(e["namespace"]) for e in matching if e.get("namespace")})[:20],
            "fingerprints": collections.Counter(e["fingerprint"] for e in matching).most_common(10),
        }
    slow_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for e in slow:
        key = (e.get("namespace"), e.get("plan_summary"), e.get("query_hash"))
        slow_groups[key].append(e)
    slow_summary = []
    sorted_slow_groups = sorted(
        slow_groups.items(),
        key=lambda item: (-len(item[1]), tuple("" if value is None else str(value) for value in item[0])),
    )
    for key, group in sorted_slow_groups[:50]:
        durations = []
        keys_examined = []
        docs_examined = []
        examined_for_ratio = []
        returned = []
        timestamps = []
        sample_query_shape = None
        collscan_count = 0
        sort_stage_count = 0
        for event in group:
            if event.get("duration_ms") is not None:
                durations.append(float(event["duration_ms"]))
            if event.get("keys_examined") is not None:
                keys_examined.append(float(event["keys_examined"]))
            if event.get("docs_examined") is not None:
                docs_examined.append(float(event["docs_examined"]))
            if event.get("keys_examined") is not None or event.get("docs_examined") is not None:
                examined_for_ratio.append(max(float(event.get("keys_examined") or 0), float(event.get("docs_examined") or 0)))
            if event.get("n_returned") is not None:
                returned.append(float(event["n_returned"]))
            if event.get("timestamp"):
                timestamps.append(event["timestamp"])
            if sample_query_shape is None and event.get("query_shape") is not None:
                sample_query_shape = event.get("query_shape")
            if "COLLSCAN" in str(event.get("plan_summary") or "").upper():
                collscan_count += 1
            if event.get("has_sort_stage"):
                sort_stage_count += 1
        ratios = []
        for duration_value, returned_value in zip(examined_for_ratio, returned):
            ratios.append(duration_value / max(returned_value, 1))
        slow_summary_item = {
            "namespace": key[0], "plan_summary": key[1], "query_hash": key[2],
            "app_names": sorted({str(e["app_name"]) for e in group if e.get("app_name")}),
            "count": len(group), "duration_ms": {"min": min(durations), "median": median(durations), "max": max(durations), "avg": round(mean(durations), 2)},
            "keys_examined": {"min": min(keys_examined), "median": median(keys_examined), "max": max(keys_examined), "avg": round(mean(keys_examined), 2)} if keys_examined else None,
            "docs_examined": {"min": min(docs_examined), "median": median(docs_examined), "max": max(docs_examined), "avg": round(mean(docs_examined), 2)} if docs_examined else None,
            "n_returned": {"min": min(returned), "median": median(returned), "max": max(returned), "avg": round(mean(returned), 2)} if returned else None,
            "examined_returned_ratio_max": round(max(ratios, default=0), 2),
            "collscan_count": collscan_count,
            "sort_stage_count": sort_stage_count,
            "first_seen": min(timestamps) if timestamps else None,
            "last_seen": max(timestamps) if timestamps else None,
            "sample_query_shape": sample_query_shape,
            "sample_message": _slow_message_after_plan_summary(group[0].get("message")),
        }
        if include_occurrence_details:
            slow_summary_item["occurrence_timestamps"] = _slow_occurrences(group)
        slow_summary.append(slow_summary_item)
    candidates = []
    for category, info in by_category.items():
        candidates.append({"category": category, **info})
    for repeated_error in repeated_errors:
        candidates.append({"category": "repeated_error", **repeated_error})
    return {
        "summary": {"severity_counts": dict(severity), "component_counts": dict(components), "category_counts": dict(categories), "top_message_fingerprints": top_messages, "repeated_error_groups": len(repeated_errors), "repeated_error_events": sum(item["count"] for item in repeated_errors)},
        "error_scan": {
            "total_error_events": sum(group["count"] for group in error_group_summaries),
            "unique_error_groups": len(error_group_summaries),
            "repeated_error_groups": len(repeated_errors),
            "groups": error_group_summaries,
        },
        "repeated_errors": repeated_errors,
        "slow_operations": slow_summary,
        "issue_candidates": candidates,
        "trends": [{"bucket_start": b, **dict(sorted(counts.items()))} for b, counts in sorted(buckets.items())],
    }


def _formatted_extraction_json(payload: dict[str, Any]) -> str:
    """Pretty-print extraction while keeping occurrence sections on one line."""
    markers: dict[str, str] = {}
    counter = 0

    def replace(value: Any) -> Any:
        nonlocal counter
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key == "occurrence_timestamps":
                    token = f"__COMPACT_OCCURRENCE_{counter}__"
                    counter += 1
                    markers[token] = json.dumps(child, separators=(",", ":"), sort_keys=True)
                    result[key] = token
                else:
                    result[key] = replace(child)
            return result
        if isinstance(value, list):
            return [replace(child) for child in value]
        return value

    rendered = json.dumps(replace(payload), indent=2, sort_keys=True)
    for token, compact in markers.items():
        rendered = rendered.replace(json.dumps(token), compact)
    return rendered


def main() -> int:
    total_started = time.perf_counter()
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    missing = [str(p) for p in args.inputs if not p.exists()]
    if missing:
        print("Missing input: " + ", ".join(missing), file=sys.stderr)
        return 2
    events: list[dict[str, Any]] = []
    quality = {"input_files": len(args.inputs), "lines_seen": 0, "parsed_records": 0, "skipped_records": 0, "skip_reasons": collections.Counter()}
    for path in args.inputs:
        step_started = time.perf_counter()
        for line in read_lines(path):
            quality["lines_seen"] += 1
            event, reason = parse_line(line)
            if event:
                event["source_file"] = path.name
                events.append(event)
                quality["parsed_records"] += 1
            else:
                quality["skipped_records"] += 1
                quality["skip_reasons"][reason or "unknown"] += 1
        print(f"Loading log {path.name}: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    times = [e["timestamp"] for e in events if e.get("timestamp")]
    parsed_quality = dict(quality)
    parsed_quality["skip_reasons"] = dict(quality["skip_reasons"])
    parsed_quality["skipped_ratio"] = round(quality["skipped_records"] / max(quality["lines_seen"], 1), 4)
    parsed_quality["notes"] = []
    step_started = time.perf_counter()
    driver_compatibility = extract_driver_compatibility(events)
    print(f"Analyzing driver compatibility: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    if not events:
        parsed_quality["notes"].append("No supported records were parsed")
    if parsed_quality["skipped_ratio"] > 0.2:
        parsed_quality["notes"].append("More than 20% of lines were skipped; conclusions have limited confidence")
    step_started = time.perf_counter()
    ftdc = analyze_ftdc(args.ftdc, events, args.ftdc_window_minutes, args.debug_ftdc_json)
    print(f"Loading FTDC: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    step_started = time.perf_counter()
    extracted = summarize(events, args.slow_ms, args.bucket_minutes, args.include_occurrence_details)
    print(f"Summarizing records: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    extracted["ftdc"] = ftdc
    payload = {
        "schema_version": VERSION,
        "metadata":{"parser_version": VERSION, "slow_threshold_ms": args.slow_ms, "include_occurrence_details": args.include_occurrence_details, "bucket_minutes": args.bucket_minutes, "time_range": {"first": min(times) if times else None, "last": max(times) if times else None}, "input_files": [p.name for p in args.inputs]},
        "driverCompatibility": driver_compatibility,
        "quality": parsed_quality,
        **extracted,
    }
    step_started = time.perf_counter()
    (args.output / "extraction.json").write_text(_formatted_extraction_json(payload), encoding="utf-8")
    handoff = ["# MongoDB Log Extraction Handoff", "", f"Records parsed: {quality['parsed_records']}", f"Records skipped: {quality['skipped_records']}", f"Incompatible drivers: {driver_compatibility['count']}", f"Time range: {payload['metadata']['time_range']['first']} to {payload['metadata']['time_range']['last']}", "", "Use extraction.json for the diagnostic report. Do not inspect the raw log directly."]
    (args.output / "handoff.md").write_text("\n".join(handoff) + "\n", encoding="utf-8")
    print(f"Writing output: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    print(f"Total extraction: {time.perf_counter() - total_started:.3f}s", file=sys.stderr)
    print(str(args.output / "extraction.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
