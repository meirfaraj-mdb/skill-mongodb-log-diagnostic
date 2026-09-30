---
name: aws-storage
description: AWS object storage and secret access for pipelines - stream uploads/downloads/list/exists on Amazon S3 and JSON secret loading from AWS Secrets Manager, behind the same ObjectStore interface as gcp-storage. Use when an agent must push files (e.g. Atlas logs, extraction JSON, Markdown reports) to an S3 bucket or read a pipeline secret on AWS.
---

# AWS Storage

Provides the **ObjectStore contract** on Amazon S3 and a **secret loader** for AWS Secrets Manager.
`gcp-storage` implements the same contract, so callers can switch clouds without code changes.

## Operating contract
- Uses `boto3` (provided in the AWS Lambda runtime; `pip install boto3` elsewhere). Credentials come from the
  default chain (Lambda role, ECS task role, `AWS_PROFILE`, ...). Never pass keys on the command line.
- Secret values are returned to the caller only. The CLI prints secret **key names**, never values.
- `upload_stream` first writes the entire upstream HTTP response to a temporary local file, then uses boto3 `upload_file` (multipart/replayable) for S3. This avoids rereading a non-seekable Atlas response during S3 retries; it uses disk space equal to at least one compressed log. Temporary files are removed after success or failure. Set `TMPDIR` to a filesystem with sufficient free space for the largest log.

## ObjectStore contract (shared with gcp-storage)
| Method | Meaning |
|---|---|
| `exists(key) -> bool` | Object exists (404 → False; other errors raise) |
| `list_keys(prefix) -> list[str]` | All keys under prefix (paginated) |
| `download(key, path)` | Download to a local file (creates parent dirs) |
| `upload(path, key, content_type, metadata=None) -> uri` | Upload a local file |
| `upload_stream(fileobj, key, content_type, metadata=None) -> uri` | Stage an unknown-length stream to disk, then upload to S3 |
| `put_text(key, text, content_type) -> uri` / `get_text(key) -> str` | Small UTF-8 objects |
| `uri(key) -> str` | `s3://bucket/key` |

Metadata values are stored as S3 user metadata (`x-amz-meta-*`).

## Secrets
`load_secret(secret_id) -> dict` reads `SecretString` (or base64 `SecretBinary`) and parses JSON.
`secret_id` is an ARN or a secret name.

## IAM (least privilege)
`secretsmanager:GetSecretValue` on the secret; `s3:ListBucket` on the bucket; `s3:GetObject`,
`s3:PutObject` on `bucket/prefix/*`.

## CLI
```bash
python3 scripts/aws_storage.py ls s3://bucket/prefix/2026-09-22/
python3 scripts/aws_storage.py cp ./report.md s3://bucket/prefix/2026-09-22/reports/report.md
python3 scripts/aws_storage.py cp s3://bucket/key ./local.json
python3 scripts/aws_storage.py exists s3://bucket/key
python3 scripts/aws_storage.py secret-keys arn:aws:secretsmanager:region:acct:secret:name
```
