# Local Atlas-to-folder test (macOS)

Use this test when you want to exercise the real Atlas log-download API while keeping all storage local. The local folder emulates an S3 or GCS bucket. It does not authenticate to AWS or Google Cloud, create a cloud resource, call Bedrock/Vertex, generate reports, or run index/query statistics.

## What it runs

```text
Atlas Admin API → local bucket folder → local extractor → local extracts
```

The download and extraction stages run separately. Extraction processes one node/log at a time and skips an already completed extraction unless you later run the CLI with `--force-reextract`.

## Prerequisites

* macOS with Python 3.10 or later.
* An Atlas programmatic API key that can download logs for the target project/cluster.
* The complete vendored skills in this project:

```text
skills/mongodb-atlas-logs/scripts/atlas_logs.py
skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py
skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py
skills/mongodb-log-diagnostic/scripts/driver_compatibility.py
skills/mongodb-log-diagnostic/references/analysis-prompt.md
skills/mongodb-log-diagnostic/references/extracted-signal-reference.md
```

No AWS CLI, AWS credentials, Google Cloud CLI, or cloud account permissions are required.

## Run

From the project root:

```bash
chmod +x samples/macos-local-atlas-folder-test.sh
./samples/macos-local-atlas-folder-test.sh
```

The script prompts for:

* a local bucket folder, for example `/Users/meir.faraj/Downloads/mongodb-log-local-bucket`;
* storage prefix (default `atlas-logs`);
* log date, or blank for D-1 in the chosen timezone;
* Atlas cluster name and project/group ID;
* Atlas API public key and private key; and
* log type selection (default `auto`).

It creates `.venv-local-atlas-folder/` and a local, permission-restricted `.local-atlas-folder-test.json`. The config contains the private key; do not commit or share it.

## Result layout

For a date of `2026-09-26`, outputs look like:

```text
<LOCAL_BUCKET>/
└── atlas-logs/
    └── 2026-09-26/
        └── <node-hostname>/
            ├── mongodb/
            │   └── mongodb.gz
            └── extracts/
                └── mongodb/
                    ├── extractionOccurence.json
                    ├── extractionshort.json
                    └── handoff.md
```

The script prints every raw-log and extraction file it creates. It does not create `reports/`, `indexStats/`, or `queryStats/` paths.

## Equivalent manual commands

After the script has created the config:

```bash
source .venv-local-atlas-folder/bin/activate
export CLOUD_PROVIDER=local
export ATLAS_CONFIG_FILE="$PWD/.local-atlas-folder-test.json"
export SKILLS_DIR="$PWD/skills"
export DIAG_SKILL_DIR="$PWD/skills/mongodb-log-diagnostic"

python -m agent.handler --stage download --log-date 2026-09-26
python -m agent.handler --stage extract --log-date 2026-09-26
```

To regenerate an existing extraction:

```bash
python -m agent.handler --stage extract --log-date 2026-09-26 --force-reextract
```

## Config template

See [secret.local-atlas-folder.example.json](secret.local-atlas-folder.example.json). It is a local config file, not a cloud secret. Keep the `storage_provider` as `local` and `llm_provider` as `null` for this test.
