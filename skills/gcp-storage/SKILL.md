---
name: gcp-storage
description: Google Cloud object storage and secret access for pipelines - stream uploads/downloads/list/exists on Google Cloud Storage and JSON secret loading from Google Secret Manager, behind the same ObjectStore interface as aws-storage. Use when an agent must push files (e.g. Atlas logs, extraction JSON, Markdown reports) to a GCS bucket or read a pipeline secret on Google Cloud.
---

# GCP Storage

Provides the **ObjectStore contract** on Google Cloud Storage and a **secret loader** for Google Secret Manager.
It is a drop-in equivalent of `aws-storage`.

## Operating contract
- Uses `google-cloud-storage` and `google-cloud-secret-manager`
  (`pip install google-cloud-storage google-cloud-secret-manager`). Credentials come from Application Default
  Credentials (the Cloud Run / Cloud Functions service account, or `gcloud auth application-default login`).
- Secret values are returned to the caller only. The CLI prints secret **key names**, never values.
- Stream uploads use a resumable `BlobWriter` (8 MiB chunks), so multi-GB gzip logs are never held in memory.

## ObjectStore contract (identical to aws-storage)
| Method | Meaning |
|---|---|
| `exists(key) -> bool` | Object exists |
| `list_keys(prefix) -> list[str]` | All object names under prefix |
| `download(key, path)` | Download to a local file (creates parent dirs) |
| `upload(path, key, content_type, metadata=None) -> uri` | Upload a local file |
| `upload_stream(fileobj, key, content_type, metadata=None) -> uri` | Streamed resumable upload |
| `put_text(key, text, content_type) -> uri` / `get_text(key) -> str` | Small UTF-8 objects |
| `uri(key) -> str` | `gs://bucket/key` |

Metadata values are stored as GCS custom metadata.

## Secrets
`load_secret(secret_id) -> dict` accepts any of the following and parses the payload as JSON:
- `projects/<p>/secrets/<name>/versions/<v>`
- `projects/<p>/secrets/<name>` (uses `latest`)
- a bare `<name>` (uses `$GOOGLE_CLOUD_PROJECT` and `latest`)

## IAM (least privilege)
- `roles/secretmanager.secretAccessor` on the secret.
- `roles/storage.objectUser` on the bucket, or `roles/storage.objectViewer` + `roles/storage.objectCreator`.
  Listing is required.

## CLI
```bash
python3 scripts/gcp_storage.py ls gs://bucket/prefix/2026-09-22/
python3 scripts/gcp_storage.py cp ./report.md gs://bucket/prefix/2026-09-22/reports/report.md
python3 scripts/gcp_storage.py cp gs://bucket/key ./local.json
python3 scripts/gcp_storage.py exists gs://bucket/key
python3 scripts/gcp_storage.py secret-keys projects/my-proj/secrets/atlas-log-agent
```
