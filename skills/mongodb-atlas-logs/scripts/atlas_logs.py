#!/usr/bin/env python3
"""MongoDB Atlas log downloader (cloud-agnostic skill script).

Derived from lambda-downloading-logs/lambda_function.py: same Atlas API
calls, host selection and day window, without any cloud SDK.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from http.client import IncompleteRead
from urllib.parse import quote, urlencode
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener
from zoneinfo import ZoneInfo

logger = logging.getLogger("atlas_logs")

# ``cluster_name`` is optional: empty means "every cluster of the project" (see list_clusters).
REQUIRED_KEYS = ["atlas_public_key", "atlas_private_key", "group_id", "timezone", "api_version"]
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
# Label of an Atlas node host name after the cluster prefix: shard-00-00, config-00-01,
# legacy shard-00-00-ab1cd, private-endpoint variants ... (never another cluster's name).
_NODE_LABEL_SUFFIX = re.compile(r"^(?:shard|config)-\d+-\d+(?:-[a-z0-9]+)*$")
_HOST_LABEL_PREFIX = re.compile(r"^(.+?)-(?:shard|config)-\d+-\d+(?:-[a-z0-9]+)*$")
_RS_PREFIX = re.compile(r"^(.+?)-(?:shard|config)-\d+$")


class NoMatchingHosts(RuntimeError):
    """No data-bearing process of the project belongs to the requested cluster."""


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def validate_config(config: dict) -> None:
    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing:
        raise ValueError(f"Missing Atlas config values: {', '.join(missing)}")


def previous_day(timezone_name: str, now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo(timezone_name))
    return (now.date() - timedelta(days=1)).isoformat()


def day_window(log_date: str, timezone_name: str) -> tuple[int, int]:
    zone = ZoneInfo(timezone_name)
    d = date.fromisoformat(log_date)
    start = datetime(d.year, d.month, d.day, tzinfo=zone)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return int(start.timestamp()), int(end.timestamp())


class AtlasClient:
    def __init__(self, config: dict):
        self.base_url = config.get("atlas_base_url", "https://cloud.mongodb.com/api/atlas/v2").rstrip("/")
        self.api_version = config["api_version"]
        self.timeout = int(config.get("http_timeout_seconds", 120))
        self.max_retries = int(config.get("max_retries", 5))
        pm = HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, "https://cloud.mongodb.com", config["atlas_public_key"], config["atlas_private_key"])
        self.opener = build_opener(HTTPDigestAuthHandler(pm))

    @staticmethod
    def _retry_delay(attempt, retry_after=None):
        try:
            return max(0, int(retry_after))
        except (TypeError, ValueError):
            return min(2 ** (attempt - 1), 30)

    def _request(self, url, accept):
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            req = Request(url, headers={"Accept": accept, "User-Agent": "mongodb-atlas-logs-skill/1.0"}, method="GET")
            started = time.perf_counter()
            try:
                resp = self.opener.open(req, timeout=self.timeout)
                logger.info("Atlas GET ok attempt=%d status=%s elapsed=%.3fs", attempt,
                            getattr(resp, "status", "?"), time.perf_counter() - started)
                return resp
            except HTTPError as error:
                last_error = error
                retryable = error.code in RETRYABLE_HTTP_CODES
                logger.warning("Atlas HTTP %s attempt=%d retryable=%s", error.code, attempt, retryable)
                if not retryable or attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(attempt, error.headers.get("Retry-After"))
                error.close()
                time.sleep(delay)
            except URLError as error:
                last_error = error
                logger.warning("Atlas network error attempt=%d: %s", attempt, error)
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._retry_delay(attempt))
        raise last_error or RuntimeError("Atlas request failed")

    def get_json(self, path, query=None):
        url = f"{self.base_url}{path}" + ("?" + urlencode(query) if query else "")
        with self._request(url, f"application/vnd.atlas.{self.api_version}+json") as resp:
            return json.load(resp)

    def download_log(self, group_id, host_name, log_name, start_date, end_date):
        path = (f"/groups/{quote(group_id, safe='')}/clusters/{quote(host_name, safe='')}"
                f"/logs/{quote(log_name, safe='')}.gz")
        url = f"{self.base_url}{path}?{urlencode({'startDate': start_date, 'endDate': end_date})}"
        return self._request(url, f"application/vnd.atlas.{self.api_version}+gzip")


def list_processes(client, group_id: str) -> list[dict]:
    processes, page, size = [], 1, 500
    while True:
        payload = client.get_json(f"/groups/{quote(group_id, safe='')}/processes",
                                  {"itemsPerPage": size, "pageNum": page, "includeCount": "true", "pretty": "false"})
        batch = payload.get("results", [])
        processes.extend(batch)
        if not batch or len(batch) < size:
            return processes
        page += 1


def list_clusters(client, group_id: str, include_paused: bool = False) -> list[str]:
    """Names of the project's clusters (paused clusters are skipped: they produce no logs)."""
    names, page, size = [], 1, 500
    while True:
        payload = client.get_json(f"/groups/{quote(group_id, safe='')}/clusters",
                                  {"itemsPerPage": size, "pageNum": page, "includeCount": "true"})
        batch = payload.get("results", []) if isinstance(payload, dict) else []
        for item in batch:
            name = item.get("name") if isinstance(item, dict) else None
            if not name:
                continue
            if item.get("paused") and not include_paused:
                logger.info("Cluster %s is paused: skipped", name)
                continue
            names.append(name)
        if not batch or len(batch) < size:
            return names
        page += 1


