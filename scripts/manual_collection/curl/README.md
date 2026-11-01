# curl / batch samples (no Python)

Very small, read-only samples, one folder per feature. Each folder has:

- `run.sh` for Linux/macOS (bash + curl)
- `run.cmd` for Windows (cmd + `curl.exe`, built into Windows 10 1803+ and Windows 11)

They write the same layout and envelope (`schema_version: 2`) as the Python collectors, so the agent can read the result in `existing_bucket` mode:

```
<MDIAG_OUT>/<YYYY-MM-DD>/<host_dir>/{mongodb/<log>.gz, metrics/metrics.json, advisor/advisor.json,
                                     queryStats/query-stats.json, indexStats/index-stats.json,
                                     indexDefinitions/index-definitions.json}
<MDIAG_OUT>/<YYYY-MM-DD>/cluster/{manifests/processes.json, queryStats/query-stats.json, advisor/cluster-advisor.json}
```

`07` and `08` read database commands (`$queryStats`, `$indexStats`). There is no HTTP API for these, so they use `mongosh` (still no Python).

## Features and rights needed

| Folder | What | Endpoint / command | Output | Rights needed |
|---|---|---|---|---|
| `01_list_processes` | Lists processes and writes `hosts.generated.txt` | `GET /groups/{G}/processes` | `cluster/manifests/processes.json` | Atlas: **Project Read Only** |
| `02_download_logs` | mongod/mongos logs (`.gz`), skips files already present | `GET /groups/{G}/clusters/{hostname}/logs/{mongodb\|mongos}.gz` | `<host>/mongodb/<log>.gz` | Atlas: **Project Data Access Read Only** (or Project Cluster Log Viewer, Data Access Read Write/Admin, Stream Processing Owner) |
| `03_process_metrics` | Process + disk measurements | `GET /processes/{pid}/measurements`, `/disks/{part}/measurements` | `<host>/metrics/metrics.json` | Atlas: **Project Read Only** |
| `04_performance_advisor` | Suggested indexes per host | `GET /processes/{pid}/performanceAdvisor/suggestedIndexes` | `<host>/advisor/advisor.json` | Atlas: **Project Read Only**. Values in sample queries are redacted unless the key has **Project Data Access Read Only** or higher. |
| `05_query_shape_insights` | Query shapes per host; if empty or failed, **one** cluster-wide call copied to the node (`scope:"cluster"`). Skips nodes that already have shapes. | `GET /groups/{G}/clusters/{C}/queryShapeInsights/summaries` | `<host>/queryStats/query-stats.json`, `cluster/queryStats/query-stats.json` | Atlas: **Project Observability Viewer** or **Project Data Access Read Only** (or Read Write / Admin / Project Owner). M10+, MongoDB 8.0+ only. |
| `06_cluster_advisor` | Drop-index suggestions + schema advice | `GET /clusters/{C}/performanceAdvisor/dropIndexSuggestions`, `/schemaAdvice` | `cluster/advisor/cluster-advisor.json` | Atlas: **Project Read Only** |
| `07_mongosh_query_stats` | `$queryStats` (top 1000 by exec time). Skips nodes that already have shapes. | `mongosh` → `admin.aggregate([{$queryStats:{}}])` | `<host>/queryStats/query-stats.json` | DB user: **`queryStatsRead`** (+ `queryStatsReadTransformed` if you use `transformIdentifiers`). Built-in **`clusterMonitor`** has both. |
| `08_mongosh_index_stats` | `$indexStats` + `getIndexes` for every user collection (never reads documents) | `mongosh` → `listDatabases`, `listCollections`, `listIndexes`, `$indexStats` | `<host>/indexStats/index-stats.json`, `<host>/indexDefinitions/index-definitions.json` | DB user: `listDatabases`, `listCollections`, `listIndexes`, `indexStats` (**`clusterMonitor`**, or the `diagObservability` custom role below) |
| `09_opsmanager_metrics` | Ops Manager host + disk measurements (`--list` saves the hosts and their hostIds) | `GET /api/public/v1.0/groups/{G}/hosts/{hostId}/measurements`, `/disks/{part}/measurements` | `<host>/metrics/metrics.json` | Ops Manager: **Project Read Only** |
| `10_opsmanager_advisor` | Ops Manager suggested indexes | `GET /api/public/v1.0/groups/{G}/hosts/{hostId}/performanceAdvisor/suggestedIndexes` | `<host>/advisor/advisor.json` | Ops Manager: **Project Read Only**. Field values need **Project Data Access Read Only** (or Read/Write, Admin, Project Owner). Ops Manager has no query-stats API: use `07`. |

**Smallest single Atlas key for 01–06:** **Project Data Access Read Only**. It covers Read Only, logs, unredacted advisor values and Query Shape Insights. If you don't want the key to have data access, use **Project Read Only + Project Observability Viewer + Project Cluster Log Viewer** (advisor sample values will then be redacted).

**Database user for 07/08 (least privilege)**: the same custom role as the main manual README:

