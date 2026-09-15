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
    from driver_compatibility import DriverCompatibilityAccumulator, extract_driver_compatibility
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ftdc_decoder import decode_ftdc_bytes, emit_source_debug
    from driver_compatibility import DriverCompatibilityAccumulator, extract_driver_compatibility

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


def analyze_ftdc(paths: list[Path] | None, event_window: Iterable[dict[str, Any]], window_minutes: int, debug_source_json: bool = False) -> dict[str, Any]:
    log_timestamps = sorted(str(event["timestamp"]) for event in event_window if event.get("timestamp"))
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
    log_start = min(log_epochs, default=None)
    log_end = max(log_epochs, default=None)
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
        event for event in event_window
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
        "ftdc_time_range": {"first": min(sample_timestamps, default=None), "last": max(sample_timestamps, default=None)},
        "filtered_outside_log_timeline": filtered_outside,
        "filtered_without_timestamp": filtered_without_timestamp,
    })
    return base


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--slow-ms", type=float, default=1000.0)
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
            "working_millis": scalar(attr.get("workingMillis")),
            "duration_millis": scalar(attr.get("durationMillis")),
            "cpu_nanos": scalar(attr.get("cpuNanos")),
            "storage": attr.get("storage") if isinstance(attr.get("storage"), dict) and attr.get("storage") else None,
            "wait_for_write_concern_duration_millis": scalar(attr.get("waitForWriteConcernDurationMillis")),
            "reslen": scalar(attr.get("reslen")),
            "num_yields": scalar(attr.get("numYields")),
            "total_oplog_slot_duration_micros": scalar(attr.get("totalOplogSlotDurationMicros")),
            "ninserted": scalar(attr.get("ninserted")),
            "keys_inserted": scalar(attr.get("keysInserted")),
            "n_matched": scalar(attr.get("nMatched")),
            "n_modified": scalar(attr.get("nModified")),
            "n_upserted": scalar(attr.get("nUpserted")),
            "keys_deleted": scalar(attr.get("keysDeleted")),
            "ordered": scalar(attr.get("ordered")),
            "keys_examined": scalar(attr.get("keysExamined")),
            "docs_examined": scalar(attr.get("docsExamined")),
            "n_returned": scalar(attr.get("nreturned")),
            "n_batches": scalar(attr.get("nBatches")),
            "cursor_id": scalar(attr.get("cursorid")),
            "plan_cache_shape_hash": scalar(attr.get("planCacheShapeHash")),
            "plan_cache_key": scalar(attr.get("planCacheKey")),
            "query_framework": scalar(attr.get("queryFramework")),
            "plan_summary": scalar(attr.get("planSummary")),
            "query_hash": scalar(attr.get("queryHash") or attr.get("queryShapeHash")),
            "has_sort_stage": bool(attr.get("hasSortStage", False)),
            "queue_time_us": scalar(attr.get("queues", {}).get("execution", {}).get("totalTimeQueuedMicros")) if isinstance(attr.get("queues"), dict) else None,
            "app_name": scalar(attr.get("appName")),
            "query_shape": query_shape(attr.get("originatingCommand") if isinstance(attr.get("originatingCommand"), dict) else attr.get("command")) if isinstance(attr.get("originatingCommand") if isinstance(attr.get("originatingCommand"), dict) else attr.get("command"), dict) else None,
            "is_change_stream": _contains_change_stream(attr.get("command")) or _contains_change_stream(attr.get("originatingCommand")),
            "operation_name": scalar(attr.get("commandName") or attr.get("operation") or attr.get("op")) or (command_names[0] if command_names else inferred.get("operation_name")),
            "error_code": scalar(attr.get("code")) or inferred.get("error_code"),
            "error_code_name": scalar(attr.get("codeName")) or inferred.get("error_code_name"),
            "index_name": scalar(attr.get("indexName")) or inferred.get("index_name"),
            "index_names": [str(item.get("name")) for item in index_entries if isinstance(item, dict) and item.get("name")][:20],
            "duplicate_key_fields": inferred.get("duplicate_key_fields", []),
            "raw_line": text,
            "_driver_obj": obj,
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
            "working_millis": None,
            "duration_millis": None,
            "cpu_nanos": None,
            "storage": None,
            "wait_for_write_concern_duration_millis": None,
            "reslen": None,
            "num_yields": None,
            "total_oplog_slot_duration_micros": None,
            "ninserted": None,
            "keys_inserted": None,
            "n_matched": None,
            "n_modified": None,
            "n_upserted": None,
            "keys_deleted": None,
            "ordered": None,
            "keys_examined": None,
            "docs_examined": None,
            "n_returned": None,
            "n_batches": None,
            "cursor_id": None,
            "plan_cache_shape_hash": None,
            "plan_cache_key": None,
            "query_framework": None,
            "is_change_stream": False,
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



