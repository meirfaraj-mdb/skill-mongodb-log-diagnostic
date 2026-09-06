# Analyze MongoDB Extraction and Generate Diagnostic Report

You are a senior MongoDB performance and production reliability engineer.

Analyze the attached file `/home/user/extraction.json` and generate a complete, evidence-based MongoDB diagnostic report in Markdown using only the logs and log-derived data. Do not expect, request, or analyze FTDC data.

## Analysis requirements

1. Read and validate the JSON before drawing conclusions. Preserve redacted values exactly as provided.
2. Analyze all major log-derived sections, including:
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
7. Use only evidence present in the extraction. If a resource, replication, connection-pool, or host-level signal is not represented in the logs, mark it as unverified and state what additional data would be required.
8. If any arrays appear capped, truncated, filtered, or incomplete, state that limitation and do not interpret missing rows as missing events or correlations.

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

## 6. Log-based timing and resource evidence
Present relevant log-derived timing and resource observations in a table when the extraction contains sufficient evidence:

| Log signal or event | Log time or range | Component | Observation | Evidence source | Confidence |
|---|---|---|---|---|---|

Use timestamps and temporal proximity only as corroborating evidence. Do not call a resource, storage, lock, replication, or connection signal causal solely because it appears near a slow operation or error.

Explain which latency contributors are supported by the logs, weakened by the logs, or still unverified. Clearly identify resource signals that require external metrics or additional diagnostic data.

## 7. Causal assessment
Separate:

- confirmed root causes or defects
- likely contributors
- correlations that require additional validation
- unrelated or independent issues

Explain whether the evidence supports one dominant bottleneck or multiple concurrent problems.

## 8. Recommended execution order
Provide a numbered action plan. Put configuration and data-integrity defects first, then resource validation, query/index validation, workload remediation, and finally re-testing.

Each action should include an expected validation result.

## 9. Validation plan
Specify what to measure after remediation, including p95/p99 latency, write latency, collection scans, sort stages, documents examined per result, authentication failures, authorization failures, duplicate-key errors, index-build failures, connection errors, replication lag, checkpoint behavior when represented in logs, and connection-pool behavior when represented in logs.

For signals not available in the extraction, state explicitly that they require external monitoring or a new diagnostic capture.

## 10. Important limitations
List all limitations, including missing timestamps, filtered records, truncated arrays, aggregate-only metrics, redacted fields, incomplete client attribution, unavailable host/resource metrics, and any inability to establish causality.

## Writing rules

- Use concise but technically precise language suitable for MongoDB engineers and customer stakeholders.
- Use UTC timestamps consistently.
- Use exact numbers from the extraction and do not silently round away important differences.
- Avoid claiming that an issue is fixed unless the extraction proves remediation.
- Do not recommend increasing timeouts or pool sizes as a first response to unresolved authentication, storage, query, or replication problems.
- Do not suggest dropping unique indexes or making production changes without stating the required data review and validation.
- If data is contradictory or incomplete, call it out explicitly and explain the next diagnostic step.
- Do not infer FTDC metrics or any other data absent from the extraction.
- End with a short list of the top three actions that should happen next.
