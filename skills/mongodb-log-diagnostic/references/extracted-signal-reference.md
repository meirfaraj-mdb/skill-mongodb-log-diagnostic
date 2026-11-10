# Extracted signal reference

Applies to parser version 1.7 (`metadata.parser_version`). Sections and fields marked 1.5, 1.6 or 1.7 are absent from older extractions; when analyzing an older file, report them as "not extracted by this parser version", not as zero.

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
- `issue_candidates`: grouped candidates with category, severity, recurrence, first/last seen, affected components, and evidence metrics. Since 1.7, `hot_write_target` candidates come first, then `concurrency_conflict`, so consumers that cap the list keep the contention evidence.
- `trends`: chronological buckets for errors, warnings, slow operations, write conflicts, and candidate categories.
- `concurrency` (parser 1.6): write-conflict totals and breakdowns, lock-wait and flow-control totals, transaction outcomes, conflict error codes, and `hot_write_targets` — single documents (equality targets) written repeatedly. See section 8.
- `timeline` (parser 1.6): uncapped per-minute counters for slow load, CPU, write-concern wait, write conflicts, lock wait, and transaction outcomes. See section 9.
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
- `concurrency_conflict` (1.6): a record with `writeConflicts > 0` or `temporarilyUnavailableErrors > 0`, a conflict error code (`WriteConflict`, `NoSuchTransaction`, `LockTimeout`, `LockBusy`, `TemporarilyUnavailable`, `TransactionTooOld`, `TransientTransactionError`, `PreparedTransactionInProgress`), or write-conflict, transient-transaction, lock-timeout, prepare-conflict, or transaction-abort language.
- `hot_write_target` (1.6): one entry per `concurrency.hot_write_targets` item with `count >= 50` and at least one of `share_of_namespace_writes >= 0.2`, `write_conflicts > 0`, or `max_per_minute >= 100`. It carries `target_id`, `filter_fields`, `write_conflicts`, `max_per_minute`, `peak_minute`, and `share_of_namespace_writes`. This is the signature of one document (counter or aggregate) updated by many concurrent writers.
- `repeated_error`: an error group with `count >= 2`.

When a field is absent, preserve that uncertainty. Do not fill missing values from raw logs.
## FTDC timeline rule

FTDC resource metrics and correlations are analyzed only for records whose UTC timestamps fall within the inclusive first-to-last timestamp range of the parsed log events. If the log has no usable timestamps, FTDC is not analyzed. Use `ftdc.timeline_status`, `ftdc.log_time_range`, `ftdc.ftdc_time_range`, `ftdc.filtered_outside_log_timeline`, and `ftdc.filtered_without_timestamp` to explain coverage.

`ftdc.concurrency` (1.6) holds the server-wide concurrency counters that the generic resource summary does not keep: `metrics.operation.writeConflicts`, `metrics.operation.temporarilyUnavailableErrors*`, `transactions.total{Started,Committed,Aborted,Prepared,...}`, `transactions.current{Active,Inactive,Open,Prepared}`, `flowControl.{timeAcquiringMicros,isLagged,isLaggedCount,targetRateLimit}`, `locks.<resource>.{timeAcquiringMicros,acquireWaitCount,deadlockCount}.<mode>`, and read/write tickets or execution queues (`out`, `available`, `totalTimeQueuedMicros`). Fields:

- `metrics_found`: matched FTDC metric names (at most 200).
- `peaks`: per metric `{max, at, kind}`. `kind: "delta"` means `max` is the largest increase between two consecutive FTDC samples (cumulative counters are differenced; the first sample and counter resets after a restart are skipped) and `at` is that sample's timestamp; `kind: "gauge"` means the largest sampled value.
- `occurrence_timestamps`: `YYYYMMDD` → `HHHMM` → `{metric: value}` where counters are summed sample deltas within the UTC minute and gauges are the minute maximum; full file only. At most 14 UTC days are kept.

FTDC `writeConflicts` counts every write conflict on the node, including those of fast operations that were never logged, so it is the authoritative magnitude when present; the log-side `concurrency` section is a lower bound limited to logged operations.


