# Atlas local-run test: local folder, S3, or GCS

The macOS runner executes the pipeline on **your Mac**. Atlas is remote; choose where the logs, extracts, observability files, and optional reports are stored. It does **not** create a bucket, access a cloud secret, or deploy an agent.

## Before running

- Python 3.10+ on macOS and Atlas API key with permission to list project processes and download logs.
- The full diagnostic skill must be present under `skills/mongodb-log-diagnostic/`, especially `scripts/extract_mongodb_log.py`, `scripts/ftdc_decoder.py`, `scripts/driver_compatibility.py` and its `references/` files. Without these, extraction will not run.
- For **local folder**, no cloud credentials are required unless the optional report provider is Bedrock or Vertex.
- For **S3**, an existing bucket and AWS credentials/profile usable by boto3 (for example `aws sso login --profile NAME`). The identity needs `s3:ListBucket` for the chosen prefix and `s3:GetObject`, `s3:PutObject` under `<bucket>/<prefix>/*`; the script also calls `sts:GetCallerIdentity` to check identity. Access to Bedrock is needed only if chosen for reports. No Secrets Manager permission is needed.
- For **GCS**, an existing bucket, `gcloud`, Google Application Default Credentials (`gcloud auth application-default login` if not already set), and permission to list/read/create objects in the chosen bucket/prefix (for example bucket-level `roles/storage.objectUser`). Vertex access is needed only if chosen for reports. No Secret Manager permission is needed.
- For direct Claude reports supply `ANTHROPIC_API_KEY`; for `claude_cli` log in to Claude Code CLI. These report choices are independent of storage.

## Run

```bash
chmod +x samples/macos-local-atlas-folder-test.sh
./samples/macos-local-atlas-folder-test.sh
```

The parallel runner `samples/macos-local-atlas-vertex-test.sh` offers the same storage choices, with a separate virtualenv/config file. Both default to **local** storage and **no reports**. Choose `s3` or `gcs` and enter an existing bucket *name*, its region/project and an isolated test prefix. The script checks credentials and bucket listing, and shows the target before writing. It installs only the required storage SDK into its local virtualenv. Choose a report provider separately (`none`, `bedrock`, `vertex`, `anthropic`, `claude_cli`).

```text
<prefix>/<YYYY-MM-DD>/<node>/mongodb/<log>.gz
<prefix>/<YYYY-MM-DD>/<node>/extracts/<log>/{extractionOccurence.json,extractionshort.json,handoff.md}
<prefix>/<YYYY-MM-DD>/<node>/queryStats/query-stats.json       # if enabled
<prefix>/<YYYY-MM-DD>/<node>/indexStats/index-stats.json       # if enabled
<prefix>/<YYYY-MM-DD>/<node>/reports/<log>/report.md          # if enabled
<prefix>/<YYYY-MM-DD>/cluster/reports/                         # if enabled
```

The same keys apply to the local folder, `s3://<bucket>/`, or `gs://<bucket>/`. Atlas download skips existing raw objects; extraction skips when all three outputs already exist. Choose an isolated prefix for testing. There is **no automatic cleanup** of real-bucket objects. The generated `.local-atlas-*-test.json` contains Atlas credentials, has restricted file permissions, and must not be committed or shared. The script does not test real cloud writes until the run begins; list access alone does not guarantee write access.

For separate manual stage invocations, activate the script's `.venv-local-atlas-*` environment and set `CLOUD_PROVIDER=local`, `ATLAS_CONFIG_FILE` to its generated JSON, `SKILLS_DIR` to the project's `skills/` and `DIAG_SKILL_DIR` to `skills/mongodb-log-diagnostic/`. The storage backend is selected by `storage_provider` in that JSON; `CLOUD_PROVIDER=local` means the **config is local**, not that the bucket must be local.

### Reuse interactive choices

Both macOS Atlas runners can save the chosen non-secret settings to `~/.localsample` after choosing the storage target. Answer `yes` to the save prompt. The next run shows them as editable prompt defaults. The file is JSON with owner-only (`0600`) permissions; you may inspect or remove it with `cat ~/.localsample` or `rm ~/.localsample`. Atlas API keys, Anthropic API keys, MongoDB connection URIs, and log dates are **not** stored, so you must supply credentials each time (the date defaults to yesterday). Existing local run config files still contain Atlas credentials; keep those private.


If the skill scripts are missing, run `./samples/restore-vendored-skills.sh` first (GitHub access required), or accept the restore prompt. The runners offer to save non-secret choices to `~/.localsample` **before** cloud authentication, so an S3 bucket/region/profile/prefix choice persists even if the first AWS check fails.

### S3 download troubleshooting

S3 uploads stage one complete compressed log under the system temporary directory before multipart upload. Ensure the Mac has at least the size of the largest `.gz` log in free temporary disk space; set `TMPDIR` before running to change that location. If Atlas resets the connection mid-log, the downloader retries the whole response (up to the configured retry count); it does not upload a partial S3 object. A completed S3 object is skipped on rerun.


`cloud=local storage=s3` is normal: `local` means the runner reads its JSON config on your Mac; `s3` selects the storage backend. If a node's Atlas download or S3 upload fails, the run now stops before extraction and reports the node and error. Successful uploads are resumable. `ListBucket` preflight does not establish object-level `GetObject` (needed for `head_object`/extract) or `PutObject` access. Review the stage error for `AccessDenied`, `NoSuchBucket`, `PermanentRedirect`, or other S3 errors. Never share the generated config or Atlas credentials when reporting failures.
