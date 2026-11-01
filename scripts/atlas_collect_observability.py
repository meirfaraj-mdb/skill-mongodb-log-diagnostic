#!/usr/bin/env python3
"""Manual, read-only Atlas observability export (standalone, Python 3.9+ standard library only).

Collects, per data-bearing process of ONE Atlas cluster, the files the diagnostic agent reads in
``existing_bucket`` mode:

  <out>/<log-date>/<host_dir>/metrics/metrics.json                  process + disk measurements
  <out>/<log-date>/<host_dir>/advisor/advisor.json                  Performance Advisor suggested indexes
  <out>/<log-date>/<host_dir>/queryStats/query-shape-insights.json  Query Shape Insights (24 h)
  <out>/<log-date>/<host_dir>/queryStats/query-stats.json           same QSI payload, only if absent
                                                                    (a mongosh $queryStats file wins)
  <out>/<log-date>/cluster/advisor/cluster-advisor.json             drop-index + schema advice
  <out>/<log-date>/cluster/manifests/atlas-api.json                 run receipt

Credentials (never written to disk or logs), first match wins:
  ATLAS_CLIENT_ID + ATLAS_CLIENT_SECRET      service account (OAuth2 client credentials)
  ATLAS_PUBLIC_KEY + ATLAS_PRIVATE_KEY       programmatic API key (HTTP digest)
  --config FILE with atlas_public_key/atlas_private_key (same keys as the agent secret)
  otherwise an interactive prompt.
Required role: Project Read Only (Project Observability Viewer is enough for metrics/QSI;
Performance Advisor needs Project Data Access Read Only or higher).

Examples:
  export ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=...
  python3 atlas_collect_observability.py --group-id 5f... --cluster-name prod-cluster \
          --timezone Asia/Jerusalem --out ./observability-manual
  # align every window to the log day (same boundaries as the downloaded logs):
  python3 atlas_collect_observability.py ... --log-date 2026-10-31
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import logging
import os
import re
import ssl
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, HTTPSHandler, Request, build_opener

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = "1.0.0"
COLLECTOR = {"name": "manual-atlas-api-collector", "version": VERSION}
log = logging.getLogger("atlas_collect")

# Same measurement set the agent requests: default graph groups + high-resource investigation,
# validated against the documented Atlas ``m`` values (agent/metrics_catalog.py).
DEFAULT_PROCESS_METRICS = [
    "CACHE_BYTES_READ_INTO", "CACHE_BYTES_WRITTEN_FROM", "CACHE_DIRTY_BYTES", "CACHE_USED_BYTES",
    "CONNECTIONS", "CURSORS_TOTAL_OPEN", "CURSORS_TOTAL_TIMED_OUT", "DB_STORAGE_TOTAL", "DB_DATA_SIZE_TOTAL",
    "DOCUMENT_METRICS_RETURNED", "DOCUMENT_METRICS_INSERTED", "DOCUMENT_METRICS_UPDATED",
    "DOCUMENT_METRICS_DELETED", "EXTRA_INFO_PAGE_FAULTS", "MEMORY_RESIDENT", "MEMORY_VIRTUAL", "MEMORY_MAPPED",
    "NETWORK_BYTES_IN", "NETWORK_BYTES_OUT", "NETWORK_NUM_REQUESTS", "OPCOUNTER_CMD", "OPCOUNTER_QUERY",
    "OPCOUNTER_UPDATE", "OPCOUNTER_DELETE", "OPCOUNTER_TTL_DELETED", "OPCOUNTER_GETMORE", "OPCOUNTER_INSERT",
    "OPERATIONS_SCAN_AND_ORDER", "OPERATIONS_QUERIES_KILLED", "OP_EXECUTION_TIME_READS",
    "OP_EXECUTION_TIME_WRITES", "OP_EXECUTION_TIME_COMMANDS", "OPLOG_MASTER_TIME", "QUERY_EXECUTOR_SCANNED",
    "QUERY_EXECUTOR_SCANNED_OBJECTS", "QUERY_TARGETING_SCANNED_PER_RETURNED",
    "QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED", "TICKETS_AVAILABLE_READS", "TICKETS_AVAILABLE_WRITE",
    "OPERATION_THROTTLING_REJECTED_OPERATIONS", "QUERY_SPILL_TO_DISK_DURING_SORT", "FTS_MEMORY_RESIDENT",
    "FTS_MEMORY_VIRTUAL", "FTS_DISK_UTILIZATION", "FTS_PROCESS_CPU_USER", "FTS_PROCESS_CPU_KERNEL",
    "FTS_PROCESS_NORMALIZED_CPU_USER", "FTS_PROCESS_NORMALIZED_CPU_KERNEL", "SYSTEM_NETWORK_IN",
    "SYSTEM_NETWORK_OUT", "MAX_SYSTEM_NETWORK_IN", "MAX_SYSTEM_NETWORK_OUT", "SYSTEM_MEMORY_USED",
    "SYSTEM_MEMORY_AVAILABLE", "SYSTEM_MEMORY_FREE", "SYSTEM_NORMALIZED_CPU_USER", "SYSTEM_NORMALIZED_CPU_KERNEL",
    "SYSTEM_NORMALIZED_CPU_NICE", "SYSTEM_NORMALIZED_CPU_IOWAIT", "SYSTEM_NORMALIZED_CPU_IRQ",
    "SYSTEM_NORMALIZED_CPU_SOFTIRQ", "SYSTEM_NORMALIZED_CPU_GUEST", "SYSTEM_NORMALIZED_CPU_STEAL",
    "MAX_SYSTEM_NORMALIZED_CPU_USER", "MAX_SYSTEM_NORMALIZED_CPU_KERNEL", "MAX_SYSTEM_NORMALIZED_CPU_NICE",
    "MAX_SYSTEM_NORMALIZED_CPU_IOWAIT", "MAX_SYSTEM_NORMALIZED_CPU_IRQ", "MAX_SYSTEM_NORMALIZED_CPU_SOFTIRQ",
    "MAX_SYSTEM_NORMALIZED_CPU_GUEST", "MAX_SYSTEM_NORMALIZED_CPU_STEAL", "PROCESS_NORMALIZED_CPU_USER",
    "PROCESS_NORMALIZED_CPU_KERNEL", "GLOBAL_LOCK_CURRENT_QUEUE_TOTAL", "GLOBAL_LOCK_CURRENT_QUEUE_READERS",
    "GLOBAL_LOCK_CURRENT_QUEUE_WRITERS", "SWAP_USAGE_USED",
]
DEFAULT_DISK_METRICS = ["DISK_QUEUE_DEPTH", "DISK_PARTITION_IOPS_TOTAL", "DISK_PARTITION_IOPS_READ",
                        "DISK_PARTITION_IOPS_WRITE", "DISK_PARTITION_LATENCY_READ", "DISK_PARTITION_LATENCY_WRITE",
                        "DISK_PARTITION_SPACE_PERCENT_USED"]
# Shown in the Atlas UI but not in the documented ``m`` list: requested alone, with a no-``m`` fallback.
UNDOCUMENTED_DISK_METRICS = ["MAX_DISK_QUEUE_DEPTH"]
MAX_M_PER_REQUEST = 10
GRANULARITIES = {"PT1M", "PT5M", "PT1H", "P1D"}
FINE_RETENTION = timedelta(hours=48)
QSI_RETENTION = timedelta(days=7)
DATA_PROCESS_TYPES = {"REPLICA_PRIMARY", "REPLICA_SECONDARY", "SHARD_PRIMARY", "SHARD_SECONDARY",
                      "SHARD_CONFIG_PRIMARY", "SHARD_CONFIG_SECONDARY", "SHARD_MONGOS", "SHARD_STANDALONE",
                      "STANDALONE"}

# ----------------------------------------------------------------------------- redaction (agent rules)
SECRET_RE = re.compile(r"password|credential|token|secret|api.?key", re.I)
SHAPE_KEYS = {"filter", "query", "q", "pipeline", "parsedQuery", "queryShape", "sample_query_shape",
              "partialFilterExpression", "indexBounds", "command",
              # Performance Advisor / slow-log specific: example predicates carry literal values.
              "predicates", "sort", "update", "u", "documents", "let"}
DROP_KEYS = {"remote", "clientIp", "ipAddress", "uri", "connectionString", "comment", "raw", "client_ip"}


def _shape(value, depth=0):
    if depth > 12:
        return "?"
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:100]
                if isinstance(k, str) and not SECRET_RE.search(k)}
    if isinstance(value, list):
        return [_shape(v, depth + 1) for v in value[:30]]
    return "?"


def sanitize(value, depth=0):
    """Drop secrets/client endpoints and reduce query objects to field/operator structure."""
    if depth > 16:
        return "<depth limit>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if SECRET_RE.search(str(key)):
                continue
            if key == "client":
                app = item.get("application", {}) if isinstance(item, dict) else {}
                if isinstance(app, dict) and isinstance(app.get("name"), str):
                    out[key] = {"application": {"name": app["name"][:160]}}
                continue
            if key in DROP_KEYS:
                continue
            if key in SHAPE_KEYS and isinstance(item, (dict, list)):
                out[key] = _shape(item)
                if key == "queryShape" and isinstance(item, dict):
                    if isinstance(item.get("cmdNs"), dict):
                        out[key]["cmdNs"] = {k: str(v)[:160] for k, v in item["cmdNs"].items() if k in {"db", "coll"}}
                    if isinstance(item.get("command"), str):
                        out[key]["command"] = item["command"][:40]
                continue
            out[key] = sanitize(item, depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize(v, depth + 1) for v in value[:10000]]
    if isinstance(value, str) and re.search(r"(?:mongodb(?:\+srv)?|https?)://[^\s]*@", value):
        return "<redacted credential-bearing URI>"
    return value


# ----------------------------------------------------------------------------- HTTP
class ApiError(RuntimeError):
    def __init__(self, status, error_code, url_path):
        super().__init__(f"HTTP {status} {error_code or ''} on {url_path}".strip())
        self.status, self.error_code, self.url_path = status, error_code, url_path

    def info(self):
        return {"status": self.status, "error_code": self.error_code, "path": self.url_path}


class AtlasClient:
    def __init__(self, base_url, api_version, creds, timeout=120, retries=5, ca_file=None, allow_http=False,
                 pace_seconds=0.0):
        self.base_url = base_url.rstrip("/")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" and not allow_http:
            raise SystemExit("Atlas base URL must use HTTPS")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.api_version, self.timeout, self.retries, self.pace = api_version, timeout, retries, pace_seconds
        self.calls = 0
        handlers = []
        if ca_file:
            handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        self.token, self.token_expiry, self.creds = None, 0.0, creds
        if creds["type"] == "digest":
            pm = HTTPPasswordMgrWithDefaultRealm()
            pm.add_password(None, self.origin, creds["public"], creds["private"])
            handlers.append(HTTPDigestAuthHandler(pm))
        self.opener = build_opener(*handlers)

    def _bearer(self):
        if self.creds["type"] != "service_account":
            return None
        if self.token and time.time() < self.token_expiry - 60:
            return self.token
        basic = base64.b64encode(f"{self.creds['client_id']}:{self.creds['client_secret']}".encode()).decode()
        req = Request(self.creds.get("token_url") or self.origin + "/api/oauth/token",
                      data=b"grant_type=client_credentials", method="POST",
                      headers={"Authorization": "Basic " + basic, "Accept": "application/json",
                               "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except HTTPError as exc:
            raise SystemExit(f"Service-account token request failed: HTTP {exc.code}") from None
        self.token = payload["access_token"]
        self.token_expiry = time.time() + float(payload.get("expires_in", 3600))
        return self.token

    def get(self, path_or_url, params=None, version=None):
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise ValueError("Refusing cross-origin request")
        path = urlsplit(url).path
        for attempt in range(1, self.retries + 1):
            headers = {"Accept": f"application/vnd.atlas.{version or self.api_version}+json",
                       "User-Agent": f"mongodb-manual-observability/{VERSION}"}
            bearer = self._bearer()
            if bearer:
                headers["Authorization"] = "Bearer " + bearer
            if self.pace:
                time.sleep(self.pace)
            self.calls += 1
            try:
                with self.opener.open(Request(url, headers=headers), timeout=self.timeout) as resp:
                    return json.load(resp)
            except HTTPError as exc:
                code = None
                try:
                    code = json.loads(exc.read().decode("utf-8", "replace")).get("errorCode")
                except Exception:
                    pass
                retry = exc.code in {429, 500, 502, 503, 504} and attempt < self.retries
                if exc.code == 401 and bearer and attempt < self.retries:
                    self.token = None
                    retry = True
                if not retry:
                    raise ApiError(exc.code, code, path) from None
                wait = exc.headers.get("Retry-After") if exc.headers else None
                delay = int(wait) if wait and str(wait).isdigit() else min(2 ** attempt, 60)
                log.warning("HTTP %s on %s, retry %d/%d in %ss", exc.code, path, attempt, self.retries, delay)
                time.sleep(delay)
            except (URLError, TimeoutError, ConnectionError) as exc:
                if attempt >= self.retries:
                    raise ApiError(None, type(exc).__name__, path) from None
                time.sleep(min(2 ** attempt, 60))
        raise ApiError(None, "retries_exhausted", path)

    def list_all(self, path, params=None, max_pages=50):
        """Atlas list endpoints: pageNum/itemsPerPage until a short page."""
        items, page = [], 1
        while page <= max_pages:
            payload = self.get(path, dict(params or {}, itemsPerPage=500, pageNum=page))
            batch = payload.get("results", []) if isinstance(payload, dict) else []
            items.extend(batch)
            if len(batch) < 500:
                return items
            page += 1
        return items

    def pages(self, path, params=None, max_pages=10):
        """Follow ``links[rel=next]``; returns (pages, truncated)."""
        out, url = [], None
        payload = self.get(path, params)
        for _ in range(max_pages):
            out.append(payload)
            links = payload.get("links", []) if isinstance(payload, dict) else []
            nxt = next((x.get("href") for x in links if isinstance(x, dict) and x.get("rel") == "next"), None)
            if not nxt:
                return out, False
            url = urljoin(self.base_url + "/", nxt)
            payload = self.get(url)
        return out, True


# ----------------------------------------------------------------------------- helpers
def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(dt):
    return int(dt.timestamp() * 1000)


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def zone(name):
    if name in (None, "", "UTC"):
        return timezone.utc
    if ZoneInfo is None:
        raise SystemExit("zoneinfo unavailable: use --timezone UTC or Python 3.9+")
    return ZoneInfo(name)


def resolve_window(args, now):
    """Return (log_date, since, until, basis, meta).

    Default: rolling ``--hours`` (24) ending now, stored under yesterday's date (local timezone).
    ``--log-date``: [local midnight, next local midnight) of that day, clamped to now.
    """
    tz = zone(args.timezone)
    if args.log_date:
        d = date.fromisoformat(args.log_date)
        start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
        nxt = d + timedelta(days=1)
        end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(timezone.utc)
        if start >= now:
            raise SystemExit(f"log day {args.log_date} has not started yet")
        return args.log_date, start, min(end, now), "log_day_window", {
            "log_date": args.log_date, "timezone": args.timezone, "complete_day": end <= now}
    log_date = (now.astimezone(tz).date() - timedelta(days=1)).isoformat()
    return log_date, now - timedelta(hours=args.hours), now, "requested_api_window", {
        "rolling_hours": args.hours, "timezone": args.timezone, "folder_log_date": log_date}


def window_meta(since, until, extra):
    return {"since": since.isoformat(), "until": until.isoformat(),
            "hours": round((until - since).total_seconds() / 3600, 2), **extra}


class Writer:
    def __init__(self, root, log_date, redact, overwrite):
        self.root, self.log_date, self.redact, self.overwrite = Path(root), log_date, redact, overwrite
        self.files = []

    def path(self, *parts):
        return self.root / self.log_date / Path(*parts)

    def exists(self, *parts):
        return self.path(*parts).is_file()

    def replaceable_by_qsi(self, *parts):
        """True when absent or written by a previous QSI run (never replace a mongosh $queryStats file)."""
        target = self.path(*parts)
        if not target.is_file():
            return True
        try:
            return json.loads(target.read_text()).get("source") == "atlas_query_shape_insights"
        except (ValueError, AttributeError, OSError):
            return False

    def write(self, parts, payload):
        target = self.path(*parts)
        if target.exists() and not self.overwrite:
            backup = target.with_name(target.name + "." + datetime.now().strftime("%Y%m%dT%H%M%S") + ".bak")
            target.rename(backup)
            log.info("Existing %s kept as %s", target, backup.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(sanitize(payload) if self.redact else payload, indent=2, default=str))
        tmp.replace(target)
        self.files.append({"path": str(target.relative_to(self.root)), "bytes": target.stat().st_size})
        return str(target)


def envelope(source, host, basis, data, **extra):
    return {"schema_version": 2, "source": source, "host": host,
            "retrievedAt": datetime.now(timezone.utc).isoformat(), "temporal_basis": basis, "data": data,
            **extra, "collector": COLLECTOR}


# ----------------------------------------------------------------------------- discovery
def cluster_processes(client, args):
    group = quote(args.group_id, safe="")
    processes = client.list_all(f"/groups/{group}/processes")
    cluster = None
    try:
        cluster = client.get(f"/groups/{group}/clusters/{quote(args.cluster_name, safe='')}")
    except ApiError as exc:
        raise SystemExit(f"Cluster lookup failed ({exc}); check --group-id/--cluster-name and API key role")
    if args.hostnames:
        wanted = set(args.hostnames)
        selected = [p for p in processes if p.get("hostname") in wanted or p.get("userAlias") in wanted
                    or p.get("id") in wanted]
    elif args.host_regex:
        rx = re.compile(args.host_regex, re.I)
        selected = [p for p in processes if rx.search(f"{p.get('hostname', '')} {p.get('userAlias', '')}")]
    else:
        # Atlas process aliases are "<srv-label>-shard-NN-NN.<project>.mongodb.net" / "-config-NN-NN".
        srv = (cluster.get("connectionStrings") or {}).get("standardSrv") or ""
        label = urlsplit(srv).hostname.split(".")[0] if srv.startswith("mongodb+srv://") else ""
        if not label:
            raise SystemExit("Cannot derive the cluster host prefix; pass --host-regex or --hostnames")
        rx = re.compile(rf"^{re.escape(label)}-(shard|config)-\d+-\d+\.", re.I)
        selected = [p for p in processes if rx.search(p.get("userAlias") or "")]
    selected = [p for p in selected if p.get("typeName") in DATA_PROCESS_TYPES or p.get("typeName") is None]
    if not args.include_mongos:
        selected = [p for p in selected if p.get("typeName") != "SHARD_MONGOS"]
    if not args.include_config:
        selected = [p for p in selected if not str(p.get("typeName", "")).startswith("SHARD_CONFIG")]
    if not selected:
        raise SystemExit("No process matched the cluster; pass --host-regex or --hostnames")
    # host_dir = sanitized hostname, as the log downloader uses; disambiguate same host/different port.
    by_host = {}
    for p in selected:
        by_host.setdefault(p["hostname"], []).append(p)
    nodes = []
    for host, procs in sorted(by_host.items()):
        procs.sort(key=lambda p: (p.get("typeName") == "SHARD_MONGOS", int(p.get("port") or 27017)))
        for i, p in enumerate(procs):
            pid = p.get("id") or f"{host}:{p.get('port') or 27017}"
            host_dir = safe_filename(host) if i == 0 else safe_filename(f"{host}_{p.get('port')}")
            nodes.append({"host": host, "host_dir": host_dir, "process_id": pid, "port": p.get("port"),
                          "type": p.get("typeName"), "replica_set": p.get("replicaSetName"),
                          "user_alias": p.get("userAlias"), "version": p.get("version")})
    meta = {"name": cluster.get("name"), "clusterType": cluster.get("clusterType"),
            "mongoDBVersion": cluster.get("mongoDBVersion"), "stateName": cluster.get("stateName")}
    return nodes, meta


# ----------------------------------------------------------------------------- collectors
def collect_metrics(client, args, node, since, until, basis, extra, now):
    group = quote(args.group_id, safe="")
    base = f"/groups/{group}/processes/{quote(node['process_id'], safe='')}"
    granularity, note = args.granularity, None
    if granularity in {"PT1M", "PT5M"} and since < now - FINE_RETENTION:
        granularity = "PT1H"
        note = f"{args.granularity} is not retained for a window starting {since.isoformat()}; used PT1H"
    window = [("start", iso(since)), ("end", iso(until)), ("granularity", granularity)]
    names = DEFAULT_PROCESS_METRICS if not args.metrics else args.metrics
    pages, errors, truncated = [], [], False

    def fetch(batch):
        return client.pages(base + "/measurements", window + [("m", n) for n in batch], args.max_pages)

    if args.all_metrics:
        batches = [[]]
    else:
        batches = [names[i:i + MAX_M_PER_REQUEST] for i in range(0, len(names), MAX_M_PER_REQUEST)]
    for batch in batches:
        try:
            got, cut = fetch(batch)
            pages.extend(got)
            truncated = truncated or cut
        except ApiError as exc:
            if exc.status != 400 or len(batch) <= 1:
                errors.append({"metrics": batch, **exc.info()})
                continue
            for name in batch:  # one invalid/unavailable metric must not drop the batch
                try:
                    got, cut = fetch([name])
                    pages.extend(got)
                except ApiError as single:
                    errors.append({"metrics": [name], **single.info()})
    if errors and not pages:
        raise RuntimeError("all measurement requests failed: " + json.dumps(errors[:3]))
    disks, disk_errors = collect_disks(client, args, base, window)
    meta = window_meta(since, until, {"granularity": granularity, **extra})
    if note:
        meta.update(requested_granularity=args.granularity, granularity_note=note)
    return envelope("atlas_api_metrics", node["host"], basis, pages, truncated=truncated, window=meta,
                    processId=node["process_id"], disk_measurements=disks, disk_errors=disk_errors,
                    measurement_plan={"requested": [] if args.all_metrics else names, "unsupported": [],
                                      "aliases": {}, "all_metrics": args.all_metrics},
                    errors=errors, limitations=[
                        "Atlas measurement retention and availability depend on tier/granularity; "
                        "missing metrics are reported in errors, never fabricated."])


def collect_disks(client, args, base, window):
    out, errors = [], []
    try:
        listing = client.list_all(base + "/disks")
    except ApiError as exc:
        return out, [{"step": "list_disks", **exc.info()}]
    partitions = list(dict.fromkeys(d.get("partitionName") for d in listing if d.get("partitionName")))
    for name in partitions[:4]:
        path = base + "/disks/" + quote(name, safe="") + "/measurements"
        got = []
        for i in range(0, len(DEFAULT_DISK_METRICS), MAX_M_PER_REQUEST):
            batch = DEFAULT_DISK_METRICS[i:i + MAX_M_PER_REQUEST]
            try:
                got.extend(client.pages(path, window + [("m", m) for m in batch], args.max_pages)[0])
            except ApiError as exc:
                errors.append({"step": "disk_measurements", "partitionName": name, "metrics": batch, **exc.info()})
        for metric in UNDOCUMENTED_DISK_METRICS:
            try:
                got.extend(client.pages(path, window + [("m", metric)], args.max_pages)[0])
                continue
            except ApiError as exc:
                if exc.status != 400:
                    errors.append({"step": "disk_measurements", "partitionName": name, "metrics": [metric],
                                   **exc.info()})
                    continue
            try:  # no ``m`` -> all partition measurements, keep only the wanted series
                pages = client.pages(path, window, args.max_pages)[0]
                kept = [{**pg, "measurements": [m for m in pg.get("measurements", []) if m.get("name") == metric]}
                        for pg in pages if isinstance(pg, dict)]
                if any(pg["measurements"] for pg in kept):
                    got.extend(kept)
            except ApiError as exc:
                errors.append({"step": "disk_measurements", "partitionName": name, "metrics": [metric],
                               **exc.info()})
        if got:
            out.append({"partitionName": name, "data": got})
    return out, errors


def collect_advisor(client, args, node, since, until, basis, extra):
    group = quote(args.group_id, safe="")
    path = f"/groups/{group}/processes/{quote(node['process_id'], safe='')}/performanceAdvisor/suggestedIndexes"
    params = {"since": ms(since), "duration": ms(until) - ms(since), "nIndexes": args.advisor_max_indexes,
              "nExamples": 0}
    pages, truncated = client.pages(path, params, args.max_pages)
    return envelope("atlas_api_advisor", node["host"], basis, pages, truncated=truncated,
                    processId=node["process_id"], window=window_meta(since, until, extra),
                    limitations=["Performance Advisor candidates are not verified gaps; compare with "
                                 "index definitions and explain plans."])


def collect_qsi(client, args, node, since, until, basis, extra, now):
    oldest = now - QSI_RETENTION + timedelta(minutes=10)
    if since < oldest:
        raise RuntimeError(f"window starts {since.isoformat()}, before the 7-day Query Shape Insights retention")
    group, cluster = quote(args.group_id, safe=""), quote(args.cluster_name, safe="")
    base = f"/groups/{group}/clusters/{cluster}/queryShapeInsights"
    params = [("since", ms(since)), ("until", ms(until)), ("nSummaries", args.qsi_max_summaries),
              ("processIds", node["process_id"])]
    payload = client.get(base + "/summaries", params)
    details, detail_errors = [], []
    summaries = payload.get("summaries") or payload.get("results") or [] if isinstance(payload, dict) else []
    hashes = [s.get("queryShapeHash") for s in summaries if isinstance(s, dict) and s.get("queryShapeHash")]
    for qhash in hashes[:args.qsi_details]:
        try:
            details.append({"queryShapeHash": qhash, "data": client.get(
                f"{base}/{quote(qhash, safe='')}/details",
                [("since", ms(since)), ("until", ms(until)), ("processIds", node["process_id"])])})
        except ApiError as exc:
            detail_errors.append({"queryShapeHash": qhash, **exc.info()})
    return envelope("atlas_query_shape_insights", node["host"], basis, payload, processId=node["process_id"],
                    endpoint=base + "/summaries", window=window_meta(since, until, extra),
                    details=details, detail_errors=detail_errors, limitations=[
                        "Atlas Query Shape Insights (M10+, MongoDB 8.0+); window bounded by 7-day retention; "
                        "at most 100 summaries per process."])


def collect_cluster_advisor(client, args):
    group, cluster = quote(args.group_id, safe=""), quote(args.cluster_name, safe="")
    out, errors = {}, []
    for key, path in (("drop_index_suggestions", f"/groups/{group}/clusters/{cluster}/performanceAdvisor/dropIndexSuggestions"),
                      ("schema_advice", f"/groups/{group}/clusters/{cluster}/performanceAdvisor/schemaAdvice")):
        try:
            out[key] = client.get(path)
        except ApiError as exc:
            errors.append({"kind": key, **exc.info()})
    return envelope("atlas_api_cluster_advisor", None, "current_snapshot", out, errors=errors,
                    limitations=["Drop-index suggestions are review candidates only; never drop automatically."])


# ----------------------------------------------------------------------------- main
def load_creds(args):
    env = os.environ
    if env.get("ATLAS_CLIENT_ID") and env.get("ATLAS_CLIENT_SECRET"):
        return {"type": "service_account", "client_id": env["ATLAS_CLIENT_ID"],
                "client_secret": env["ATLAS_CLIENT_SECRET"], "token_url": env.get("ATLAS_TOKEN_URL")}
    if env.get("ATLAS_PUBLIC_KEY") and env.get("ATLAS_PRIVATE_KEY"):
        return {"type": "digest", "public": env["ATLAS_PUBLIC_KEY"], "private": env["ATLAS_PRIVATE_KEY"]}
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if cfg.get("atlas_public_key") and cfg.get("atlas_private_key"):
            return {"type": "digest", "public": cfg["atlas_public_key"], "private": cfg["atlas_private_key"]}
    if sys.stdin.isatty():
        return {"type": "digest", "public": input("Atlas public key: ").strip(),
                "private": getpass.getpass("Atlas private key: ")}
    raise SystemExit("No Atlas credentials: set ATLAS_PUBLIC_KEY/ATLAS_PRIVATE_KEY or ATLAS_CLIENT_ID/ATLAS_CLIENT_SECRET")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="optional JSON with group_id, cluster_name, timezone and/or Atlas key pair")
    p.add_argument("--group-id")
    p.add_argument("--cluster-name")
    p.add_argument("--timezone", help="IANA timezone of the log day (default UTC)")
    p.add_argument("--log-date", help="YYYY-MM-DD: align windows to this local day (default rolling --hours)")
    p.add_argument("--hours", type=float, default=24.0, help="rolling window ending now (default 24)")
    p.add_argument("--out", default="observability-manual", help="output root")
    p.add_argument("--kinds", default="metrics,advisor,query_stats,cluster_advisor",
                   help="comma list of metrics,advisor,query_stats,cluster_advisor")
    p.add_argument("--granularity", default="PT5M", choices=sorted(GRANULARITIES))
    p.add_argument("--metrics", nargs="+", help="override the measurement list")
    p.add_argument("--all-metrics", action="store_true", help="request every process measurement (no m filter)")
    p.add_argument("--hostnames", nargs="+", help="explicit hostnames/aliases/processIds")
    p.add_argument("--host-regex", help="regex on 'hostname userAlias' (multi-cluster projects)")
    p.add_argument("--include-mongos", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--include-config", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--qsi-max-summaries", type=int, default=100)
    p.add_argument("--qsi-details", type=int, default=0,
                   help="also fetch QSI details for the top N shapes per process (default 0)")
    p.add_argument("--qsi-as-query-stats", choices=["if-absent", "always", "never"], default="if-absent",
                   help="also write QSI as queryStats/query-stats.json (agent input); default if-absent")
    p.add_argument("--advisor-max-indexes", type=int, default=50)
    p.add_argument("--api-version", default="2025-03-12")
    p.add_argument("--base-url", default="https://cloud.mongodb.com/api/atlas/v2")
    p.add_argument("--ca-file", help="custom CA bundle (TLS inspection proxies)")
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--pace", type=float, default=0.2, help="seconds between API calls (rate limits)")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--no-redact", action="store_true", help="store raw API payloads")
    p.add_argument("--keep-existing", action="store_true", help="rename existing files to *.bak instead of overwriting")
    p.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        args.group_id = args.group_id or cfg.get("group_id")
        args.cluster_name = args.cluster_name or cfg.get("cluster_name")
        args.timezone = args.timezone or cfg.get("timezone")
    args.timezone = args.timezone or "UTC"
    if not args.group_id or not args.cluster_name:
        p.error("--group-id and --cluster-name are required (or in --config)")
    args.kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    unknown = set(args.kinds) - {"metrics", "advisor", "query_stats", "cluster_advisor"}
    if unknown:
        p.error("unknown --kinds: " + ", ".join(sorted(unknown)))
    args.qsi_max_summaries = min(max(args.qsi_max_summaries, 1), 100)
    if not 0 < args.hours <= 24 * 7:
        p.error("--hours must be in (0, 168]")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = AtlasClient(args.base_url, args.api_version, load_creds(args), timeout=args.timeout,
                         ca_file=args.ca_file, allow_http=args.allow_http, pace_seconds=args.pace)
    now = datetime.now(timezone.utc)
    log_date, since, until, basis, extra = resolve_window(args, now)
    writer = Writer(args.out, log_date, not args.no_redact, not args.keep_existing)
    nodes, cluster_meta = cluster_processes(client, args)
    log.info("Cluster %s: %d process(es); window %s -> %s; folder %s", args.cluster_name, len(nodes),
             iso(since), iso(until), log_date)
    receipt = {"collector": COLLECTOR, "deployment": "atlas", "group_id": args.group_id,
               "cluster": cluster_meta, "log_date": log_date, "window": window_meta(since, until, extra),
               "started": now.isoformat(), "nodes": [], "errors": []}
    for node in nodes:
        entry = {**node, "artifacts": {}}
        for kind, fn in (("metrics", lambda: collect_metrics(client, args, node, since, until, basis, extra, now)),
                         ("advisor", lambda: collect_advisor(client, args, node, since, until, basis, extra)),
                         ("query_stats", lambda: collect_qsi(client, args, node, since, until, basis, extra, now))):
            if kind not in args.kinds:
                continue
            if kind == "query_stats" and node["type"] and str(node["type"]).startswith("SHARD_CONFIG"):
                entry["artifacts"][kind] = {"status": "skipped", "reason": "config server"}
                continue
            try:
                payload = fn()
                if kind == "metrics":
                    entry["artifacts"][kind] = {"status": "collected", "uri": writer.write(
                        [node["host_dir"], "metrics", "metrics.json"], payload),
                        "errors": len(payload.get("errors", [])) + len(payload.get("disk_errors", []))}
                elif kind == "advisor":
                    entry["artifacts"][kind] = {"status": "collected", "uri": writer.write(
                        [node["host_dir"], "advisor", "advisor.json"], payload)}
                else:
                    uri = writer.write([node["host_dir"], "queryStats", "query-shape-insights.json"], payload)
                    target = [node["host_dir"], "queryStats", "query-stats.json"]
                    mode = args.qsi_as_query_stats
                    if mode == "always" or (mode == "if-absent" and writer.replaceable_by_qsi(*target)):
                        uri = writer.write(target, payload)
                    entry["artifacts"][kind] = {"status": "collected", "uri": uri}
                log.info("%s %s: collected", node["host_dir"], kind)
            except (ApiError, RuntimeError, ValueError) as exc:
                info = exc.info() if isinstance(exc, ApiError) else {"error_type": type(exc).__name__,
                                                                      "message": str(exc)[:300]}
                entry["artifacts"][kind] = {"status": "failed", **info}
                receipt["errors"].append({"host_dir": node["host_dir"], "kind": kind, **info})
                log.warning("%s %s failed: %s", node["host_dir"], kind, info)
        receipt["nodes"].append(entry)
    if "cluster_advisor" in args.kinds:
        payload = collect_cluster_advisor(client, args)
        receipt["cluster_advisor"] = {"uri": writer.write(["cluster", "advisor", "cluster-advisor.json"], payload),
                                      "errors": payload["errors"]}
        # Cluster-level advice is optional (tier/role dependent): warnings, not a partial collection.
        receipt["warnings"] = [{"kind": e["kind"], "status": e.get("status")} for e in payload["errors"]]
    receipt.update(finished=datetime.now(timezone.utc).isoformat(), api_calls=client.calls,
                   files=writer.files, status="partial" if receipt["errors"] else "completed")
    writer.overwrite = True
    writer.write(["cluster", "manifests", "atlas-api.json"], receipt)
    print(json.dumps({k: receipt[k] for k in ("status", "log_date", "window", "api_calls")} |
                     {"nodes": {n["host_dir"]: {k: v["status"] for k, v in n["artifacts"].items()}
                                for n in receipt["nodes"]}, "out": str(Path(args.out).resolve())}, indent=2))
    return 2 if receipt["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
