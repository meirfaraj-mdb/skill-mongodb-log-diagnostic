# Deploy to Vertex AI Agent Engine: GCS existing-bucket mode

This deployment uses the Google ADK wrapper on Vertex AI Agent Engine. It does not use the Atlas API. The deployed agent reads existing MongoDB `.gz` logs from one Cloud Storage bucket, creates extracts, and writes extracts and reports back into that same bucket.

## Before starting

* Run the deployment from macOS or Cloud Shell with the project directory as the current directory.
* Ensure the full diagnostic skill is vendored under `skills/mongodb-log-diagnostic/`. The deployment script validates these required files before making cloud changes:

```text
scripts/extract_mongodb_log.py
scripts/ftdc_decoder.py
scripts/driver_compatibility.py
references/analysis-prompt.md
references/extracted-signal-reference.md
```

* The deploying identity needs permission to enable APIs, create/use a service account, grant IAM bindings on the secret and buckets, create/update Secret Manager versions, and deploy an Agent Engine agent. If these permissions are centrally managed, ask an administrator to perform those steps or provide an approved runtime service account.
* The runtime service account needs `roles/secretmanager.secretAccessor` on the one secret, `roles/storage.objectUser` on the log/report GCS bucket, and `roles/aiplatform.user` in the project.

## Expected GCS layout

The GCS log bucket must already contain raw logs with this layout. Multiple log files are supported per node.

```text
gs://LOG_BUCKET/atlas-logs/2026-09-26/node-0/mongodb/mongodb.gz
gs://LOG_BUCKET/atlas-logs/2026-09-26/node-0/mongodb/mongos.gz
gs://LOG_BUCKET/atlas-logs/2026-09-26/node-1/mongodb/mongodb.gz
```

The agent adds outputs in the same node directory:

```text
atlas-logs/2026-09-26/node-0/extracts/mongodb/extractionOccurence.json
atlas-logs/2026-09-26/node-0/extracts/mongodb/extractionshort.json
atlas-logs/2026-09-26/node-0/extracts/mongodb/handoff.md
atlas-logs/2026-09-26/node-0/reports/mongodb/report.md
atlas-logs/2026-09-26/node-0/reports/mongodb/diff.json
atlas-logs/2026-09-26/cluster/reports/cluster-summary.md
atlas-logs/2026-09-26/cluster/reports/manifest.json
```

## Run the interactive deployer

```bash
chmod +x samples/macos-deploy-vertex-existing-bucket.sh
./samples/macos-deploy-vertex-existing-bucket.sh
```

The script asks for:

* GCP project ID
* Agent Engine region, default `us-central1`
* input/output GCS bucket name
* GCS prefix, default `atlas-logs`
* Agent Engine staging bucket (or permission to create one)
* Secret Manager secret name
* Agent Engine runtime service-account name
* deployment display name
* report name and timezone
* Vertex model and model location, defaults `claude-sonnet-5` and `global`

It creates or adds a new version to the Secret Manager secret using the GCS-only JSON payload, grants least-privilege runtime permissions, packages the checked-in agent, and invokes the existing ADK Agent Engine deploy command.

## Secret created by the script

The script creates this shape, with your values substituted. It contains no Atlas API keys.

```json
{
  "input_mode": "existing_bucket",
  "bucket": "my-existing-mongodb-log-bucket",
  "prefix": "atlas-logs",
  "timezone": "Asia/Jerusalem",
  "report_name": "my-mongodb-cluster",
  "storage_provider": "gcs",
  "llm_provider": "vertex",
  "vertex_model": "claude-sonnet-5",
  "vertex_location": "global"
}
```

## Test after deployment

In the Agent Engine chat/test interface, request:

```text
Analyze existing bucket logs for 2026-09-26 without using Atlas API.
```

This calls `run_existing_bucket_diagnostics("2026-09-26")`. It skips Atlas download, discovers `mongodb/*.gz` objects, skips node/log extracts that already have `extractionOccurence.json`, and writes the remaining extracts and reports to the bucket.

## Important limitation

The script calls the installed `adk deploy agent_engine` CLI. Before a production deployment, run:

```bash
adk deploy agent_engine --help
```

If your installed ADK version uses different option names for `--service_account`, `--staging_bucket`, or `--requirements_file`, update `deploy/gcp/deploy-adk-agent-engine.sh` to match that CLI version before running the interactive script.
