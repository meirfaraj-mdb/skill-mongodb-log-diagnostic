"""Read-only MongoDB index and query-shape observability collector."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import requests


def resolve_query_shape_source(config: dict) -> str:
    source = str(config.get("query_shape_source", "auto")).lower()
    if source == "auto":
        return "atlas_api" if config.get("deployment_type", "atlas").lower() == "atlas" else "mongodb"
    if source not in {"atlas_api", "mongodb", "bucket", "disabled"}:
        raise ValueError("query_shape_source must be atlas_api|mongodb|bucket|disabled|auto")
    return source


def _client(config: dict, host: str):
    from pymongo import MongoClient
    template = config.get("mongodb_uri_template")
    if not template:
        raise ValueError("mongodb_uri_template is required for direct MongoDB observability")
    return MongoClient(template.replace("{host}", host), appname="mongodb-log-diagnostic", serverSelectionTimeoutMS=15000)


def collect_index_stats(config: dict, host: str) -> dict:
    max_collections = int(config.get("index_stats_max_collections", 1000))
    rows, seen = [], 0
    with _client(config, host) as client:
        for db_info in client.list_databases(nameOnly=True).get("databases", []):
            db_name = db_info["name"]
            if db_name in {"admin", "config", "local"}:
                continue
            db = client[db_name]
            for coll_info in db.list_collections():
                if coll_info.get("type") != "collection" or coll_info["name"].startswith("system."):
                    continue
                seen += 1
                if seen > max_collections:
                    return {"host": host, "truncated": True, "max_collections": max_collections, "collections": rows}
                rows.append({"namespace": f"{db_name}.{coll_info['name']}", "indexes": list(db[coll_info['name']].aggregate([{"$indexStats": {}}]))})
    return {"host": host, "truncated": False, "collections": rows}


def _atlas_url(config: dict, host: str) -> str:
    template = config.get("atlas_query_stats_url")
    if not template:
        raise ValueError("atlas_query_stats_url is required for Atlas query-shape collection")
    return template.format(group_id=quote(str(config["group_id"]), safe=""), cluster_name=quote(str(config["cluster_name"]), safe=""), host=quote(host, safe=""))


def collect_query_shapes(config: dict, host: str) -> dict:
    source = resolve_query_shape_source(config)
    if source == "mongodb":
        hours = int(config.get("query_shape_window_hours", 24))
        limit = int(config.get("query_stats_max_summaries", 100))
        with _client(config, host) as client:
            rows = list(client.admin.aggregate([{"$queryStats": {}}, {"$limit": limit}]))
        return {"host": host, "source": "mongodb", "captured_at": datetime.now(timezone.utc).isoformat(), "requested_window_hours": hours, "summaries": rows}
    if source != "atlas_api":
        raise ValueError(f"Cannot collect source {source!r}")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=int(config.get("query_shape_window_hours", 24)))
    params = {config.get("atlas_query_stats_start_param", "startDate"): start.isoformat().replace("+00:00", "Z"), config.get("atlas_query_stats_end_param", "endDate"): end.isoformat().replace("+00:00", "Z")}
    process_id = (config.get("atlas_process_ids") or {}).get(host)
    if process_id:
        params[config.get("atlas_query_stats_process_id_param", "processIds")] = process_id
    response = requests.get(_atlas_url(config, host), params=params, auth=(config["atlas_public_key"], config["atlas_private_key"]), headers={"Accept": f"application/vnd.atlas.{config.get('api_version', '2025-03-12')}+json"}, timeout=int(config.get("http_timeout_seconds", 120)))
    response.raise_for_status()
    payload = response.json()
    return {"host": host, "source": "atlas_api", "window_start": params[config.get("atlas_query_stats_start_param", "startDate")], "window_end": params[config.get("atlas_query_stats_end_param", "endDate")], "payload": payload}
