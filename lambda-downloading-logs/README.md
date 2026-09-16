# MongoDB Atlas Log Archiver Lambda

This package contains a Python AWS Lambda that downloads the previous calendar day's MongoDB Atlas host logs and streams them into Amazon S3.

## Lambda configuration

Runtime: Python 3.12

Environment variable:

```text
ATLAS_SECRET_ID=arn:aws:secretsmanager:<region>:<account>:secret:<name>
```

The Lambda execution role needs:

- `secretsmanager:GetSecretValue`
- `s3:PutObject` on the destination bucket

## Secret value

```json
{
  "atlas_public_key": "YOUR_ATLAS_PUBLIC_KEY",
  "atlas_private_key": "YOUR_ATLAS_PRIVATE_KEY",
  "group_id": "YOUR_24_CHARACTER_PROJECT_ID",
  "cluster_name": "my-cluster",
  "s3_bucket": "my-atlas-log-bucket",
  "s3_prefix": "atlas-logs",
  "timezone": "Asia/Jerusalem",
  "api_version": "2025-03-12",
  "log_names": ["auto"],
  "host_selector": "^my-cluster-",
  "http_timeout_seconds": 120,
  "max_retries": 5
}
```

Use `log_names: ["mongodb-audit-log"]` or `log_names: ["mongos-audit-log"]` for audit logs when database auditing is enabled.

## Scheduling

Create an EventBridge schedule that runs once per day, preferably after midnight in the configured timezone.

## Deployment

Upload `atlas-log-archiver-lambda.zip` to Lambda and set the handler to:

```text
lambda_function.lambda_handler
```

The package uses only the Python standard library and the AWS Lambda-provided `boto3` package.
