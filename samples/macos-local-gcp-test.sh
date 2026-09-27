#!/usr/bin/env bash
# macOS local bootstrap for the GCP/Vertex existing-bucket diagnostic flow.
# Run from the project root: ./samples/macos-local-gcp-test.sh
# This never calls the Atlas API. It uses a local folder as the object store.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

say_note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

[[ "$(uname -s)" == "Darwin" ]] || die "This bootstrap is for macOS."
need python3
need cp

# The extractor is deliberately checked before setting up cloud credentials.
for required in \
  skills/mongodb-log-diagnostic/SKILL.md \
  skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py \
  skills/mongodb-log-diagnostic/scripts/driver_compatibility.py \
  skills/mongodb-log-diagnostic/references/analysis-prompt.md \
  skills/mongodb-log-diagnostic/references/extracted-signal-reference.md; do
  [[ -f "$required" ]] || die "Missing vendored skill file: $required"
done

say_note "Local bucket configuration"
read -r -p "Local bucket folder (for example /Users/me/mongodb-log-bucket): " LOCAL_BUCKET
[[ -n "$LOCAL_BUCKET" ]] || die "A local bucket folder is required."
LOCAL_BUCKET="${LOCAL_BUCKET/#\~/$HOME}"
mkdir -p "$LOCAL_BUCKET"
LOCAL_BUCKET="$(cd "$LOCAL_BUCKET" && pwd)"

read -r -p "Log date [YYYY-MM-DD]: " LOG_DATE
[[ "$LOG_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "Log date must be YYYY-MM-DD."
read -r -p "GCP project ID (required for real Vertex report generation; leave blank for extract-only): " GCP_PROJECT
read -r -p "Vertex location [global]: " VERTEX_LOCATION
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
read -r -p "Run mode: extract only (e) or extract + Vertex reports (r) [e]: " MODE
MODE="${MODE:-e}"
[[ "$MODE" == "e" || "$MODE" == "r" ]] || die "Choose e or r."
[[ "$MODE" == "e" || -n "$GCP_PROJECT" ]] || die "A GCP project ID is required for reports."

say_note "Installing Google Cloud CLI if needed"
if ! command -v gcloud >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install --cask google-cloud-sdk
  else
    cat >&2 <<'MSG'
Homebrew is required to install the Google Cloud CLI automatically.
Install Homebrew from https://brew.sh, reopen Terminal, then rerun this script.
MSG
    exit 1
  fi
fi
gcloud --version

say_note "Creating Python virtual environment"
python3 -m venv .venv-local-gcp
# shellcheck disable=SC1091
source .venv-local-gcp/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-gcp.txt

say_note "Creating local existing-bucket configuration"
CONFIG_FILE="$PROJECT_ROOT/.local-gcp-existing-bucket.json"
python - "$CONFIG_FILE" "$LOCAL_BUCKET" "$GCP_PROJECT" "$VERTEX_LOCATION" <<'PY'
import json, sys
path, bucket, project, location = sys.argv[1:]
config = {
  "input_mode": "existing_bucket",
  "bucket": bucket,
  "prefix": "atlas-logs",
  "timezone": "Asia/Jerusalem",
  "report_name": "local-mongodb-log-test",
  "storage_provider": "local",
  "llm_provider": "vertex",
  "vertex_model": "claude-sonnet-5",
  "vertex_location": location,
  "slow_ms": 1000,
  "expected_node_count": 3,
  "report_max_tokens": 16000,
  "report_max_input_chars": 600000,
  "cluster_summary": True
}
if project:
  config["vertex_project"] = project
with open(path, "w", encoding="utf-8") as f:
  json.dump(config, f, indent=2)
PY
chmod 600 "$CONFIG_FILE"

DAY_DIR="$LOCAL_BUCKET/atlas-logs/$LOG_DATE"
say_note "Checking local log layout"
printf 'Expected input layout: %s/<host>/<log-name>.gz\n' "$DAY_DIR"
if ! find "$DAY_DIR" -mindepth 2 -maxdepth 2 -type f -name '*.gz' -print -quit 2>/dev/null | grep -q .; then
  cat >&2 <<MSG
No .gz log was found. Copy raw logs into, for example:
  $DAY_DIR/node-0/mongodb.gz
Then rerun this script.
MSG
  exit 1
fi
find "$DAY_DIR" -mindepth 2 -maxdepth 2 -type f -name '*.gz' -print

export CLOUD_PROVIDER=local
export ATLAS_CONFIG_FILE="$CONFIG_FILE"
export SKILLS_DIR="$PROJECT_ROOT/skills"
export DIAG_SKILL_DIR="$PROJECT_ROOT/skills/mongodb-log-diagnostic"
export GOOGLE_CLOUD_PROJECT="$GCP_PROJECT"

if [[ "$MODE" == "r" ]]; then
  say_note "Authenticating Application Default Credentials for Vertex"
  gcloud config set project "$GCP_PROJECT"
  gcloud auth application-default login
  say_note "Running extract and Vertex report stages"
  python -m agent.handler --stage all --log-date "$LOG_DATE"
else
  say_note "Running fully local extract stage"
  python -m agent.handler --stage extract --log-date "$LOG_DATE"
fi

say_note "Results"
find "$DAY_DIR" -path '*/extract/mongodb/*' -type f -print
if [[ "$MODE" == "r" ]]; then
  find "$DAY_DIR/reports" -type f -print 2>/dev/null || true
fi
printf '\nFinished. Local config (not committed): %s\n' "$CONFIG_FILE"
