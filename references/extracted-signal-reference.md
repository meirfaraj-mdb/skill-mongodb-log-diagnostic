# Extracted signal reference

## AI-facing file description

`extractionOccurence.json` is the authoritative, machine-generated diagnostic handoff for MongoDB log analysis. It contains redacted and aggregated evidence extracted from each supported log line; it is not a copy of the raw log and must be treated as the only evidence source for the diagnostic agent. The file is designed to answer: what signals were observed, how often they occurred, where they occurred, when they occurred, and which bounded metrics support each finding. `extractionshort.json` has the same analytical sections but removes occurrence arrays for a smaller handoff.

The agent should use group-level fields for conclusions and occurrence fields for recurrence and time distribution. Missing fields mean the source record did not provide that evidence; the agent must preserve that uncertainty. Aggregated statistics describe observations, not root cause, and temporal correlation must not be presented as causation without additional validation.

For slow operations, `slow.operations` contains one bounded group per namespace/plan/query-hash combination. `slow.global_stats` summarizes every slow record included by the extractor, including records explicitly labeled `msg: "Slow query"` even when `durationMillis` is below the configured threshold. A change-stream `getMore` can therefore be a normal `COLLSCAN` and should be reported as a change-stream slow-query signal, grouped by namespace rather than treated as an automatic index defect. Use `change_stream_count`, `collscan_count`, `duration_ms`, and `cpuNanos` to describe the global workload before discussing individual groups.

The wrapper emits `extractionOccurence.json` with these top-level sections and also emits `extractionshort.json` with occurrence arrays removed:

- `metadata`: input files, parser version, thresholds, record counts, time range, and data-quality notes.
- `driverCompatibility`: bundled analysis of client-driver metadata and incompatibility messages, including compatible-driver versions and `incompatible_drivers`; each driver version includes distinct IPs, application names, and platform names.
- `summary`: severity, component, message-fingerprint, issue-category, and time-bucket counts.
- `slow`: aggregated slow-operation evidence under `operations` plus `global_stats` for total slow count, COLLSCAN count, change-stream count, duration statistics, and CPU statistics. Explicit MongoDB `msg: "Slow query"` records are included even when their duration is below the configured threshold.
- `slow_operations`: backward-compatible alias of `slow.operations`.
- `issue_candidates`: grouped candidates with category, severity, recurrence, first/last seen, affected components, and evidence metrics.
- `trends`: chronological buckets for errors, warnings, slow operations, and candidate categories.
- `quality`: parsed, skipped, malformed, and unsupported-record counts.

The extractor is intentionally conservative. It records a candidate when the log contains a recognizable signal; it does not establish root cause. The report agent must corroborate candidates across time and related signals.

## Candidate interpretation

- `fatal_assertion`: fatal/error severity or assertion, invariant, panic, crash, or corruption language.
- `availability_replication`: election, heartbeat, rollback, stepdown, sync source, replication lag, majority, or flow-control language.
- `slow_operation`: duration at or above the configured threshold.
- `inefficient_query`: collection scan, in-memory sort, high examined/returned ratio, or high examined counts.
- `storage_resource`: WiredTiger, checkpoint, cache, journal, disk, filesystem, memory, or resource pressure language.
- `connection_network`: handshake, authentication, TLS, timeout, connection failure, or connection lifecycle language.
- `sharding_topology`: balancer, chunk, migration, stale config, mongos, topology, or router language.
- `warning_pattern`: repeated warning or informational message fingerprint that may merit review.

When a field is absent, preserve that uncertainty. Do not fill missing values from raw logs.
## FTDC timeline rule

FTDC resource metrics and correlations are analyzed only for records whose UTC timestamps fall within the inclusive first-to-last timestamp range of the parsed log events. If the log has no usable timestamps, FTDC is not analyzed. Use `ftdc.timeline_status`, `ftdc.log_time_range`, `ftdc.ftdc_time_range`, `ftdc.filtered_outside_log_timeline`, and `ftdc.filtered_without_timestamp` to explain coverage.


