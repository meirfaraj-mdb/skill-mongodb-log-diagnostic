# MongoDB Log Diagnostic Agent (AWS or Google Cloud)

A daily diagnostic pipeline built from skills. **AWS** uses a Fargate task plus Bedrock; **Google Cloud** uses a Google ADK wrapper deployed to Vertex AI Agent Engine, with Vertex AI for report generation.

```
skills/
  mongodb-atlas-logs/            Atlas skill (was lambda-downloading-logs; now cloud-agnostic, stdlib only)
  aws-storage/                   AWS skill: S3 ObjectStore + Secrets Manager
  gcp-storage/                   Google skill: GCS ObjectStore + Secret Manager (same interface)
  mongodb-log-diagnostic/        vendored copy (extractor + analyzers + references/*.md)
agent/                           orchestration: download -> extract -> report (+ n-1/n-8 diff)
deploy/aws/                      Dockerfile, Step Functions, IAM policy, deploy.sh
deploy/gcp/                      ADK packaging and Vertex AI Agent Engine deploy script
google_adk_agent/             ADK root agent; tools call the shared deterministic pipeline
```

## Flow (identical on both clouds)
| Step | Skills used | Output in the bucket |
|---|---|---|
| 1. Download D-1 logs for every node | `mongodb-atlas-logs` + `aws-storage`/`gcp-storage` (streamed upload) | `<prefix>/<D-1>/<host>/mongodb/<log-name>.gz` (same keys as the old lambda) |
| 2. Extract per node | `mongodb-log-diagnostic/scripts/extract_mongodb_log.py` | `<prefix>/<D-1>/<host>/extracts/<log-name>/{extractionOccurence.json, extractionshort.json, handoff.md}` |
| 3. Report per node + diff vs D-2 (n-1) and D-8 (n-8) if they exist | `references/analysis-prompt.md` + `references/extracted-signal-reference.md` → LLM | `<prefix>/<D-1>/<host>/reports/<log-name>/{report.md,diff.json}; cluster/reports/{cluster-summary.md,manifest.json}` |

## Cloud equivalence
| Concern | AWS | Google Cloud |
|---|---|---|
| Secret | Secrets Manager (`ATLAS_SECRET_ID` = ARN) | Secret Manager (`ATLAS_SECRET_ID` = `projects/<p>/secrets/<name>[/versions/<v>]`) |
| Bucket | S3 (`aws-storage` skill) | GCS (`gcp-storage` skill) |
| Compute / wrapper | Fargate task | Google ADK agent on Vertex AI Agent Engine |
| Agent interface | Deterministic ECS task arguments | ADK `root_agent` with `run_daily_diagnostics` and `run_diagnostic_stage` tools |
| Report LLM | Bedrock Converse (`bedrock_model_id`) | Vertex AI (`vertex_model`), called by the shared report stage |
| Scheduling | EventBridge Scheduler → ECS RunTask | Cloud Scheduler or another approved scheduler → Agent Engine query endpoint |
| Deploy | Fargate deployment files | `deploy/gcp/package_adk_agent.sh` and `deploy/gcp/deploy-adk-agent-engine.sh` |

The shared pipeline uses `CLOUD_PROVIDER=aws|gcp|local`. AWS sets `aws`; the Agent Engine wrapper sets `gcp` before invoking the shared stages.

## Secret (one JSON, same keys on both clouds)
The lambda's keys are unchanged. **Existing AWS secrets keep working as-is**: `s3_bucket` / `s3_prefix` are still accepted. See `secret.example.json`.

| Key | Notes |
|---|---|
| `atlas_public_key`, `atlas_private_key`, `group_id`, `cluster_name`, `timezone`, `api_version`, `host_selector`/`hostnames`, `log_names` | Atlas skill (see `skills/mongodb-atlas-logs/references/config-schema.md`) |
| `bucket` (or `s3_bucket` / `gcs_bucket`), `prefix` (or `s3_prefix` / `gcs_prefix`) | Accepts `s3://…` / `gs://…` / `file://…` to force the storage provider |
| `storage_provider`, `llm_provider` | Optional overrides. Defaults: AWS = `s3` + `bedrock`, GCP = `gcs` + `vertex`; local report option = `anthropic` (direct Claude API) |
| `bedrock_model_id`, `bedrock_region` | AWS LLM |
| `vertex_model`, `vertex_location`, `vertex_project` | GCP LLM (a `claude-*` model id uses the Anthropic publisher; anything else uses Gemini) |
| `slow_ms`, `expected_node_count` (3), `report_max_tokens`, `report_max_input_chars`, `cluster_summary` | Pipeline tuning |

## Extraction behavior
Extraction is sequential per node/log: the agent downloads one raw log, runs the extractor, uploads `extractionOccurence.json`, `extractionshort.json`, and `handoff.md`, then begins the next node. By default, the Atlas downloader skips each `<node>/mongodb/<log-name>.gz` already present in storage. Extraction skips a node/log only when **all three** outputs (`extractionOccurence.json`, `extractionshort.json`, and `handoff.md`) are present in its `extracts/<log-name>/` directory; partial output is regenerated and uploaded before the next node/log. Use `--force-reextract` for a local/CLI rerun, or set the ADK stage tool’s `skip_existing` to `false` to regenerate that node’s outputs.

