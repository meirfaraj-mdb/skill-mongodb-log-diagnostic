"""Optional indexStats and query-shape observability collection stage."""
from __future__ import annotations
import json
from .common import Layout, logger
from . import skills


def _bucket_query_shape_receipts(store, layout: Layout, log_date: str) -> list[dict]:
    """Return already-uploaded query-shape files without accessing MongoDB or Atlas."""
    marker = "/queryStats/query-stats.json"
    day = layout.day(log_date) + "/"
    nodes = []
    for key in store.list_keys(day):
        if key.startswith(day) and key.endswith(marker):
            host = key[len(day):].split("/", 1)[0]
            nodes.append({"host": host, "queryStats": "reused_existing_bucket", "uri": store.uri(key)})
    return nodes


def run(config: dict, log_date: str, store=None) -> dict:
    from .providers import get_store
    store = store or get_store(config)
    layout = Layout.from_config(config)
    # Bucket-only mode is deliberately non-invasive. Existing query-shape files are
    # usable by report_stage, but this stage must not contact MongoDB or Atlas.
    if config.get("input_mode") == "existing_bucket":
        nodes = _bucket_query_shape_receipts(store, layout, log_date)
        return {"status": "reused_existing_bucket" if nodes else "no_existing_query_shapes",
                "reason": "Bucket-only mode never accesses MongoDB or Atlas", "nodes": nodes}
    if not config.get("observability_enabled", False):
        return {"status": "disabled", "nodes": []}

    collector = skills.observability()
    hosts = config.get("index_stats_hosts") or config.get("hostnames") or []
    if not hosts:
        raise ValueError("Set index_stats_hosts (or hostnames) when observability_enabled=true")
    source = collector.resolve_query_shape_source(config)
    results = []
    # Sequential on purpose: each node's collection/upload completes before next node.
    for host in hosts:
        node = {"host": host, "query_shape_source": source, "uploads": {}, "errors": []}
        if config.get("index_stats_enabled", True):
            try:
                data = collector.collect_index_stats(config, host)
                node["uploads"]["indexStats"] = store.put_text(
                    layout.index_stats(log_date, host), json.dumps(data, default=str, indent=2), "application/json")
            except Exception as exc:
                logger.exception("indexStats failed host=%s", host)
                node["errors"].append({"indexStats": str(exc)[:1000]})
        if source == "bucket":
            key = layout.query_stats(log_date, host)
            node["queryStats"] = "reused_existing_bucket" if store.exists(key) else "not_present_in_bucket"
        elif source != "disabled":
            try:
                data = collector.collect_query_shapes(config, host)
                node["uploads"]["queryStats"] = store.put_text(
                    layout.query_stats(log_date, host), json.dumps(data, default=str, indent=2), "application/json")
            except Exception as exc:
                logger.exception("queryStats failed host=%s", host)
                node["errors"].append({"queryStats": str(exc)[:1000]})
        results.append(node)
    return {"status": "completed", "query_shape_source": source, "nodes": results}