```js
db.getSiblingDB("admin").createRole({ role: "diagObservability", roles: [], privileges: [
  { resource: { cluster: true }, actions: ["queryStatsRead", "serverStatus", "listDatabases"] },
  { resource: { db: "", collection: "" }, actions: ["listCollections", "listIndexes", "indexStats", "collStats"] } ] })
```
On Atlas, create it as a **custom database role** with the same actions.

## Setup

```bash
# Atlas – API key (HTTP digest)            ...or a service account (OAuth bearer token)
export ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=...   # export ATLAS_CLIENT_ID=... ATLAS_CLIENT_SECRET=...
export ATLAS_GROUP_ID=<project id> ATLAS_CLUSTER=<cluster name>
export MDIAG_OUT=./observability-manual MDIAG_DATE=2026-10-31     # optional; date defaults to today UTC
cd curl
./01_list_processes/run.sh            # review hosts.generated.txt, save as hosts.txt
./02_download_logs/run.sh; ./03_process_metrics/run.sh; ./04_performance_advisor/run.sh
./05_query_shape_insights/run.sh; ./06_cluster_advisor/run.sh
MDIAG_USER=diag_reader ./07_mongosh_query_stats/run.sh     # password is prompted per node

# Ops Manager
export OM_BASE_URL=https://opsmanager.example.net:8443 OM_GROUP_ID=... OM_PUBLIC_KEY=... OM_PRIVATE_KEY=...
./09_opsmanager_metrics/run.sh --list   # find hostIds -> lines "hostId hostname:port" in hosts.txt
./09_opsmanager_metrics/run.sh; ./10_opsmanager_advisor/run.sh
```

Windows (`cmd`), same variables. `MDIAG_DATE` is required:
```bat
set ATLAS_PUBLIC_KEY=...& set ATLAS_PRIVATE_KEY=...& set ATLAS_GROUP_ID=...& set ATLAS_CLUSTER=...
set MDIAG_DATE=2026-10-31
curl\03_process_metrics\run.cmd
```

### Variables

| Variable | Use |
|---|---|
| `ATLAS_PUBLIC_KEY` / `ATLAS_PRIVATE_KEY` | Atlas API key (digest) |
| `ATLAS_CLIENT_ID` / `ATLAS_CLIENT_SECRET` | Atlas service account. The token comes from `POST https://cloud.mongodb.com/api/oauth/token`. |
| `ATLAS_GROUP_ID`, `ATLAS_CLUSTER` | Project id; cluster name (needed by 05, 06) |
| `ATLAS_API_VERSION` | Default `2025-03-12`. Logs always use `application/vnd.atlas.2023-02-01+gzip`. |
| `OM_BASE_URL`, `OM_GROUP_ID`, `OM_PUBLIC_KEY`, `OM_PRIVATE_KEY`, `OM_CA_FILE` | Ops Manager (09, 10) |
| `CURL_CA_FILE` | Extra CA bundle (proxies / private PKI) |
| `MDIAG_OUT`, `MDIAG_DATE`, `HOSTS_FILE` | Output root, day folder, hosts list (`./hosts.txt`) |
| `MDIAG_START` / `MDIAG_END` | Optional window, ISO UTC. Default: last 24 h. On Windows set `MDIAG_START_ISO`/`MDIAG_END_ISO` **and** `MDIAG_START_S`/`MDIAG_END_S` (epoch seconds). On macOS set `MDIAG_START_S`/`MDIAG_END_S` too. |
| `MDIAG_GRANULARITY`, `MDIAG_PARTITION` | Metrics granularity (`PT5M`), disk partition (`data`) |
| `MDIAG_HOST_FILTER` | Regex filter for `hosts.generated.txt` (01) |
| `MDIAG_REFRESH=1` | Re-collect query stats even if shapes are already present (05, 07) |
| `MDIAG_USER`, `MDIAG_AUTH_DB`, `MDIAG_MONGOSH_OPTS` | mongosh user, auth db (`admin`), extra options (e.g. `--tls --tlsCAFile ca.pem`) |

Hosts file (see `hosts.example.txt`):
- Atlas, and mongosh (07/08): `processId(hostname:port) [typeName] [host_dir]`.
- Ops Manager (09/10): `hostId hostname:port [host_dir]`.

A co-located `SHARD_MONGOS` is written to `<hostname>_<port>`.

## Security notes

- Everything is **read-only** (`GET` only, plus the OAuth token `POST`). Nothing changes on the cluster or project.
- Secrets never go on the command line. They are written to a private temporary curl config file (`curl -K`):
  - Linux/macOS: mode 600, deleted on exit.
  - Windows: `%TEMP%`, deleted by `cleanup`.
- mongosh prompts for the password.
- **No redaction:** unlike the Python collectors, these samples save the API answers as-is. Advisor sample queries, Query Shape Insights and logs can contain literal values, so review the output before sharing it.
- The caller's IP must be on the API key's or service account's **API access list**. For service accounts the list is checked when the token is used, not when it is created.
- The `.cmd` versions are kept minimal: JSON is assembled with `echo`/`type`, and values containing `%`, `^`, `&` or `!` in keys or paths need escaping.
