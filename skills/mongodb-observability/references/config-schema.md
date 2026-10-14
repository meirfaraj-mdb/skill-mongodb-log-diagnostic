# Observability artifact and configuration contract

See repository-root `OBSERVABILITY_USAGE.md` for the complete environment schema and producer file layouts.

The loader accepts `MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED`, `DEPLOYMENT_TYPE`, `OBSERVABILITY_HOSTS`, `OBSERVABILITY_SOURCES`, `MONGODB_URI_TEMPLATE`, `MONGODB_TIMEOUT_MS`, `MONGODB_MAX_TIME_MS`, `OBSERVABILITY_MAX_ROWS`, `INDEX_STATS_ENABLED`, `INDEX_STATS_MAX_COLLECTIONS`, `QUERY_SHAPE_SOURCE`, `QUERY_SHAPE_WINDOW_HOURS`, `QUERY_STATS_MAX_SUMMARIES`, `ATLAS_PROCESS_IDS`, `QUERY_PLAN_REQUESTS`, `PROFILER_ENABLED`, `ATLAS_BASE_URL`, `OPS_MANAGER_BASE_URL`, `OPS_MANAGER_HOST_IDS`, `OPS_MANAGER_PUBLIC_KEY`, `OPS_MANAGER_PRIVATE_KEY`, and `OBSERVABILITY_API_MAX_PAGES`. Every abbreviated suffix in this list uses the same `MONGODB_LOG_DIAG_` prefix.

`OBSERVABILITY_SOURCES`, `ATLAS_PROCESS_IDS`, and `OPS_MANAGER_HOST_IDS` are JSON objects; `QUERY_PLAN_REQUESTS` is a JSON list. Hosts are comma-separated or a JSON list. Booleans use true/false or 1/0. The existing Atlas API secret accepts only atlas_public_key and atlas_private_key; no observability settings are added to that secret.

Source keys: query_stats, index_definitions, index_stats, coll_stats, query_plans, metrics, advisor, profiler. Bucket mode overrides all source selections. Live source selections are validated against each kind's capability matrix. No Ops Manager query statistics API is implemented.

Historical producer snapshots must identify collection time and counter reset/entry timestamps. Missing metadata disables historical claims rather than creating false 24-hour evidence.
