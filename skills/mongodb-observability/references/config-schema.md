# Observability artifact and configuration contract

See repository-root `OBSERVABILITY_USAGE.md` for the complete environment schema and producer file layouts.

The loader accepts `MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED`, `DEPLOYMENT_TYPE`, `OBSERVABILITY_HOSTS`, `OBSERVABILITY_SOURCES`, `MONGODB_URI_TEMPLATE`, `MONGODB_TIMEOUT_MS`, `MONGODB_MAX_TIME_MS`, `OBSERVABILITY_MAX_ROWS`, `INDEX_STATS_ENABLED`, `INDEX_STATS_MAX_COLLECTIONS`, `QUERY_SHAPE_SOURCE`, `QUERY_SHAPE_WINDOW_HOURS`, `QUERY_STATS_MAX_SUMMARIES`, `ATLAS_PROCESS_IDS`, `QUERY_PLAN_REQUESTS`, `PROFILER_ENABLED`, `ATLAS_BASE_URL`, `OPS_MANAGER_BASE_URL`, `OPS_MANAGER_HOST_IDS`, `OPS_MANAGER_PUBLIC_KEY`, `OPS_MANAGER_PRIVATE_KEY`, and `OBSERVABILITY_API_MAX_PAGES`. Every abbreviated suffix in this list uses the same `MONGODB_LOG_DIAG_` prefix.

`OBSERVABILITY_SOURCES`, `ATLAS_PROCESS_IDS`, and `OPS_MANAGER_HOST_IDS` are JSON objects; `QUERY_PLAN_REQUESTS` is a JSON list. Hosts are comma-separated or a JSON list. Booleans use true/false or 1/0. The existing Atlas API secret accepts only atlas_public_key and atlas_private_key; no observability settings are added to that secret.

Source keys: query_stats, index_definitions, index_stats, coll_stats, query_plans, metrics, advisor, profiler. Bucket mode overrides all source selections. Live source selections are validated against each kind's capability matrix. No Ops Manager query statistics API is implemented.

Historical producer snapshots must identify collection time and counter reset/entry timestamps. Missing metadata disables historical claims rather than creating false 24-hour evidence.

## Per-node metric graphs (report APPENDIX 1)

* `MONGODB_LOG_DIAG_METRICS_GRANULARITY`: `PT1M`, `PT5M` (default), `PT1H` or `P1D` for the 24h measurements window.
* `MONGODB_LOG_DIAG_METRICS_GRAPHS_ENABLED`: true/false (default true). Adds APPENDIX 1 (before APPENDIX 2, Performance Advisor, and APPENDIX 3, the reported query forms) to a node report only when that node's metrics file has data points; the graph is embedded in `report.md` as a base64 PNG (`data:image/png;base64,...`).
* `MONGODB_LOG_DIAG_METRICS_GRAPH_GROUPS`: JSON list of group names to graph; defaults to the 21 groups in `agent/metrics_catalog.py` (`DEFAULT_SELECTED_GROUPS`): the 18 original groups plus `NORMALIZED_SYSTEM_CPU`, `MAX_NORMALIZED_SYSTEM_CPU` and `DISK_QUEUE_DEPTH` (avg `DISK_QUEUE_DEPTH` + `MAX_DISK_QUEUE_DEPTH`).
* `MONGODB_LOG_DIAG_METRICS_GRAPH_CONFIG`: optional JSON object with the slowInvest `cluster.per_node.graph` shape: `{"group_of_metrics": {"GROUP": {"list_of_metrics": [...]}}, "metrics": ["GROUP", ...]}`. Entries are merged over the built-in groups.

Only measurements needed by the selected groups are requested (repeated `m` parameters, at most 10 per call). Names are validated against the documented Atlas `m` values; template aliases are mapped (`FTS_PROCESS_RESIDENT_MEMORY`→`FTS_MEMORY_RESIDENT`, `FTS_PROCESS_VIRTUAL_MEMORY`→`FTS_MEMORY_VIRTUAL`, `FTS_DISK_USAGE`→`FTS_DISK_UTILIZATION`) and names without an API equivalent are listed in the appendix instead of being requested. A batch rejected with HTTP 400 is retried per metric. Disk-partition metrics (`DISK_QUEUE_DEPTH`, `MAX_DISK_QUEUE_DEPTH`) are requested per partition from `/processes/{id}/disks/{partition}/measurements` (up to 4 partitions); `MAX_DISK_QUEUE_DEPTH` is not in the documented `m` list, so it is requested alone and, if rejected, read from a request without `m`. With several partitions each one gets its own line in the graph. Graphs are embedded in `report.md` as a base64 PNG (no separate image file); the embedded image is replaced by a placeholder before node reports are sent to the cluster-summary model.

## Performance Advisor (report APPENDIX 2)

* `MONGODB_LOG_DIAG_ADVISOR_APPENDIX_ENABLED`: true/false (default true). Adds APPENDIX 2 between the metrics graphs and the query forms, only when the node's `advisor/advisor.json` contains at least one suggested index, advisor query shape or slow namespace outside admin/config/local and `__mdb_internal*`. Rendered deterministically by `agent/advisor_appendix.py`.
* Accepted formats: the collector envelope (`{"source": "atlas_api_advisor"|"ops_manager_advisor", "window", "data": [API pages]}`), a bare API page (`{"suggestedIndexes": [...], "shapes": [...]}`), index keys as `index: [{field: 1}, ...]` or `indexes: [{key: {...}}]`, and Ops Manager `slow_namespaces`.
* Content: suggested index keys (field names and directions only), number of shapes impacted, weight, whether the same key already exists in `indexDefinitions/index-definitions.json` (when supplied); advisor query shapes with executions, average ms, max docs examined/returned and inefficiency score. Example predicates and raw operations are never printed.