def _contains_change_stream(value: Any) -> bool:
    if isinstance(value, dict):
        if "$changeStream" in value:
            return True
        return any(_contains_change_stream(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_change_stream(child) for child in value)
    return False


def is_slow_operation(event: dict[str, Any], slow_ms: float) -> bool:
    message = str(event.get("message") or "").strip().lower()
    # MongoDB explicitly labels these records even when duration is below the configured threshold.
    explicitly_slow = message == "slow query" or message.startswith("slow query ")
    duration = event.get("duration_ms")
    return explicitly_slow or (duration is not None and float(duration) >= slow_ms)


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


def _slow_message_after_plan_summary(message: Any) -> str | None:
    text = redact_message(str(message or ""))
    index = text.find("planSummary")
    return text[index:] if index >= 0 else None


def _bounded_add(values: list[Any], value: Any, limit: int = 2048) -> None:
    if value is not None and len(values) < limit:
        values.append(value)


def _add_stat(state: dict[str, Any], name: str, value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    item = state.setdefault(name, {"values": [], "count": 0, "total": 0.0, "min": number, "max": number})
    item["count"] += 1
    item["total"] += number
    item["min"] = min(item["min"], number)
    item["max"] = max(item["max"], number)
    _bounded_add(item["values"], number)


def _render_stat(item: dict[str, Any] | None) -> dict[str, float] | None:
    if not item or not item.get("count"):
        return None
    values = item["values"]
    return {"min": item["min"], "median": median(values) if values else item["total"] / item["count"], "max": item["max"], "avg": round(item["total"] / item["count"], 2)}


def _slow_occurrence(event: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    compact = _compact_timestamp(event.get("timestamp"))
    if not compact:
        return None
    fields = (
        ("duration_ms", "duration_ms"), ("working_millis", "workingMillis"), ("duration_millis", "durationMillis"),
        ("cpu_nanos", "cpuNanos"), ("storage", "storage"), ("wait_for_write_concern_duration_millis", "waitForWriteConcernDurationMillis"),
        ("reslen", "reslen"), ("num_yields", "numYields"), ("total_oplog_slot_duration_micros", "totalOplogSlotDurationMicros"),
        ("ninserted", "ninserted"), ("keys_inserted", "keysInserted"), ("n_matched", "nMatched"), ("n_modified", "nModified"),
        ("n_upserted", "nUpserted"), ("keys_deleted", "keysDeleted"), ("ordered", "ordered"), ("docs_examined", "docs_examined"),
        ("keys_examined", "keys_examined"), ("n_returned", "n_returned"), ("n_batches", "nBatches"), ("cursor_id", "cursorid"), ("plan_cache_shape_hash", "planCacheShapeHash"), ("plan_cache_key", "planCacheKey"), ("query_framework", "queryFramework"), ("has_sort_stage", "has_sort_stage"), ("namespace", "namespace"),
    )
    detail = {dest: event[src] for src, dest in fields if event.get(src) is not None}
    slow_message = _slow_message_after_plan_summary(event.get("message"))
    if slow_message:
        detail["slow_message"] = slow_message
    return compact[0], compact[1], detail


class StreamingSummary:
    """Bounded, one-pass summary state; raw events are never retained."""
    MAX_GROUPS = 10000
    MAX_OCCURRENCES_PER_GROUP = 2000

    def __init__(self, slow_ms: float, bucket_minutes: int) -> None:
        self.slow_ms = slow_ms
        self.bucket_minutes = bucket_minutes
        self.severity = collections.Counter()
        self.components = collections.Counter()
        self.messages = collections.Counter()
        self.categories = collections.Counter()
        self.category_details: dict[str, dict[str, Any]] = {}
        self.buckets: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.error_groups: dict[str, dict[str, Any]] = {}
        self.slow_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.slow_global = {"count": 0, "collscan_count": 0, "change_stream_count": 0, "stats": {}}
        self.first_timestamp = None
        self.last_timestamp = None
        self.parsed_records = 0
        self.error_events = 0
        self.find_events: list[dict[str, Any]] = []

    @staticmethod
    def _limited_set(state: set[str], value: Any, limit: int = 100) -> None:
        if value is not None and len(state) < limit:
            state.add(str(value))

    def _error_state(self, event: dict[str, Any]) -> dict[str, Any] | None:
        key = str(event.get("fingerprint") or "unknown")
        state = self.error_groups.get(key)
        if state is None:
            if len(self.error_groups) >= self.MAX_GROUPS:
                return None
            state = self.error_groups[key] = {
                "fingerprint": key, "count": 0, "severities": set(), "components": set(), "event_ids": set(),
                "message_samples": [], "sample_message": redact_message(str(event.get("message") or "")),
                "sample_log": sample_log(event), "first_seen": None, "last_seen": None,
                "occurrences": collections.defaultdict(collections.Counter), "operation": {},
            }
        state["count"] += 1
        self._limited_set(state["severities"], event.get("severity"))
        self._limited_set(state["components"], event.get("component"))
        self._limited_set(state["event_ids"], event.get("event_id"), 20)
        message = redact_message(str(event.get("message") or ""))
        if message not in state["message_samples"] and len(state["message_samples"]) < 5:
            state["message_samples"].append(message)
        timestamp = event.get("timestamp")
        if timestamp and (state["first_seen"] is None or timestamp < state["first_seen"]): state["first_seen"] = timestamp
        if timestamp and (state["last_seen"] is None or timestamp > state["last_seen"]): state["last_seen"] = timestamp
        compact = _compact_timestamp(timestamp)
        if compact: state["occurrences"][compact[0]][compact[1][:2] + "H" + compact[1][3:5]] += 1
        operation = state["operation"]
        for key, value in (("namespaces", event.get("namespace")), ("operations", event.get("operation_name")), ("query_hashes", event.get("query_hash")), ("error_codes", event.get("error_code")), ("error_code_names", event.get("error_code_name")), ("index_names", event.get("index_name")), ("plan_summaries", event.get("plan_summary")), ("app_names", event.get("app_name"))):
            if value is not None:
                operation.setdefault(key, set())
                self._limited_set(operation[key], value)
        for key, value in (("query_shapes", event.get("query_shape")), ("duplicate_key_fields", event.get("duplicate_key_fields"))):
            if value is not None:
                operation.setdefault(key, [])
                if isinstance(value, list):
                    for item in value[:5]:
                        if item not in operation[key] and len(operation[key]) < 5: operation[key].append(item)
                elif value not in operation[key] and len(operation[key]) < 5: operation[key].append(value)
        for key, value in (("duration_ms", event.get("duration_ms")), ("workingMillis", event.get("working_millis")), ("durationMillis", event.get("duration_millis")), ("cpuNanos", event.get("cpu_nanos")), ("keys_examined", event.get("keys_examined")), ("docs_examined", event.get("docs_examined")), ("n_returned", event.get("n_returned"))):
            _add_stat(operation, key, value)
        return state

    def add(self, event: dict[str, Any]) -> None:
        self.parsed_records += 1
        timestamp = event.get("timestamp")
        if timestamp and (self.first_timestamp is None or timestamp < self.first_timestamp): self.first_timestamp = timestamp
        if timestamp and (self.last_timestamp is None or timestamp > self.last_timestamp): self.last_timestamp = timestamp
        severity = event.get("severity")
        component = event.get("component")
        if severity: self.severity[str(severity)] += 1
        if component: self.components[str(component)] += 1
        fp = event.get("fingerprint")
        if fp:
            self.messages[str(fp)] += 1
            if len(self.messages) > 5000: self.messages = collections.Counter(dict(self.messages.most_common(2500)))
        error = is_error_event(event)
        if error:
            self.error_events += 1
            self._error_state(event)
        category = category_for(event)
        if category:
            self.categories[category] += 1
            detail = self.category_details.setdefault(category, {"count": 0, "first_seen": None, "last_seen": None, "components": set(), "namespaces": set(), "fingerprints": collections.Counter()})
            detail["count"] += 1
            if timestamp and (detail["first_seen"] is None or timestamp < detail["first_seen"]): detail["first_seen"] = timestamp
            if timestamp and (detail["last_seen"] is None or timestamp > detail["last_seen"]): detail["last_seen"] = timestamp
            self._limited_set(detail["components"], component)
            self._limited_set(detail["namespaces"], event.get("namespace"), 20)
            if fp: detail["fingerprints"][str(fp)] += 1
        duration = event.get("duration_ms")
        slow = is_slow_operation(event, self.slow_ms)
        b = bucket(timestamp, self.bucket_minutes)
        if b:
            if error: self.buckets[b]["errors"] += 1
            if severity == "W": self.buckets[b]["warnings"] += 1
            if slow: self.buckets[b]["slow_operations"] += 1
            if category: self.buckets[b][category] += 1
        if "find" in str(event.get("operation_name") or "").lower() and len(self.find_events) < 200:
            self.find_events.append({"timestamp": timestamp, "message": event.get("message"), "severity": severity, "component": component})
        if not slow: return
        self.slow_global["count"] += 1
        if "COLLSCAN" in str(event.get("plan_summary") or "").upper(): self.slow_global["collscan_count"] += 1
        if event.get("is_change_stream"): self.slow_global["change_stream_count"] += 1
        _add_stat(self.slow_global["stats"], "duration_ms", event.get("duration_ms"))
        _add_stat(self.slow_global["stats"], "cpuNanos", event.get("cpu_nanos"))
        key = (event.get("namespace"), event.get("plan_summary"), event.get("query_hash"))
        state = self.slow_groups.get(key)
        if state is None:
            if len(self.slow_groups) >= self.MAX_GROUPS: return
            state = self.slow_groups[key] = {"namespace": key[0], "plan_summary": key[1], "query_hash": key[2], "count": 0, "app_names": set(), "stats": {}, "first_seen": None, "last_seen": None, "sample_query_shape": event.get("query_shape"), "sample_message": _slow_message_after_plan_summary(event.get("message")), "collscan_count": 0, "sort_stage_count": 0, "ratio_max": 0.0, "occurrences": collections.defaultdict(list)}
        state["count"] += 1
        self._limited_set(state["app_names"], event.get("app_name"))
        for name, value in (("duration_ms", event.get("duration_ms")), ("cpuNanos", event.get("cpu_nanos")), ("keys_examined", event.get("keys_examined")), ("docs_examined", event.get("docs_examined")), ("n_returned", event.get("n_returned"))): _add_stat(state["stats"], name, value)
        if "COLLSCAN" in str(event.get("plan_summary") or "").upper(): state["collscan_count"] += 1
        if event.get("has_sort_stage"): state["sort_stage_count"] += 1
        examined = max(float(event.get("keys_examined") or 0), float(event.get("docs_examined") or 0))
        returned = float(event.get("n_returned") or 0)
        if returned or examined: state["ratio_max"] = max(state["ratio_max"], examined / max(returned, 1))
        if timestamp and (state["first_seen"] is None or timestamp < state["first_seen"]): state["first_seen"] = timestamp
        if timestamp and (state["last_seen"] is None or timestamp > state["last_seen"]): state["last_seen"] = timestamp
        occurrence = _slow_occurrence(event)
        if occurrence and sum(len(items) for items in state["occurrences"].values()) < self.MAX_OCCURRENCES_PER_GROUP:
            state["occurrences"][occurrence[0]].append({occurrence[1]: occurrence[2]})

    def _operation_details(self, operation: dict[str, Any]) -> dict[str, Any] | None:
        if not operation: return None
        result = {}
        for key, value in operation.items():
            if key in {"namespaces", "operations", "query_hashes", "error_codes", "error_code_names", "index_names", "app_names"}:
                result[key] = sorted(value)
            elif isinstance(value, dict) and "count" in value:
                result[key] = _render_stat(value)
            else:
                result[key] = value
        return result or None

    def _error_output(self, state: dict[str, Any]) -> dict[str, Any]:
        occurrences = {date: [{minute: {"occur": count}} for minute, count in sorted(values.items())] for date, values in sorted(state["occurrences"].items())}
        return {"fingerprint": state["fingerprint"], "count": state["count"], "severities": sorted(state["severities"]), "first_seen": state["first_seen"], "last_seen": state["last_seen"], "components": sorted(state["components"]), "event_ids": sorted(state["event_ids"])[:20], "message_samples": state["message_samples"], "sample_message": state["sample_message"], "sample_log": state["sample_log"], "operation_details": self._operation_details(state["operation"]), "occurrence_timestamps": occurrences}

    def finish(self) -> dict[str, Any]:
        errors = [self._error_output(state) for state in self.error_groups.values()]
        errors.sort(key=lambda item: (-item["count"], item["fingerprint"]))
        repeated = [item for item in errors if item["count"] >= 2]
        slow_output = []
        for state in sorted(self.slow_groups.values(), key=lambda item: (-item["count"], str(item["namespace"]), str(item["plan_summary"])))[:50]:
            stats = {name: _render_stat(value) for name, value in state["stats"].items()}
            item = {"namespace": state["namespace"], "plan_summary": state["plan_summary"], "query_hash": state["query_hash"], "app_names": sorted(state["app_names"]), "count": state["count"], "duration_ms": stats.get("duration_ms"), "cpuNanos": ({**stats["cpuNanos"], "total": state["stats"]["cpuNanos"]["total"]} if stats.get("cpuNanos") else None), "keys_examined": stats.get("keys_examined"), "docs_examined": stats.get("docs_examined"), "n_returned": stats.get("n_returned"), "collscan_count": state["collscan_count"], "sort_stage_count": state["sort_stage_count"], "examined_returned_ratio_max": round(state["ratio_max"], 2), "first_seen": state["first_seen"], "last_seen": state["last_seen"], "sample_query_shape": state["sample_query_shape"], "sample_message": state["sample_message"], "occurrence_timestamps": {date: values for date, values in sorted(state["occurrences"].items())}}
            slow_output.append(item)
        by_category = {}
        for category, detail in self.category_details.items():
            by_category[category] = {"count": detail["count"], "first_seen": detail["first_seen"], "last_seen": detail["last_seen"], "components": sorted(detail["components"]), "namespaces": sorted(detail["namespaces"])[:20], "fingerprints": detail["fingerprints"].most_common(10)}
        candidates = [{"category": category, **data} for category, data in by_category.items()] + [{"category": "repeated_error", **item} for item in repeated]
        global_stats = {"count": self.slow_global["count"], "collscan_count": self.slow_global["collscan_count"], "change_stream_count": self.slow_global["change_stream_count"]}
        for name, value in self.slow_global["stats"].items():
            rendered = _render_stat(value)
            if rendered:
                rendered["total"] = value["total"]
            global_stats[name] = rendered
        return {"summary": {"severity_counts": dict(self.severity), "component_counts": dict(self.components), "category_counts": dict(self.categories), "top_message_fingerprints": [{"fingerprint": key, "count": count} for key, count in self.messages.most_common(20)], "repeated_error_groups": len(repeated), "repeated_error_events": sum(item["count"] for item in repeated)}, "error_scan": {"total_error_events": self.error_events, "unique_error_groups": len(errors), "repeated_error_groups": len(repeated), "groups": errors}, "repeated_errors": repeated, "slow": {"operations": slow_output, "global_stats": global_stats}, "slow_operations": slow_output, "issue_candidates": candidates, "trends": [{"bucket_start": key, **dict(sorted(value.items()))} for key, value in sorted(self.buckets.items())]}


def summarize(stream: Iterable[dict[str, Any]], slow_ms: float, bucket_minutes: int) -> dict[str, Any]:
    """Compatibility wrapper for callers that already provide an iterable."""
    analyzer = StreamingSummary(slow_ms, bucket_minutes)
    for event in stream: analyzer.add(event)
    return analyzer.finish()


def _without_occurrence_details(value: Any) -> Any:
    if isinstance(value, dict): return {key: _without_occurrence_details(child) for key, child in value.items() if key != "occurrence_timestamps"}
    if isinstance(value, list): return [_without_occurrence_details(child) for child in value]
    return value


def _formatted_extraction_json(payload: dict[str, Any]) -> str:
    def _json_default(value: Any) -> Any:
        if isinstance(value, set): return sorted(value)
        raise TypeError(f"unsupported JSON value: {type(value).__name__}")
    markers: dict[str, str] = {}
    counter = 0
    def replace(value: Any) -> Any:
        nonlocal counter
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key in {"occurrence_timestamps", "sample_query_shape"}:
                    token = f"__COMPACT_JSON_{counter}__"; counter += 1; markers[token] = json.dumps(child, separators=(",", ":"), sort_keys=True, default=_json_default); result[key] = token
                else: result[key] = replace(child)
            return result
        if isinstance(value, list): return [replace(child) for child in value]
        return value
    rendered = json.dumps(replace(payload), indent=2, sort_keys=True, default=_json_default)
    for token, compact in markers.items(): rendered = rendered.replace(json.dumps(token), compact)
    return rendered


def main() -> int:
    total_started = time.perf_counter()
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    missing = [str(p) for p in args.inputs if not p.exists()]
    if missing:
        print("Missing input: " + ", ".join(missing), file=sys.stderr); return 2
    analyzer = StreamingSummary(args.slow_ms, args.bucket_minutes)
    from driver_compatibility import DriverCompatibilityAccumulator
    driver = DriverCompatibilityAccumulator()
    quality = {"input_files": len(args.inputs), "lines_seen": 0, "parsed_records": 0, "skipped_records": 0, "skip_reasons": collections.Counter(), "notes": []}
    for path in args.inputs:
        step_started = time.perf_counter()
        for line in read_lines(path):
            quality["lines_seen"] += 1
            event, reason = parse_line(line)
            if event:
                event["source_file"] = path.name
                quality["parsed_records"] += 1
                # Consume this record immediately; never append it to a whole-log events list.
                analyzer.add(event)
                driver.add(event)
                del event
            else:
                quality["skipped_records"] += 1; quality["skip_reasons"][reason or "unknown"] += 1
        print(f"Loading log {path.name}: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    parsed_quality = dict(quality); parsed_quality["skip_reasons"] = dict(quality["skip_reasons"]); parsed_quality["skipped_ratio"] = round(quality["skipped_records"] / max(quality["lines_seen"], 1), 4)
    if not quality["parsed_records"]: parsed_quality["notes"].append("No supported records were parsed")
    if parsed_quality["skipped_ratio"] > 0.2: parsed_quality["notes"].append("More than 20% of lines were skipped; conclusions have limited confidence")
    step_started = time.perf_counter(); driver_compatibility = driver.finish(); print(f"Analyzing driver compatibility: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    step_started = time.perf_counter()
    ftdc_events = [{"timestamp": analyzer.first_timestamp}] if analyzer.first_timestamp else []
    if analyzer.last_timestamp and analyzer.last_timestamp != analyzer.first_timestamp: ftdc_events.append({"timestamp": analyzer.last_timestamp})
    ftdc_events.extend(analyzer.find_events)
    ftdc = analyze_ftdc(args.ftdc, ftdc_events, args.ftdc_window_minutes, args.debug_ftdc_json)
    print(f"Loading FTDC: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    step_started = time.perf_counter(); extracted = analyzer.finish(); print(f"Summarizing records: {time.perf_counter() - step_started:.3f}s", file=sys.stderr)
    extracted["ftdc"] = ftdc
    payload = {"schema_version": VERSION, "metadata": {"parser_version": VERSION, "slow_threshold_ms": args.slow_ms, "bucket_minutes": args.bucket_minutes, "time_range": {"first": analyzer.first_timestamp, "last": analyzer.last_timestamp}, "input_files": [p.name for p in args.inputs]}, "driverCompatibility": driver_compatibility, "quality": parsed_quality, **extracted}
    occurrence_path = args.output / "extractionOccurence.json"; short_path = args.output / "extractionshort.json"
    occurrence_path.write_text(_formatted_extraction_json(payload), encoding="utf-8"); short_path.write_text(_formatted_extraction_json(_without_occurrence_details(payload)), encoding="utf-8")
    handoff = ["# MongoDB Log Extraction Handoff", "", f"Records parsed: {quality['parsed_records']}", f"Records skipped: {quality['skipped_records']}", f"Driver log records: {driver_compatibility['count']}", f"Incompatible driver groups: {driver_compatibility['incompatible_count']}", f"Time range: {analyzer.first_timestamp} to {analyzer.last_timestamp}", "", "Use extractionOccurence.json for full diagnostics with occurrence details. Use extractionshort.json for the compact handoff without occurrence arrays. Do not inspect the raw log directly."]
    (args.output / "handoff.md").write_text("\n".join(handoff) + "\n", encoding="utf-8")
    print(f"Writing output: {time.perf_counter() - step_started:.3f}s", file=sys.stderr); print(f"Total extraction: {time.perf_counter() - total_started:.3f}s", file=sys.stderr)
    print(str(occurrence_path)); print(str(short_path)); return 0


if __name__ == "__main__": raise SystemExit(main())
