# Analyze MongoDB Extraction and Generate Diagnostic Report

You are a senior MongoDB performance and production reliability engineer.

Analyze the attached file `/home/user/extraction.json` and generate a complete, evidence-based MongoDB diagnostic report in Markdown.

## Analysis requirements

1. Read and validate the JSON before drawing conclusions. Preserve redacted values exactly as provided.
2. Analyze all major sections, including:
   - `metadata` and parser/schema versions
   - `quality` and parsing completeness
   - `error_scan` and `repeated_errors`
   - `slow_operations`
   - `issue_candidates`
   - `summary` and `trends`
3. Identify and prioritize confirmed MongoDB issues, likely contributing factors, and items that remain unverified.
4. For every important finding, include the supporting evidence:
   - event count
   - time range
   - component
   - event ID or fingerprint when available
   - namespace, query hash, operation, or plan summary when available
   - latency, documents examined, returned documents, sort stages, and scan type when available
5. Distinguish clearly between:
   - confirmed error or configuration defect
   - strong performance indication
   - plausible contributing factor
   - hypothesis requiring validation
   - unsupported conclusion
6. Do not infer causality from frequency, temporal proximity, or a MongoDB component name alone. Do not invent missing metric values, client identities, indexes, or workload behavior.

## Required report structure

# MongoDB Log Diagnostic — Issues and Resolution

## 1. Executive summary
Summarize the main production risks, the likely latency contributors, the highest-priority actions, and the overall confidence level.

## 2. Scope, inputs, and data quality
Document the input file, parser/schema versions, log time range, records and lines processed, skipped or filtered records, timestamp quality, and any inconsistencies between extraction runs.

## 3. Prioritized findings
Use a table with these columns:

| Priority | Finding | Evidence | Impact | Recommended resolution | Confidence |
|---|---|---|---|---|---|

Prioritize P0, P1, and P2 findings. Keep independent defects separate instead of combining unrelated causes.

## 4. Confirmed error findings
For each repeated or material error, explain the MongoDB behavior, likely application or deployment implication, remediation, and validation steps. Include authentication failures, authorization failures, duplicate-key errors, index-build failures, WiredTiger/storage warnings, replication/availability signals, and connection/network errors when present.

## 5. Query and latency analysis
Identify the slowest and most expensive operations. Discuss:

- collection scans
- incomplete or mismatched indexes
- blocking sorts
- large `$or` predicates
- high documents-examined-to-returned ratios
- expensive aggregation or array processing
- large `getMore` batches
- hot-document and write-amplification risks
- slow indexed single-document updates

For each index recommendation, label it as a candidate for testing—not an instruction to apply blindly. Recommend representative `explain("executionStats")` validation.

## 6. Causal assessment
Separate:

- confirmed root causes or defects
- likely contributors
- correlations that require additional validation
- unrelated or independent issues

Explain whether the evidence supports one dominant bottleneck or multiple concurrent problems.

## 7. Recommended execution order
Provide a numbered action plan. Put configuration and data-integrity defects first, then query/index validation, workload remediation, and re-testing.

Each action should include an expected validation result.

## 8. Validation plan
Specify what to measure after remediation, including p95/p99 latency, write latency, collection scans, sort stages, documents examined per result, authentication failures, authorization failures, duplicate-key errors, index-build failures, connection errors, replication lag, CPU, I/O, and connection-pool behavior.

## 9. Important limitations
List all limitations, including missing timestamps, filtered records, aggregate-only metrics, redacted fields, incomplete client attribution, and any inability to establish causality.

## Writing rules

- Use concise but technically precise language suitable for MongoDB engineers and customer stakeholders.
- Use UTC timestamps consistently.
- Use exact numbers from the extraction and do not silently round away important differences.
- Avoid claiming that an issue is fixed unless the extraction proves remediation.
- Do not recommend increasing timeouts or pool sizes as a first response to unresolved authentication, storage, query, or replication problems.
- Do not suggest dropping unique indexes or making production changes without stating the required data review and validation.
- If data is contradictory or incomplete, call it out explicitly and explain the next diagnostic step.
- End with a short list of the top three actions that should happen next.

## Extraction-file interpretation contract

