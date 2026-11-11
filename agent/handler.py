"""Entry points for the MongoDB log diagnostic pipeline."""
from __future__ import annotations

import argparse
import json
import os

from . import download_stage, extract_stage, observability_stage, report_stage, skills
from .common import logger
from .providers import load_config

_CONFIG = None


def _config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def _all_clusters_mode(config: dict) -> bool:
    """Empty cluster_name in atlas_api mode = every cluster of the project, one folder each."""
    return config.get("input_mode", "atlas_api") == "atlas_api" and not str(config.get("cluster_name") or "").strip()


def cluster_config(config: dict, cluster_name: str) -> dict:
    """Config for one cluster of an all-clusters run: <prefix>/<cluster>/<day>/..."""
    folder = skills.atlas_logs().safe_filename(cluster_name)
    return {**config, "cluster_name": cluster_name,
            "prefix": f"{config['prefix'].strip('/')}/{folder}"}


def run_all_clusters(event: dict, config: dict) -> dict:
    """No cluster selected: list the project clusters and run the stages once per cluster.

    Each cluster is isolated under ``<prefix>/<cluster>/...`` (only its own nodes), so
    extraction, observability, reports and notifications never mix clusters. A failing or
    empty cluster does not stop the others; the run fails only when every cluster failed.
    """
    atlas = skills.atlas_logs()
    clusters = event.get("clusters") or atlas.list_clusters(atlas.AtlasClient(config), config["group_id"])
    log_date = event.get("log_date") or os.environ.get("LOG_DATE")
    if not log_date or log_date == "auto":
        log_date = atlas.previous_day(config["timezone"])
    if not clusters:
        raise RuntimeError(f"No cluster found in project {config.get('group_id')}")
    stage = event.get("stage") or "all"
    # Explicit log selections (AWS Step Functions extract map) carry their cluster.
    by_cluster: dict | None = None
    if event.get("logs"):
        by_cluster = {}
        for entry in event["logs"]:
            by_cluster.setdefault(entry.get("cluster"), []).append(entry)
        clusters = [c for c in clusters if c in by_cluster] or [c for c in by_cluster if c]
    logger.info("No cluster selected: %d cluster(s) in project: %s", len(clusters), clusters)
    out: dict = {"log_date": log_date, "stage": stage, "cloud": config.get("cloud"), "clusters": {}}
    all_logs, failures = [], []
    for name in clusters:
        cfg = cluster_config(config, name)
        cluster_event = {**event, "log_date": log_date}
        if by_cluster is not None:
            cluster_event["logs"] = by_cluster.get(name, [])
        if stage not in ("all", "download", "observability") and not _has_day_data(cfg, log_date):
            logger.info("Cluster %s has no stored data for %s: skipped", name, log_date)
            out["clusters"][name] = {"prefix": cfg["prefix"], "status": "skipped_no_data"}
            continue
        try:
            result = run_pipeline(cluster_event, cfg)
            out["clusters"][name] = {"prefix": cfg["prefix"], **result}
            for entry in (result.get("download") or {}).get("logs") or []:
                all_logs.append({**entry, "cluster": name})
        except Exception as error:
            if isinstance(error, atlas.NoMatchingHosts):
                logger.warning("Cluster %s has no data-bearing process: skipped", name)
                out["clusters"][name] = {"prefix": cfg["prefix"], "status": "skipped_no_hosts"}
                continue
            logger.exception("Cluster %s failed", name)
            failures.append(name)
            out["clusters"][name] = {"prefix": cfg["prefix"], "status": "failed",
                                     "error": f"{type(error).__name__}: {str(error)[:500]}"}
    if failures and len(failures) == len(clusters):
        raise RuntimeError(f"All clusters failed: {failures}")
    if failures:
        out["failed_clusters"] = failures
    if stage in ("all", "download"):
        # Flat list (each entry tagged with its cluster) for the per-log extract fan-out.
        out["download"] = {"log_date": log_date, "logs": all_logs}
    return out


def _has_day_data(config: dict, log_date: str) -> bool:
    from .common import Layout
    from .providers import get_store
    try:
        day = Layout.from_config(config).day(log_date) + "/"
        return any(True for _ in get_store(config).list_keys(day))
    except Exception:  # unknown: let the stage itself decide
        return True


def run_pipeline(event: dict, config: dict | None = None) -> dict:
    config = config or _config()
    if _all_clusters_mode(config):
        # Each cluster then re-enters here with its own non-empty cluster_name and prefix.
        return run_all_clusters(event, config)
    stage = event.get("stage") or "all"
    log_date = event.get("log_date") or os.environ.get("LOG_DATE")
    if not log_date or log_date == "auto":
        log_date = skills.atlas_logs().previous_day(config["timezone"])
    out: dict = {"log_date": log_date, "stage": stage, "cloud": config.get("cloud")}
    logs = event.get("logs")
    if stage in ("all", "download"):
        if config.get("input_mode") == "existing_bucket":
            out["download"] = {"status": "skipped_existing_bucket", "log_date": log_date,
                               "logs": logs or []}
        else:
            out["download"] = download_stage.run(
                config, log_date, skip_existing=event.get("skip_existing", True))
            logs = out["download"]["logs"]
    if stage in ("all", "extract"):
        out["extract"] = extract_stage.run(
            config, log_date, logs=logs,
            skip_existing=not event.get("force_reextract", False))
    if stage in ("all", "observability"):
        if config.get("input_mode") == "existing_bucket":
            out["observability"] = {"status": "skipped_existing_bucket", "log_date": log_date}
        else:
            out["observability"] = observability_stage.run(config, log_date)
    if stage in ("all", "report", "cluster-summary", "cluster-summary-md"):
        if stage == "cluster-summary-md":
            # Only the alternative summary built from the stored node report.md files.
            config = {**config, "cluster_summary_source": "node_reports"}
        out["report"] = report_stage.run(
            config, log_date, summary_only=stage in ("cluster-summary", "cluster-summary-md"))
    if stage == "notify":
        # Re-run only the P0 Jira / P1 Slack step from the stored cluster reports (idempotent: ledger + JQL).
        from . import notifications
        out["notify"] = notifications.run(config, log_date)
    return out


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    logger.info("Lambda invocation stage=%s log_date=%s", event.get("stage", "all"),
                event.get("log_date"))
    return run_pipeline(event)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="MongoDB log diagnostic agent")
    parser.add_argument("--stage", default=os.environ.get("STAGE", "all"),
                        choices=["all", "download", "extract", "observability", "report", "cluster-summary",
                                 "cluster-summary-md", "notify"])
    parser.add_argument("--log-date", default=None)
    parser.add_argument("--no-skip-existing", action="store_true",
                        help="re-download logs even if already stored")
    parser.add_argument("--force-reextract", action="store_true",
                        help="re-run a node extraction even when it already exists in storage")
    args = parser.parse_args(argv)
    result = run_pipeline({"stage": args.stage, "log_date": args.log_date,
                           "skip_existing": not args.no_skip_existing,
                           "force_reextract": args.force_reextract})
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
