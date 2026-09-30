#!/usr/bin/env bash
# Shared storage selection for the two interactive macOS Atlas test runners.
# Source this file after defining prompt, note, and die.
choose_atlas_storage() {
  note "Storage configuration"
  prompt STORAGE_PROVIDER "Storage (local/s3/gcs)" "local"
  STORAGE_PROVIDER=$(printf '%s' "$STORAGE_PROVIDER" | tr '[:upper:]' '[:lower:]')
  [[ "$STORAGE_PROVIDER" == local || "$STORAGE_PROVIDER" == s3 || "$STORAGE_PROVIDER" == gcs ]] || die "Choose local, s3, or gcs."
  LOCAL_BUCKET=""; BUCKET=""; STORAGE_AWS_PROFILE=""; STORAGE_AWS_REGION=""; STORAGE_GCP_PROJECT=""
  case "$STORAGE_PROVIDER" in
    local)
      prompt LOCAL_BUCKET "Local folder to emulate the bucket" "$HOME/Downloads/mongodb-log-local-bucket"
      mkdir -p "$LOCAL_BUCKET"; LOCAL_BUCKET="$(cd "$LOCAL_BUCKET" && pwd)"; BUCKET="$LOCAL_BUCKET"
      ;;
    s3)
      prompt BUCKET "Existing S3 bucket name (no s3:// or prefix)" ""
      prompt STORAGE_AWS_REGION "S3 bucket region" "eu-west-1"
      prompt STORAGE_AWS_PROFILE "AWS profile (blank uses default credentials)" ""
      ;;
    gcs)
      prompt BUCKET "Existing GCS bucket name (no gs:// or prefix)" ""
      prompt STORAGE_GCP_PROJECT "GCP project ID for storage authentication" ""
      [[ -n "$STORAGE_GCP_PROJECT" ]] || die "GCP project ID is required for GCS."
      ;;
  esac
  [[ -n "$BUCKET" ]] || die "Bucket is required."
  if [[ "$STORAGE_PROVIDER" != local ]]; then
    [[ "$BUCKET" != *://* && "$BUCKET" != */* && "$BUCKET" != *' '* ]] || die "Enter bucket name only, not URI or path."
  fi
  prompt PREFIX "Bucket prefix (choose an isolated test prefix for real buckets)" "atlas-logs"
  PREFIX="${PREFIX#/}"; PREFIX="${PREFIX%/}"
  [[ -n "$PREFIX" && "$PREFIX" != *'..'* && "$PREFIX" != *' '* ]] || die "Use a non-empty, safe prefix."
}

prepare_atlas_storage() {
  case "$STORAGE_PROVIDER" in
    local) return ;;
    s3)
      python -m pip install 'boto3>=1.34'
      [[ -z "$STORAGE_AWS_PROFILE" ]] || export AWS_PROFILE="$STORAGE_AWS_PROFILE"
      export AWS_DEFAULT_REGION="$STORAGE_AWS_REGION" AWS_REGION="$STORAGE_AWS_REGION"
      note "Checking AWS credentials and S3 access"
      python - "$BUCKET" "$PREFIX" <<'PY'
import boto3, sys
bucket, prefix = sys.argv[1:]
print('AWS identity:', boto3.client('sts').get_caller_identity()['Arn'])
s3 = boto3.client('s3')
s3.list_objects_v2(Bucket=bucket, Prefix=prefix+'/', MaxKeys=1)
print('S3 list access OK:', 's3://'+bucket+'/'+prefix+'/')
PY
      ;;
    gcs)
      command -v gcloud >/dev/null || die "gcloud is required for GCS. Install Google Cloud CLI and rerun."
      python -m pip install 'google-cloud-storage>=2.14' 'google-auth>=2.27'
      export GOOGLE_CLOUD_PROJECT="$STORAGE_GCP_PROJECT"
      note "Checking Google Application Default Credentials for GCS"
      if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
        gcloud auth application-default login
      fi
      python - "$BUCKET" "$PREFIX" <<'PY'
from google.cloud import storage
import sys
bucket, prefix = sys.argv[1:]
client = storage.Client()
next(iter(client.list_blobs(bucket, prefix=prefix+'/', max_results=1)), None)
print('GCS list access OK:', 'gs://'+bucket+'/'+prefix+'/')
PY
      ;;
  esac
}

confirm_atlas_storage() {
  [[ "$STORAGE_PROVIDER" == local ]] && return
  local uri reply
  if [[ "$STORAGE_PROVIDER" == s3 ]]; then uri="s3://$BUCKET/$PREFIX/"; else uri="gs://$BUCKET/$PREFIX/"; fi
  printf '\nDownload/extract/observability/reports may write to %s (existing complete outputs are skipped).\n' "$uri"
  read -r -p 'Confirm real bucket target (yes/no) [no]: ' reply
  [[ "$reply" == yes ]] || die "Cancelled; no objects were written."
}

show_atlas_storage_outputs() {
  local day="$1"
  if [[ "$STORAGE_PROVIDER" == local ]]; then
    note "Files under $BUCKET/$PREFIX/$day"
    find "$BUCKET/$PREFIX/$day" -type f \( -path '*/mongodb/*.gz' -o -path '*/extracts/*/*' -o -path '*/reports/*/*' -o -path '*/indexStats/*' -o -path '*/queryStats/*' -o -path '*/cluster/reports/*' \) -print 2>/dev/null || true
  else
    note "Objects under $BUCKET/$PREFIX/$day"
    python - "$PREFIX/$day/" <<'PY'
from agent.providers import load_config, get_store
import sys
store = get_store(load_config())
for key in store.list_keys(sys.argv[1]):
    print(store.uri(key))
PY
  fi
}
