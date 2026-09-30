# Local Atlas download and Vertex report test

This test downloads logs from Atlas, lets you choose a local folder, an existing S3 bucket, or an existing GCS bucket, extracts each node/log sequentially, and optionally calls Vertex AI to write reports back to the selected storage. It does not require Secret Manager or Agent Engine. See [storage prerequisites and permissions](local-atlas-folder-test.md).

## Prerequisites

* A working local Atlas download test and the complete vendored skills.
* Google Cloud CLI (`gcloud`) installed.
* A billed GCP project with Vertex AI API enabled.
* Your Google identity has the Vertex AI User role (or equivalent permission to invoke the approved Claude model).
* Access to the approved Vertex Claude Sonnet 5 model.

## Run

```bash
chmod +x samples/macos-local-atlas-vertex-test.sh
./samples/macos-local-atlas-vertex-test.sh
```

Select `vertex` for reports (the default is `none`). The script runs `gcloud auth application-default login`; complete that browser login with the Google identity that has Vertex access.

Defaults:

* Vertex location: `global`
* Model: `claude-sonnet-5`

## Output

```text
<selected-storage>/<prefix>/<date>/<node>/mongodb/<log>.gz
<selected-storage>/<prefix>/<date>/<node>/extracts/<log>/...
<selected-storage>/<prefix>/<date>/<node>/reports/<log>/report.md
<selected-storage>/<prefix>/<date>/cluster/reports/cluster-summary.md
<selected-storage>/<prefix>/<date>/cluster/reports/manifest.json
```

The report uses the bundled offline driver-CVE catalog. A local config file named `.local-atlas-vertex-test.json` contains Atlas credentials; do not commit it.

## Optional observability test

The runner can collect Atlas Query Shape Insights after extraction and before optional Vertex reports. Choose `yes`, enter the Atlas node hostnames, and keep the default `atlas_api` source to retrieve the previous 24 hours through the existing Atlas API key. Outputs go to the selected storage:

```text
<prefix>/<date>/<node>/queryStats/query-stats.json
```

Direct `indexStats` is optional and requires a read-only MongoDB URI template with `{host}`.

## Use direct Claude instead of Vertex

Choose `anthropic` at the report-provider prompt. Supply `ANTHROPIC_API_KEY` through your shell or the hidden prompt and select the Anthropic API model ID available to your account. The proposed default is `claude-sonnet-5`; change it if your Anthropic account uses a different identifier. This path installs the Anthropic SDK, requires no `gcloud` login, and keeps the selected storage. The key is never written to `.local-atlas-vertex-test.json`.

## Use your local Claude Code CLI (no API key)

Install Claude Code on your Mac and run `claude` once to sign in. Select `claude_cli` at the report-provider prompt. Leave the model blank to use your CLI default, or enter a model accepted by your installed CLI. No `ANTHROPIC_API_KEY` or cloud model credentials are used for report generation in this option; S3/GCS storage still requires its own credentials. It still contacts Claude over the network; this is **not offline inference**. The CLI runs in a temporary directory with tools disabled; verify your organization's policy permits sending the extracted diagnostic data to your Claude account. Atlas access is still needed for the download stage.

To rerun only reports, set `llm_provider` to `claude_cli` in the script's generated `.local-atlas-*-test.json`, activate the corresponding virtual environment, and run `python -m agent.handler --stage report --log-date YYYY-MM-DD` with `CLOUD_PROVIDER=local`, `ATLAS_CONFIG_FILE`, and `DIAG_SKILL_DIR` set as in the local test guide.