def _strip_port(host: str) -> str:
    host = (host or "").strip().lower()
    return host.rsplit(":", 1)[0] if host.rsplit(":", 1)[-1].isdigit() else host


def _hosts_from_uri(uri: str) -> tuple[list[str], str | None]:
    """Hosts and replicaSet of a non-SRV ``mongodb://`` connection string."""
    if not isinstance(uri, str) or not uri.startswith("mongodb://"):
        return [], None
    rest = uri[len("mongodb://"):]
    hosts_part, _, tail = rest.partition("/")
    hosts_part = hosts_part.rsplit("@", 1)[-1]
    replica_set = None
    match = re.search(r"(?:^|[?&])replicaSet=([^&]+)", tail)
    if match:
        replica_set = match.group(1)
    return [_strip_port(h) for h in hosts_part.split(",") if h.strip()], replica_set


def cluster_identity(client, group_id: str, cluster_name: str) -> dict:
    """What identifies the processes of ``cluster_name`` in the project's /processes list.

    Atlas processes carry no cluster name. They are matched through (1) the host names of
    the cluster connection strings, (2) host-label prefixes (``<prefix>-shard-NN-NN`` /
    ``<prefix>-config-NN-NN``) derived from those hosts, from the replica-set names and
    from the cluster name itself, and (3) replica-set name prefixes.
    """
    hosts, replica_sets = set(), set()
    try:
        details = client.get_json(f"/groups/{quote(group_id, safe='')}/clusters/{quote(cluster_name, safe='')}")
    except HTTPError as error:
        if error.code == 404:
            raise ValueError(f"Cluster {cluster_name!r} was not found in project {group_id}") from error
        logger.warning("Cluster details unavailable for %s (HTTP %s): matching by cluster name only",
                       cluster_name, error.code)
        details = {}
    except Exception as error:  # network/parse: the name-based rule still applies
        logger.warning("Cluster details unavailable for %s (%s): matching by cluster name only",
                       cluster_name, type(error).__name__)
        details = {}
    if not isinstance(details, dict):
        details = {}
    strings = details.get("connectionStrings") if isinstance(details.get("connectionStrings"), dict) else {}
    for uri in [details.get("mongoURI"), strings.get("standard"), strings.get("private")]:
        found, replica_set = _hosts_from_uri(uri)
        hosts.update(found)
        if replica_set:
            replica_sets.add(replica_set.lower())
    name = cluster_name.lower()
    # Atlas host names start with the (lower-case) cluster name, truncated for long names.
    prefixes = {name, name[:23]}
    for host in hosts:
        match = _HOST_LABEL_PREFIX.match(host.split(".", 1)[0])
        if match:
            prefixes.add(match.group(1))
    for replica_set in replica_sets:
        match = _RS_PREFIX.match(replica_set)
        if match:
            prefixes.add(match.group(1))
    return {"cluster_name": cluster_name, "hosts": hosts, "prefixes": {p for p in prefixes if p},
            "replica_sets": replica_sets}


def process_in_cluster(process: dict, identity: dict) -> bool:
    names = [_strip_port(process.get(k) or "") for k in ("hostname", "userAlias")]
    names = [n for n in names if n]
    if any(n in identity["hosts"] for n in names):
        return True
    for host in names:
        label = host.split(".", 1)[0]
        for prefix in identity["prefixes"]:
            # Exact suffix rule: "prod" never matches "prod-eu-shard-00-00".
            if label.startswith(prefix + "-") and _NODE_LABEL_SUFFIX.match(label[len(prefix) + 1:]):
                return True
    replica_set = str(process.get("replicaSetName") or "").lower()
    if replica_set:
        if replica_set in identity["replica_sets"]:
            return True
        match = _RS_PREFIX.match(replica_set)
        if match and match.group(1) in identity["prefixes"]:
            return True
    return False