Before interpreting the JSON, read `references/extracted-signal-reference.md`. It defines the exact machine-readable contract for `extractionOccurence.json` and `extractionshort.json`.

- Use `extractionOccurence.json` as the authoritative file when both files are present. It contains aggregate data plus `occurrence_timestamps` for error groups and slow-operation groups.
- `extractionshort.json` contains the same aggregate data and removes only `occurrence_timestamps`. Missing occurrence arrays in this file are intentional and must not be interpreted as zero occurrences.
- `slow_operations` is an alias of `slow.operations`; never add the two arrays together.
- Use group-level fields for conclusions and occurrence arrays for recurrence and time distribution.
- Preserve missing values as unknown. Do not reconstruct redacted values or invent absent metrics.

Use normalized UTC ISO timestamps for ordinary timestamps:

```text
YYYY-MM-DDTHH:MM:SS[.fraction]Z
```

Use the compact occurrence formats exactly:

- Error occurrences: `YYYYMMDD` → `HHHMM` → `{ "occur": number }`, where `H` is a literal separator and the value is a UTC minute bucket.
- Slow occurrences: `YYYYMMDD` → `HH:MM:SS.cc` → metric object, where `cc` is centiseconds and the time is UTC.

Do not silently convert these keys into local time or confuse error minute buckets with exact slow-operation timestamps.

## Driver compatibility and CVE/update analysis

Analyze `driverCompatibility` separately from MongoDB server findings. Do not wait for a CVE ID to be present in the extraction: the extractor normally provides driver names and versions, while CVE identification requires an explicit advisory lookup.

Inspect **every** driver/version in `driverCompatibility`, including `distinct_compatible_drivers`, not only `incompatible_drivers`. A driver can be compatible with the observed MongoDB server and still be vulnerable, end-of-life, or behind a security fix.

Distinguish:

- `driverCompatibility.count` and `driver_log_count`: parsed log records containing driver metadata.
- `driverCompatibility.incompatible_count`: incompatible driver/version groups.
- `incompatible_drivers`: incompatible driver groups with driver name, version, reason, time range, evidence, distinct IPs, application names, and platforms when present.
- `distinct_compatible_drivers`: observed compatible versions; compatibility does not prove that a version is current or free of every CVE.

### Mandatory CVE retrieval procedure

For each distinct driver and observed version:

1. Normalize only the driver name for lookup; preserve the observed version exactly in the report. Remove wrapper labels after separators such as `|` only for the lookup key.
2. Perform an explicit security lookup using the available authoritative sources, in this order where applicable: the driver/vendor security advisories and release notes, MongoDB driver release notes, the relevant package ecosystem advisory database, NVD/CVE records, and GitHub Security Advisories. Search using the exact driver name and observed version, then the driver name plus `CVE`, `security advisory`, and supported release line.
3. Check whether the observed version falls inside the advisory’s affected range. Do not report a CVE merely because the driver name appears in a search result.
4. Record the CVE/advisory identifier, severity, affected range, fixed version, advisory publication date, and source link. Prefer a vendor advisory when it disagrees with a generic database.
5. Determine two separate targets: the minimum patched version that fixes the specific advisory on the relevant major line, and the recommended update version that is an explicit supported release target. Do not write only `latest`.
6. If online lookup or authoritative advisory data is unavailable, say `CVE lookup could not be completed from the available sources`; do not guess a CVE, severity, fixed version, or update version. This is a lookup limitation, not proof that the driver is safe.
7. If no matching advisory is found after checking the available sources, state `No matching advisory found in checked sources as of <UTC date>` and list the sources checked. Do not state that the driver has no vulnerabilities.

### Required red warning table

Whenever any driver is affected by a confirmed CVE, has an available security advisory, is end-of-life, or requires an update for compatibility, place this block near the top of the report and before ordinary findings:

> **⚠️ DRIVER CVE WARNINGS — UPDATE REVIEW REQUIRED**
>
> The following driver versions require security or compatibility review. Version targets are advisory recommendations and must be validated against application compatibility before deployment.

