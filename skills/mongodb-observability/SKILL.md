# MongoDB Observability Collection Skill

Collect bounded, read-only per-node diagnostics: query statistics, index definitions and access stats, collection storage/latency snapshots, configured queryPlanner explains, monitoring measurements and advisor candidates. Optional profiler collection only reads existing profiler records.

Use `agent.observability_stage` or `scripts/prepare_observability.py`. See `OBSERVABILITY_USAGE.md` in the repository root for environment settings, artifact schemas and usage.

## Source rules

* `existing_bucket`: inventory all artifact types; never call Atlas, MongoDB or Ops Manager.
* Query stats: bucket, direct MongoDB or Atlas Query Shape Insights. Ops Manager query stats API is unsupported.
* Index definitions/stats, collection stats and explain plans: bucket or direct MongoDB. Node-local commands require direct node connections.
* Metrics and advisor: bucket, Atlas API or Ops Manager API. Unsupported version, endpoint or permission failures are reported as partial collection, not silently replaced.
* Atlas metrics request only the measurements of the configured graph groups (`agent/metrics_catalog.py`), validated against the documented `m` values and batched by 10. Node reports end with `## APPENDIX`: APPENDIX 1 = reported query hashes/redacted forms, APPENDIX 2 = all selected metric groups on one A4 page.

## Temporal and safety rules

* API lookbacks default to the latest 24 hours at collection time, not the partition/log date.
* `$queryStats` is cumulative per retained entry; `$indexStats` is cumulative since its own reset. Neither is inherently a rolling 24h series.
* Capture timestamped comparable daily snapshots for deltas; reject counter/reset discontinuities.
* Collection size/reusable space is a current snapshot; growth needs a prior comparable snapshot. Reusable storage is not proof that compaction is required.
* Hidden age requires verified transition metadata, not index-counter age. Never delete indexes automatically.
* Keep exact hash schemes distinct. Rank workload queries by total execution time, average latency and volume; profiler counts are sampled observations.
* Compare advisor candidates against existing keys/options and matching query plans. No index gap is established from a slow operation alone.
* Never log credentials, client IPs or raw query literals. Explain only explicitly supplied find requests with queryPlanner verbosity and timeouts. Do not enable profiler, execute write commands, install indexes or change server settings.
* Each kind's failure is independent; preserve successful kinds and return partial status. Collection caps and timeouts are configurable.
