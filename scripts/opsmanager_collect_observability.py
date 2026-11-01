#!/usr/bin/env python3
"""Manual, read-only Ops Manager / Cloud Manager observability export (standalone, stdlib only).

Collects, per monitored data-bearing host of ONE deployment (replica set or sharded cluster), the
files the diagnostic agent reads in ``existing_bucket`` mode:

  <out>/<log-date>/<host_dir>/metrics/metrics.json      host measurements (+ disk partitions)
  <out>/<log-date>/<host_dir>/advisor/advisor.json      Performance Advisor suggested indexes
  <out>/<log-date>/<host_dir>/advisor/slow-query-logs.json   only with --slow-query-logs (opt-in)
  <out>/<log-date>/cluster/manifests/ops-manager-api.json    run receipt (host-id mapping, errors)

Query statistics are not available from the Ops Manager API: run mongodb_collect_observability.js
($queryStats, indexes, index/collection stats) on each node with mongosh.

Credentials (never written to disk or logs): OM_PUBLIC_KEY + OM_PRIVATE_KEY (programmatic API key,
HTTP digest), or --config FILE with ops_manager_public_key/ops_manager_private_key, or a prompt.
Required role: Project Read Only (Project Data Access Read Only for Performance Advisor).

Examples:
  export OM_PUBLIC_KEY=... OM_PRIVATE_KEY=...
  python3 opsmanager_collect_observability.py --base-url https://opsmanager.example.net:8443 \
          --group-id 5f... --cluster-name rs-prod --timezone Asia/Jerusalem --list
  python3 opsmanager_collect_observability.py --base-url https://opsmanager.example.net:8443 \
          --group-id 5f... --cluster-name rs-prod --timezone Asia/Jerusalem --out ./observability-manual
  # Cloud Manager: --base-url https://cloud.mongodb.com
"""
from __future__ import annotations

import argparse
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
COLLECTOR = {"name": "manual-ops-manager-api-collector", "version": VERSION}
log = logging.getLogger("om_collect")
API_PREFIX = "/api/public/v1.0"
MAX_M_PER_REQUEST = 10
GRANULARITIES = {"PT10S", "PT1M", "PT5M", "PT1H", "P1D"}
DATA_HOST_TYPES = {"REPLICA_PRIMARY", "REPLICA_SECONDARY", "SHARD_PRIMARY", "SHARD_SECONDARY", "SHARD_CONFIG",
                   "SHARD_MONGOS", "SHARD_STANDALONE", "STANDALONE", "RECOVERING"}
DISK_METRICS_HINT = "DISK_PARTITION_* measurements require hardware monitoring on the host (Automation/Monitoring agent)."

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