<table>
<thead>
<tr><th>⚠️ Driver</th><th>Observed version</th><th>CVE / advisory</th><th>Severity</th><th>Affected range</th><th>Minimum patched version</th><th>Recommended update version</th><th>Evidence / source</th><th>Confidence</th></tr>
</thead>
<tbody>
<tr><td>Driver name</td><td>Observed version</td><td>CVE-ID or advisory</td><td>Critical/High/Medium/Low/Unknown</td><td>Affected versions</td><td>First fixed version</td><td>Explicit target version</td><td>Extraction evidence and authoritative source link</td><td>High/Medium/Low</td></tr>
</tbody>
</table>

Populate one row per driver/CVE or advisory. Replace the example row; do not leave a generic empty table when a warning exists. If multiple CVEs affect the same version, create separate rows. If a driver needs an update for compatibility but no CVE is confirmed, identify the advisory as `Compatibility update — no CVE confirmed` and keep the CVE field explicitly marked `Not confirmed`.

Rules for completing the warning table:

1. Use the driver name before wrapper separators such as `|`. Preserve the observed version exactly as extracted.
2. `Minimum patched version` must be the first version that fixes the specific CVE on the relevant supported release line, confirmed by an authoritative advisory.
3. `Recommended update version` must be an explicit target version. Prefer the newest supported patch release on the same major line when it is the safe compatibility choice; otherwise recommend a supported release line after checking application compatibility.
4. Include source links and the UTC date of the lookup. A source link is required for a confirmed CVE claim.
5. Never invent a CVE, severity, affected range, fixed version, or target version. Use `Not determinable from supplied evidence` when the exact value cannot be verified.
6. Separate “incompatible with the observed MongoDB/server context” from “vulnerable to a CVE.” An incompatibility finding is not automatically a security vulnerability.
7. Recommend validation after any update: application-driver compatibility testing, representative workload tests, authentication/TLS checks, connection-pool behavior, and post-deployment error review. Do not claim that an update was applied.

If no affected, advisory-relevant, obsolete, or incompatible driver is found after inspecting all observed driver versions, include this statement instead of an empty warning table: `No driver CVE or driver-update warning was identified in the extracted data after checking the available authoritative sources.`

## Optional historical comparison: n-1 and n-8

If files labeled `n-1` and/or `n-8` are provided, use them as comparison baselines:

- `n-1` means yesterday’s extraction.
- `n-8` means the extraction from one week ago, using the supplied labels rather than guessing from file modification time.
- Compare the current extraction with each available baseline independently. If only one baseline is supplied, perform only that comparison. If neither is supplied, omit historical-comparison claims.
- Verify that the files are compatible extraction types and identify parser/schema versions and covered UTC time ranges before comparing.
- Do not compare raw event counts as trends when covered durations, parser versions, or input populations differ without explaining the limitation.
- Compare, where fields exist: quality and parsed/skipped counts, error-group counts and fingerprints, repeated errors, slow global statistics, slow-operation groups, COLLSCAN and change-stream counts, duration/CPU statistics, issue candidates, driver versions, and incompatible groups.
- Match errors by fingerprint/category and slow operations by namespace + `plan_summary` + `query_hash` when available. If a query hash is missing, state that the match is approximate.

Add this table when at least one baseline exists:

| Signal | Current | n-1 (yesterday) | Change vs n-1 | n-8 (one week ago) | Change vs n-8 | Interpretation / limitation |
|---|---:|---:|---:|---:|---:|---|

Use exact counts and statistics from the files. For percentages, state the denominator and do not calculate a rate when the baseline duration or population is not comparable.

Label a signal as `new`, `resolved`, `increased`, `decreased`, `stable`, or `not comparable` only when the extracted evidence supports that label. A missing group in a short file means occurrence detail was removed, not necessarily that the group was absent.

Historical comparison is corroborating context; it does not prove causality or remediation. If the input files are not clearly labeled, state that a reliable n-1/n-8 comparison cannot be made.

## Evidence rules

- Never expose credentials, connection strings, tokens, UUIDs, full query literals, document contents, or client IPs. Use redacted or aggregated evidence.
- Do not recommend an index solely from a single slow query. Require recurrence and validate with `explain()` and workload context.
- Do not infer an outage from warnings alone; corroborate with errors, topology changes, duration, or recurrence.
- Do not state that an issue is fixed. Recommend validation steps instead.
- Prefer UTC timestamps when presenting extracted time windows.