## Report input size

* `MONGODB_LOG_DIAG_REPORT_MAX_INPUT_CHARS`: unset by default. Per node: 80,000 characters, or 200,000 when the node has metrics and the resource investigation runs (`MONGODB_LOG_DIAG_RESOURCE_INVESTIGATION_ENABLED`, default true), so a busy node keeps every occurrence inside its high-resource episodes and hot-document windows. Lower it for models with a small context window. An explicit positive value applies to every node. Larger inputs are reduced (occurrences outside resource episodes, baselines, raw stats, then a bounded per-document reduction), never truncated mid-JSON.

## Cluster summary: new or sharply increasing issues

When a node has BOTH baselines (n-1 and n-8 `extractionshort.json`), `agent/trend_alerts.py` compares the day with each baseline and the cluster summary gets `## New or sharply increasing issues (vs n-1 and n-8)` right after the executive summary (the table is inserted deterministically; the model only comments on it). Listed when bad and either new (absent from both baselines) or at least +`MONGODB_LOG_DIAG_TREND_INCREASE_PCT` % (default 200, i.e. 3×) above n-1 or n-8 with at least `MONGODB_LOG_DIAG_TREND_MIN_ABS_INCREASE` (default 5) more occurrences:

* slow-query groups that are COLLSCAN (change streams excluded), poorly indexed (docs examined/returned ≥ `MONGODB_LOG_DIAG_TREND_RATIO_THRESHOLD`, default 100) or use an in-memory sort; also a 3× median-latency increase for such a query;
* error groups (fingerprint/category), with a redacted message template;
* totals: slow operations, COLLSCAN operations, error events and groups, F/E/W severities, slow CPU and duration, incompatible drivers, plus new incompatible driver versions.

System namespaces and platform-managed components are skipped. Nodes missing a baseline are listed as not compared. The full result is stored in `cluster/reports/trend-alerts.json`.

### High resource usage investigation

When a node has metrics, its report gets a `## High resource usage investigation` section and the whole report is based on `extractionOccurence.json` (never `extractionshort.json`). If the input is too large, occurrences outside the high-resource episode windows are trimmed first (episode occurrences are always kept). Rules (`agent/resource_investigation.py`):

* A — CPU: normalized system CPU (sum of `SYSTEM_NORMALIZED_CPU_*` except GUEST) > 90%, or > 70% for more than 20 minutes.
* B — Memory: `SYSTEM_MEMORY_USED / (USED + AVAILABLE)` > 75%; context from connections (~1 MB each), open cursors, global lock queue, errors and slow operations in the window.
* C — Disk: `DISK_QUEUE_DEPTH` > 10 on at least 3 samples (per partition, from `/processes/{id}/disks/{partition}/measurements`).

Every rule is evaluated on the average series (`SYSTEM_NORMALIZED_CPU_*`, `DISK_QUEUE_DEPTH`). The max series (`MAX_SYSTEM_NORMALIZED_CPU_*`, `MAX_DISK_QUEUE_DEPTH`) are collected, graphed and quoted as context only.

Each triggered episode is correlated (±5 min) with slow-operation and error occurrences of the extract. The deterministic result is written as `resource-investigation.json` next to `report.md` and, if the model omits the section, rendered as a fallback.

* `MONGODB_LOG_DIAG_CONCURRENCY_FINDINGS_ENABLED`: true/false (default true). Computes the `concurrency_findings` document from the FULL `extractionOccurence.json` (parser >= 1.6: uncapped minute timeline, hot write targets) before any size reduction: incident windows, hot documents and a verdict per window (`hot_document_contention_likely`, `hot_document_possible`, `no_contention_evidence`). The report gets `## Write contention (hot document)` (P1) when contention is detected (deterministic fallback when the model omits it); every resource episode gets a `contention` entry. Stored as `concurrency-findings.json` next to `report.md`. Thresholds: `MONGODB_LOG_DIAG_CONCURRENCY_THRESHOLDS` (JSON, keys of `agent/concurrency_findings.DEFAULT_THRESHOLDS`).
* `MONGODB_LOG_DIAG_CONCURRENCY_GRAPHS_ENABLED`: true/false (default true). Embeds `### Write contention timeline (log)` as a base64 PNG inside the write contention section (or the high resource usage section when no contention is detected but load bursts exist): whole-day slow operations/min and write conflicts/min with incident windows shaded, plus one zoom panel per window with writes/min to the hot document. Needs matplotlib; without it a one-line note replaces the graph. Works without Atlas metrics.
* `MONGODB_LOG_DIAG_RESOURCE_INVESTIGATION_ENABLED`: true/false (default true). Also requests the extra CPU/memory/connection/cursor/queue measurements and the disk IOPS/latency measurements. The CPU and disk-queue groups are graphed even when this is off.
* `MONGODB_LOG_DIAG_RESOURCE_THRESHOLDS`: optional JSON overriding `cpu_peak_percent` (90), `cpu_sustained_percent` (70), `cpu_sustained_minutes` (20), `memory_percent` (75), `disk_queue_depth` (10), `disk_min_points` (3), `connection_memory_mb` (1), `correlation_margin_minutes` (5), `top_groups` (10).

