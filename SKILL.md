---
name: mongodb-log-diagnostic
description: Extracts actionable signals from MongoDB JSON or legacy plaintext logs and optional FTDC diagnostic data through bundled parsers, then guides an agent to present prioritized findings, resource-contention correlations, likely causes, possible fixes, validation steps, trends, and next actions. Use for MongoDB log troubleshooting, performance investigations, incident reviews, and customer health checks when the agent must not analyze raw logs directly.
---

# MongoDB Log Diagnostic

Use this skill whenever a MongoDB log is the source of a troubleshooting or diagnostic request.

## Operating contract

Do not inspect, search, or reason over the raw log or FTDC bytes directly. First run the bundled extractor and use its structured output as the sole source for analysis. Treat source files as opaque inputs owned by the extractor. The skill directory must keep these files separate: `SKILL.md`, `scripts/extract_mongodb_log.py`, `scripts/ftdc_decoder.py`, and `scripts/test_extract_mongodb_log.sh`; never execute `SKILL.md` with Bash.

## File boundaries

Canvas displays this `SKILL.md` file as the informational skill document. The executable validator is a separate bundled file at `scripts/test_extract_mongodb_log.sh`; it is not Markdown and must not be saved with a `.md` extension. Its first line is `#!/usr/bin/env bash`. The Python extractor is another separate file at `scripts/extract_mongodb_log.py`; its first line is `#!/usr/bin/env python3`. The standalone FTDC decoder is at `scripts/ftdc_decoder.py`; its first line is `#!/usr/bin/env python3`.

`SKILL.md` is agent guidance and must be read as Markdown, never executed by Bash. The bundled executable files are separate: `scripts/extract_mongodb_log.py` is the extractor, `scripts/ftdc_decoder.py` is the FTDC decoder, and `scripts/test_extract_mongodb_log.sh` is the Bash validator. Preserve these paths and contents when copying the skill; do not use `SKILL.md` as a substitute for any script. Before running the validator, confirm all four file types: `head -n 1 scripts/test_extract_mongodb_log.sh` must return `#!/usr/bin/env bash`; `head -n 1 scripts/extract_mongodb_log.py` must return `#!/usr/bin/env python3`; `head -n 1 scripts/ftdc_decoder.py` must return `#!/usr/bin/env python3`; and `head -n 1 SKILL.md` must return `---`. If the `.sh`, `.py`, or decoder file starts with `---`, `name:`, or `description:`, `SKILL.md` was copied over it and the local checkout must be restored.

To restore a flat bundle from the repository root, use `unzip -o ~/Downloads/mongodb-log-diagnostic-files.zip`; do not rename or copy `SKILL.md` into `scripts/`. On macOS Bash 3.2, use the bundled validator as-is; it does not expand an empty FTDC array.

The extractor supports:

- MongoDB structured JSON logs, one document per line.
- MongoDB legacy plaintext logs.
- Gzip-compressed inputs when the filename ends in `.gz`.
- Multiple log files in one run.
- Optional FTDC files or a `diagnostic.data` directory via repeatable `--ftdc /path` flags; the extractor accepts JSON/JSONL exports and delegates framed BSON FTDC block, zlib, BSON attribute, and delta decoding to `scripts/ftdc_decoder.py` without requiring a third-party BSON package.

The extractor uses bundled analyzers only. The `driverCompatibility` module reads structured connection metadata, applies Hatchet-derived application-driver filtering and major/minor compatibility checks, excludes internal connection drivers, and adds `driverCompatibility.incompatible_drivers` to `extraction.json` with per-driver-version IP lists. Driver names stop at the first `|`; wrapper labels such as Mongoose are excluded as separate drivers.

## Workflow

1. Create a temporary working directory outside the input directory.
2. Run the wrapper from the skill root, or use absolute paths; do not prepend `scripts/` twice when already inside the `scripts` directory:

   ```bash
   python3 scripts/extract_mongodb_log.py \
     --output /tmp/mongodb-log-extract \
     --slow-ms 1000 \
     --ftdc /path/to/diagnostic.data \
     /path/to/mongod.log /path/to/other.log.gz
   ```

   Repeat `--ftdc /path/to/another.ftdc` for multiple FTDC paths. The validator accepts optional `FTDC_DIR=/path/to/diagnostic.data` or `DIAG_DIR=/path/to/diagnostic.data` environment variables. Source FTDC debug output is disabled by default; set `DEBUG_FTDC_JSON=1` to print source JSON or decoded source records to stderr. The generated `extraction.json` is never used as source debug output. When neither FTDC variable is set, the validator automatically uses the first existing `diagnostic.data` or `diag` directory under the skill root or inputs directory, if available. If the validator begins with `---`, stop: the wrong file was copied into `scripts/test_extract_mongodb_log.sh`. From any working directory, invoke the validator by its absolute path or use the script-directory-resolved bundled validator: `/usr/bin/env bash /path/to/mongodb-log-diagnostic/scripts/test_extract_mongodb_log.sh`. Set `DIAGNOSTIC_DUMP=1` in the Bash invocation to create `./tmp/run-<timestamp>-<pid>/` (or `TEMP_DIR=/path`) for validator/extractor timing and validation logs; without that switch, no temporary diagnostic files are created.