## Compatibility and occurrence details
`driverCompatibility.distinct_compatible_drivers` lists every distinct compatible application-driver version found, grouped by driver, with `distinctIps`, `distinctAppNames`, and `distinctPlatforms` per version. `incompatible_drivers` exposes the same arrays per incompatible version. Error-group `occurrence_timestamps` are always included, grouped by UTC date and hour/minute with occurrence counts; slow-operation timestamps are always included and retain centisecond detail. Slow-operation occurrences include `keys_examined` and the per-record metrics listed in section 4; distinct `app_names` and statistics extracted after `planSummary` are group fields. Slow occurrence detail is capped at 2,000 entries per group as a time-stratified sample since 1.7 (section 6); the uncapped minute series is `timeline`.


`driverCompatibility.count` is the number of parsed log records containing driver metadata; `driverCompatibility.driver_log_count` is the same explicit counter, while `driverCompatibility.incompatible_count` counts incompatible driver/version groups.

## Exact output contract for the diagnostic AI

### 1. Which file to use

Use `extractionOccurence.json` for the authoritative diagnostic analysis. It contains all aggregate sections plus `occurrence_timestamps` under error groups and slow-operation groups. Use `extractionshort.json` only when a compact handoff is needed; it has the same aggregate values and the same group fields, but every `occurrence_timestamps` key is recursively removed. Do not interpret the short file as a different analysis or as evidence that no events occurred. A missing occurrence array in the short file is intentional.

Both files are JSON documents. JSON object key order is not meaningful. Arrays such as groups, versions, timestamps, and samples are already sorted by the extractor where ordering matters.

### 2. Root object and section meanings

The root object normally contains:

