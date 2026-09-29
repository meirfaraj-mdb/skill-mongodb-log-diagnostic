"""Read-only MongoDB observability collectors.

Atlas Query Shape Insights uses the documented Atlas Admin API v2 endpoint:
GET /groups/{groupId}/clusters/{clusterName}/queryShapeInsights/summaries.
The endpoint is cluster-scoped; a per-node result is obtained with ``processIds``.
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


def collect_query_shapes(config: dict, host: str | None = None, process_id: str | None = None) -> dict:
    """Fetch the requested rolling window of Atlas query-shape summaries.

    ``process_id`` is normally derived from Atlas process discovery by the caller.
    Omit it only when a cluster-wide snapshot is explicitly wanted.
    """
    if not config.get("group_id") or not config.get("cluster_name"):
        raise ValueError("group_id and cluster_name are required for Atlas query-shape collection")
    if not config.get("atlas_public_key") or not config.get("atlas_private_key"):
        raise ValueError("Atlas API credentials are required for Atlas query-shape collection")

    window_hours = int(config.get("query_shape_window_hours", 24))
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=window_hours)
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
        "window": {"since": since.isoformat(), "until": until.isoformat(), "hours": window_hours},
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }


def _mongo_client(config: dict, host: str):
    template = config.get("mongodb_uri_template")
    if not template:
        raise ValueError("mongodb_uri_template is required for direct MongoDB observability")
    from pymongo import MongoClient
    return MongoClient(template.format(host=host), serverSelectionTimeoutMS=int(config.get("mongodb_timeout_ms", 30_000)))


def collect_query_stats_mongodb(config: dict, host: str) -> dict:
    with _mongo_client(config, host) as client:
        rows = list(client.admin.aggregate([{"$queryStats": {}}]))
    return {"source": "mongodb_queryStats", "host": host, "retrievedAt": datetime.now(timezone.utc).isoformat(), "data": rows}


def collect_index_stats(config: dict, host: str) -> dict:
    limit = int(config.get("index_stats_max_collections", 1000))
    collected: list[dict] = []
    with _mongo_client(config, host) as client:
        for db_name in client.list_database_names():
            if db_name in {"admin", "config", "local"}:
                continue
            db = client[db_name]
            for coll in db.list_collection_names():
                if coll.startswith("system."):
                    continue
                if len(collected) >= limit:
                    return {"source": "mongodb_indexStats", "host": host, "truncated": True, "collections": collected}
                collected.append({"namespace": f"{db_name}.{coll}", "indexes": list(db[coll].aggregate([{"$indexStats": {}}]))})
    return {"source": "mongodb_indexStats", "host": host, "truncated": False, "collections": collected}
