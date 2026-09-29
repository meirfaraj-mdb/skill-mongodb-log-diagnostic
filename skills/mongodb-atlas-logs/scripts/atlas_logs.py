#!/usr/bin/env python3
"""Cloud-agnostic MongoDB Atlas log downloader."""
from __future__ import annotations
import json, logging, re, shutil, time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener
from zoneinfo import ZoneInfo

logger = logging.getLogger("atlas_logs")
REQUIRED_KEYS = ["atlas_public_key", "atlas_private_key", "group_id", "cluster_name", "timezone", "api_version"]
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)

def validate_config(config: dict) -> None:
    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing: raise ValueError(f"Missing Atlas config values: {', '.join(missing)}")

def previous_day(timezone_name: str, now: datetime | None = None) -> str:
    return ((now or datetime.now(ZoneInfo(timezone_name))).date() - timedelta(days=1)).isoformat()

def day_window(log_date: str, timezone_name: str) -> tuple[int, int]:
    zone, d = ZoneInfo(timezone_name), date.fromisoformat(log_date)
    return (int(datetime(d.year, d.month, d.day, tzinfo=zone).timestamp()),
            int(datetime(*(d + timedelta(days=1)).timetuple()[:3], tzinfo=zone).timestamp()))

class AtlasClient:
    def __init__(self, config: dict):
        self.base_url = config.get("atlas_base_url", "https://cloud.mongodb.com/api/atlas/v2").rstrip("/")
        self.api_version, self.timeout, self.max_retries = config["api_version"], int(config.get("http_timeout_seconds", 120)), int(config.get("max_retries", 5))
        passwords = HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, "https://cloud.mongodb.com", config["atlas_public_key"], config["atlas_private_key"])
        self.opener = build_opener(HTTPDigestAuthHandler(passwords))
    @staticmethod
    def _retry_delay(attempt, retry_after=None):
        try: return max(0, int(retry_after))
        except (TypeError, ValueError): return min(2 ** (attempt - 1), 30)
    def _request(self, url, accept):
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.opener.open(Request(url, headers={"Accept": accept, "User-Agent": "mongodb-atlas-logs-skill/1.0"}, method="GET"), timeout=self.timeout)
            except HTTPError as err:
                if err.code not in RETRYABLE_HTTP_CODES or attempt >= self.max_retries: raise
                delay = self._retry_delay(attempt, err.headers.get("Retry-After")); err.close(); time.sleep(delay)
            except URLError:
                if attempt >= self.max_retries: raise
                time.sleep(self._retry_delay(attempt))
        raise RuntimeError("Atlas request failed")
    def get_json(self, path, query=None):
        url = f"{self.base_url}{path}" + ("?" + urlencode(query) if query else "")
        with self._request(url, f"application/vnd.atlas.{self.api_version}+json") as response: return json.load(response)
    def download_log(self, group_id, host_name, log_name, start_date, end_date):
        path = f"/groups/{quote(group_id, safe='')}/clusters/{quote(host_name, safe='')}/logs/{quote(log_name, safe='')}.gz"
        return self._request(f"{self.base_url}{path}?{urlencode({'startDate': start_date, 'endDate': end_date})}", f"application/vnd.atlas.{self.api_version}+gzip")

def list_processes(client, group_id: str) -> list[dict]:
    result, page = [], 1
    while True:
        payload = client.get_json(f"/groups/{quote(group_id, safe='')}/processes", {"itemsPerPage": 500, "pageNum": page, "includeCount": "true", "pretty": "false"})
        batch = payload.get("results", []); result.extend(batch)
        if len(batch) < 500: return result
        page += 1

def process_matches_cluster(process: dict, config: dict) -> bool:
    """Optional filters only. Without one, include every data-bearing process."""
    hostname, alias = str(process.get("hostname", "")), str(process.get("userAlias", ""))
    hostnames = config.get("hostnames")
    if hostnames:
        return hostname in set(hostnames) or alias in set(hostnames)
    selector = config.get("host_selector")
    if selector:
        return bool(re.search(selector, f"{hostname} {alias}", re.I))
    return True

def log_names_for_process(process: dict, config: dict) -> list[str]:
    configured = config.get("log_names", ["auto"]); configured = [configured] if isinstance(configured, str) else configured
    is_mongos = process.get("typeName") == "SHARD_MONGOS"
    if "auto" in configured: return ["mongos" if is_mongos else "mongodb"]
    return [n for n in configured if not (is_mongos and n.startswith("mongodb")) and not (not is_mongos and n.startswith("mongos"))]

def target_hosts(config: dict, client=None) -> list[dict]:
    validate_config(config); client = client or AtlasClient(config)
    targets = [p for p in list_processes(client, config["group_id"]) if p.get("typeName") != "NO_DATA" and process_matches_cluster(p, config)]
    if not targets: raise RuntimeError("No data-bearing Atlas processes matched optional hostnames/host_selector.")
    return targets

def archive_logs(config: dict, log_date: str | None = None, sink=None, exists=None, client=None) -> dict:
    if sink is None: raise ValueError("sink is required")
    validate_config(config); client = client or AtlasClient(config); log_date = log_date or previous_day(config["timezone"])
    start, end, logs, skipped, failed = *day_window(log_date, config["timezone"]), [], [], []
    for process in target_hosts(config, client):
        host = process.get("hostname")
        if not host: continue
        for log_name in log_names_for_process(process, config):
            entry = {"host": host, "host_dir": safe_filename(host), "log_name": log_name, "log_date": log_date, "process_type": process.get("typeName"), "relative_path": f"{log_date}/{safe_filename(host)}/{log_name}.gz"}
            if exists and exists(entry): logs.append({**entry, "status": "skipped_existing"}); skipped.append(entry["relative_path"]); continue
            response = None
            try:
                response = client.download_log(config["group_id"], host, log_name, start, end)
                with response: location = sink(entry, response, {"cluster": config["cluster_name"], "host": host, "log-name": log_name, "start-date": str(start), "end-date": str(end)})
                logs.append({**entry, "status": "downloaded", "location": location})
            except Exception as error:
                logger.exception("Download failed host=%s log=%s", host, log_name); failed.append({"host": host, "log_name": log_name, "error": str(error)[:500]})
            finally:
                if response is not None: response.close()
    if not logs: raise RuntimeError(json.dumps({"log_date": log_date, "failed": failed}))
    return {"log_date": log_date, "cluster": config["cluster_name"], "logs": logs, "skipped_existing": skipped, "failed": failed}

def local_dir_sink(output_dir: str | Path):
    root = Path(output_dir)
    def sink(entry, fileobj, metadata):
        path = root / entry["relative_path"]; path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out: shutil.copyfileobj(fileobj, out, 8 * 1024 * 1024)
        path.with_suffix(".gz.meta.json").write_text(json.dumps(metadata, indent=2)); return str(path)
    return sink
