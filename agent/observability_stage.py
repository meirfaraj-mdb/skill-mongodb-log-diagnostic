"""Optional indexStats and query-shape observability collection stage."""
from __future__ import annotations

import json

from .common import Layout, logger
from . import skills


def _discover_bucket_nodes(store, layout: Layout, log_date: str) -> list[dict]:
    """Discover nodes from objects already stored for a day (bucket-only mode)."""
    day = layout.day(log_date) + "/"
    nodes: dict[str, dict] = {}
    for key in store.list_keys(day):
        if not key.startswith(day):
            continue
        remainder = key[len(day):]
        parts = remainder.split("/")
        if len(parts) < 2 or parts[0] == "cluster":
            continue
        # Directory names are all we have in bucket-only mode.
        nodes.setdefault(parts[0], {"host": parts[0], "host_dir": parts[0]})
    return [nodes[name] for name in sorted(nodes)]


def _discover_atlas_nodes(config: dict) -> list[dict]:
    """Discover data-bearing Atlas processes; no manually supplied host list.

    ``host`` is retained for a direct MongoDB URI template. ``host_dir`` matches
    the downloader's bucket path, so punctuation-safe object keys remain stable.
    """
    atlas = skills.atlas_logs()
    processes = atlas.target_hosts(config)

    def _pid(process):
        host = process.get("hostname")
        port = process.get("port") or process.get("portNumber") or 27017
        return host if host.rsplit(":", 1)[-1].isdigit() and host.count(":") == 1 else f"{host}:{port}"

    def _facts(process):
        return {k: process.get(k) for k in ("typeName", "hostname", "userAlias", "port") if process.get(k) is not None}

    # On Atlas sharded clusters mongos shares the hostname of a shard mongod (other port).
    # Query Shape Insights is collected on mongos only for sharded clusters, so a host
    # running a mongos uses the mongos processId for query_stats.
    mongos = {}
    for process in processes:
        if process.get("hostname") and process.get("typeName") == "SHARD_MONGOS":
            mongos.setdefault(process["hostname"], process)
    nodes = []
    seen = set()
    for process in processes:
        host = process.get("hostname")
        if not host or host in seen:
            continue
        seen.add(host)
        # Query Shape Insights is cluster-scoped; filter its response to this
        # Atlas process using the documented processIds query parameter.
        configured_id = config.get("atlas_process_ids", {}).get(host)
        process_id = configured_id or _pid(process)
        node = {"host": host, "host_dir": atlas.safe_filename(host), "process_id": process_id,
                # Used to explain/retry empty Query Shape Insights results.
                "process": _facts(process)}
        router = mongos.get(host)
        if router is not None and not configured_id and router is not process:
            node["query_stats_process_id"] = _pid(router)
            node["query_stats_process"] = _facts(router)
        nodes.append(node)
    if not nodes:
        raise RuntimeError("Atlas returned no data-bearing processes for observability.")
    return nodes


def _call_collector(collector, config, info, kind, source, log_date):
    """Pass Atlas process facts when the collector supports them (older collectors/stubs do not)."""
    import inspect
    try:
        params = inspect.signature(collector.collect).parameters
        accepts = "process" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        accepts = False
    process_id, process = info.get("process_id"), info.get("process")
    if kind == "query_stats" and info.get("query_stats_process_id"):
        process_id, process = info["query_stats_process_id"], info.get("query_stats_process") or process
    if accepts and process:
        return collector.collect(config, info["host"], process_id, kind, source,
                                 log_date=log_date, process=process)
    return collector.collect(config, info["host"], process_id, kind, source, log_date=log_date)


