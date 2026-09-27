# GCP and local test guide

This guide tests the existing-bucket mode. It does not call the Atlas API. The pipeline reads existing compressed MongoDB logs, generates extracts, and writes extracts and reports back to the configured bucket or local directory.

## Prerequisites

* Use the complete project checkout.
* Populate `skills/mongodb-log-diagnostic/` with the vendored extractor scripts and report-reference files before running a real extraction.
* Install the local dependencies:

```bash
python3 -m pip install -r requirements-gcp.txt
```

## GCP secret: existing GCS logs only

Use [`secret.gcp-existing-bucket.example.json`](secret.gcp-existing-bucket.example.json) as the Secret Manager payload. It contains no Atlas credentials.

Create the secret from Cloud Shell:

```bash
gcloud secrets create atlas-log-agent   --replication-policy=automatic   --data-file=samples/secret.gcp-existing-bucket.example.json
```

If the secret already exists, add a new version instead:

```bash
gcloud secrets versions add atlas-log-agent   --data-file=samples/secret.gcp-existing-bucket.example.json
```

The runtime service account needs:

* `roles/secretmanager.secretAccessor` on `atlas-log-agent`
* `roles/storage.objectUser` on the log/report bucket
* `roles/aiplatform.user` in the project

## Required GCS layout

The agent expects raw compressed logs at this exact shape:

```text
<prefix>/<YYYY-MM-DD>/<host>/mongodb/<log-name>.gz
```

Example:

```text
atlas-logs/2026-09-26/node-0/mongodb/mongodb.gz
atlas-logs/2026-09-26/node-1/mongodb/mongodb.gz
atlas-logs/2026-09-26/node-2/mongodb/mongodb.gz
```

For a basic GCS check:

```bash
gcloud storage ls gs://my-existing-mongodb-log-bucket/atlas-logs/2026-09-26/
```

A successful run creates extracts under each node and reports under the date directory:

```text
atlas-logs/2026-09-26/node-0/extracts/mongodb/extractionOccurence.json
atlas-logs/2026-09-26/node-0/extracts/mongodb/extractionshort.json
atlas-logs/2026-09-26/node-0/extracts/mongodb/handoff.md
atlas-logs/2026-09-26/node-0/reports/mongodb/report.md
atlas-logs/2026-09-26/cluster/reports/manifest.json
```

## Local test: copied bucket data, no cloud access

This test uses a local directory as the object store. It does not use GCS, Secret Manager, Vertex AI, or Atlas.

1. Copy one or more raw log objects into a local bucket directory:

```bash
mkdir -p /tmp/mongodb-log-bucket/atlas-logs/2026-09-26/node-0
cp /path/to/mongodb.gz /tmp/mongodb-log-bucket/atlas-logs/2026-09-26/node-0/mongodb/mongodb.gz
```

2. Copy the local secret template and set its absolute `bucket` path if needed:

```bash
cp samples/secret.local-existing-bucket.example.json samples/secret.local.json
```

3. Set the runtime paths and run extraction only. This is the fully offline extractor test:

```bash
export CLOUD_PROVIDER=local
export ATLAS_CONFIG_FILE="$PWD/samples/secret.local.json"
export SKILLS_DIR="$PWD/skills"
export DIAG_SKILL_DIR="$PWD/skills/mongodb-log-diagnostic"

python3 -m agent.handler --stage extract --log-date 2026-09-26
```

4. Confirm the local extracts:

```bash
find /tmp/mongodb-log-bucket/atlas-logs/2026-09-26 -path '*extract/mongodb/*' -type f
```

The `report` and `all` stages call the configured LLM. For an entirely offline local test, run the included deterministic test instead:

```bash
python3 tests/test_multicloud.py
```

To generate actual reports locally, configure a real Vertex or Bedrock identity and set the secret `llm_provider` accordingly. Then run:

```bash
python3 -m agent.handler --stage report --log-date 2026-09-26
```

## Vertex ADK test

After deploying the ADK agent, submit this request through Agent Engine:

```text
Analyze existing bucket logs for 2026-09-26 without using Atlas API.
```

The ADK wrapper calls `run_existing_bucket_diagnostics("2026-09-26")`. It skips Atlas download, discovers the date’s existing GCS logs, generates extracts, uses D-2/D-8 extracts if present, and uploads reports.