class OpsManagerClient:
    def __init__(self, base_url, public_key, private_key, timeout=120, retries=4, ca_file=None, insecure=False,
                 allow_http=False, pace_seconds=0.0):
        base_url = base_url.rstrip("/")
        if base_url.endswith(API_PREFIX):
            base_url = base_url[: -len(API_PREFIX)]
        parts = urlsplit(base_url)
        if parts.scheme != "https" and not allow_http:
            raise SystemExit("Ops Manager URL must use HTTPS (pass --allow-http only for an isolated lab)")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.base_url = base_url + API_PREFIX
        self.timeout, self.retries, self.pace, self.calls = timeout, retries, pace_seconds, 0
        handlers = []
        if insecure:
            log.warning("TLS certificate verification DISABLED (--insecure)")
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            handlers.append(HTTPSHandler(context=ctx))
        elif ca_file:
            handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        pm = HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, self.origin, public_key, private_key)
        handlers.append(HTTPDigestAuthHandler(pm))
        self.opener = build_opener(*handlers)

    def get(self, path_or_url, params=None):
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise ValueError("Refusing cross-origin request")
        path = urlsplit(url).path
        for attempt in range(1, self.retries + 1):
            if self.pace:
                time.sleep(self.pace)
            self.calls += 1
            try:
                req = Request(url, headers={"Accept": "application/json",
                                            "User-Agent": f"mongodb-manual-observability/{VERSION}"})
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except HTTPError as exc:
                code = None
                try:
                    code = json.loads(exc.read().decode("utf-8", "replace")).get("errorCode")
                except Exception:
                    pass
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
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
        out = []
        payload = self.get(path, params)
        for _ in range(max_pages):
            out.append(payload)
            links = payload.get("links", []) if isinstance(payload, dict) else []
            nxt = next((x.get("href") for x in links if isinstance(x, dict) and x.get("rel") == "next"), None)
            if not nxt:
                return out, False
            payload = self.get(urljoin(self.base_url + "/", nxt))
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
def discover_hosts(client, args):
    group = quote(args.group_id, safe="")
    clusters = client.list_all(f"/groups/{group}/clusters")
    hosts = client.list_all(f"/groups/{group}/hosts")
    selected_clusters = []
    if args.cluster_name:
        name = args.cluster_name
        selected_clusters = [c for c in clusters if name in (c.get("clusterName"), c.get("replicaSetName"), c.get("id"))]
        if not selected_clusters and not (args.hostnames or args.host_regex):
            names = sorted({c.get("clusterName") or c.get("replicaSetName") for c in clusters} - {None})
            raise SystemExit(f"No cluster named {name!r}. Known: {', '.join(names) or 'none'} (use --list)")
    ids = {c["id"] for c in selected_clusters}
    if args.hostnames:
        wanted = set(args.hostnames)
        chosen = [h for h in hosts if h.get("hostname") in wanted or h.get("id") in wanted
                  or f"{h.get('hostname')}:{h.get('port')}" in wanted]
    elif args.host_regex:
        rx = re.compile(args.host_regex, re.I)
        chosen = [h for h in hosts if rx.search(f"{h.get('hostname', '')}:{h.get('port', '')} {h.get('replicaSetName', '')}")]
    else:
        chosen = [h for h in hosts if h.get("clusterId") in ids]
    chosen = [h for h in chosen if h.get("typeName") in DATA_HOST_TYPES]
    if not args.include_mongos:
        chosen = [h for h in chosen if h.get("typeName") != "SHARD_MONGOS"]
    if not args.include_config:
        chosen = [h for h in chosen if h.get("typeName") != "SHARD_CONFIG"]
    if not chosen:
        raise SystemExit("No monitored data-bearing host matched; use --list, --hostnames or --host-regex")
    by_host = {}
    for h in chosen:
        by_host.setdefault(h["hostname"], []).append(h)
    nodes = []
    for hostname, items in sorted(by_host.items()):
        items.sort(key=lambda h: (h.get("typeName") == "SHARD_MONGOS", int(h.get("port") or 27017)))
        for i, h in enumerate(items):
            fmt = args.host_dir_format if i == 0 else "{hostname}_{port}"
            host_dir = safe_filename(fmt.format(hostname=hostname, port=h.get("port"), short=hostname.split(".")[0]))
            nodes.append({"host": f"{hostname}:{h.get('port')}", "hostname": hostname, "host_dir": host_dir,
                          "host_id": h["id"], "port": h.get("port"), "type": h.get("typeName"),
                          "replica_set": h.get("replicaSetName"), "version": h.get("version"),
                          "hidden": h.get("hidden"), "cluster_id": h.get("clusterId")})
    meta = [{"id": c.get("id"), "clusterName": c.get("clusterName"), "replicaSetName": c.get("replicaSetName"),
             "typeName": c.get("typeName"), "shardName": c.get("shardName")} for c in selected_clusters]
    return nodes, meta, clusters


