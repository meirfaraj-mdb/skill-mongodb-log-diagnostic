#!/usr/bin/env python3
"""Prepare per-node evidence with the deployment env file; local output unless --upload."""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_env(path):
    # Parse data, not shell code. Never source/execute user-controlled .env content.
    from dotenv import dotenv_values
    values = dotenv_values(path, interpolate=False)
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--log-date", help="Output day; defaults to yesterday in deployment timezone")
    parser.add_argument("--output-dir", default="observability-output")
    parser.add_argument("--upload", action="store_true", help="Write to configured INPUT_BUCKET (or BUCKET)")
    parser.add_argument("--collect-live", action="store_true", help="Explicitly collect live evidence even when deployment INPUT_MODE=existing_bucket")
    parser.add_argument("--hosts", nargs="+", help="Explicit node addresses; overrides env host list")
    args = parser.parse_args(argv)
    load_env(args.env_file)
    from agent.providers import load_config, LocalStore, get_store
    from agent.safe_logging import log_config, log_generated, with_write_logging
    from agent.observability_stage import run
    config = load_config()
    day = args.log_date or (datetime.now(ZoneInfo(config["timezone"])).date() - timedelta(days=1)).isoformat()
    date.fromisoformat(day)
    config["observability_enabled"] = True
    if args.collect_live and config["input_mode"] == "existing_bucket":
        config["input_mode"] = "atlas_api"
        from agent.observability_contract import ARTIFACTS, resolve_source
        if any(resolve_source(config, kind) == "atlas_api" for kind in ARTIFACTS):
            from agent.providers import load_raw_config
            original = os.environ.get("MONGODB_LOG_DIAG_INPUT_MODE")
            try:
                os.environ["MONGODB_LOG_DIAG_INPUT_MODE"] = "atlas_api"
                config.update(load_raw_config(config["cloud"]))
            finally:
                if original is None:
                    os.environ.pop("MONGODB_LOG_DIAG_INPUT_MODE", None)
                else:
                    os.environ["MONGODB_LOG_DIAG_INPUT_MODE"] = original
    if args.hosts:
        config["observability_hosts"] = args.hosts
    # Existing-bucket mode only inventories files. Live collection requires explicit atlas_api mode
    # (or a configuration loaded without Atlas credentials for MongoDB/Ops Manager sources).
    if config["input_mode"] == "existing_bucket":
        store = get_store(config)
    else:
        store = get_store(config) if args.upload else LocalStore(args.output_dir)
    log_config(config)  # Include CLI overrides in the effective configuration.
    store = with_write_logging(store)
    receipt = run(config, day, store=store)
    target = Path(args.output_dir)
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / "manifest.json"
    manifest.write_text(json.dumps(receipt, indent=2, default=str))
    log_generated(manifest.resolve().as_uri(), local_path=manifest)
    print(json.dumps(receipt, indent=2, default=str))
    if receipt["status"] == "partial":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