def _query_stats_rows(text) -> int:
    """Number of query-shape rows in an existing query-stats file (QSI or mongosh $queryStats)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return 0

    def walk(obj, depth=0):
        if depth > 8:
            return 0
        if isinstance(obj, list):
            if any(isinstance(x, dict) and ("queryShapeHash" in x or ("key" in x and "metrics" in x)) for x in obj):
                return sum(1 for x in obj if isinstance(x, dict))
            return sum(walk(x, depth + 1) for x in obj)
        if isinstance(obj, dict):
            # The cluster-wide side copy never counts as this node's data.
            return sum(walk(v, depth + 1) for k, v in obj.items() if k != "cluster_wide")
        return 0
    return walk(data)


def collect_nodes(config, log_date, store, nodes):
    """One snapshot per kind/node; existing_bucket is strictly read-only.

    API collectors receive ``log_date`` so their windows match the log day in the
    configured project timezone (not a rolling window at collection time).
    """
    from .observability_contract import (ARTIFACTS, QueryShapeWindowUnavailable, artifact_key,
                                         resolve_source)
    layout = Layout.from_config(config)
    collector = None
    results = []
    cluster_key, cluster_uri = None, None
    for info in nodes:
        host, host_dir = info["host"], info["host_dir"]
        receipt = {"host": host, "host_dir": host_dir, "artifacts": {}, "errors": []}
        for kind in ARTIFACTS:
            source = None
            try:
                source = resolve_source(config, kind)
                key = artifact_key(layout, log_date, host_dir, kind)
                if source == "bucket":
                    receipt["artifacts"][kind] = {"status": "available" if store.exists(key) else "not_present",
                                                  "source": "bucket", "uri": store.uri(key)}
                elif source == "disabled":
                    receipt["artifacts"][kind] = {"status": "disabled"}
                elif (kind == "query_stats" and config.get("query_stats_skip_existing", True)
                      and store.exists(key) and _query_stats_rows(store.get_text(key))):
                    # Already collected with data (also older than QSI retention): keep it, no API call.
                    rows = _query_stats_rows(store.get_text(key))
                    receipt["artifacts"][kind] = {"status": "skipped_existing", "source": source,
                                                  "uri": store.uri(key), "shapes": rows}
                    logger.info("Query stats already present node=%s shapes=%d: skipped", host_dir, rows)
                else:
                    collector = collector or skills.observability()
                    data = _call_collector(collector, config, info, kind, source, log_date)
                    cluster_wide = data.pop("cluster_wide", None) if isinstance(data, dict) else None
                    if cluster_wide and cluster_key is None:
                        # Stored once per day: never duplicated as another node's evidence.
                        cluster_key = layout.cluster_query_stats(log_date)
                        cluster_uri = store.put_text(cluster_key, json.dumps(cluster_wide, default=str, indent=2),
                                                     "application/json")
                    receipt["artifacts"][kind] = {"status": "collected", "source": source,
                                                  "uri": store.put_text(key, json.dumps(data, default=str, indent=2), "application/json")}
                    if kind == "query_stats" and isinstance(data, dict) and data.get("scope") == "cluster":
                        receipt["artifacts"][kind]["scope"] = "cluster"
                    diagnostics = data.get("diagnostics") if isinstance(data, dict) else None
                    if kind == "query_stats" and isinstance(diagnostics, dict) and diagnostics.get("reason"):
                        receipt["artifacts"][kind]["empty_reason"] = diagnostics["reason"]
                        logger.info("Query Shape Insights node=%s reason=%s per_process=%s cluster_wide=%s",
                                    host_dir, diagnostics["reason"], diagnostics.get("per_process_summaries"),
                                    diagnostics.get("cluster_wide_summaries"))
                    window = data.get("window") if isinstance(data, dict) else None
                    if isinstance(window, dict):
                        receipt["artifacts"][kind]["window"] = window
            except QueryShapeWindowUnavailable as exc:
                # Expected for backfills older than API retention: never store data from
                # another day under this log day's key.
                logger.info("Observability window unavailable node=%s kind=%s: %s", host_dir, kind, exc)
                receipt["artifacts"][kind] = {"status": "out_of_retention", "source": source,
                                              "reason": str(exc)}
            except Exception as exc:
                # Raw driver/API messages can contain credentials and query literals.
                logger.warning("Observability failed node=%s kind=%s error_type=%s", host_dir, kind, type(exc).__name__)
                receipt["errors"].append({"kind": kind, "error_type": type(exc).__name__})
        results.append(receipt)
    errors = any(item["errors"] for item in results)
    output = {"status": "partial" if errors else "reused_existing_bucket" if config.get("input_mode") == "existing_bucket" else "completed",
            "nodes": results, "log_date": log_date,
            "collection_time_note": "Atlas API windows are aligned to the log day in the configured timezone; "
                                    "Query Shape Insights days older than the 7-day retention are out_of_retention."}
    if cluster_uri:
        output["cluster_query_stats"] = {"status": "collected", "uri": cluster_uri}
    return output


def run(config: dict, log_date: str, store=None) -> dict:
    from .providers import get_store
    store = store or get_store(config)
    layout = Layout.from_config(config)
    if config.get("input_mode") == "existing_bucket":
        nodes = _discover_bucket_nodes(store, layout, log_date)
    elif not config.get("observability_enabled", False):
        return {"status": "disabled", "nodes": []}
    elif config.get("observability_hosts"):
        from urllib.parse import quote
        nodes = [{"host": host, "host_dir": quote(host, safe="._-"),
                  "process_id": config.get("atlas_process_ids", {}).get(host)}
                 for host in config["observability_hosts"]]
    elif config.get("deployment_type", "atlas") == "atlas":
        nodes = _discover_atlas_nodes(config)
    else:
        raise ValueError("Set MONGODB_LOG_DIAG_OBSERVABILITY_HOSTS for non-Atlas deployments")
    return collect_nodes(config, log_date, store, nodes)
