# macOS local AWS + Atlas test

This guide runs the project on a Mac while using real AWS services: an isolated Amazon S3 bucket/prefix for test output, AWS Secrets Manager for the Atlas configuration, and optionally Amazon Bedrock for reports. The Atlas API downloads the requested day’s logs; the local process extracts them and uploads the raw logs and extracts to S3.

It is a functional integration test. It can incur Atlas, S3, Secrets Manager, and (if selected) Bedrock charges. Run it only against an Atlas project and S3 account you are authorized to use.

## What the test does

1. Creates a disposable S3 bucket or uses a bucket you specify.
2. Reads an existing AWS Secrets Manager secret, or creates a dedicated temporary test secret from values you enter.
3. Uses Atlas Admin API credentials in that secret to download daily process logs.
4. Uploads raw logs to `s3://<bucket>/<prefix>/<date>/<node>/mongodb/<log>.gz`.
5. Extracts one node/log at a time. It uploads the three extraction files before continuing to the next node/log.
6. Optionally invokes Bedrock to generate node and cluster reports.
7. Offers to delete the dedicated secret and the bucket it created. It never deletes an existing bucket automatically.

The script does not enable observability collection. It does not connect to MongoDB nodes for `$indexStats` or call Atlas Query Shape Insights.

## Prerequisites

* macOS, Python 3, and Homebrew if the script needs to install AWS CLI.
* AWS credentials configured through `aws configure`, `aws sso login`, or a valid named profile.
* An Atlas API key with permission to download logs in the target Atlas project.
* The vendored diagnostic skill files present in `skills/mongodb-log-diagnostic/`, including `scripts/` and `references/`.
* For reports: Bedrock model access and a valid inference-profile or model ID.

Your AWS principal needs these permissions for the test scope:

* `sts:GetCallerIdentity`
* `s3:CreateBucket`, `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`; add `s3:DeleteObject` and `s3:DeleteBucket` only if you want the script’s optional cleanup
* `secretsmanager:GetSecretValue`; add `secretsmanager:CreateSecret` and `secretsmanager:DeleteSecret` only when creating the dedicated test secret
* `bedrock:InvokeModel` only when generating reports

## Secret format

Use [secret.aws-atlas-local-test.example.json](secret.aws-atlas-local-test.example.json) as the template for an AWS Secrets Manager secret. Replace every placeholder before creating it.

Required Atlas fields are `atlas_public_key`, `atlas_private_key`, `group_id`, `cluster_name`, `timezone`, and `api_version`. The secret also specifies the target S3 bucket and isolated prefix.

Do not commit a populated secret file. The bootstrap creates a temporary file with mode `0600` only when you choose to create a dedicated test secret, uploads it to Secrets Manager, and removes the local copy.

## Run

From the project root:

```bash
chmod +x samples/macos-local-aws-atlas-test.sh
./samples/macos-local-aws-atlas-test.sh
```

The script prompts for:

* AWS profile and region
* new disposable S3 bucket or an existing test bucket
* an isolated S3 prefix and log date
* existing secret ARN/name, or Atlas credentials to create a dedicated test secret
* whether to generate Bedrock reports and, if so, its inference-profile/model ID

For an extract-only test, answer `n` to Bedrock reports. This runs `download` and then `extract`. For report testing, answer `y`; it runs `all`.

## Expected S3 layout

For each Atlas node and raw log type:

```text
s3://<bucket>/<prefix>/<YYYY-MM-DD>/<node>/
├── mongodb/<log-name>.gz
├── extracts/<log-name>/
│   ├── extractionOccurence.json
│   ├── extractionshort.json
│   └── handoff.md
└── reports/<log-name>/                  # only when Bedrock reports run
    ├── report.md
    └── diff.json
```

Cluster-level report files, when enabled, are written to:

```text
s3://<bucket>/<prefix>/<YYYY-MM-DD>/cluster/reports/
```

Rerunning extraction skips a node/log when `extractionOccurence.json` already exists. To rerun it manually, use `--force-reextract`.

## Existing secret mode

If you select an existing secret, its `bucket` and `prefix` control where the pipeline writes. The bucket/prefix prompts still help identify the test target, but the values inside the secret are authoritative. Use a dedicated test prefix in the secret; do not point a local test at production output paths.

## Cleanup

At the end, the script asks before deleting a dedicated test secret or a bucket it created. Choose cleanup only after confirming the test results. AWS deletion may remain visible for a short time due to service propagation.

**Defaults:** Bedrock uses `eu-west-1` (Ireland) and `eu.anthropic.claude-sonnet-5`. You may override either value when prompted if your organization provides a different approved inference-profile ID.
