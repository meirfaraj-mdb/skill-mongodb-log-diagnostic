# Local Atlas download, extraction, and Bedrock reports

Use this after the local Atlas test works. Atlas downloads and all logs, extracts, and reports remain in the local folder. Amazon Bedrock is called only to generate Markdown reports.

## Required AWS access

Your local AWS credentials must be able to identify the caller and invoke the approved Bedrock model or inference profile in the selected Region. No S3, Secrets Manager, Lambda, or Fargate permissions are needed for this local flow.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "bedrock:InvokeModel",
    "Resource": "*"
  }]
}
```

The selected model must already be enabled for the AWS account and Region. Use the exact model ID or inference-profile ID supplied by your AWS/Bedrock administrator.

## Run

```bash
chmod +x samples/macos-local-atlas-folder-test.sh
./samples/macos-local-atlas-folder-test.sh
```

When asked, select `yes` for Bedrock reports, then provide:

* Bedrock Region
* Approved model or inference-profile ID
* AWS profile name, or leave it empty to use default local credentials

The script installs `boto3`, verifies the active identity with STS, downloads Atlas logs, extracts one node/log at a time, and runs the report stage.

## Outputs

```text
<local-bucket>/<prefix>/<YYYY-MM-DD>/<node>/mongodb/<log>.gz
<local-bucket>/<prefix>/<YYYY-MM-DD>/<node>/extracts/<log>/...
<local-bucket>/<prefix>/<YYYY-MM-DD>/<node>/reports/<log>/report.md
<local-bucket>/<prefix>/<YYYY-MM-DD>/cluster/reports/cluster-summary.md
<local-bucket>/<prefix>/<YYYY-MM-DD>/cluster/reports/manifest.json
```

Reports use the bundled offline driver-CVE snapshot; they do not perform live CVE web lookups.

## Generate reports later

After extraction, set Bedrock properties in `.local-atlas-folder-test.json` as shown in `secret.local-atlas-folder.example.json`, then run:

```bash
source .venv-local-atlas-folder/bin/activate
export CLOUD_PROVIDER=local
export ATLAS_CONFIG_FILE="$PWD/.local-atlas-folder-test.json"
export SKILLS_DIR="$PWD/skills"
export DIAG_SKILL_DIR="$PWD/skills/mongodb-log-diagnostic"
export AWS_PROFILE="your-profile"  # omit if using default credentials
python -m agent.handler --stage report --log-date YYYY-MM-DD
```

**Defaults:** Bedrock uses `eu-west-1` (Ireland) and `eu.anthropic.claude-sonnet-5`. You may override either value when prompted if your organization provides a different approved inference-profile ID.

## Optional observability test

The local runner now asks whether to collect observability after extraction and before reports.
Choose `yes` to collect **Atlas Query Shape Insights for the previous 24 hours**. Enter the Atlas node hostnames as a comma-separated list. This creates, per node:

```text
<prefix>/<date>/<node>/queryStats/query-stats.json
```

The default source is `atlas_api`, so no direct MongoDB connection is needed. You may additionally choose direct `indexStats`; that requires a read-only MongoDB URI template containing `{host}`.
