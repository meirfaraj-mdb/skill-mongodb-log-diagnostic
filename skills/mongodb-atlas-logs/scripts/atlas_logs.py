#!/usr/bin/env python3
"""Cloud-agnostic MongoDB Atlas process-log downloader.

Uses only Python's standard library.  It supports the project pipeline through
``archive_logs`` and can also archive logs to a local directory from the CLI.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
DEFAULT_BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"
SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")
REQUIRED_KEYS = ("atlas_public_key", "atlas_private_key", "group_id", "cluster_name", "timezone", "api_version")


def safe(value: str) -> str:
    """Return a storage-safe hostname/log component."""
    return SAFE_PART.sub("_", str(value)).strip("_") or "unknown"


def previous_day(timezone: str) -> str:
    return (datetime.now(ZoneInfo(timezone)) - timedelta(days=1)).date().isoformat()


def day_window(log_date: str, timezone: str) -> tuple[int, int]:
    """Local-midnight epoch window. DST days may be 23/24/25 hours."""
    day = datetime.strptime(log_date, "%Y-%m-%d").date()
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def validate_config(config: dict) -> None:
    missing = [key for key in REQUIRED_KEYS if not config.get(key)]
    if missing:
        raise ValueError("Missing Atlas configuration key(s): " + ", ".join(missing))
    try:
        ZoneInfo(str(config["timezone"]))
    except Exception as exc:
        raise ValueError(f"Invalid timezone {config['timezone']!r}") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{24}", str(config["group_id"])):
        raise ValueError("group_id must be a 24-character Atlas project ID")
    log_names = config.get("log_names", ["auto"])
    if isinstance(log_names, str):
        log_names = [log_names]
    allowed = {"auto", "mongodb", "mongos", "mongodb-audit-log", "mongos-audit-log"}
    unsupported = set(log_names) - allowed
    if unsupported:
        raise ValueError("Unsupported log_names: " + ", ".join(sorted(unsupported)))


class AtlasClient:
    """Small Atlas Admin API v2 client using HTTP Digest authentication."""

    def __init__(self, config: dict):
        validate_config(config)
        self.config = config
        self.base_url = str(config.get("atlas_base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = int(config.get("http_timeout_seconds", 120))
        self.retries = int(config.get("max_retries", 5))
        manager = HTTPPasswordMgrWithDefaultRealm()
        # A default realm is required because Atlas supplies the Digest realm in 401.
        manager.add_password(None, self.base_url, str(config["atlas_public_key"]), str(config["atlas_private_key"]))
        self.opener = build_opener(HTTPDigestAuthHandler(manager))

    def _url(self, path: str, query: dict | None = None) -> str:
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        return url

    def _request(self, path: str, query: dict | None = None):
        url = self._url(path, query)
        headers = {
            "Accept": f"application/vnd.atlas.{self.config['api_version']}+json",
            "User-Agent": "mongodb-log-diagnostic-atlas-skill/1.0",
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self.opener.open(Request(url, headers=headers), timeout=self.timeout)
            except HTTPError as exc:
                # Do not retry bad credentials/invalid input/not found; retries are for limits/outages.
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt >= self.retries:
                    raise
                last_error = exc
            except URLError as exc:
                if attempt >= self.retries:
                    raise
                last_error = exc
            delay = min(30, 2 ** attempt)
            LOG.warning("Atlas request retry %s/%s after %s", attempt + 1, self.retries, type(last_error).__name__)
            time.sleep(delay)
        assert last_error is not None
        raise last_error

    def get_json(self, path: str, query: dict | None = None) -> dict:
        with self._request(path, query) as response:
            return json.loads(response.read().decode("utf-8"))

    def download_log(self, group: str, host: str, log_name: str, start: int, end: int) -> BinaryIO:
        # Atlas process names contain dots/colons; quote them as one path segment.
        path = f"groups/{quote(group, safe='')}/processes/{quote(host, safe='')}/logs/{quote(log_name, safe='')}.gzip"
        return self._request(path, {"startDate": start, "endDate": end})


def _process_name(process: dict) -> str:
    return str(process.get("userAlias") or process.get("hostname") or process.get("id") or "")


def _selected_processes(processes: list[dict], config: dict) -> list[dict]:
    exact = {str(x) for x in (config.get("hostnames") or [])}
    selector = config.get("host_selector")
    regex = re.compile(str(selector)) if selector else None
    cluster = str(config["cluster_name"])
    out: list[dict] = []
    for process in processes:
        if str(process.get("typeName", "")).upper() == "NO_DATA":
            continue
        name = _process_name(process)
        host = str(process.get("hostname") or "")
        searchable = f"{host} {name}"
        selected = (name in exact or host in exact) if exact else (bool(regex.search(searchable)) if regex else (name == cluster or name.startswith(cluster + "-") or host == cluster or host.startswith(cluster + "-")))
        if selected and name:
            out.append(process)
    return out


def _log_names(process: dict, configured: Any) -> list[str]:
    values = [configured] if isinstance(configured, str) else list(configured or ["auto"])
    type_name = str(process.get("typeName", "")).upper()
    default = "mongos" if "MONGOS" in type_name else "mongodb"
    return [default if item == "auto" else str(item) for item in values]


def _metadata(config: dict, entry: dict, start: int, end: int) -> dict[str, str]:
    return {
        "cluster": str(config["cluster_name"]), "host": entry["host"], "log-name": entry["log_name"],
        "log-date": entry["log_date"], "start-date": str(start), "end-date": str(end),
    }


def archive_logs(config: dict, log_date: str | None = None, *, sink: Callable[[dict, BinaryIO, dict], Any],
                 exists: Callable[[dict], bool] | None = None, client: Any | None = None) -> dict:
    """Discover selected Atlas processes and stream logs to ``sink`` sequentially.

    Errors for one process/log are returned in ``failed``; the invocation only
    raises when every requested log failed (and none were skipped as existing).
    """
    validate_config(config)
    date = log_date or previous_day(str(config["timezone"]))
    start, end = day_window(date, str(config["timezone"]))
    client = client or AtlasClient(config)
    listing = client.get_json(f"groups/{quote(str(config['group_id']), safe='')}/clusters/{quote(str(config['cluster_name']), safe='')}/processes")
    processes = _selected_processes(listing.get("results", []), config)
    if not processes:
        raise RuntimeError(f"No Atlas processes matched cluster/selector for {config['cluster_name']!r}")

    logs: list[dict] = []
    failed: list[dict] = []
    for process in processes:
        host = _process_name(process)
        host_dir = safe(host)
        for log_name in _log_names(process, config.get("log_names", ["auto"])):
            entry = {"host": host, "host_dir": host_dir, "log_name": log_name, "log_date": date,
                     "relative_path": f"{date}/{host_dir}/mongodb/{safe(log_name)}.gz",
                     "process_type": process.get("typeName")}
            if exists and exists(entry):
                entry["status"] = "skipped_existing"
                logs.append(entry)
                continue
            try:
                stream = client.download_log(str(config["group_id"]), host, log_name, start, end)
                try:
                    entry["uri"] = sink(entry, stream, _metadata(config, entry, start, end))
                finally:
                    close = getattr(stream, "close", None)
                    if close:
                        close()
                entry["status"] = "downloaded"
                logs.append(entry)
            except Exception as exc:
                LOG.warning("Atlas log download failed host=%s log=%s error=%s", host, log_name, type(exc).__name__)
                failed.append({"host": host, "log_name": log_name, "error": str(exc), "error_type": type(exc).__name__})
    if not logs:
        raise RuntimeError(f"No Atlas logs were produced for {date}; failures={failed}")
    return {"log_date": date, "cluster": config["cluster_name"], "logs": logs, "failed": failed}


def local_dir_sink(output_dir: str | Path) -> Callable[[dict, BinaryIO, dict], str]:
    root = Path(output_dir)
    def sink(entry: dict, stream: BinaryIO, metadata: dict) -> str:
        path = root / entry["relative_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            while True:
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        return str(path)
    return sink


def _load_config(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    import os
    raw = os.environ.get("ATLAS_CONFIG_JSON")
    if not raw:
        raise ValueError("Pass --config FILE or set ATLAS_CONFIG_JSON")
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download MongoDB Atlas process logs")
    parser.add_argument("--config", help="JSON configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-hosts")
    download = commands.add_parser("download")
    download.add_argument("--output-dir", required=True)
    download.add_argument("--log-date")
    args = parser.parse_args(argv)
    config = _load_config(args.config)
    try:
        if args.command == "list-hosts":
            client = AtlasClient(config)
            payload = client.get_json(f"groups/{quote(str(config['group_id']), safe='')}/clusters/{quote(str(config['cluster_name']), safe='')}/processes")
            selected = _selected_processes(payload.get("results", []), config)
            print(json.dumps({"cluster": config["cluster_name"], "hosts": [{"hostname": _process_name(x), "typeName": x.get("typeName")} for x in selected]}, indent=2))
            return 0
        output = Path(args.output_dir)
        result = archive_logs(config, args.log_date, sink=local_dir_sink(output),
                              exists=lambda entry: (output / entry["relative_path"]).is_file())
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