## Compatibility and occurrence details
`driverCompatibility.distinct_compatible_drivers` lists every distinct compatible application-driver version found, grouped by driver, with `distinctIps`, `distinctAppNames`, and `distinctPlatforms` per version. `incompatible_drivers` exposes the same arrays per incompatible version. Error-group `occurrence_timestamps` are always included, grouped by UTC date and hour/minute with occurrence counts; slow-operation timestamps are always included and retain centisecond detail. Slow-operation occurrences include `keys_examined`, distinct top-level `app_names`, and statistics extracted after `planSummary`.


`driverCompatibility.count` is the number of parsed log records containing driver metadata; `driverCompatibility.driver_log_count` is the same explicit counter, while `driverCompatibility.incompatible_count` counts incompatible driver/version groups.

## Exact output contract for the diagnostic AI

### 1. Which file to use

Use `extractionOccurence.json` for the authoritative diagnostic analysis. It contains all aggregate sections plus `occurrence_timestamps` under error groups and slow-operation groups. Use `extractionshort.json` only when a compact handoff is needed; it has the same aggregate values and the same group fields, but every `occurrence_timestamps` key is recursively removed. Do not interpret the short file as a different analysis or as evidence that no events occurred. A missing occurrence array in the short file is intentional.

Both files are JSON documents. JSON object key order is not meaningful. Arrays such as groups, versions, timestamps, and samples are already sorted by the extractor where ordering matters.

### 2. Root object and section meanings

The root object normally contains:

- `schema_version`: extractor schema identifier.
- `metadata`: `parser_version`, `slow_threshold_ms`, `bucket_minutes`, `input_files`, and `time_range`.
- `driverCompatibility`: driver metadata and compatibility results.
- `quality`: `input_files`, `lines_seen`, `parsed_records`, `skipped_records`, `skip_reasons`, `skipped_ratio`, and notes.
- `summary`: severity counts, component counts, category counts, top message fingerprints, and repeated-error totals.
- `error_scan.groups`: every bounded error group, including single occurrences.
- `repeated_errors`: only error groups with `count >= 2`; this is a subset of `error_scan.groups`.
- `slow.operations`: bounded slow-operation groups.
- `slow.global_stats`: totals and statistics across all slow records accepted by the extractor.
- `slow_operations`: compatibility alias containing the same array as `slow.operations`.
- `issue_candidates`: category-level and repeated-error candidates; these are observations to investigate, not confirmed root causes.
- `trends`: time-bucket counters.
- `ftdc`: optional FTDC resource and temporal-correlation data.

If `ftdc.status` is `not_provided`, no FTDC path was supplied. If it is `no_records` or `error`, do not treat missing FTDC metrics as zero.

### 3. Date and time formats

All parsed log timestamps are normalized to UTC. Normal event and group timestamps use ISO 8601 UTC:

```text
YYYY-MM-DDTHH:MM:SS[.fraction]Z
```

Examples include `2026-09-01T10:06:07Z` and `2026-09-01T10:06:07.158Z`. Input offsets are converted to `Z`; do not compare local clock strings without converting them. The same format is used by `metadata.time_range.first/last`, group `first_seen/last_seen`, FTDC time ranges, and `trends[].bucket_start`.

Full slow-operation occurrence timestamps use compact UTC keys with centisecond precision:

```json
"occurrence_timestamps": {
  "20260901": [
    {"10:06:07.15": {"durationMillis": 111, "cpuNanos": 120350711}}
  ]
}
```

The date key is `YYYYMMDD`; the nested time key is `HH:MM:SS.cc`, where `cc` is hundredths of a second. Slow occurrence details are one-key objects so multiple events at the same time can be represented as separate list entries.

Error occurrence timestamps use minute buckets rather than centiseconds:

```json
"occurrence_timestamps": {
  "20260901": [
    {"10H06": {"occur": 4}}
  ]
}
```

