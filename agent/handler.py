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


def run_pipeline(event: dict, config: dict | None = None) -> dict:
    config = config or _config()
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
    if stage in ("all", "report", "cluster-summary"):
        out["report"] = report_stage.run(
            config, log_date, summary_only=(stage == "cluster-summary"))
    return out


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    logger.info("Lambda invocation stage=%s log_date=%s", event.get("stage", "all"),
                event.get("log_date"))
    return run_pipeline(event)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="MongoDB log diagnostic agent")
    parser.add_argument("--stage", default=os.environ.get("STAGE", "all"),
                        choices=["all", "download", "extract", "observability", "report", "cluster-summary"])
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
