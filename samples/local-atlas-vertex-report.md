# Local Atlas download and Vertex report test

This test downloads logs from Atlas, uses a local directory as the simulated bucket, extracts each node/log sequentially, and optionally calls Vertex AI to write reports back to that same local directory. It does not use GCS, Secret Manager, Agent Engine, AWS, or Bedrock.

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
<local-bucket>/<prefix>/<date>/<node>/mongodb/<log>.gz
<local-bucket>/<prefix>/<date>/<node>/extracts/<log>/...
<local-bucket>/<prefix>/<date>/<node>/reports/<log>/report.md
<local-bucket>/<prefix>/<date>/cluster/reports/cluster-summary.md
<local-bucket>/<prefix>/<date>/cluster/reports/manifest.json
```

The report uses the bundled offline driver-CVE catalog. A local config file named `.local-atlas-vertex-test.json` contains Atlas credentials; do not commit it.

## Optional observability test

The runner can collect Atlas Query Shape Insights after extraction and before optional Vertex reports. Choose `yes`, enter the Atlas node hostnames, and keep the default `atlas_api` source to retrieve the previous 24 hours through the existing Atlas API key. Outputs are local:

```text
<prefix>/<date>/<node>/queryStats/query-stats.json
```

Direct `indexStats` is optional and requires a read-only MongoDB URI template with `{host}`.

## Use direct Claude instead of Vertex

Choose `anthropic` at the report-provider prompt. Supply `ANTHROPIC_API_KEY` through your shell or the hidden prompt and select the Anthropic API model ID available to your account. The proposed default is `claude-sonnet-5`; change it if your Anthropic account uses a different identifier. This path installs the Anthropic SDK, requires no `gcloud` login, and keeps all storage local. The key is never written to `.local-atlas-vertex-test.json`.