def _strong_match(process: dict, identity: dict) -> bool:
    names = {_strip_port(process.get(k) or "") for k in ("hostname", "userAlias")} - {""}
    replica_set = str(process.get("replicaSetName") or "").lower()
    return bool(names & identity["hosts"]) or bool(replica_set and replica_set in identity["replica_sets"])


def expand_identity(identity: dict, processes: list[dict]) -> dict:
    """Learn the internal prefixes of the cluster from the processes that matched it exactly.

    Atlas lists processes under internal names (``atlas-<id>-shard-00-00`` / replica set
    ``atlas-<id>-shard-0``) while ``userAlias`` keeps the cluster-name form. A process matched
    through an exact connection-string host or replica-set name gives the internal prefix that
    the cluster's other shards / config servers share.
    """
    prefixes = set(identity["prefixes"])
    for process in processes:
        if not _strong_match(process, identity):
            continue
        for key in ("hostname", "userAlias"):
            match = _HOST_LABEL_PREFIX.match(_strip_port(process.get(key) or "").split(".", 1)[0])
            if match:
                prefixes.add(match.group(1))
        match = _RS_PREFIX.match(str(process.get("replicaSetName") or "").lower())
        if match:
            prefixes.add(match.group(1))
    return {**identity, "prefixes": prefixes}


def process_matches_cluster(process: dict, config: dict, identity: dict | None = None) -> bool:
    """Is this project process one of the target nodes?

    ``hostnames`` (exact list) is an explicit override. Otherwise, when ``cluster_name`` is
    set, the process must belong to that cluster; ``host_selector`` only narrows further.
    """
    hostname, alias = process.get("hostname", ""), process.get("userAlias", "")
    if config.get("hostnames"):
        return hostname in set(config["hostnames"]) or alias in set(config["hostnames"])
    if identity is not None and not process_in_cluster(process, identity):
        return False
    if config.get("host_selector"):
        return bool(re.search(config["host_selector"], f"{hostname} {alias}", re.I))
    return True


def log_names_for_process(process: dict, config: dict) -> list[str]:
    configured = config.get("log_names", ["auto"])
    if isinstance(configured, str):
        configured = [configured]
    is_mongos = process.get("typeName") == "SHARD_MONGOS"
    if "auto" in configured:
        return ["mongos" if is_mongos else "mongodb"]
    return [n for n in configured
            if not (is_mongos and n.startswith("mongodb")) and not (not is_mongos and n.startswith("mongos"))]


def target_hosts(config: dict, client=None) -> list[dict]:
    """Data-bearing processes of ``cluster_name`` only (never the other clusters of the project).

    An empty ``cluster_name`` keeps the project-wide selection; the agent instead iterates
    list_clusters() and calls this once per cluster.
    """
    validate_config(config)
    client = client or AtlasClient(config)
    cluster = (config.get("cluster_name") or "").strip()
    identity = None
    if cluster and not config.get("hostnames"):
        identity = cluster_identity(client, config["group_id"], cluster)
    processes = list_processes(client, config["group_id"])
    if identity is not None:
        identity = expand_identity(identity, processes)
    targets = [p for p in processes
               if p.get("typeName") != "NO_DATA" and process_matches_cluster(p, config, identity)]
    if not targets:
        raise NoMatchingHosts(f"No hosts matched cluster {cluster or '(all)'!r} among {len(processes)} "
                              "project processes. Check cluster_name, host_selector or hostnames.")
    logger.info("Cluster %s: %d of %d project processes selected", cluster or "(all)", len(targets), len(processes))
    return targets