# ----------------------------------------------------------------------------- collectors
def collect_metrics(client, args, node, since, until, basis, extra, now):
    base = f"/groups/{quote(args.group_id, safe='')}/hosts/{quote(node['host_id'], safe='')}"
    granularity, note = args.granularity, None
    fine_limit = timedelta(hours=args.fine_retention_hours)
    if granularity in {"PT10S", "PT1M", "PT5M"} and since < now - fine_limit:
        granularity = "PT1H"
        note = f"{args.granularity} assumed not retained before {fine_limit} ago; used PT1H"
    window = [("start", iso(since)), ("end", iso(until)), ("granularity", granularity)]
    batches = ([args.metrics[i:i + MAX_M_PER_REQUEST] for i in range(0, len(args.metrics), MAX_M_PER_REQUEST)]
               if args.metrics else [[]])
    pages, errors, truncated = [], [], False
    for batch in batches:
        try:
            got, cut = client.pages(base + "/measurements", window + [("m", m) for m in batch], args.max_pages)
            pages.extend(got)
            truncated = truncated or cut
        except ApiError as exc:
            if exc.status != 400 or len(batch) <= 1:
                errors.append({"metrics": batch, **exc.info()})
                continue
            for name in batch:
                try:
                    pages.extend(client.pages(base + "/measurements", window + [("m", name)], args.max_pages)[0])
                except ApiError as single:
                    errors.append({"metrics": [name], **single.info()})
    if errors and not pages:
        raise RuntimeError("all measurement requests failed: " + json.dumps(errors[:3]))
    disks, disk_errors = [], []
    if args.disks:
        try:
            parts = [d.get("partitionName") for d in client.list_all(base + "/disks") if d.get("partitionName")]
            for name in list(dict.fromkeys(parts))[:4]:
                try:
                    got, _ = client.pages(base + "/disks/" + quote(name, safe="") + "/measurements", window,
                                          args.max_pages)
                    disks.append({"partitionName": name, "data": got})
                except ApiError as exc:
                    disk_errors.append({"step": "disk_measurements", "partitionName": name, **exc.info()})
        except ApiError as exc:
            disk_errors.append({"step": "list_disks", **exc.info(), "hint": DISK_METRICS_HINT})
    databases, db_errors = [], []
    if args.database_metrics:
        try:
            for item in client.list_all(base + "/databases"):
                name = item.get("databaseName")
                if not name or name in {"admin", "config", "local"}:
                    continue
                try:
                    got, _ = client.pages(base + "/databases/" + quote(name, safe="") + "/measurements", window,
                                          args.max_pages)
                    databases.append({"databaseName": name, "data": got})
                except ApiError as exc:
                    db_errors.append({"databaseName": name, **exc.info()})
        except ApiError as exc:
            db_errors.append({"step": "list_databases", **exc.info()})
    meta = window_meta(since, until, {"granularity": granularity, **extra})
    if note:
        meta.update(requested_granularity=args.granularity, granularity_note=note)
    return envelope("ops_manager_metrics", node["host"], basis, pages, truncated=truncated, window=meta,
                    hostId=node["host_id"], disk_measurements=disks, disk_errors=disk_errors,
                    database_measurements=databases, database_errors=db_errors,
                    measurement_plan={"requested": args.metrics or [], "unsupported": [], "aliases": {},
                                      "all_metrics": not args.metrics},
                    errors=errors, limitations=[
                        "Ops Manager metric names/retention depend on version and monitoring settings; "
                        "missing metrics are reported, never fabricated."])


def collect_advisor(client, args, node, since, until, basis, extra):
    base = f"/groups/{quote(args.group_id, safe='')}/hosts/{quote(node['host_id'], safe='')}/performanceAdvisor"
    params = {"since": ms(since), "duration": ms(until) - ms(since), "nIndexes": args.advisor_max_indexes,
              "nExamples": 0}
    pages, truncated = client.pages(base + "/suggestedIndexes", params, args.max_pages)
    namespaces, ns_error = None, None
    try:
        namespaces = client.get(base + "/namespaces", {"since": ms(since), "duration": ms(until) - ms(since)})
    except ApiError as exc:
        ns_error = exc.info()
    return envelope("ops_manager_advisor", node["host"], basis, pages, truncated=truncated, hostId=node["host_id"],
                    window=window_meta(since, until, extra), slow_namespaces=namespaces,
                    slow_namespaces_error=ns_error, limitations=[
                        "Performance Advisor candidates are not verified gaps; compare with index definitions "
                        "and explain plans. Requires the host profiler/slow-log data seen by the agent."])