## Offline driver CVE checks
Reports do not access the internet. `skills/mongodb-log-diagnostic/references/offline-driver-cves.json` is a manually maintained snapshot. The pipeline matches extracted driver name/version pairs against it, includes the catalog refresh date in every report context and manifest, and labels a no-match as *no match in this snapshot*—not proof that no CVE exists. Update the file through a reviewed change; update `catalog_version` and `refreshed_at` every time. MongoDB’s Security Bulletins and Alerts are the intended refresh sources.

## Testing examples
* [GCP and local test guide](samples/gcp-local-test.md)
* [GCP existing-bucket secret](samples/secret.gcp-existing-bucket.example.json)
* [Local existing-bucket secret](samples/secret.local-existing-bucket.example.json)
* [macOS local GCP bootstrap script](samples/macos-local-gcp-test.sh)
* [AWS + Atlas local test guide](samples/aws-local-atlas-test.md)
* [macOS AWS + Atlas bootstrap script](samples/macos-local-aws-atlas-test.sh)
* [AWS + Atlas test secret template](samples/secret.aws-atlas-local-test.example.json)
* [Local Atlas-to-folder test guide](samples/local-atlas-folder-test.md) — optional Bedrock reports, with all files stored locally
* [macOS local Atlas-to-folder script](samples/macos-local-atlas-folder-test.sh)
* [Local Atlas-to-folder config template](samples/secret.local-atlas-folder.example.json)
* [Vertex existing-bucket deployment guide](samples/vertex-existing-bucket-deploy.md)
* [Interactive macOS/Cloud Shell Vertex deployer](samples/macos-deploy-vertex-existing-bucket.sh)

## Existing bucket mode (GCP, no Atlas API)
Use this mode when `.gz` MongoDB logs are already in Cloud Storage. The pipeline makes **no Atlas API request**: it discovers the raw logs, downloads each one temporarily, runs the local extractor, then uploads the extracts and reports back to the same bucket.

Create a Secret Manager secret from [`secret.gcp-existing-bucket.example.json`](samples/secret.gcp-existing-bucket.example.json). It deliberately contains **no** `atlas_public_key`, `atlas_private_key`, `group_id`, `cluster_name`, or other Atlas API configuration.

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

**Required bucket layout**

```text
# Input: already present in GCS
atlas-logs/2026-09-26/node-0/mongodb/mongodb.gz
atlas-logs/2026-09-26/node-1/mongodb/mongodb.gz
atlas-logs/2026-09-26/node-2/mongodb/mongodb.gz

# Created by this agent
atlas-logs/2026-09-26/node-0/extracts/mongodb/extractionOccurence.json
atlas-logs/2026-09-26/node-0/extracts/mongodb/extractionshort.json
atlas-logs/2026-09-26/node-0/extracts/mongodb/handoff.md
atlas-logs/2026-09-26/node-0/reports/mongodb/report.md
atlas-logs/2026-09-26/cluster/reports/manifest.json
```

The Agent Engine runtime service account needs `roles/secretmanager.secretAccessor` on this secret, `roles/storage.objectUser` on this bucket, and `roles/aiplatform.user` in the project. Invoke `run_existing_bucket_diagnostics(log_date)` with an explicit date, for example `2026-09-26`. It skips download, discovers raw `.gz` objects, extracts them, uses D-2/D-8 extracts if available, and uploads reports. If extracts already exist and only reports are needed, invoke `report` for that date.

Vertex defaults to `claude-sonnet-5` when `vertex_model` is omitted.

## Deploy
**AWS**
```bash
ACCOUNT=… REGION=… SECRET_ARN=… LAMBDA_ROLE_ARN=… SFN_ROLE_ARN=… SCHEDULER_ROLE_ARN=… ./deploy/aws/deploy.sh
```
The Lambda role needs `deploy/aws/lambda-execution-policy.json`.

**Google Cloud — ADK on Vertex AI Agent Engine**
```bash
# The secret and GCS bucket already exist.  Grant the Agent Engine runtime service account:
# * roles/secretmanager.secretAccessor on the one secret
# * roles/storage.objectUser on the report/log bucket
# * roles/aiplatform.user in the project
PROJECT=… REGION=us-central1 STAGING_BUCKET=gs://… \
RUNTIME_SA=mongodb-log-agent@${PROJECT}.iam.gserviceaccount.com \
SECRET_NAME=atlas-log-agent ./deploy/gcp/deploy-adk-agent-engine.sh
```
This packages the shared pipeline and vendored diagnostic skill with `google_adk_agent`, then deploys `root_agent` to Agent Engine; it does not clone any GitHub content. The wrapper has three tools: `run_daily_diagnostics` (download → extract → report), `run_existing_bucket_diagnostics` (GCS/local bucket data without Atlas API), and `run_diagnostic_stage` for a named recovery/backfill stage. The secret value never enters the ADK package; the runtime uses its service account to read it.

