# Manual observability collection (security-restricted environments)

These are standalone scripts for customers who can't give the agent credentials. The customer runs them, reviews the output, and sends the folder. They don't import `agent/` or `skills/`, and they only read data.

| Script | Runs where | Collects | Needs |
|---|---|---|---|
| `mongodb_collect_observability.js` | `mongosh`, on **each node** | `$queryStats`; for every non-system db/collection: indexes, `$indexStats`, `$collStats` (storage, latency, queryExecStats, count) | mongosh 2.x; MongoDB 8.0+ for `$queryStats` (7.0.12+ on Atlas) |
| `atlas_collect_observability.py` | any host with HTTPS to Atlas | process + disk metrics (same 77 measurements the agent requests), Performance Advisor per process, Query Shape Insights for 24 h, cluster drop-index/schema advice | Python 3.9+, standard library only |
| `opsmanager_collect_observability.py` | any host with HTTPS to Ops Manager | host metrics (all measurements) + disk partitions (+ per-db metrics if requested), Performance Advisor (+ optional redacted slow logs) | Python 3.9+, standard library only |
| `run_mongosh_all_nodes.sh` | jump host | runs the JS on every `host:port` in a file | mongosh |

The output goes in the same per-node layout the agent reads in `existing_bucket` mode, so the customer can upload the folder into the bucket prefix as is:

```
<out>/<YYYY-MM-DD>/<host_dir>/{queryStats,indexDefinitions,indexStats,collStats,metrics,advisor}/*.json
<out>/<YYYY-MM-DD>/cluster/manifests/*.json        # run receipts: status, windows, errors, file list
```

`<host_dir>` is the sanitized hostname, the same name the log downloader uses. If the folder names have to match your log folders, set `MDIAG_HOST_DIR` (JS) or `--host-dir-format` (Ops Manager).

## 1. MongoDB (mongosh, per node)

Read-only user (minimum):
```js
db.getSiblingDB("admin").createRole({ role: "diagObservability", privileges: [
  { resource: { cluster: true }, actions: ["queryStatsRead", "serverStatus", "listDatabases"] },
  { resource: { db: "", collection: "" }, actions: ["listCollections", "listIndexes", "indexStats", "collStats"] }
], roles: [] })
db.getSiblingDB("admin").createUser({ user: "diag_reader", pwd: passwordPrompt(), roles: ["diagObservability"] })
```
(`clusterMonitor` also works. For HMAC-transformed query stats, add `queryStatsReadTransformed`.)

```bash
export MDIAG_OUT_DIR=./observability-manual MDIAG_TIMEZONE=Asia/Jerusalem
mongosh "mongodb://node1:27017/?directConnection=true&tls=true" -u diag_reader --file mongodb_collect_observability.js
# or for all nodes:
MDIAG_USER=diag_reader ./run_mongosh_all_nodes.sh hosts.txt
```
- Connect **directly** to each data-bearing node. `$indexStats` and `$collStats` are node-local. `secondaryPreferred` is set so secondaries work.
- Sharded clusters: also run it on each `mongos`, because routed queries appear in `$queryStats` on the mongos. Set `MDIAG_SKIP=index_definitions,index_stats,coll_stats` there.
- Useful settings: `MDIAG_INCLUDE_DBS` / `MDIAG_EXCLUDE_DBS`, `MDIAG_MAX_COLLECTIONS` (5000), `MDIAG_MAX_QUERY_STATS` (1000, sorted by total exec time — same cap as the agent), `MDIAG_QUERYSTATS_HMAC_KEY` (hashes field and collection names), `MDIAG_WT_DETAILS=true` (full WiredTiger blobs; off by default, only reusable-space bytes are kept).

## 2. Atlas API

```bash
export ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=...        # or ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET (service account)
python3 atlas_collect_observability.py --group-id <projectId> --cluster-name <cluster> \
        --timezone Asia/Jerusalem --out ./observability-manual
```
- Role: **Project Read Only**. Performance Advisor also needs Project Data Access Read Only. If the key has an IP access list, the IP that runs the script must be on it.
- Window: the last 24 h by default (`--hours`). Use `--log-date YYYY-MM-DD` to match the log day exactly. Query Shape Insights only covers the last 7 days (M10+, MongoDB 8.0+).
- Processes are found from the cluster's SRV prefix. In multi-cluster projects with non-standard names, use `--host-regex` or `--hostnames`.
- QSI is written to `queryStats/query-shape-insights.json`. It's copied to `query-stats.json` only if no mongosh `$queryStats` file is already there. Run mongosh first if you want both.
- Options: `--granularity PT1M|PT5M|PT1H`, `--all-metrics`, `--qsi-details N`, `--kinds metrics,advisor,query_stats,cluster_advisor`, `--ca-file` for TLS-inspecting proxies.

## 3. Ops Manager / Cloud Manager API

```bash
export OM_PUBLIC_KEY=... OM_PRIVATE_KEY=...
python3 opsmanager_collect_observability.py --base-url https://opsmanager:8443 --group-id <projectId> --cluster-name <name> --list
python3 opsmanager_collect_observability.py --base-url https://opsmanager:8443 --group-id <projectId> \
        --cluster-name <name> --timezone Asia/Jerusalem --ca-file corp-ca.pem --out ./observability-manual
```
- `--list` shows the clusters and the matched hosts (with host IDs) before anything is collected.
- The Ops Manager API has no query statistics. Run the mongosh script for `$queryStats`.
- Disk partition metrics need hardware monitoring on the host. If they're missing, the run still succeeds and records them in `disk_errors`.
- The receipt includes `agent_config_hint` (`OBSERVABILITY_HOSTS`, `OPS_MANAGER_HOST_IDS`) in case you later switch to the agent's live mode.
- `--slow-query-logs N` is opt-in. Structured log lines are parsed and reduced to query shapes, and unstructured lines are removed.

## Security behaviour (all scripts)
- Read-only: no writes, no profiler or parameter changes, no document reads.
- Secrets are never written: keys come from env vars or a prompt. Output and receipts never contain keys, tokens or connection strings.
- Redaction (on by default, `--no-redact` / `MDIAG_REDACT=false` to disable) uses the same rules as the agent. Query literals in filters, pipelines, predicates and partial filters become `?`. Client IPs, `remote`, `comment`, raw examples and credential-bearing URIs are dropped. Only the application name of the client is kept.
- HTTPS only, the API host only (pagination can't follow links to another host), and retries with backoff on 429/5xx.
- Each item fails on its own: the error is recorded in the file or receipt, and the exit code is `2` (partial). Data is never made up to fill a gap.
- Review the folder before sharing. `jq` or `grep` on `?` and on your own sensitive field names is a quick check.

## No-Python samples (curl / Windows batch)

[`curl/`](curl/README.md) has one folder per feature, each with a very small `run.sh` (bash + curl) and `run.cmd` (Windows `curl.exe`):
- Atlas: processes, logs, metrics, Performance Advisor, Query Shape Insights with the cluster-wide fallback, cluster advisor.
- mongosh: `$queryStats`, `$indexStats`, `$collStats`.
- Ops Manager: metrics, advisor.

[`curl/README.md`](curl/README.md) also lists the API-key roles and database privileges each feature needs.