def collect_slow_logs(client, args, node, since, until, basis, extra):
    base = f"/groups/{quote(args.group_id, safe='')}/hosts/{quote(node['host_id'], safe='')}/performanceAdvisor"
    payload = client.get(base + "/slowQueryLogs", {"since": ms(since), "duration": ms(until) - ms(since),
                                                    "nLogs": args.slow_query_logs})
    rows = payload.get("slowQueries", []) if isinstance(payload, dict) else []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("line"), str):
            line = row["line"]
            try:  # structured (4.4+) log line: keep it JSON so the sanitizer reduces filters to shapes
                row["line"] = json.loads(line)
            except ValueError:
                row["line"] = "<unstructured log line removed>"
    return envelope("ops_manager_slow_query_logs", node["host"], basis, rows, hostId=node["host_id"],
                    window=window_meta(since, until, extra), limitations=[
                        "Opt-in. Lines are parsed and redacted to query shapes; prefer the downloaded mongod logs."])


# ----------------------------------------------------------------------------- main
def load_creds(args):
    if os.environ.get("OM_PUBLIC_KEY") and os.environ.get("OM_PRIVATE_KEY"):
        return os.environ["OM_PUBLIC_KEY"], os.environ["OM_PRIVATE_KEY"]
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if cfg.get("ops_manager_public_key") and cfg.get("ops_manager_private_key"):
            return cfg["ops_manager_public_key"], cfg["ops_manager_private_key"]
    if sys.stdin.isatty():
        return input("Ops Manager public key: ").strip(), getpass.getpass("Ops Manager private key: ")
    raise SystemExit("No credentials: set OM_PUBLIC_KEY and OM_PRIVATE_KEY")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="optional JSON: ops_manager_base_url, group_id, cluster_name, timezone, keys")
    p.add_argument("--base-url", help="Ops Manager URL, e.g. https://opsmanager.example.net:8443")
    p.add_argument("--group-id")
    p.add_argument("--cluster-name", help="cluster display name, replica set name or cluster id")
    p.add_argument("--hostnames", nargs="+", help="explicit hostname, hostname:port or host id")
    p.add_argument("--host-regex", help="regex on 'hostname:port replicaSetName'")
    p.add_argument("--host-dir-format", default="{hostname}",
                   help="node folder name; keep identical to the log folders. {hostname} {port} {short}")
    p.add_argument("--include-mongos", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--include-config", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--list", action="store_true", help="print clusters and matched hosts, then exit")
    p.add_argument("--timezone", help="IANA timezone of the log day (default UTC)")
    p.add_argument("--log-date", help="YYYY-MM-DD: align windows to this local day (default rolling --hours)")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--out", default="observability-manual")
    p.add_argument("--kinds", default="metrics,advisor", help="comma list of metrics,advisor")
    p.add_argument("--granularity", default="PT5M", choices=sorted(GRANULARITIES))
    p.add_argument("--fine-retention-hours", type=float, default=48.0,
                   help="minute-level retention configured in Ops Manager (default 48)")
    p.add_argument("--metrics", nargs="+", help="restrict to these measurement names (default: all)")
    p.add_argument("--disks", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--database-metrics", action="store_true", help="also per-database measurements")
    p.add_argument("--advisor-max-indexes", type=int, default=50)
    p.add_argument("--slow-query-logs", type=int, default=0, metavar="N",
                   help="opt-in: also fetch up to N Performance Advisor slow-query log lines (redacted)")
    p.add_argument("--ca-file", help="CA bundle for a private Ops Manager certificate")
    p.add_argument("--insecure", action="store_true", help="disable TLS verification (not recommended)")
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--pace", type=float, default=0.1)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--no-redact", action="store_true")
    p.add_argument("--keep-existing", action="store_true", help="rename existing files to *.bak")
    p.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        args.base_url = args.base_url or cfg.get("ops_manager_base_url")
        args.group_id = args.group_id or cfg.get("group_id")
        args.cluster_name = args.cluster_name or cfg.get("cluster_name")
        args.timezone = args.timezone or cfg.get("timezone")
    args.timezone = args.timezone or "UTC"
    if not args.base_url or not args.group_id:
        p.error("--base-url and --group-id are required (or in --config)")
    if not (args.cluster_name or args.hostnames or args.host_regex or args.list):
        p.error("one of --cluster-name, --hostnames or --host-regex is required")
    args.kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if set(args.kinds) - {"metrics", "advisor"}:
        p.error("--kinds accepts metrics,advisor")
    if not 0 < args.hours <= 24 * 7:
        p.error("--hours must be in (0, 168]")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    public, private = load_creds(args)
    client = OpsManagerClient(args.base_url, public, private, timeout=args.timeout, ca_file=args.ca_file,
                              insecure=args.insecure, allow_http=args.allow_http, pace_seconds=args.pace)
    if args.list:
        group = quote(args.group_id, safe="")
        clusters = client.list_all(f"/groups/{group}/clusters")
        print(json.dumps({"clusters": [{k: c.get(k) for k in ("id", "clusterName", "replicaSetName", "typeName",
                                                                "shardName")} for c in clusters]}, indent=2))
        if args.cluster_name or args.hostnames or args.host_regex:
            nodes, _, _ = discover_hosts(client, args)
            print(json.dumps({"hosts": nodes}, indent=2))
        return 0
    now = datetime.now(timezone.utc)
    log_date, since, until, basis, extra = resolve_window(args, now)
    writer = Writer(args.out, log_date, not args.no_redact, not args.keep_existing)
    nodes, cluster_meta, _ = discover_hosts(client, args)
    log.info("%d host(s); window %s -> %s; folder %s", len(nodes), iso(since), iso(until), log_date)
    receipt = {"collector": COLLECTOR, "deployment": "ops_manager", "base_url": client.origin,
               "group_id": args.group_id, "clusters": cluster_meta, "log_date": log_date,
               "window": window_meta(since, until, extra), "started": now.isoformat(), "nodes": [], "errors": [],
               "agent_config_hint": {
                   "MONGODB_LOG_DIAG_OBSERVABILITY_HOSTS": [n["host_dir"] for n in nodes],
                   "MONGODB_LOG_DIAG_OPS_MANAGER_HOST_IDS": {n["host_dir"]: n["host_id"] for n in nodes}}}
    for node in nodes:
        entry = {**node, "artifacts": {}}
        jobs = [("metrics", ["metrics", "metrics.json"],
                 lambda: collect_metrics(client, args, node, since, until, basis, extra, now)),
                ("advisor", ["advisor", "advisor.json"],
                 lambda: collect_advisor(client, args, node, since, until, basis, extra))]
        if args.slow_query_logs > 0:
            jobs.append(("slow_query_logs", ["advisor", "slow-query-logs.json"],
                         lambda: collect_slow_logs(client, args, node, since, until, basis, extra)))
        for kind, rel, fn in jobs:
            if kind != "slow_query_logs" and kind not in args.kinds:
                continue
            try:
                payload = fn()
                entry["artifacts"][kind] = {"status": "collected", "uri": writer.write([node["host_dir"], *rel], payload)}
                log.info("%s %s: collected", node["host_dir"], kind)
            except (ApiError, RuntimeError, ValueError) as exc:
                info = exc.info() if isinstance(exc, ApiError) else {"error_type": type(exc).__name__,
                                                                      "message": str(exc)[:300]}
                entry["artifacts"][kind] = {"status": "failed", **info}
                receipt["errors"].append({"host_dir": node["host_dir"], "kind": kind, **info})
                log.warning("%s %s failed: %s", node["host_dir"], kind, info)
        receipt["nodes"].append(entry)
    receipt.update(finished=datetime.now(timezone.utc).isoformat(), api_calls=client.calls, files=writer.files,
                   status="partial" if receipt["errors"] else "completed")
    writer.overwrite = True
    writer.write(["cluster", "manifests", "ops-manager-api.json"], receipt)
    print(json.dumps({"status": receipt["status"], "log_date": log_date, "window": receipt["window"],
                      "api_calls": client.calls,
                      "nodes": {n["host_dir"]: {k: v["status"] for k, v in n["artifacts"].items()}
                                for n in receipt["nodes"]},
                      "out": str(Path(args.out).resolve())}, indent=2))
    return 2 if receipt["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