The error time key is `HHHMM`: the hour, the literal separator `H`, and the minute. `occur` is the number of error events in that UTC minute. Do not confuse this compact error key with the exact slow-operation time key.

### 4. Slow-operation structure

Each item in `slow.operations` represents one group keyed by:

```text
(namespace, plan_summary, query_hash)
```

Important group fields are:

- `namespace`: namespace from `attr.ns`, when present.
- `plan_summary`: for example `COLLSCAN` or `IXSCAN`.
- `query_hash`: group query hash when present.
- `app_names`: distinct application names in the group.
- `count`: number of slow records in this group.
- `duration_ms`, `cpuNanos`, `keys_examined`, `docs_examined`, and `n_returned`: statistics objects when values were present.
- `collscan_count` and `sort_stage_count`: counts within the group.
- `examined_returned_ratio_max`: maximum examined-to-returned ratio using the larger of keys/docs examined.
- `first_seen` and `last_seen`: normalized UTC timestamps.
- `sample_query_shape`: redacted structural command shape; literal values are replaced with `?` and sensitive fields are omitted.
- `sample_message`: redacted message suffix when available.
- `occurrence_timestamps`: full-file occurrence evidence only.

An explicitly labeled MongoDB `msg: "Slow query"` record is included even if `durationMillis` is below `slow_threshold_ms`. Therefore a record with `durationMillis: 111` is still a slow record when MongoDB itself emitted `msg: "Slow query"`.

A change-stream `getMore` may have `plan_summary: "COLLSCAN"` as normal cursor behavior. Identify it using the change-stream marker in the originating command and `slow.global_stats.change_stream_count`; report it by namespace and do not automatically recommend an index solely because it is a COLLSCAN.

Slow occurrence details preserve available source metrics using normalized output names, including `durationMillis`, `workingMillis`, `cpuNanos`, `reslen`, `numYields`, `nBatches`, `cursorid`, `planCacheShapeHash`, `planCacheKey`, `queryFramework`, `docs_examined`, `keys_examined`, `n_returned`, `has_sort_stage`, and `namespace`.

### 5. Global slow statistics

`slow.global_stats` summarizes every accepted slow record, not only the first 50 displayed groups:

```json
"global_stats": {
  "count": 3,
  "collscan_count": 3,
  "change_stream_count": 1,
  "duration_ms": {"min": 111.0, "median": 1250.0, "max": 1500.0, "avg": 953.67, "total": 2861.0},
  "cpuNanos": {"min": 5000000.0, "median": 6000000.0, "max": 120350711.0, "avg": 43785237.0, "total": 131355711.0}
}
```

`count` is the number of accepted slow log records. `collscan_count` is the number of those records whose plan summary contains `COLLSCAN`. `change_stream_count` is the number recognized as change-stream operations. Statistic objects use `min`, `median`, `max`, and `avg`; `total` is included for global CPU/duration statistics and group CPU statistics when available. A `null` statistic means the source records did not provide that metric.

### 6. Full versus short occurrence behavior

The following interpretation is mandatory:

- A full-file error group has both aggregate fields such as `count`, `first_seen`, and `last_seen`, and `occurrence_timestamps`.
- A full-file slow group has both aggregate metric statistics and exact centisecond `occurrence_timestamps`.
- The short file retains all aggregate counts, statistics, groups, samples, query shapes, and global slow statistics.
- The short file removes only occurrence arrays; it must not be used to conclude that a group had zero occurrences.
- `sample_query_shape` is not an occurrence array and remains in both files.
- `slow_operations` and `slow.operations` are aliases, not separate populations; do not add their counts together.

### 7. Evidence and uncertainty rules

Use `count` fields from the relevant scope: a slow group count is not the global slow count, an error-group count is not the total error count, and `driverCompatibility.count` is the number of driver-bearing log records rather than the incompatible-group count. Prefer exact group and global statistics over raw-message wording. Never reconstruct omitted timestamps, query literals, IPs, or documents from the redacted samples. State “not present in the extracted data” when a field is absent.