3. Read `/tmp/mongodb-log-extract/extraction.json`. If the extractor reports `ftdc.status` as `no_records` or `error`, report that FTDC could not be interpreted instead of treating missing metrics as zero. Read `handoff.md` only for a quick orientation; `extraction.json` is authoritative. When FTDC is provided, use the `ftdc` section for resource metrics, contention windows, and temporal correlations with `find` issues.
4. Validate extraction quality before diagnosing:
   - Confirm input count, parsed line count, skipped-line count, and time range.
   - Check `driverCompatibility.status` and list every `incompatible_drivers` finding, including the driver name before any `|`, version, distinct IPs, reason, time range, and evidence.
   - When FTDC is supplied, check `ftdc.status`, per-file status, record count, resource sample count, and FTDC time range.
   - Use `ftdc.resource_metrics` for summarized CPU, memory, WiredTiger/cache, disk/I/O, connections, tickets, queues, flow-control, lock, eviction, and latency signals.
   - Use `ftdc.find_correlations` to identify FTDC samples near slow or inefficient `find` events. Treat these as temporal overlap only; do not claim resource contention caused the query issue without workload and host-metric validation.
   - If parsing is empty or the skipped-line ratio is high, report limited confidence and do not invent conclusions.
5. Produce the diagnostic report from extracted evidence only.
6. Distinguish observations from hypotheses. Tie every issue to counts, timestamps, component, namespace, plan summary, duration, or another field in the handoff.
7. Prioritize issues by impact, recurrence, confidence, and operational risk. Avoid treating every warning as an incident.

## Agent responsibility

Act as a diagnostic analyst and advisor, not merely an extractor. Present the findings in a form a MongoDB operator can act on:

- Explain what was observed, where it occurred, when it occurred, and how often it occurred.
- Separate confirmed observations from likely causes and unresolved hypotheses.
- For every material finding, provide one or more possible fixes or mitigations. Label them as possibilities, not guaranteed solutions.
- Prefer the least disruptive remediation first and distinguish immediate containment from durable remediation.
- Include a concrete validation step for every proposed fix, such as `explain()`, replica-set health checks, Atlas metrics, workload comparison, staging tests, or post-change log verification.
- Do not execute, simulate, or claim execution of configuration changes, index creation, failover, certificate rotation, or other remediation.
- If the extracted evidence is insufficient to recommend a safe fix, say what additional evidence is needed.

Use this finding shape:

```text
[P1] Inefficient query pattern — app.orders
Evidence: 37 occurrences; median duration 2.1s; COLLSCAN; 10,000 documents examined for 2 returned; observed 10:00–10:45 UTC.
Likely cause: The query shape may lack a suitable index or may be using an incomplete index.
Possible fixes: Test a compound index matching the equality, sort, and range predicates; review the query shape and projection; consider Atlas Search only when the workload is search-oriented.
Validation: Run representative `explain("executionStats")` in staging, compare examined/returned and latency, then monitor the same query shape in production.
Confidence: High
```

## FTDC correlation guidance

When diagnostic data is available, first restrict FTDC records to the inclusive UTC time range covered by the parsed log events. Do not summarize or correlate FTDC samples outside that range, and do not use FTDC when the log has no usable timestamps; report the timeline status and filtered counts instead. Within the matched timeline, correlate samples with `find` events using the configured `--ftdc-window-minutes` window (default five minutes). Prioritize overlaps involving high or changing CPU, memory pressure, WiredTiger cache/eviction, disk or I/O latency, exhausted tickets, queues, flow control, locks, page faults, or connection pressure. Report the matching query namespace, duration, plan summary, and query hash when available, then label the result as an observation requiring validation rather than a root-cause conclusion.

