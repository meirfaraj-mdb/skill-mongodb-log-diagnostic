# Observability artifact and configuration contract

See repository-root `OBSERVABILITY_USAGE.md` for the complete environment schema and producer file layouts.

The loader accepts `MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED`, `DEPLOYMENT_TYPE`, `OBSERVABILITY_HOSTS`, `OBSERVABILITY_SOURCES`, `MONGODB_URI_TEMPLATE`, `MONGODB_TIMEOUT_MS`, `MONGODB_MAX_TIME_MS`, `OBSERVABILITY_MAX_ROWS`, `INDEX_STATS_ENABLED`, `INDEX_STATS_MAX_COLLECTIONS`, `QUERY_SHAPE_SOURCE`, `QUERY_SHAPE_WINDOW_HOURS`, `QUERY_STATS_MAX_SUMMARIES`, `ATLAS_PROCESS_IDS`, `QUERY_PLAN_REQUESTS`, `PROFILER_ENABLED`, `ATLAS_BASE_URL`, `OPS_MANAGER_BASE_URL`, `OPS_MANAGER_HOST_IDS`, `OPS_MANAGER_PUBLIC_KEY`, `OPS_MANAGER_PRIVATE_KEY`, and `OBSERVABILITY_API_MAX_PAGES`. Every abbreviated suffix in this list uses the same `MONGODB_LOG_DIAG_` prefix.

`OBSERVABILITY_SOURCES`, `ATLAS_PROCESS_IDS`, and `OPS_MANAGER_HOST_IDS` are JSON objects; `QUERY_PLAN_REQUESTS` is a JSON list. Hosts are comma-separated or a JSON list. Booleans use true/false or 1/0. The existing Atlas API secret accepts only atlas_public_key and atlas_private_key; no observability settings are added to that secret.

Source keys: query_stats, index_definitions, index_stats, coll_stats, query_plans, metrics, advisor, profiler. Bucket mode overrides all source selections. Live source selections are validated against each kind's capability matrix. No Ops Manager query statistics API is implemented.

Historical producer snapshots must identify collection time and counter reset/entry timestamps. Missing metadata disables historical claims rather than creating false 24-hour evidence.

## Per-node metric graphs (report APPENDIX 2)

* `MONGODB_LOG_DIAG_METRICS_GRANULARITY`: `PT1M`, `PT5M` (default), `PT1H` or `P1D` for the 24h measurements window.
* `MONGODB_LOG_DIAG_METRICS_GRAPHS_ENABLED`: true/false (default true). Adds APPENDIX 2 to a node report only when that node's metrics file has data points; the graph is embedded in `report.md` as a base64 PNG (`data:image/png;base64,...`).
* `MONGODB_LOG_DIAG_METRICS_GRAPH_GROUPS`: JSON list of group names to graph; defaults to the 21 groups in `agent/metrics_catalog.py` (`DEFAULT_SELECTED_GROUPS`): the 18 original groups plus `NORMALIZED_SYSTEM_CPU`, `MAX_NORMALIZED_SYSTEM_CPU` and `DISK_QUEUE_DEPTH` (avg `DISK_QUEUE_DEPTH` + `MAX_DISK_QUEUE_DEPTH`).
* `MONGODB_LOG_DIAG_METRICS_GRAPH_CONFIG`: optional JSON object with the slowInvest `cluster.per_node.graph` shape: `{"group_of_metrics": {"GROUP": {"list_of_metrics": [...]}}, "metrics": ["GROUP", ...]}`. Entries are merged over the built-in groups.

Only measurements needed by the selected groups are requested (repeated `m` parameters, at most 10 per call). Names are validated against the documented Atlas `m` values; template aliases are mapped (`FTS_PROCESS_RESIDENT_MEMORY`→`FTS_MEMORY_RESIDENT`, `FTS_PROCESS_VIRTUAL_MEMORY`→`FTS_MEMORY_VIRTUAL`, `FTS_DISK_USAGE`→`FTS_DISK_UTILIZATION`) and names without an API equivalent are listed in the appendix instead of being requested. A batch rejected with HTTP 400 is retried per metric. Disk-partition metrics (`DISK_QUEUE_DEPTH`, `MAX_DISK_QUEUE_DEPTH`) are requested per partition from `/processes/{id}/disks/{partition}/measurements` (up to 4 partitions); `MAX_DISK_QUEUE_DEPTH` is not in the documented `m` list, so it is requested alone and, if rejected, read from a request without `m`. With several partitions each one gets its own line in the graph. Graphs are embedded in `report.md` as a base64 PNG (no separate image file); the embedded image is replaced by a placeholder before node reports are sent to the cluster-summary model.

### High resource usage investigation

When a node has metrics, its report gets a `## High resource usage investigation` section and the whole report is based on `extractionOccurence.json` (never `extractionshort.json`). If the input is too large, occurrences outside the high-resource episode windows are trimmed first (episode occurrences are always kept). Rules (`agent/resource_investigation.py`):

* A — CPU: normalized system CPU (sum of `SYSTEM_NORMALIZED_CPU_*` except GUEST) > 90%, or > 70% for more than 20 minutes.
* B — Memory: `SYSTEM_MEMORY_USED / (USED + AVAILABLE)` > 75%; context from connections (~1 MB each), open cursors, global lock queue, errors and slow operations in the window.
* C — Disk: `DISK_QUEUE_DEPTH` > 10 on at least 3 samples (per partition, from `/processes/{id}/disks/{partition}/measurements`).

Every rule is evaluated on the average series (`SYSTEM_NORMALIZED_CPU_*`, `DISK_QUEUE_DEPTH`). The max series (`MAX_SYSTEM_NORMALIZED_CPU_*`, `MAX_DISK_QUEUE_DEPTH`) are collected, graphed and quoted as context only.

Each triggered episode is correlated (±5 min) with slow-operation and error occurrences of the extract. The deterministic result is written as `resource-investigation.json` next to `report.md` and, if the model omits the section, rendered as a fallback.

* `MONGODB_LOG_DIAG_RESOURCE_INVESTIGATION_ENABLED`: true/false (default true). Also requests the extra CPU/memory/connection/cursor/queue measurements and the disk IOPS/latency measurements. The CPU and disk-queue groups are graphed even when this is off.
* `MONGODB_LOG_DIAG_RESOURCE_THRESHOLDS`: optional JSON overriding `cpu_peak_percent` (90), `cpu_sustained_percent` (70), `cpu_sustained_minutes` (20), `memory_percent` (75), `disk_queue_depth` (10), `disk_min_points` (3), `connection_memory_mb` (1), `correlation_margin_minutes` (5), `top_groups` (10).

