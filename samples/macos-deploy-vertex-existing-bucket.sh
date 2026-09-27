#!/usr/bin/env bash
# Interactive macOS / Cloud Shell deployment for the GCS existing-bucket mode.
# Run from the project root: ./samples/macos-deploy-vertex-existing-bucket.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
confirm() { read -r -p "$1 [y/N]: " answer; [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; }

for file in \
  skills/mongodb-log-diagnostic/SKILL.md \
  skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py \
  skills/mongodb-log-diagnostic/scripts/driver_compatibility.py \
  skills/mongodb-log-diagnostic/references/analysis-prompt.md \
  skills/mongodb-log-diagnostic/references/extracted-signal-reference.md; do
  [[ -f "$file" ]] || die "Missing vendored skill file: $file"
done

if ! command -v gcloud >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    note "Installing Google Cloud CLI through Homebrew"
    brew install --cask google-cloud-sdk
  else
    die "Install the Google Cloud CLI, authenticate with gcloud auth login, then rerun."
  fi
fi
if ! command -v adk >/dev/null 2>&1; then
  note "Creating deployment virtual environment and installing ADK dependencies"
  python3 -m venv .venv-deploy-adk
  # shellcheck disable=SC1091
  source .venv-deploy-adk/bin/activate
  python -m pip install --upgrade pip
  python -m pip install -r requirements-gcp.txt
fi
command -v adk >/dev/null 2>&1 || die "ADK CLI was not installed. Activate .venv-deploy-adk and retry."

note "Deployment configuration"
read -r -p "GCP project ID: " PROJECT
[[ -n "$PROJECT" ]] || die "Project ID is required."
read -r -p "Agent Engine region [us-central1]: " REGION
REGION="${REGION:-us-central1}"
read -r -p "GCS log/report bucket name (without gs://): " LOG_BUCKET
LOG_BUCKET="${LOG_BUCKET#gs://}"; LOG_BUCKET="${LOG_BUCKET%/}"
[[ -n "$LOG_BUCKET" ]] || die "Log bucket is required."
read -r -p "GCS prefix [atlas-logs]: " PREFIX
PREFIX="${PREFIX:-atlas-logs}"; PREFIX="${PREFIX#/}"; PREFIX="${PREFIX%/}"
read -r -p "Agent Engine staging bucket (without gs://, blank to create one): " STAGING_BUCKET
if [[ -z "$STAGING_BUCKET" ]]; then STAGING_BUCKET="${PROJECT}-mongodb-log-agent-staging"; fi
STAGING_BUCKET="${STAGING_BUCKET#gs://}"; STAGING_BUCKET="${STAGING_BUCKET%/}"
read -r -p "Secret Manager secret name [atlas-log-agent]: " SECRET_NAME
SECRET_NAME="${SECRET_NAME:-atlas-log-agent}"
read -r -p "Runtime service-account name [mongodb-log-agent]: " SA_NAME
SA_NAME="${SA_NAME:-mongodb-log-agent}"
RUNTIME_SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
read -r -p "Agent display name [mongodb-log-diagnostic]: " DISPLAY_NAME
DISPLAY_NAME="${DISPLAY_NAME:-mongodb-log-diagnostic}"
read -r -p "Report name [mongodb-cluster]: " REPORT_NAME
REPORT_NAME="${REPORT_NAME:-mongodb-cluster}"
read -r -p "Timezone [Asia/Jerusalem]: " TIMEZONE
TIMEZONE="${TIMEZONE:-Asia/Jerusalem}"
read -r -p "Vertex report model [claude-sonnet-5]: " VERTEX_MODEL
VERTEX_MODEL="${VERTEX_MODEL:-claude-sonnet-5}"
read -r -p "Vertex model location [global]: " VERTEX_LOCATION
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"

cat <<SUMMARY

Will deploy:
  project:          $PROJECT
  Agent Engine:     $REGION / $DISPLAY_NAME
  runtime account:  $RUNTIME_SA
  GCS log bucket:   gs://$LOG_BUCKET/$PREFIX
  staging bucket:   gs://$STAGING_BUCKET
  secret:           $SECRET_NAME (no Atlas credentials)
  report model:     $VERTEX_MODEL ($VERTEX_LOCATION)
SUMMARY
confirm "Continue and create/update cloud resources?" || { echo "Cancelled."; exit 0; }

note "Authenticating and enabling services"
gcloud auth login
gcloud config set project "$PROJECT"
gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com storage.googleapis.com iam.googleapis.com --project "$PROJECT"

note "Checking GCS buckets"
gcloud storage buckets describe "gs://$LOG_BUCKET" --project "$PROJECT" >/dev/null || die "Log bucket does not exist or is not accessible: gs://$LOG_BUCKET"
if ! gcloud storage buckets describe "gs://$STAGING_BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
  confirm "Staging bucket gs://$STAGING_BUCKET does not exist. Create it in $REGION?" || die "A staging bucket is required."
  gcloud storage buckets create "gs://$STAGING_BUCKET" --project "$PROJECT" --location "$REGION"
fi

note "Creating or finding the Agent Engine runtime service account"
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "MongoDB log diagnostic Agent Engine runtime"
fi

note "Granting runtime permissions"
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNTIME_SA" --role="roles/aiplatform.user" --quiet
gcloud storage buckets add-iam-policy-binding "gs://$LOG_BUCKET" --member="serviceAccount:$RUNTIME_SA" --role="roles/storage.objectUser"
gcloud storage buckets add-iam-policy-binding "gs://$STAGING_BUCKET" --member="serviceAccount:$RUNTIME_SA" --role="roles/storage.objectUser"

note "Creating GCS-only secret payload"
PAYLOAD="$(mktemp "${TMPDIR:-/tmp}/mongodb-log-agent-secret.XXXXXX.json")"
trap 'rm -f "$PAYLOAD"' EXIT
python3 - "$PAYLOAD" "$LOG_BUCKET" "$PREFIX" "$TIMEZONE" "$REPORT_NAME" "$VERTEX_MODEL" "$VERTEX_LOCATION" "$PROJECT" <<'PY'
import json, sys
out, bucket, prefix, timezone, report_name, model, location, project = sys.argv[1:]
payload = {
  "input_mode": "existing_bucket", "bucket": bucket, "prefix": prefix,
  "timezone": timezone, "report_name": report_name, "storage_provider": "gcs",
  "llm_provider": "vertex", "vertex_model": model, "vertex_location": location,
  "vertex_project": project, "slow_ms": 1000, "expected_node_count": 3,
  "report_max_tokens": 16000, "report_max_input_chars": 600000, "cluster_summary": True
}
with open(out, "w", encoding="utf-8") as handle: json.dump(payload, handle, indent=2)
PY
if gcloud secrets describe "$SECRET_NAME" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud secrets versions add "$SECRET_NAME" --project "$PROJECT" --data-file="$PAYLOAD"
else
  gcloud secrets create "$SECRET_NAME" --project "$PROJECT" --replication-policy=automatic --data-file="$PAYLOAD"
fi
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --project "$PROJECT" --member="serviceAccount:$RUNTIME_SA" --role="roles/secretmanager.secretAccessor"

note "Checking installed ADK deploy command"
adk deploy agent_engine --help >/dev/null || die "Your installed ADK CLI does not support 'adk deploy agent_engine'."

note "Deploying the ADK wrapper to Vertex AI Agent Engine"
PROJECT="$PROJECT" REGION="$REGION" STAGING_BUCKET="gs://$STAGING_BUCKET" \
RUNTIME_SA="$RUNTIME_SA" SECRET_NAME="$SECRET_NAME" DISPLAY_NAME="$DISPLAY_NAME" \
./deploy/gcp/deploy-adk-agent-engine.sh

note "Deployment submitted"
printf 'Test the deployed agent with: Analyze existing bucket logs for YYYY-MM-DD without using Atlas API.\n'
