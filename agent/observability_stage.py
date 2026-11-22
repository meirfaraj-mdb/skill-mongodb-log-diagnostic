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


_TRANSIENT_STATUSES = {408, 425, 429}


def _transient(error) -> bool:
    """Retryable failure (throttling, 5xx, network) as opposed to a metric this process never has."""
    if not isinstance(error, dict):
        return True
    status = error.get("status")
    if status is None:
        # NotReturned: Atlas answered but has no such series for this partition (permanent).
        return error.get("error_type") != "NotReturned"
    try:
        status = int(status)
    except (TypeError, ValueError):
        return True
    return status in _TRANSIENT_STATUSES or status >= 500


def _wanted_measurements(config, source) -> tuple[list, list]:
    """Process and disk measurement names the collector would request with this config."""
    if source != "atlas_api":
        return [], []
    from .metrics_catalog import plan_measurement_requests
    from .resource_investigation import DISK_METRICS, INVESTIGATION_METRICS
    value = config.get("resource_investigation_enabled", True)
    investigate = value if isinstance(value, bool) else str(value).strip().lower() not in {"0", "false", "no", "off"}
    plan = plan_measurement_requests(config, extra=INVESTIGATION_METRICS if investigate else [])
    disk = list(dict.fromkeys(plan.get("disk", []) + (DISK_METRICS if investigate else [])))
    return plan["requested"], disk


def _reusable_metrics(text, config, log_date, source) -> tuple[bool, str]:
    """Whether a stored metrics.json can replace a new Atlas/Ops Manager call.

    Reused only when it covers the whole log day (a file written while the day was still in
    progress is refreshed), in the same timezone and requested granularity, holds every
    measurement this config needs, and has no retryable (429/5xx/network) failure.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False, "unreadable"
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        return False, "old_or_unknown_format"
    if data.get("source") != f"{source}_metrics":
        return False, "different_source"
    if not data.get("data"):
        return False, "no_data"
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    if window.get("log_date") != log_date or window.get("complete_day") is not True:
        return False, "incomplete_day"
    if (window.get("timezone") or "UTC") != (config.get("timezone") or "UTC"):
        return False, "different_timezone"
    wanted_granularity = str(config.get("metrics_granularity") or "PT5M").upper()
    if str(window.get("requested_granularity") or window.get("granularity") or "").upper() != wanted_granularity:
        return False, "different_granularity"
    plan = data.get("measurement_plan") if isinstance(data.get("measurement_plan"), dict) else {}
    process_names, disk_names = _wanted_measurements(config, source)
    if set(process_names) - set(plan.get("requested") or []):
        return False, "missing_measurements"
    if disk_names:
        stored_disk = plan.get("disk")
        if stored_disk is not None and set(disk_names) - set(stored_disk):
            return False, "missing_disk_measurements"
        if stored_disk is None and not data.get("disk_measurements") and not data.get("disk_errors"):
            return False, "missing_disk_measurements"
    if any(_transient(e) for e in (data.get("errors") or []) + (data.get("disk_errors") or [])):
        return False, "transient_errors"
    return True, "complete"


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
                reused, refetch = None, None
                if (kind == "metrics" and source in {"atlas_api", "ops_manager"}
                        and config.get("metrics_skip_existing", True) and store.exists(key)):
                    text = store.get_text(key)
                    ok, why = _reusable_metrics(text, config, log_date, source)
                    if ok:
                        reused = json.loads(text)
                    else:
                        refetch = why
                        logger.info("Metrics present but refreshed node=%s reason=%s", host_dir, why)
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
                elif reused is not None:
                    # Complete log day already stored: keep it, no Atlas/Ops Manager call.
                    receipt["artifacts"][kind] = {"status": "skipped_existing", "source": source,
                                                  "uri": store.uri(key), "window": reused.get("window")}
                    logger.info("Metrics already present node=%s: skipped", host_dir)
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
                    if refetch:
                        receipt["artifacts"][kind]["refetch_reason"] = refetch
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
