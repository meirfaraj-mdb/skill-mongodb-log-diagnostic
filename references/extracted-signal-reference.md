# Extracted signal reference

The wrapper emits `extraction.json` with these top-level sections:

- `metadata`: input files, parser version, thresholds, record counts, time range, and data-quality notes.
- `driverCompatibility`: bundled analysis of client-driver metadata and incompatibility messages, including the `incompatible_drivers` list used by the diagnostic report.
- `summary`: severity, component, message-fingerprint, issue-category, and time-bucket counts.
- `slow_operations`: aggregated slow-operation evidence including namespace, operation type, plan summary, duration statistics, examined/returned statistics, sort-stage counts, query shape/hash when available, and redacted command-shape hints.
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
