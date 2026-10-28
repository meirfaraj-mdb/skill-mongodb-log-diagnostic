# Observability artifact and configuration contract

See repository-root `OBSERVABILITY_USAGE.md` for the complete environment schema and producer file layouts.

The loader accepts `MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED`, `DEPLOYMENT_TYPE`, `OBSERVABILITY_HOSTS`, `OBSERVABILITY_SOURCES`, `MONGODB_URI_TEMPLATE`, `MONGODB_TIMEOUT_MS`, `MONGODB_MAX_TIME_MS`, `OBSERVABILITY_MAX_ROWS`, `INDEX_STATS_ENABLED`, `INDEX_STATS_MAX_COLLECTIONS`, `QUERY_SHAPE_SOURCE`, `QUERY_SHAPE_WINDOW_HOURS`, `QUERY_STATS_MAX_SUMMARIES`, `ATLAS_PROCESS_IDS`, `QUERY_PLAN_REQUESTS`, `PROFILER_ENABLED`, `ATLAS_BASE_URL`, `OPS_MANAGER_BASE_URL`, `OPS_MANAGER_HOST_IDS`, `OPS_MANAGER_PUBLIC_KEY`, `OPS_MANAGER_PRIVATE_KEY`, and `OBSERVABILITY_API_MAX_PAGES`. Every abbreviated suffix in this list uses the same `MONGODB_LOG_DIAG_` prefix.

`OBSERVABILITY_SOURCES`, `ATLAS_PROCESS_IDS`, and `OPS_MANAGER_HOST_IDS` are JSON objects; `QUERY_PLAN_REQUESTS` is a JSON list. Hosts are comma-separated or a JSON list. Booleans use true/false or 1/0. The existing Atlas API secret accepts only atlas_public_key and atlas_private_key; no observability settings are added to that secret.

Source keys: query_stats, index_definitions, index_stats, coll_stats, query_plans, metrics, advisor, profiler. Bucket mode overrides all source selections. Live source selections are validated against each kind's capability matrix. No Ops Manager query statistics API is implemented.

Historical producer snapshots must identify collection time and counter reset/entry timestamps. Missing metadata disables historical claims rather than creating false 24-hour evidence.

## Per-node metric graphs (report APPENDIX 2)

* `MONGODB_LOG_DIAG_METRICS_GRANULARITY`: `PT1M`, `PT5M` (default), `PT1H` or `P1D` for the 24h measurements window.
* `MONGODB_LOG_DIAG_METRICS_GRAPHS_ENABLED`: true/false (default true). Adds APPENDIX 2 to every node report.
* `MONGODB_LOG_DIAG_METRICS_GRAPH_GROUPS`: JSON list of group names to graph; defaults to the 18 groups in `agent/metrics_catalog.py` (`DEFAULT_SELECTED_GROUPS`).
* `MONGODB_LOG_DIAG_METRICS_GRAPH_CONFIG`: optional JSON object with the slowInvest `cluster.per_node.graph` shape: `{"group_of_metrics": {"GROUP": {"list_of_metrics": [...]}}, "metrics": ["GROUP", ...]}`. Entries are merged over the built-in groups.

Only measurements needed by the selected groups are requested (repeated `m` parameters, at most 10 per call). Names are validated against the documented Atlas `m` values; template aliases are mapped (`FTS_PROCESS_RESIDENT_MEMORY`→`FTS_MEMORY_RESIDENT`, `FTS_PROCESS_VIRTUAL_MEMORY`→`FTS_MEMORY_VIRTUAL`, `FTS_DISK_USAGE`→`FTS_DISK_UTILIZATION`) and names without an API equivalent are listed in the appendix instead of being requested. A batch rejected with HTTP 400 is retried per metric. Graphs are stored next to `report.md` as `metrics-appendix.png` and `metrics-appendix.svg`.

