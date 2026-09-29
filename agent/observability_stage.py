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
    nodes = []
    seen = set()
    for process in processes:
        host = process.get("hostname")
        if not host or host in seen:
            continue
        seen.add(host)
        # Query Shape Insights is cluster-scoped; filter its response to this
        # Atlas process using the documented processIds query parameter.
        port = process.get("port") or process.get("portNumber") or 27017
        configured_id = config.get("atlas_process_ids", {}).get(host)
        process_id = configured_id or (host if host.rsplit(":", 1)[-1].isdigit() and host.count(":") == 1 else f"{host}:{port}")
        nodes.append({"host": host, "host_dir": atlas.safe_filename(host), "process_id": process_id})
    if not nodes:
        raise RuntimeError("Atlas returned no data-bearing processes for observability.")
    return nodes


def _bucket_query_shape_receipts(store, layout: Layout, log_date: str) -> list[dict]:
    """Return already-uploaded query-shape files without accessing MongoDB or Atlas."""
    marker = "/queryStats/query-stats.json"
    day = layout.day(log_date) + "/"
    nodes = []
    for key in store.list_keys(day):
        if key.startswith(day) and key.endswith(marker):
            host_dir = key[len(day):].split("/", 1)[0]
            nodes.append({"host": host_dir, "queryStats": "reused_existing_bucket", "uri": store.uri(key)})
    return nodes


def run(config: dict, log_date: str, store=None) -> dict:
    from .providers import get_store

    store = store or get_store(config)
    layout = Layout.from_config(config)

    # Never contact Atlas or MongoDB in bucket-only mode.
    if config.get("input_mode") == "existing_bucket":
        nodes = _bucket_query_shape_receipts(store, layout, log_date)
        return {
            "status": "reused_existing_bucket" if nodes else "no_existing_query_shapes",
            "reason": "Bucket-only mode never accesses MongoDB or Atlas",
            "nodes": nodes,
        }
    if not config.get("observability_enabled", False):
        return {"status": "disabled", "nodes": []}

    # Atlas is the source of truth for Atlas/direct-MongoDB observability. This
    # permits observability before downloading logs and removes hostname prompts.
    nodes = _discover_atlas_nodes(config)
    collector = skills.observability()
    source = collector.resolve_query_shape_source(config)
    results = []

    # Sequential on purpose: a node's collection/upload completes before next.
    for node_info in nodes:
        host, host_dir = node_info["host"], node_info["host_dir"]
        node = {"host": host, "host_dir": host_dir, "query_shape_source": source, "uploads": {}, "errors": []}
        if config.get("index_stats_enabled", True):
            try:
                data = collector.collect_index_stats(config, host)
                node["uploads"]["indexStats"] = store.put_text(
                    layout.index_stats(log_date, host_dir), json.dumps(data, default=str, indent=2), "application/json"
                )
            except Exception as exc:
                logger.exception("indexStats failed host=%s", host)
                node["errors"].append({"indexStats": str(exc)[:1000]})
        if source == "bucket":
            key = layout.query_stats(log_date, host_dir)
            node["queryStats"] = "reused_existing_bucket" if store.exists(key) else "not_present_in_bucket"
        elif source != "disabled":
            try:
                data = collector.collect_query_shapes(config, host, node_info.get("process_id"))
                node["uploads"]["queryStats"] = store.put_text(
                    layout.query_stats(log_date, host_dir), json.dumps(data, default=str, indent=2), "application/json"
                )
            except Exception as exc:
                logger.exception("queryStats failed host=%s", host)
                node["errors"].append({"queryStats": str(exc)[:1000]})
        results.append(node)
    return {"status": "completed", "query_shape_source": source, "nodes": results}
