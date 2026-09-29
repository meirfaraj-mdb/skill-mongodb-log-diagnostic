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

Select `yes` for Vertex reports. The script runs `gcloud auth application-default login`; complete that browser login with the Google identity that has Vertex access.

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
