"""Shared filenames and bounded, credential-free observability evidence contracts."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ARTIFACTS = {
    "query_stats": ("queryStats", "query-stats.json"),
    "index_definitions": ("indexDefinitions", "index-definitions.json"),
    "index_stats": ("indexStats", "index-stats.json"),
    "coll_stats": ("collStats", "coll-stats.json"),
    "query_plans": ("queryPlans", "query-plans.json"),
    "metrics": ("metrics", "metrics.json"),
    "advisor": ("advisor", "advisor.json"),
    "profiler": ("profiler", "profiler.json"),
}

# Atlas Query Shape Insights only serves summaries for the last 7 days.
QUERY_SHAPE_RETENTION = timedelta(days=7)
# Safety margin for clock skew / request latency at the oldest edge of retention.
QUERY_SHAPE_RETENTION_MARGIN = timedelta(minutes=10)


class QueryShapeWindowUnavailable(ValueError):
    """The requested log day is outside Query Shape Insights retention, or has not started."""


def artifact_key(layout, day, node, kind):
    directory, filename = ARTIFACTS[kind]
    return f"{layout.day(day)}/{node}/{directory}/{filename}"


def log_day_window(log_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    """Return [local midnight, next local midnight) for ``log_date`` as UTC datetimes.

    Same day boundaries as atlas_logs.day_window(), so query shapes, metrics and advisor
    data cover exactly the same day as the downloaded logs. DST days are 23h or 25h.
    """
    zone = ZoneInfo(timezone_name or "UTC")
    d = date.fromisoformat(log_date)
    nxt = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day, tzinfo=zone)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def query_shape_window(log_date: str, timezone_name: str, now: datetime | None = None,
                       retention: timedelta = QUERY_SHAPE_RETENTION,
                       margin: timedelta = QUERY_SHAPE_RETENTION_MARGIN) -> tuple[datetime, datetime, bool]:
    """Return ``(since, until, complete_day)`` for the log day, bounded by API retention.

    Raises QueryShapeWindowUnavailable when the day starts before the retention horizon
    (it cannot be fully served) or has not started yet. For the current day ``until`` is
    clamped to ``now`` and ``complete_day`` is False.
    """
    now = now or datetime.now(timezone.utc)
    since, until = log_day_window(log_date, timezone_name)
    oldest = now - retention + margin
    if since < oldest:
        raise QueryShapeWindowUnavailable(
            f"log day {log_date} starts {since.isoformat()}, before the "
            f"{retention.days}-day Query Shape Insights retention horizon ({oldest.isoformat()})")
    if since >= now:
        raise QueryShapeWindowUnavailable(f"log day {log_date} has not started yet")
    complete_day = until <= now
    return since, min(until, now), complete_day


def timestamp(value):
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, dict):
            value = value.get("$date")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc)
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def shape(value, depth=0):
    """Never render query literals. Preserve field/operator structure only."""
    if depth > 12:
        return "?"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:100]
                if isinstance(k, str) and not re.search(r"password|credential|token|secret|api.?key", k, re.I)}
    if isinstance(value, list):
        return [shape(v, depth + 1) for v in value[:30]]
    return "?"


def sanitize(value, depth=0):
    """Redact sensitive query fields and client endpoints before storage/model input."""
    if depth > 16:
        return "<depth limit>"
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if re.search(r"password|credential|token|secret|api.?key", str(key), re.I):
                continue
            if key == "client":
                if isinstance(item, dict):
                    app = item.get("application", {})
                    if isinstance(app, dict) and isinstance(app.get("name"), str):
                        output[key] = {"application": {"name": app["name"][:160]}}
                continue
            if key in {"remote", "clientIp", "ipAddress", "uri", "connectionString", "comment"}:
                continue
            if key in {"filter", "query", "q", "pipeline", "parsedQuery", "queryShape", "sample_query_shape",
                       "partialFilterExpression", "indexBounds", "command"} and isinstance(item, (dict, list)):
                output[key] = shape(item)
                if key == "queryShape" and isinstance(item, dict):
                    if isinstance(item.get("cmdNs"), dict):
                        output[key]["cmdNs"] = {k: str(v)[:160] for k, v in item["cmdNs"].items() if k in {"db", "coll"}}
                    if isinstance(item.get("command"), str):
                        output[key]["command"] = item["command"][:40]
            else:
                output[key] = sanitize(item, depth + 1)
        return output
    if isinstance(value, list):
        return [sanitize(v, depth + 1) for v in value[:10000]]
    if isinstance(value, str) and re.search(r"(?:mongodb(?:\+srv)?|https?)://[^\s]*@", value):
        return "<redacted credential-bearing URI>"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def resolve_source(config, kind):
    if config.get("input_mode") == "existing_bucket":
        return "bucket"
    sources = config.get("observability_sources", {})
    source = sources.get(kind, config.get(kind + "_source", "auto"))
    if kind == "query_stats" and kind not in sources:
        source = config.get("query_shape_source", source)
    if kind == "index_stats" and not config.get("index_stats_enabled", True):
        return "disabled"
    if kind == "profiler" and not config.get("profiler_enabled", False):
        return "disabled"
    deployment = config.get("deployment_type", "atlas")
    if source == "auto":
        if kind == "query_stats":
            source = "atlas_api" if deployment == "atlas" else "mongodb"
        elif kind in {"metrics", "advisor"}:
            source = "ops_manager" if deployment == "ops_manager" else "atlas_api" if deployment == "atlas" else "disabled"
        else:
            source = "mongodb" if config.get("mongodb_uri_template") else "disabled"
    supported = {
        "query_stats": {"bucket", "mongodb", "atlas_api", "disabled"},
        "index_definitions": {"bucket", "mongodb", "disabled"},
        "index_stats": {"bucket", "mongodb", "disabled"},
        "coll_stats": {"bucket", "mongodb", "disabled"},
        "query_plans": {"bucket", "mongodb", "disabled"},
        "profiler": {"bucket", "mongodb", "disabled"},
        "metrics": {"bucket", "atlas_api", "ops_manager", "disabled"},
        "advisor": {"bucket", "atlas_api", "ops_manager", "disabled"},
    }
    if source not in supported[kind]:
        raise ValueError(f"Unsupported source {source!r} for {kind}")
    return source