def archive_logs(config: dict, log_date: str | None = None, sink=None, exists=None, client=None) -> dict:
    """Download each target host log for `log_date` and hand it to `sink(entry, fileobj, metadata)`.

    `exists(entry) -> bool` (optional) lets the caller skip logs already archived.
    """
    if sink is None:
        raise ValueError("sink is required (use local_dir_sink() for local output)")
    validate_config(config)
    client = client or AtlasClient(config)
    log_date = log_date or previous_day(config["timezone"])
    start, end = day_window(log_date, config["timezone"])
    group_id, cluster = config["group_id"], config.get("cluster_name") or ""

    logs, skipped, failed = [], [], []
    for process in target_hosts(config, client):
        host = process.get("hostname")
        if not host:
            continue
        for log_name in log_names_for_process(process, config):
            host_dir = safe_filename(host)
            entry = {"host": host, "host_dir": host_dir, "log_name": log_name, "log_date": log_date,
                     "process_type": process.get("typeName"),
                     "relative_path": f"{log_date}/{host_dir}/mongodb/{log_name}.gz"}
            if exists is not None and exists(entry):
                logger.info("Already archived, skipping %s", entry["relative_path"])
                skipped.append(entry["relative_path"])
                logs.append({**entry, "status": "skipped_existing"})
                continue
            metadata = {"cluster": cluster, "host": host, "log-name": log_name,
                        "start-date": str(start), "end-date": str(end)}
            response = None
            started = time.perf_counter()
            try:
                # An Atlas HTTP response can reset halfway through a long read.
                # Restart from a fresh response; the sink must not publish a
                # partially read object (the S3 sink stages before uploading).
                for transfer_attempt in range(1, getattr(client, "max_retries", 3) + 1):
                    try:
                        response = client.download_log(group_id, host, log_name, start, end)
                        with response:
                            location = sink(entry, response, metadata)
                        break
                    except (ConnectionResetError, IncompleteRead, TimeoutError, URLError) as interrupted:
                        if transfer_attempt >= getattr(client, "max_retries", 3):
                            raise
                        logger.warning("Atlas log stream interrupted host=%s log=%s attempt=%d/%d: %s",
                                       host, log_name, transfer_attempt, getattr(client, "max_retries", 3), interrupted)
                        time.sleep(client._retry_delay(transfer_attempt) if hasattr(client, "_retry_delay") else min(2 ** (transfer_attempt - 1), 30))
                    finally:
                        if response is not None:
                            response.close()
                            response = None
                logs.append({**entry, "status": "downloaded", "location": location,
                             "elapsed_seconds": round(time.perf_counter() - started, 3)})
            except Exception as error:
                logger.exception("Download failed host=%s log=%s", host, log_name)
                failed.append({"host": host, "log_name": log_name, "error": str(error)[:500]})
            finally:
                if response is not None:
                    response.close()
    if not logs:
        raise RuntimeError(json.dumps({"log_date": log_date, "failed": failed}))
    return {"log_date": log_date, "cluster": cluster, "logs": logs, "skipped_existing": skipped, "failed": failed}


def local_dir_sink(output_dir: str | Path):
    root = Path(output_dir)

    def sink(entry, fileobj, metadata):
        path = root / entry["relative_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out:
            shutil.copyfileobj(fileobj, out, 8 * 1024 * 1024)
        path.with_suffix(".gz.meta.json").write_text(json.dumps(metadata, indent=2))
        return str(path)
    return sink


def _load_cli_config(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if os.environ.get("ATLAS_CONFIG_JSON"):
        return json.loads(os.environ["ATLAS_CONFIG_JSON"])
    raise SystemExit("Provide --config FILE or ATLAS_CONFIG_JSON")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", help="JSON file with Atlas config (see references/config-schema.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-clusters")
    sub.add_parser("list-hosts")
    d = sub.add_parser("download")
    d.add_argument("--output-dir", required=True)
    d.add_argument("--log-date")
    d.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)
    config = _load_cli_config(args.config)
    validate_config(config)
    if args.cmd == "list-clusters":
        print(json.dumps(list_clusters(AtlasClient(config), config["group_id"]), indent=2))
        return 0
    # Empty cluster_name: every cluster of the project, each under its own <cluster>/ folder.
    clusters = [config["cluster_name"]] if config.get("cluster_name") else \
        list_clusters(AtlasClient(config), config["group_id"])
    multi = not config.get("cluster_name")
    if args.cmd == "list-hosts":
        out = {}
        for name in clusters:
            try:
                hosts = target_hosts({**config, "cluster_name": name})
            except NoMatchingHosts:
                if not multi:
                    raise
                hosts = []
            out[name] = [{"hostname": h.get("hostname"), "typeName": h.get("typeName"),
                          "logs": log_names_for_process(h, config)} for h in hosts]
        print(json.dumps(out if multi else out[clusters[0]], indent=2))
        return 0
    results, errors = {}, 0
    for name in clusters:
        root = Path(args.output_dir) / safe_filename(name) if multi else Path(args.output_dir)
        exists = None if args.overwrite else (lambda e, root=root: (root / e["relative_path"]).is_file())
        try:
            results[name] = archive_logs({**config, "cluster_name": name}, args.log_date,
                                         sink=local_dir_sink(root), exists=exists)
        except Exception as error:  # one cluster never blocks the others
            if not multi:
                raise
            errors += 1
            logger.exception("Cluster %s download failed", name)
            results[name] = {"cluster": name, "error": str(error)[:500]}
    print(json.dumps(results if multi else results[clusters[0]], indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
