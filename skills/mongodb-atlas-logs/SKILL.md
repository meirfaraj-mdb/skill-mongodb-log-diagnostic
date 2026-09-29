---
name: mongodb-atlas-logs
description: Downloads MongoDB Atlas host logs (mongod / mongos / audit) for a calendar day through the Atlas Admin API v2 with Digest auth, host selection, and retries. Cloud-agnostic - writes to a local directory or streams each log to any caller-supplied sink (S3, GCS, ...). Use when an agent or pipeline needs raw Atlas logs before running skill-mongodb-log-diagnostic.
---

# MongoDB Atlas Logs

This skill replaces the download logic of `lambda-downloading-logs/lambda_function.py`. It keeps the same
Atlas API calls, host selection rules, and time window, but has no AWS dependency. Upload is delegated to
a storage skill (`aws-storage`, `gcp-storage`) or to a local directory.

## Operating contract
- Standard library only (Python >= 3.10). No boto3 / google SDK import.
- Never print or log the Atlas private key. Only `group_id`, `cluster_name` and host names are logged.
- Treat downloaded logs as opaque gzip streams. Do not decompress or inspect them here: analysis belongs to
  `skill-mongodb-log-diagnostic`.
- A failed host does not abort the others. Failures are returned in `failed[]`, and the call raises only when
  no log could be produced at all.

## Configuration (same keys as the original lambda secret)
See `references/config-schema.md`. Required: `atlas_public_key`, `atlas_private_key`, `group_id`,
`cluster_name`, `timezone`, `api_version`. Optional: `hostnames`, `host_selector`, `log_names`
(`["auto"]`, `mongodb`, `mongos`, `mongodb-audit-log`, `mongos-audit-log`), `atlas_base_url`,
`http_timeout_seconds`, `max_retries`.

## Time window
`log_date` defaults to **D-1 in `timezone`**. The window is local midnight to the next local midnight (DST-safe),
sent as epoch seconds `startDate` / `endDate`, exactly like the lambda.

## Output layout (relative)
`<log_date>/<safe(host)>/mongodb/<log_name>.gz`, where `safe()` replaces `[^A-Za-z0-9._-]` with `_`. A storage
caller prefixes this with its own bucket prefix, so the result is identical to the lambda's
`<s3_prefix>/<date>/<host>/mongodb/<log>.gz`.

## CLI
```bash
# config from JSON file (or env ATLAS_CONFIG_JSON)
python3 scripts/atlas_logs.py --config atlas.json list-hosts
python3 scripts/atlas_logs.py --config atlas.json download --output-dir ./logs [--log-date 2026-09-22]
```
Prints a JSON result `{log_date, cluster, logs[], failed[]}`.

## Library (streaming to any storage)
```python
import atlas_logs
def sink(entry, fileobj, metadata):          # entry: host, host_dir, log_name, log_date, relative_path
    store.upload_stream(fileobj, f"{prefix}/{entry['relative_path']}", "application/gzip", metadata)
def exists(entry):                           # optional: skip logs already archived
    return store.exists(f"{prefix}/{entry['relative_path']}")
result = atlas_logs.archive_logs(config, log_date=None, sink=sink, exists=exists)
```