- `schema_version`: extractor schema identifier.
- `metadata`: `parser_version`, `slow_threshold_ms`, `bucket_minutes`, `input_files`, and `time_range`. When a log is streamed on stdin (input `-`), `input_files` records the name passed with `--input-name` (for example the original `.gz` name) instead of `-`.
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
- `concurrency`: write conflicts, lock wait, flow control, transactions, and hot write targets (1.6).
- `timeline`: uncapped per-minute load and concurrency counters (1.6).
- `ftdc`: optional FTDC resource, concurrency, and temporal-correlation data.

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
    {"10:06:07.15": {"duration_ms": 111, "cpuNanos": 120350711, "docs_examined": 0, "keys_examined": 0}}
  ]
}
```

The date key is `YYYYMMDD`; the nested time key is `HH:MM:SS.cc`, where `cc` is hundredths of a second. Slow occurrence details are one-key objects so multiple events at the same time can be represented as separate list entries.

`timeline.occurrence_timestamps`, `concurrency.hot_write_targets[].occurrence_timestamps`, and `ftdc.concurrency.occurrence_timestamps` use the same `HHHMM` minute key as error groups, with a metric object instead of `occur` only.

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
- `sample_query_shape`: redacted structural command shape; literal values are replaced with `?` and sensitive fields are omitted. Since parser 1.4, top-level command options keep their logged values (validated short settings only): `$db`, `$readPreference`, `readConcern` (level), `writeConcern`, `batchSize`, `singleBatch`, `limit`, `skip`, `maxTimeMS`, `maxAwaitTimeMS`, `allowDiskUse`, `hint`, `cursor`, and cursor flags. A getMore adds `getMoreOptions` (its own `batchSize`/`maxTimeMS`). Use them for read-routing, read-concern, cursor-batching, and timeout analysis; they are settings, not user data.
- `sample_message`: redacted message suffix when available.
- `planCacheShapeHash`, `planCacheKey`, `queryFramework`, `opType` (1.5/1.6): present at the group level when every recorded occurrence has the same value; they are then removed from the occurrences. When values vary they stay per occurrence.
- `occurrences_recorded` and `occurrences_truncated` (1.6): present only when the group has more records than the 2,000-entry occurrence cap; `count` and all statistics still cover every record.
- `occurrence_sampling` (1.7): present with `occurrences_truncated`: `{method: "time_stratified", per_minute_cap, minute_thinning_rounds}`. The retained occurrences are at most `per_minute_cap` per UTC minute across the whole log day (`minute_thinning_rounds > 0`: every other minute was also dropped that many times), so every active period keeps samples.
- `writeConflicts` (1.6): `{min, median, max, avg, total}` over records that reported `writeConflicts > 0`, plus `write_conflict_records` (how many records had conflicts). Absent when no record of the group conflicted.
- `lockWaitMicros` (1.6): statistics including `total` over records with lock-acquisition wait (sum of `locks.*.timeAcquiringMicros`).
- `distinct_write_targets` and `top_write_targets` (1.6): for update/delete/findAndModify groups with an equality filter, the number of distinct target documents and the three most frequent `{target_id, count}`. One target holding most of `count` means the group is a single hot document, not a broad workload.
- `occurrence_timestamps`: full-file occurrence evidence only.

An explicitly labeled MongoDB `msg: "Slow query"` record is included even if `durationMillis` is below `slow_threshold_ms`. Therefore a record with `durationMillis: 111` is still a slow record when MongoDB itself emitted `msg: "Slow query"`.

A change-stream `getMore` may have `plan_summary: "COLLSCAN"` as normal cursor behavior. Identify it using the change-stream marker in the originating command and `slow.global_stats.change_stream_count`; report it by namespace and do not automatically recommend an index solely because it is a COLLSCAN.

Slow occurrence details keep only metrics present in the source record, using these output names:

- Timing and cost: `duration_ms`, `workingMillis`, `cpuNanos`, `numYields`, `reslen`, `nBatches`, `cursorid`, `storage` (storage-engine read bytes/time object), `waitForWriteConcernDurationMillis`, `totalOplogSlotDurationMicros`.
- Plan and volume: `docs_examined`, `keys_examined`, `n_returned`, `planCacheShapeHash`, `planCacheKey`, `queryFramework` (the last three only when they vary inside the group).
- Writes: `ninserted`, `keysInserted`, `nMatched`, `nModified`, `nUpserted`, `keysDeleted`, `ordered`.
- Concurrency (1.6; zero values are omitted): `writeConflicts`, `temporarilyUnavailableErrors`, `prepareReadConflicts`, `prepareConflictDurationMillis`, `lockWaitMicros`, `lockWaitCount`, `flowControlMicros`, `queueTimeMicros`, `opType` (when it varies), `terminationCause` (transaction records), and `target` — the pseudonymous id of the written document (see section 8).

Lossless trimming since parser 1.5: `namespace` is never repeated per occurrence (it is the group key); `has_sort_stage` appears only when `true` (absent means no sort stage); `durationMillis` appears only when it differs from `duration_ms`; `slow_message` appears only when it differs from the group `sample_message`. Absence of these keys in an occurrence is therefore not missing evidence.

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

Since 1.6, `global_stats.writeConflicts` (statistics with `total`) is present when any slow record reported write conflicts; its statistics cover only conflicting records.

`count` is the number of accepted slow log records. `collscan_count` is the number of those records whose plan summary contains `COLLSCAN`. `change_stream_count` is the number recognized as change-stream operations. Statistic objects use `min`, `median`, `max`, and `avg`; `total` is included for global CPU/duration statistics and group CPU statistics when available. A `null` statistic means the source records did not provide that metric.

### 6. Full versus short occurrence behavior

The following interpretation is mandatory:

- A full-file error group has both aggregate fields such as `count`, `first_seen`, and `last_seen`, and `occurrence_timestamps`.
- A full-file slow group has both aggregate metric statistics and exact centisecond `occurrence_timestamps`.
- The short file retains all aggregate counts, statistics, groups, samples, query shapes, and global slow statistics.
- The short file removes only occurrence arrays; it must not be used to conclude that a group had zero occurrences.
- `sample_query_shape` is not an occurrence array and remains in both files.
- `slow_operations` and `slow.operations` are aliases, not separate populations; do not add their counts together.
- `slow.operations` lists at most the 50 groups with the highest `count`; `slow.global_stats`, `timeline`, and `concurrency` cover every slow record, including groups outside the top 50.
- Each slow group keeps at most 2,000 occurrence entries (`MAX_OCCURRENCES_PER_GROUP`). Up to 1.6 these were the first 2,000 records in log order, so a late-day burst had no samples. Since 1.7 they are a time-stratified sample (`occurrence_sampling`): the per-minute cap halves each time the group exceeds the budget, so every minute with activity keeps up to `per_minute_cap` entries in log order. A truncated array is still a sample: never derive per-minute counts, a peak minute, or an end time from it — use `timeline` and the group `first_seen`/`last_seen`.
- The short file keeps `concurrency` and `timeline` aggregates (`peak_minutes`, totals, hot targets without their minute arrays) but removes their `occurrence_timestamps`.

### 7. Evidence and uncertainty rules

Use `count` fields from the relevant scope: a slow group count is not the global slow count, an error-group count is not the total error count, and `driverCompatibility.count` is the number of driver-bearing log records rather than the incompatible-group count. Prefer exact group and global statistics over raw-message wording. Never reconstruct omitted timestamps, query literals, IPs, or documents from the redacted samples. State “not present in the extracted data” when a field is absent.

### 7b. Operation names on WRITE records (parser 1.7)

A WRITE-component slow `update`/`remove` record logs the statement (`{q, u, multi, upsert, ...}`), not the command. Up to 1.6 its first key `q` was reported as the operation (`concurrency.write_conflicts.by_operation` showed `["q", n]`); since 1.7 the record `type` (`update`, `remove`) is used. Treat `q` in older extracts as `update`/`remove`.

### 8. Concurrency section (parser 1.6)

`concurrency` aggregates every parsed record that carries concurrency evidence (slow-query records, transaction records, and errors), independently of the 50-group and 2,000-occurrence limits.

```json
"concurrency": {
  "write_conflicts": {"total": 10800.0, "records_with_write_conflicts": 3600,
    "per_record": {"min": 3.0, "median": 3.0, "max": 3.0, "avg": 3.0},
    "first_seen": "2026-03-10T15:07:00.2Z", "last_seen": "2026-03-10T15:12:59.99Z",
    "by_namespace": [["appdb.items", 10800.0]], "by_operation": [["findAndModify", 10800.0]], "by_app": [["app-service-1", 10800.0]]},
  "temporarily_unavailable_errors_total": 2.0, "prepare_read_conflicts_total": 0.0,
  "lock_wait": {"micros_total": 14400000.0, "records": 3600},
  "flow_control": {"micros_total": 0.0, "records": 0}, "queue_time_micros_total": 0.0,
  "conflict_errors": {"WriteConflict": 1},
  "transactions": {"records": 40, "termination_causes": {"aborted": 30, "committed": 10}, "abort_causes": [["WriteConflict", 30]],
    "prepared": 0, "duration_ms": {...}, "timeActiveMicros": {...}, "timeInactiveMicros": {...}, "first_seen": "...", "last_seen": "..."},
  "write_target_tracking": {"logged_write_records": 4401, "tracked_targets": 402, "max_tracked_targets": 10000, "pruned_targets": 0, "scope": "..."},
  "hot_write_targets": [{"namespace": "appdb.items", "target_id": "0123456789abcdef", "filter_fields": ["id"], "count": 3600,
    "share_of_namespace_writes": 0.8182, "write_conflicts": 10800.0, "records_with_write_conflicts": 3600,
    "write_kinds": {"findAndModify": 3600}, "app_names": ["app-service-1"], "query_hashes": ["QH000001"], "plan_summaries": ["IXSCAN { id: 1 }"],
    "duration_ms": {"avg": 250.0, "max": 250.0, "total": 900000.0}, "cpuNanos": {"avg": 9000000.0, "max": 9000000.0, "total": 32400000000.0},
    "first_seen": "...", "last_seen": "...", "peak_minute": "20260310 15H07", "max_per_minute": 600,
    "count_is_lower_bound": false, "minutes_partial": false,
    "occurrence_timestamps": {"20260310": [{"15H07": {"occur": 600, "writeConflicts": 1800.0}}]}}]
}
```

Field rules:

- `write_conflicts.total` is the sum of the per-record `writeConflicts` counter (internal WiredTiger retries of the same operation). It is not the number of failed operations: a record with `writeConflicts: 3` retried three times and then succeeded. `records_with_write_conflicts` is the number of affected operations. The `by_*` lists are sorted by conflict total (top 20).
- `lock_wait` sums `timeAcquiringMicros` over all lock resources and modes; `flow_control` sums `flowControl.timeAcquiringMicros`; `queue_time_micros_total` sums admission/execution queue time. They are wait time, not CPU.
- `conflict_errors` counts error records by conflict error code name. `transactions` summarizes transaction-completion records (`terminationCause`, `abortCause`, `wasPrepared`, `timeActiveMicros`, `timeInactiveMicros`). A high `aborted` share with `WriteConflict` abort causes indicates transactions competing for the same documents.
- **Write targets.** For update, delete, and findAndModify records whose filter contains top-level equality predicates, the extractor computes `target_id`, a 16-hex-character keyed blake2b hash of the namespace, the equality field names (sorted, at most 5), and their values (extended-JSON wrappers such as `$oid` are normalized; a number and the same digits as a string hash differently). Literal values are never written to the output. The same document filter always yields the same id within one run and across runs that use the same key (`MONGODB_LOG_DIAG_TARGET_KEY`, default fixed), so ids can be compared between runs. `filter_fields` lists the hashed field names; it is a statement about the filter, not proof of a unique key. If `filter_fields` is not unique (for example a status field), one `target_id` may represent many documents — state that limitation.
- `hot_write_targets` lists the top 20 targets by `count`. `share_of_namespace_writes` is the target's share of logged writes to its namespace. `max_per_minute` and `peak_minute` (earliest minute on ties) come from the per-minute series; `occurrence_timestamps` maps each UTC minute to `{occur, writeConflicts}`.
- Coverage: `write_target_tracking.scope` states that only logged writes (slow, conflicting, or transaction records) are counted. Fast writes below the slow threshold are invisible, so `count` is a lower bound of the real write rate to that document. `count_is_lower_bound: true` means the target first appeared after the 10,000-target table had been pruned; `minutes_partial: true` means its minute series hit the 1,440-minute per-target or 200,000-cell global budget, so `max_per_minute` and `peak_minute` cover only the retained minutes (totals are still exact).

Interpretation rule: one `target_id` with a large `count`, a high `share_of_namespace_writes`, `write_conflicts > 0`, and the same `query_hashes` indicates a hot document (a counter or aggregate updated by many concurrent writers). Concurrent writes to the same document serialize: each retry after a write conflict burns CPU and holds a write ticket, so a hot document can raise node CPU and latency for unrelated operations. Report it as the leading hypothesis when its minute series aligns with the incident window in `timeline`, and confirm with FTDC `metrics.operation.writeConflicts` when available.

### 9. Timeline section (parser 1.6)

`timeline` is a per-UTC-minute series built from every parsed record. It is not capped per group, so it is the source for "when did it start, peak, and stop":

```json
"timeline": {
  "bucket": "minute", "minutes": 64, "dropped_records": 0,
  "peak_minutes": {"slow": [{"minute": "20260310 15H09", "slow": 640}], "slowCpuMs": [...], "writeConflicts": [...], "lockWaitMs": [...], "txnAborted": [...]},
  "occurrence_timestamps": {"20260310": [{"15H09": {"slow": 640, "slowCpuMs": 5400.0, "slowDurationMs": 156000.0, "writeConflicts": 1800.0,
    "conflictRecords": 600, "lockWaitMs": 2400.0, "txnAborted": 30, "txnCommitted": 10}}]}
}
```

Per-minute keys (zero values omitted): `slow` (slow records), `slowCpuMs`, `slowDurationMs`, `writeConcernWaitMs`, `writeConflicts`, `conflictRecords`, `lockWaitMs`, `flowControlMs`, `queueMs`, `conflictErrors`, `txnCommitted`, `txnAborted`, `temporarilyUnavailable`. `peak_minutes` lists the top 10 minutes per metric, each with the minute's `slow` count for context. At most 20,160 minutes (14 days) are kept; records in later new minutes are counted in `dropped_records`. `slowCpuMs` covers logged slow operations only; it is not node CPU utilization — use FTDC or Atlas metrics for that.

Sum of `slow` across all minutes equals `slow.global_stats.count` for timestamped records; use this as a consistency check.