## Running stages / backfill
* **AWS Lambda event:** `{"stage": "all|download|extract|report", "log_date": "YYYY-MM-DD"}`
* **Google ADK / Agent Engine:** ask the deployed agent to run its `run_daily_diagnostics` tool, or call `run_diagnostic_stage` for backfills.
* **Local CLI:** `python -m agent.handler --stage report --log-date 2026-09-22`
* **Replacing the old lambda:** invoke with `{"stage": "download"}`. It uses the same secret, the same keys, and skips logs that already exist.
* **Using the skills on their own:**
  * `python3 skills/mongodb-atlas-logs/scripts/atlas_logs.py --config atlas.json download --output-dir ./logs`
  * `python3 skills/aws-storage/scripts/aws_storage.py cp …`
  * `python3 skills/gcp-storage/scripts/gcp_storage.py cp …`
* **Local run, no cloud:** `CLOUD_PROVIDER=local ATLAS_CONFIG_FILE=cfg.json` with `"bucket": "file:///data/atlas"` (LLM: set `llm_provider`).

## Tests
`python3 tests/test_multicloud.py` runs fully offline and covers:
* the full pipeline against fake S3, fake GCS and the local store
* secret loaders for both clouds, config normalization (legacy `s3_*` keys), and cloud detection
* Bedrock and Vertex (Gemini and Claude) request shapes and token-limit continuation
* the Atlas skill CLI, and Cloud Run task sharding

## Vendored diagnostic skill
Place the complete external skill under `skills/mongodb-log-diagnostic/`, preserving its `scripts/` and `references/` paths. AWS and Google builds deliberately fail early if the extractor or either report reference is missing.

## Known limitations
* **Driver CVEs:** reports use the checked-in offline driver-CVE snapshot only. A no-match means no match in that dated snapshot, not that a driver is safe.
* **Baselines:** only extracts that already exist are compared. To backfill D-2/D-8, run `--stage extract --log-date <day>`.
* **Agent Engine runtime sizing:** validate the largest daily `.gz` against Agent Engine runtime limits before production; the prior Cloud Run deployment remains only as a legacy alternative.
* **Not collected:** audit logs are skipped by the extract step. FTDC is not collected because the Atlas logs API does not provide it.


## Optional observability collection

Set `observability_enabled: true` only for an Atlas or Ops Manager deployment where the runtime can reach MongoDB. The stage runs **sequentially** per node: it completes the node’s `$indexStats` collection and upload before starting the next node. It writes:

```text
<prefix>/<date>/<node>/indexStats/index-stats.json
<prefix>/<date>/<node>/queryStats/query-stats.json
```

`$indexStats` is collected directly from every configured node because its counters are node-local. The Atlas `queryStats` equivalent is collected using the Atlas Query Shape Insights API and written under each node’s `queryStats/` directory, filtered with that node’s Atlas process ID when configured. Ops Manager does not call this Atlas API. `input_mode: existing_bucket` always skips this entire stage and makes no MongoDB or Atlas connection.

See `samples/secret.atlas-observability.example.json` and `skills/mongodb-observability/SKILL.md`.

## Offline CVE catalog

The report pipeline uses `skills/mongodb-log-diagnostic/references/offline-driver-cves.json` when it has no Internet access. It reports only matching entries and the catalog refresh date. A no-match is explicitly not a clean bill of health. Refresh this checked-in snapshot manually from MongoDB Security Bulletins and Alerts.

## Local Atlas report tests

Both `samples/macos-local-atlas-folder-test.sh` and `samples/macos-local-atlas-vertex-test.sh` download from Atlas and store logs, extracts and reports in a local folder. Reporting defaults to `none`; optionally choose `bedrock`, `vertex`, `anthropic` (direct API), or `claude_cli` (installed Claude Code CLI). Direct Claude uses `ANTHROPIC_API_KEY` from your environment or a hidden prompt; the API key is not saved to the local config. Use an Anthropic API model ID your account can invoke. `claude_cli` uses your existing `claude` login instead of an API key, requires internet, and is supported only with local storage (not hosted AWS/GCP). See `samples/local-atlas-bedrock-report.md` and `samples/local-atlas-vertex-report.md`.

## Query-shape observability sources

When the optional `observability` stage is enabled, query shapes are written per node to
`<prefix>/<date>/<node>/queryStats/query-stats.json`.

- **Atlas:** set `query_shape_source: "atlas_api"` (or `"auto"`) and configure the approved
  `atlas_query_stats_url`. The stage requests the previous 24 hours by default.
- **Ops Manager/self-managed:** set `query_shape_source: "mongodb"`; the node reader runs
  `$queryStats` and writes its snapshot to the same path.
- **Existing bucket:** set `query_shape_source: "bucket"` and upload those JSON files first.
  `input_mode: "existing_bucket"` makes no Atlas API or MongoDB connection; reports simply
  include the existing query-shape JSON when present.

See `skills/mongodb-observability/SKILL.md` and
`samples/secret.existing-bucket-query-shapes.example.json`.