## Diagnostic focus

Review the extracted candidates in this order:

1. Fatal conditions, crashes,invariant/assertion failures, corruption, rollback, or authentication failures.
2. Replication and availability signals: elections, heartbeat failures, sync-source changes, replication lag, rollback, stepdown, or majority-commit pressure.
3. Slow and inefficient operations: `COLLSCAN`, high keys/docs examined, poor examined-to-returned ratio, in-memory sort, high duration, queue time, or excessive yields.
4. Storage and resource pressure: WiredTiger cache/dirty data, checkpoints, journal, disk or filesystem errors, flow control, and repeated resource warnings.
5. Connection and network behavior: connection churn, failed handshakes, TLS/auth failures, timeouts, and client concentration.
6. Sharding and topology: balancer or migration failures, stale config, chunk movement, router errors, and topology instability.
7. Repeated warnings or messages that form a time-correlated pattern.

Correlate signals by time bucket, component, namespace, connection context, query shape/hash, and message fingerprint. A single log event is evidence of occurrence, not proof of root cause.

## Required report structure

Use this structure unless the user requests another format:

# MongoDB Log Diagnostic Report

## Executive summary
State the overall health signal, the most important risk, the confidence level, and the analyzed time range.

## Extraction quality
Report input files, parsed/skipped records, time range, incompatible-driver findings, and any limitations.

## Prioritized issues
For each issue include:

- Priority: P0/P1/P2/P3.
- Category and concise title.
- Evidence: exact extracted metrics and time window.
- Likely interpretation: observation versus hypothesis.
- Confidence: high/medium/low.
- Possible fix or mitigation, including relevant trade-offs.
- Validation method and success criteria.
- Additional evidence needed, if the fix is not yet safe to recommend.

## Trends and correlations
Describe changes over time, repeated fingerprints, affected components/namespaces, and correlations between slow operations, errors, resource pressure, or topology events. Say “not observed in the extracted data” when appropriate.

## Recommended action plan
Separate immediate containment, near-term validation, and longer-term remediation. For each action, state the target finding, the possible fix, the expected benefit, the risk or trade-off, and how to validate success. Prefer safe, reversible actions and require testing before production changes.

Use category-specific guidance when supported by the extracted evidence:

- Query performance: validate query shape, plan, examined/returned ratio, sort stages, and candidate indexes with `explain()` before recommending index changes.
- Replication and availability: check member health, elections, heartbeat/connectivity, disk latency, and resource saturation before proposing topology or failover actions.
- Storage and resource pressure: correlate log signals with host or Atlas metrics before proposing cache, disk, compression, or capacity changes.
- Connections and network: inspect pool behavior, handshake/authentication failures, timeouts, and client concentration before proposing pool or timeout changes.
- Sharding and topology: validate balancer, migration, router, and metadata signals before proposing chunk or routing changes.

Never present a remediation as guaranteed, and never imply that a change has already been applied.

## Evidence appendix
List the most relevant extracted event fingerprints, slow-operation summaries, and counts used in the conclusions. Do not paste raw log lines or fullquery documents. For each slow-operation group, use `sample_query_shape` as the safe structural example when present; its literal values are redacted by the extractor. Use `sample_message` only as a redacted supporting clue. Use `error_scan.groups` to review every extracted error group, including single occurrences; use `repeated_errors` for groups with at least two occurrences. Each group includes `count`, `first_seen`, `last_seen`, occurrence timestamps, components, message samples, and a `sample_log` from the first occurrence. `sample_log.raw_line` preserves the source record structure with sensitive/query values redacted; `operation_details` provides grouped operation evidence. The optional `ftdc` section summarizes resource signals and correlates nearby FTDC samples with slow or inefficient `find` events using temporal overlap only. For repeated operation failures, use operation details for namespace, operation type, query hash, plan summary, app name, redacted query shapes, and available duration/examined/returned metrics. For unique-index duplicate failures, use the inferred `createIndexes` operation, error code/code name, collection, index name, and duplicate-key field names.

## Safety and quality rules

- Never expose credentials, connection strings, tokens, UUIDs, full query literals, document contents, or client IPs. Use redacted or aggregated evidence.
- Do not recommend an index solely from a single slow query. Require recurrence and validate with `explain()` and workload context.
- Do not infer an outage from warnings alone; corroborate with errors, topology changes, duration, or recurrence.
- Do not state that an issue is fixed. Recommend validation steps instead.
- Prefer UTC timestamps when presenting extracted time windows.
