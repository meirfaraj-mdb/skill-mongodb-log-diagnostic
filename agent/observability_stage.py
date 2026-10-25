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


def collect_nodes(config, log_date, store, nodes):
    """One snapshot per kind/node; existing_bucket is strictly read-only."""
    from .observability_contract import ARTIFACTS, artifact_key, resolve_source
    layout = Layout.from_config(config)
    collector = None
    results = []
    for info in nodes:
        host, host_dir = info["host"], info["host_dir"]
        receipt = {"host": host, "host_dir": host_dir, "artifacts": {}, "errors": []}
        for kind in ARTIFACTS:
            try:
                source = resolve_source(config, kind)
                key = artifact_key(layout, log_date, host_dir, kind)
                if source == "bucket":
                    receipt["artifacts"][kind] = {"status": "available" if store.exists(key) else "not_present",
                                                     "source": "bucket", "uri": store.uri(key)}
                elif source == "disabled":
                    receipt["artifacts"][kind] = {"status": "disabled"}
                else:
                    collector = collector or skills.observability()
                    data = collector.collect(config, host, info.get("process_id"), kind, source)
                    receipt["artifacts"][kind] = {"status": "collected", "source": source,
                        "uri": store.put_text(key, json.dumps(data, default=str, indent=2), "application/json")}
            except Exception as exc:
                # Raw driver/API messages can contain credentials and query literals.
                logger.warning("Observability failed node=%s kind=%s error_type=%s", host_dir, kind, type(exc).__name__)
                receipt["errors"].append({"kind": kind, "error_type": type(exc).__name__})
        results.append(receipt)
    errors = any(item["errors"] for item in results)
    return {"status": "partial" if errors else "reused_existing_bucket" if config.get("input_mode") == "existing_bucket" else "completed",
            "nodes": results, "log_date": log_date,
            "collection_time_note": "Live API windows are rolling at collection time, not necessarily the log day."}


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
