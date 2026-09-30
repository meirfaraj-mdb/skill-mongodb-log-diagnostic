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

When asked for a report provider, select `bedrock` (the default is `none`), then provide:

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

## Use direct Claude instead

Select `anthropic` at the report-provider prompt. Set `ANTHROPIC_API_KEY` in your shell or enter it at the hidden prompt. No AWS or Google login is needed for this option. The key is not written to the local JSON config; only `anthropic_model` is saved. The script installs the Anthropic SDK when selected. Use an Anthropic API model ID available to your account (the proposed default is `claude-sonnet-5`; override it if needed). The Atlas download and all generated files remain local.

To rerun only reporting after the script exits, export the key in your terminal, set `llm_provider` to `anthropic` and `anthropic_model` in `.local-atlas-folder-test.json`, then run the report-only command above.

## Use your local Claude Code CLI (no API key)

Install Claude Code on your Mac and run `claude` once to sign in. Select `claude_cli` at the report-provider prompt. Leave the model blank to use your CLI default, or enter a model accepted by your installed CLI. No `ANTHROPIC_API_KEY`, AWS, or GCP credentials are used for report generation in this option. It still contacts Claude over the network; this is **not offline inference**. The CLI runs in a temporary directory with tools disabled; verify your organization's policy permits sending the extracted diagnostic data to your Claude account. Atlas access is still needed for the download stage.

To rerun only reports, set `llm_provider` to `claude_cli` in the script's generated `.local-atlas-*-test.json`, activate the corresponding virtual environment, and run `python -m agent.handler --stage report --log-date YYYY-MM-DD` with `CLOUD_PROVIDER=local`, `ATLAS_CONFIG_FILE`, and `DIAG_SKILL_DIR` set as in the local test guide.

### Reuse interactive choices

Both macOS Atlas runners can save the chosen non-secret settings to `~/.localsample` after choosing the storage target. Answer `yes` to the save prompt. The next run shows them as editable prompt defaults. The file is JSON with owner-only (`0600`) permissions; you may inspect or remove it with `cat ~/.localsample` or `rm ~/.localsample`. Atlas API keys, Anthropic API keys, MongoDB connection URIs, and log dates are **not** stored, so you must supply credentials each time (the date defaults to yesterday). Existing local run config files still contain Atlas credentials; keep those private.
