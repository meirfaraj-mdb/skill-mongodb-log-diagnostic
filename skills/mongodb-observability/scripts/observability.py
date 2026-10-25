"""Read-only MongoDB observability collectors.

Atlas Query Shape Insights uses the documented Atlas Admin API v2 endpoint:
GET /groups/{groupId}/clusters/{clusterName}/queryShapeInsights/summaries.
The endpoint is cluster-scoped; a per-node result is obtained with ``processIds``.

API windows (query shapes, metrics, advisor) are aligned to the log day in the
configured project timezone, the same day boundaries used to download the logs.
Query Shape Insights only serves the last 7 days; older log days are rejected with
QueryShapeWindowUnavailable instead of silently returning another day's data.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener


def resolve_query_shape_source(config: dict) -> str:
    source = config.get("query_shape_source", "auto")
    if source == "auto":
        return "atlas_api" if config.get("deployment_type", "atlas") == "atlas" else "mongodb"
    if source not in {"atlas_api", "mongodb", "bucket", "disabled"}:
        raise ValueError("query_shape_source must be atlas_api, mongodb, bucket, disabled, or auto")
    return source


def _atlas_base(config: dict) -> str:
    return config.get("atlas_base_url", "https://cloud.mongodb.com/api/atlas/v2").rstrip("/")


def atlas_query_shape_url(config: dict) -> str:
    """Build the documented v2 summary endpoint; override remains optional."""
    override = config.get("atlas_query_stats_url")
    if override:
        return override.format(group_id=quote(config["group_id"], safe=""),
                               cluster_name=quote(config["cluster_name"], safe=""))
    return (
        f"{_atlas_base(config)}/groups/{quote(config['group_id'], safe='')}"
        f"/clusters/{quote(config['cluster_name'], safe='')}/queryShapeInsights/summaries"
    )


def _atlas_opener(config: dict):
    pm = HTTPPasswordMgrWithDefaultRealm()
    # The Atlas API uses Digest auth. Register against the origin and also the URL
    # because custom Atlas base URLs are supported for test/proxy environments.
    username, password = config["atlas_public_key"], config["atlas_private_key"]
    pm.add_password(None, "https://cloud.mongodb.com", username, password)
    pm.add_password(None, _atlas_base(config), username, password)
    return build_opener(HTTPDigestAuthHandler(pm))


def _process_id(host: str, port: int | str | None) -> str:
    # Atlas permits hostname:port. Preserve an already-qualified host:port.
    if host.rsplit(":", 1)[-1].isdigit() and host.count(":") == 1:
        return host
    return f"{host}:{port or 27017}"


def _timezone_name(config: dict) -> str:
    return config.get("timezone") or "UTC"


def collect_query_shapes(config: dict, host: str | None = None, process_id: str | None = None,
                         log_date: str | None = None, now: datetime | None = None) -> dict:
    """Fetch Atlas query-shape summaries for exactly the log day (project timezone).

    ``log_date`` (YYYY-MM-DD) is required. The day must lie within the 7-day Query Shape
    Insights retention, otherwise QueryShapeWindowUnavailable is raised. For the current
    day the window ends at ``now`` and ``window.complete_day`` is False.
    ``process_id`` is normally derived from Atlas process discovery by the caller.
    Omit it only when a cluster-wide snapshot is explicitly wanted.
    """
    if not config.get("group_id") or not config.get("cluster_name"):
        raise ValueError("group_id and cluster_name are required for Atlas query-shape collection")
    if not config.get("atlas_public_key") or not config.get("atlas_private_key"):
        raise ValueError("Atlas API credentials are required for Atlas query-shape collection")
    if not log_date:
        raise ValueError("log_date is required: query shapes must be aligned to the log day")
    tz_name = _timezone_name(config)
    since, until, complete_day = query_shape_window(log_date, tz_name, now)
    params: list[tuple[str, str]] = [
        ("since", str(int(since.timestamp() * 1000))),
        ("until", str(int(until.timestamp() * 1000))),
        ("nSummaries", str(min(max(int(config.get("query_stats_max_summaries", 100)), 1), 100))),
    ]
    requested_process_id = process_id or config.get("atlas_process_ids", {}).get(host or "")
    if requested_process_id:
        params.append(("processIds", requested_process_id))
    for command in config.get("query_shape_commands", []):
        params.append(("commands", str(command)))
    url = atlas_query_shape_url(config) + "?" + urlencode(params, doseq=True)
    api_version = config.get("api_version", "2025-03-12")
    request = Request(url, headers={
        "Accept": f"application/vnd.atlas.{api_version}+json",
        "User-Agent": "mongodb-log-diagnostic-observability/1.0",
    })
    with _atlas_opener(config).open(request, timeout=int(config.get("http_timeout_seconds", 120))) as response:
        payload = json.load(response)
    return {
        "source": "atlas_query_shape_insights",
        "endpoint": atlas_query_shape_url(config),
        "host": host,
        "processId": requested_process_id,
        "window": {"log_date": log_date, "timezone": tz_name,
                   "since": since.isoformat(), "until": until.isoformat(),
                   "hours": round((until - since).total_seconds() / 3600, 2),
                   "complete_day": complete_day},
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }


# Collector v2: imports are from the bundled application, not external services.
from agent.observability_contract import log_day_window, query_shape_window, sanitize, timestamp


def _envelope(source, host, basis, data, **extra):
    return sanitize({"schema_version": 2, "source": source, "host": host,
                     "retrievedAt": datetime.now(timezone.utc).isoformat(),
                     "temporal_basis": basis, "data": data, **extra})


def _bounded(cursor, limit):
    rows = []
    try:
        for row in cursor:
            if len(rows) >= limit:
                return rows, True
            rows.append(row)
        return rows, False
    finally:
        if hasattr(cursor, "close"):
            cursor.close()


def _mongo_client(config, host):
    template = config.get("mongodb_uri_template")
    if not template:
        raise ValueError("Set MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE for direct-node collection")
    uri = template.format(host=host)
    # Node-local counters require direct connections; do not silently read another node.
    if uri.startswith("mongodb+srv://"):
        raise ValueError("Node-local observability requires mongodb:// direct-node URI, not SRV")
    from pymongo import MongoClient
    return MongoClient(uri, directConnection=True,
                       serverSelectionTimeoutMS=int(config.get("mongodb_timeout_ms", 30000)),
                       connectTimeoutMS=int(config.get("mongodb_timeout_ms", 30000)),
                       socketTimeoutMS=int(config.get("mongodb_timeout_ms", 30000)),
                       appname="mongodb-log-diagnostic-observability")


def collect_query_stats_mongodb(config, host):
    with _mongo_client(config, host) as client:
        rows, truncated = _bounded(client.admin.aggregate(
            [{"$queryStats": {}}], maxTimeMS=int(config.get("mongodb_max_time_ms", 30000))),
            int(config.get("observability_max_rows", 1000)))
    return _envelope("mongodb_queryStats", host, "cumulative_since_entry_creation_or_reset", rows,
                     truncated=truncated,
                     limitations=["Not a rolling 24-hour series. Entries can reset or be evicted; use comparable snapshots for deltas."])


def collect_mongodb(config, host, kind):
    if kind == "query_stats":
        return collect_query_stats_mongodb(config, host)
    if kind == "query_plans":
        return collect_query_plans(config, host)
    limit = int(config.get("index_stats_max_collections", 1000))
    row_limit = int(config.get("observability_max_rows", 1000))
    max_time = int(config.get("mongodb_max_time_ms", 30000))
    collections, errors, truncated = [], [], False
    with _mongo_client(config, host) as client:
        for database in client.list_database_names():
            if database in {"admin", "config", "local"}:
                continue
            db = client[database]
            for name in db.list_collection_names():
                if name.startswith("system."):
                    continue
                if len(collections) >= limit:
                    truncated = True
                    break
                ns = f"{database}.{name}"
                try:
                    if kind == "index_definitions":
                        rows, capped = _bounded(db[name].list_indexes(), row_limit)
                        collections.append({"namespace": ns, "indexes": rows, "truncated": capped})
                    elif kind == "index_stats":
                        rows, capped = _bounded(db[name].aggregate([{"$indexStats": {}}], maxTimeMS=max_time), row_limit)
                        collections.append({"namespace": ns, "indexes": rows, "truncated": capped})
                    elif kind == "coll_stats":
                        rows, capped = _bounded(db[name].aggregate(
                            [{"$collStats": {"storageStats": {}, "latencyStats": {"histograms": False}, "count": {}}}],
                            maxTimeMS=max_time), row_limit)
                        collections.append({"namespace": ns, "stats": rows, "truncated": capped})
                    elif kind == "profiler":
                        # Never enable profiling or change its level. Read an existing profiler only.
                        continue
                    else:
                        raise ValueError("Unsupported MongoDB collector")
                except Exception as exc:
                    errors.append({"namespace": ns, "error_type": type(exc).__name__})
            if kind == "profiler":
                since = datetime.now(timezone.utc) - timedelta(hours=24)
                try:
                    rows, capped = _bounded(db["system.profile"].find({"ts": {"$gte": since}}).sort("ts", -1)
                                            .limit(row_limit + 1).max_time_ms(max_time), row_limit)
                    collections.append({"database": database, "records": rows, "truncated": capped})
                except Exception as exc:
                    errors.append({"database": database, "error_type": type(exc).__name__})
            if truncated:
                break
    basis = {"index_definitions": "current_snapshot", "index_stats": "since_index_counter_reset",
             "coll_stats": "current_storage_snapshot_and_cumulative_latency_counters",
             "profiler": "available_profiler_records_last_24h"}[kind]
    result = _envelope("mongodb_" + kind, host, basis, {}, collections=collections,
                       errors=errors, truncated=truncated)
    if kind == "profiler":
        result["limitations"] = ["Coverage depends on pre-existing profiler sampling, filtering and retention. Collection does not enable profiling."]
    return result


def collect_index_stats(config, host):
    return collect_mongodb(config, host, "index_stats")


def collect_query_plans(config, host):
    """Explain explicitly supplied find shapes, queryPlanner only; never execute arbitrary commands."""
    plans, errors = [], []
    with _mongo_client(config, host) as client:
        for item in config.get("query_plan_requests", [])[:int(config.get("observability_max_rows", 1000))]:
            try:
                namespace = item["namespace"]
                database, collection = namespace.split(".", 1)
                if database in {"admin", "local", "config"} or collection.startswith("system."):
                    raise ValueError("Internal collection explain is not permitted")
                command = {"find": collection, "filter": item.get("filter", {})}
                if "sort" in item:
                    command["sort"] = item["sort"]
                if "projection" in item:
                    command["projection"] = item["projection"]
                plan = client[database].command({"explain": command, "verbosity": "queryPlanner",
                                                 "maxTimeMS": int(config.get("mongodb_max_time_ms", 30000))})
                plans.append({"namespace": namespace, "queryShapeHash": item.get("queryShapeHash"),
                              "queryHash": item.get("queryHash"), "plan": plan})
            except Exception as exc:
                errors.append({"error_type": type(exc).__name__})
    return _envelope("mongodb_explain", host, "current_plan_snapshot", plans, errors=errors,
                     limitations=["Only explicitly configured find requests; queryPlanner does not measure execution time."])


def _api_pages(config, url, source):
    """Bounded pagination, same-origin HTTPS only; never persist raw API errors or auth headers."""
    from urllib.parse import urljoin, urlsplit
    origin = urlsplit(url)
    if origin.scheme != "https":
        raise ValueError("Observability API must use HTTPS")
    if source == "atlas_api":
        opener = _atlas_opener(config)
        accept = f"application/vnd.atlas.{config.get('api_version', '2025-03-12')}+json"
    else:
        pm = HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, f"{origin.scheme}://{origin.netloc}",
                        config["ops_manager_public_key"], config["ops_manager_private_key"])
        opener = build_opener(HTTPDigestAuthHandler(pm))
        accept = "application/json"
    pages, next_url = [], url
    for _ in range(int(config.get("observability_api_max_pages", 10))):
        parsed = urlsplit(next_url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("Refusing cross-origin API pagination")
        with opener.open(Request(next_url, headers={"Accept": accept}),
                         timeout=int(config.get("http_timeout_seconds", 120))) as response:
            payload = json.load(response)
        pages.append(payload)
        links = payload.get("links", []) if isinstance(payload, dict) else []
        next_link = next((x.get("href") for x in links if x.get("rel") == "next"), None)
        if not next_link:
            return pages, False
        next_url = urljoin(next_url, next_link)
    return pages, True


def collect_api(config, host, process_id, kind, source, log_date=None, now=None):
    if not log_date:
        raise ValueError("log_date is required: API windows must be aligned to the log day")
    if kind == "query_stats":
        if source != "atlas_api":
            raise ValueError("Ops Manager query statistics API is unsupported")
        result = collect_query_shapes(config, host, process_id, log_date=log_date, now=now)
        result["schema_version"] = 2
        result["temporal_basis"] = "log_day_window"
        result["limitations"] = ["Aligned to the log day in the configured project timezone; "
                                 "partial when window.complete_day is false. Top summaries only "
                                 "(nSummaries <= 100); sampling depends on the API."]
        return sanitize(result)
    if kind not in {"metrics", "advisor"}:
        raise ValueError("Unsupported API kind")
    group = quote(config["group_id"], safe="")
    if source == "atlas_api":
        target = quote(process_id or _process_id(host, 27017), safe="")
        base = f"{_atlas_base(config)}/groups/{group}/processes/{target}"
    else:
        base_url = config.get("ops_manager_base_url", "").rstrip("/")
        target_id = config.get("ops_manager_host_ids", {}).get(host)
        if not base_url or not target_id:
            raise ValueError("Ops Manager base URL and explicit host-ID mapping are required")
        base = f"{base_url}/groups/{group}/hosts/{quote(target_id, safe='')}"
    tz_name = _timezone_name(config)
    now = now or datetime.now(timezone.utc)
    start, day_end = log_day_window(log_date, tz_name)
    if start >= now:
        raise ValueError(f"log day {log_date} has not started yet")
    end = min(day_end, now)
    if kind == "metrics":
        params = {"start": start.isoformat(), "end": end.isoformat(), "granularity": "PT1H"}
        url = base + "/measurements?" + urlencode(params)
    else:
        params = {"since": int(start.timestamp() * 1000),
                  "duration": int((end - start).total_seconds() * 1000)}
        url = base + "/performanceAdvisor/suggestedIndexes?" + urlencode(params)
    pages, truncated = _api_pages(config, url, source)
    return _envelope(source + "_" + kind, host, "log_day_window", pages, truncated=truncated,
                     window={"log_date": log_date, "timezone": tz_name,
                             "since": start.isoformat(), "until": end.isoformat(),
                             "hours": round((end - start).total_seconds() / 3600, 2),
                             "complete_day": day_end <= now},
                     limitations=["API support, permissions and retention depend on deployment version/tier; unsupported responses are reported, not replaced with fabricated data."])


def collect(config, host, process_id, kind, source, log_date=None):
    if source == "mongodb":
        return collect_mongodb(config, host, kind)
    return collect_api(config, host, process_id, kind, source, log_date=log_date)
