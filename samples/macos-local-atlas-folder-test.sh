#!/usr/bin/env bash
# macOS: Atlas API -> local folder / S3 / GCS -> extraction -> optional reports.
set -euo pipefail
umask 077  # credential file contains Atlas API keys

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
prompt() { local var="$1" label="$2" default="${3:-}" value; if [[ "$var" != LOG_DATE ]] && declare -f sample_default >/dev/null; then default="$(sample_default "$var" "$default")"; fi; read -r -p "$label${default:+ [$default]}: " value; printf -v "$var" '%s' "${value:-$default}"; }
[[ "$(uname -s)" == Darwin ]] || die "This script is for macOS."
command -v python3 >/dev/null || die "python3 is required."

source "$PROJECT_ROOT/samples/local-atlas-storage.sh"
# Prompt to restore missing skill files from the repository before collecting credentials.
if [[ ! -f skills/mongodb-atlas-logs/scripts/atlas_logs.py ||
      ! -f skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py ||
      ! -f skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py ||
      ! -f skills/mongodb-log-diagnostic/scripts/driver_compatibility.py ]]; then
  read -r -p "Skill files missing. Restore both skills from GitHub now? (yes/no) [no]: " restore_skills
  if [[ "$(printf '%s' "${restore_skills:-no}" | tr '[:upper:]' '[:lower:]')" == yes ]]; then
    "$PROJECT_ROOT/samples/restore-vendored-skills.sh"
  fi
fi
for f in skills/mongodb-atlas-logs/scripts/atlas_logs.py \
  skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py \
  skills/mongodb-log-diagnostic/scripts/driver_compatibility.py \
  skills/mongodb-log-diagnostic/references/analysis-prompt.md; do
  [[ -f "$f" ]] || die "Missing vendored skill file: $f (run samples/restore-vendored-skills.sh)"
done

choose_atlas_storage
case "$STORAGE_PROVIDER" in
  s3) storage_module=skills/aws-storage/scripts/aws_storage.py ;;
  gcs) storage_module=skills/gcp-storage/scripts/gcp_storage.py ;;
  *) storage_module="" ;;
esac
if [[ -n "$storage_module" && ! -f "$storage_module" ]]; then
  read -r -p "Storage skill missing ($storage_module). Restore skills from GitHub now? (yes/no) [no]: " restore_storage
  if [[ "$(printf '%s' "${restore_storage:-no}" | tr '[:upper:]' '[:lower:]')" == yes ]]; then
    "$PROJECT_ROOT/samples/restore-vendored-skills.sh"
  fi
  [[ -f "$storage_module" ]] || die "Missing vendored storage file: $storage_module"
fi
prompt LOG_DATE "Log date (YYYY-MM-DD; blank means yesterday)" ""
[[ -z "$LOG_DATE" || "$LOG_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "Log date must be YYYY-MM-DD."
prompt TIMEZONE "Timezone used for yesterday" "Asia/Jerusalem"

note "Atlas API configuration"
prompt CLUSTER_NAME "Atlas cluster name" ""
prompt GROUP_ID "Atlas project (group) ID" ""
prompt ATLAS_PUBLIC_KEY "Atlas API public key" ""
read -r -s -p "Atlas API private key (input hidden): " ATLAS_PRIVATE_KEY; printf '\n'
[[ -n "$CLUSTER_NAME" && -n "$GROUP_ID" && -n "$ATLAS_PUBLIC_KEY" && -n "$ATLAS_PRIVATE_KEY" ]] || die "Atlas values are required."
prompt LOG_NAMES "Log names (comma-separated: auto,mongodb,mongos)" "auto"
prompt ENABLE_OBSERVABILITY "Collect Atlas Query Shape Insights for the last 24 hours? (yes/no)" "no"
ENABLE_OBSERVABILITY=$(printf '%s' "$ENABLE_OBSERVABILITY" | tr '[:upper:]' '[:lower:]')
[[ "$ENABLE_OBSERVABILITY" == yes || "$ENABLE_OBSERVABILITY" == no ]] || die "Enter yes or no."
OBS_HOSTS=""; QUERY_SHAPE_SOURCE="disabled"; INDEX_STATS_ENABLED="no"; MONGODB_URI_TEMPLATE=""
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  [[ -f skills/mongodb-observability/scripts/observability.py ]] || die "Missing observability skill. Run samples/restore-vendored-skills.sh"
  note "Observability configuration (nodes are discovered directly from Atlas)"
  prompt QUERY_SHAPE_SOURCE "Query-shape source (atlas_api/mongodb/bucket)" "atlas_api"
  [[ "$QUERY_SHAPE_SOURCE" == atlas_api || "$QUERY_SHAPE_SOURCE" == mongodb || "$QUERY_SHAPE_SOURCE" == bucket ]] || die "Use atlas_api, mongodb, or bucket."
  prompt INDEX_STATS_ENABLED "Also collect direct MongoDB indexStats? (yes/no)" "no"
  INDEX_STATS_ENABLED=$(printf '%s' "$INDEX_STATS_ENABLED" | tr '[:upper:]' '[:lower:]')
  [[ "$INDEX_STATS_ENABLED" == yes || "$INDEX_STATS_ENABLED" == no ]] || die "Enter yes or no."
  if [[ "$INDEX_STATS_ENABLED" == yes || "$QUERY_SHAPE_SOURCE" == mongodb ]]; then
    prompt MONGODB_URI_TEMPLATE "MongoDB URI template ({host} is replaced; input hidden is not supported)" ""
    [[ -n "$MONGODB_URI_TEMPLATE" ]] || die "A MongoDB URI template is required for direct MongoDB collection."
  fi
fi
# This unchanged provider loader does not read observability-specific env settings.
# Stop rather than silently running with the wrong query-shape/index configuration.
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  die "Observability options are not supported by the unchanged agent/providers.py; select no for this local sample, or configure observability in a separately reviewed change."
fi
prompt REPORT_PROVIDER "Report provider (none/bedrock/vertex/anthropic/claude_cli)" "none"
REPORT_PROVIDER=$(printf '%s' "$REPORT_PROVIDER" | tr '[:upper:]' '[:lower:]')
[[ "$REPORT_PROVIDER" == none || "$REPORT_PROVIDER" == bedrock || "$REPORT_PROVIDER" == vertex || "$REPORT_PROVIDER" == anthropic || "$REPORT_PROVIDER" == claude_cli ]] || die "Choose none, bedrock, vertex, anthropic, or claude_cli."
BEDROCK_REGION=""; BEDROCK_MODEL_ID=""; AWS_PROFILE_NAME=""
GCP_PROJECT=""; VERTEX_LOCATION=""; VERTEX_MODEL=""; ANTHROPIC_MODEL=""; CLAUDE_CLI_MODEL=""
case "$REPORT_PROVIDER" in
  bedrock)
    note "Amazon Bedrock configuration"
    prompt BEDROCK_REGION "AWS region" "eu-west-1"
    # Saved sample defaults may contain a Vertex location (global), which is not
    # an AWS Bedrock region and yields bedrock-runtime.global.amazonaws.com.
    if [[ ! "$BEDROCK_REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]]; then
      die "Invalid Bedrock AWS region: $BEDROCK_REGION (choose a region such as eu-west-1, not global)."
    fi
    prompt BEDROCK_MODEL_ID "Bedrock model or inference-profile ID" "eu.anthropic.claude-sonnet-5"
    if [[ "$STORAGE_PROVIDER" == s3 ]]; then
      AWS_PROFILE_NAME="$STORAGE_AWS_PROFILE"
    else
      prompt AWS_PROFILE_NAME "AWS profile (blank uses default credentials)" ""
    fi
    ;;
  vertex)
    note "Vertex AI configuration"
    if [[ "$STORAGE_PROVIDER" == gcs ]]; then
      GCP_PROJECT="$STORAGE_GCP_PROJECT"
    else
      prompt GCP_PROJECT "GCP project ID" ""
    fi
    [[ -n "$GCP_PROJECT" ]] || die "A GCP project ID is required for Vertex reports."
    prompt VERTEX_LOCATION "Vertex location" "global"
    prompt VERTEX_MODEL "Vertex Claude model ID" "claude-sonnet-5"
    ;;
  claude_cli)
    note "Claude Code CLI configuration (uses your existing CLI login; still requires network)"
    command -v claude >/dev/null || die "Install Claude Code CLI and sign in with 'claude' first."
    prompt CLAUDE_CLI_MODEL "Claude CLI model (blank uses your CLI default)" ""
    ;;
  anthropic)
    note "Direct Claude API configuration (no AWS/GCP credentials needed)"
    prompt ANTHROPIC_MODEL "Anthropic API model ID (must be enabled for your API key)" "claude-sonnet-5"
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
      read -r -s -p "Anthropic API key (input hidden): " ANTHROPIC_API_KEY; printf '\n'
      [[ -n "$ANTHROPIC_API_KEY" ]] || die "ANTHROPIC_API_KEY is required for direct Claude reports."
      export ANTHROPIC_API_KEY
    fi
    ;;
esac

# Persist selections before installing dependencies or checking S3/GCS access.
# Failed cloud authentication must not discard a newly selected S3 bucket.
save_atlas_sample_defaults

note "Creating Python environment"
python3 -m venv .venv-local-atlas-folder
source .venv-local-atlas-folder/bin/activate
python -m pip install --upgrade pip
prepare_atlas_storage
[[ ! -f skills/mongodb-log-diagnostic/requirements.txt ]] || python -m pip install -r skills/mongodb-log-diagnostic/requirements.txt
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  python -m pip install 'pymongo>=4.6' 'requests>=2.31'
fi
case "$REPORT_PROVIDER" in
  bedrock)
    python -m pip install -r requirements-aws.txt
    [[ -z "$AWS_PROFILE_NAME" ]] || export AWS_PROFILE="$AWS_PROFILE_NAME"
    export AWS_REGION="$BEDROCK_REGION" AWS_DEFAULT_REGION="$BEDROCK_REGION"
    note "Checking AWS identity used for Bedrock (region: $BEDROCK_REGION)"
    python - <<'PYCODE'
import boto3
try:
    print(boto3.client("sts").get_caller_identity()["Arn"])
except Exception as exc:
    raise SystemExit("AWS credentials are not usable: " + str(exc))
PYCODE
    ;;
  vertex)
    command -v gcloud >/dev/null || die "gcloud is required for Vertex reports. Install Google Cloud CLI, then rerun."
    python -m pip install 'google-auth>=2.27' 'requests>=2.31'
    note "Authenticating Application Default Credentials for Vertex"
    gcloud config set project "$GCP_PROJECT"
    if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
      gcloud auth application-default login
    fi
    ;;
  anthropic)
    python -m pip install 'anthropic>=0.49'
    ;;
esac

# Use a fresh keys-only file, never the prior mixed config file. Remove it on exit.
CONFIG_FILE="$(mktemp "${TMPDIR:-/tmp}/mongodb-atlas-keys.XXXXXXXX")"
cleanup_credentials() { rm -f -- "$CONFIG_FILE"; }
trap cleanup_credentials EXIT
# Pass private key via the child's environment, never its process command line.
ATLAS_PUBLIC_KEY="$ATLAS_PUBLIC_KEY" ATLAS_PRIVATE_KEY="$ATLAS_PRIVATE_KEY" \
  python - "$CONFIG_FILE" <<'PYCODE'
import json
import os
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "atlas_public_key": os.environ["ATLAS_PUBLIC_KEY"],
    "atlas_private_key": os.environ["ATLAS_PRIVATE_KEY"],
}) + "\n", encoding="utf-8")
PYCODE
chmod 600 "$CONFIG_FILE"
unset ATLAS_PUBLIC_KEY ATLAS_PRIVATE_KEY
export CLOUD_PROVIDER=local ATLAS_CONFIG_FILE="$CONFIG_FILE" \
  SKILLS_DIR="$PROJECT_ROOT/skills" DIAG_SKILL_DIR="$PROJECT_ROOT/skills/mongodb-log-diagnostic"
export MONGODB_LOG_DIAG_INPUT_MODE=atlas_api
export MONGODB_LOG_DIAG_STORAGE_PROVIDER="$STORAGE_PROVIDER"
export MONGODB_LOG_DIAG_BUCKET="$BUCKET"
export MONGODB_LOG_DIAG_PREFIX="$PREFIX"
export MONGODB_LOG_DIAG_TIMEZONE="$TIMEZONE"
export MONGODB_LOG_DIAG_CLUSTER_NAME="$CLUSTER_NAME"
export MONGODB_LOG_DIAG_GROUP_ID="$GROUP_ID"
export MONGODB_LOG_DIAG_API_VERSION=2025-03-12
export MONGODB_LOG_DIAG_LOG_NAMES="$LOG_NAMES"
export MONGODB_LOG_DIAG_SLOW_MS=1000
if [[ -n "$STORAGE_AWS_REGION" ]]; then export MONGODB_LOG_DIAG_AWS_REGION="$STORAGE_AWS_REGION"; fi
if [[ -n "$STORAGE_GCP_PROJECT" ]]; then export MONGODB_LOG_DIAG_GCP_PROJECT="$STORAGE_GCP_PROJECT"; fi
if [[ "$REPORT_PROVIDER" != none ]]; then
  export MONGODB_LOG_DIAG_LLM_PROVIDER="$REPORT_PROVIDER"
  case "$REPORT_PROVIDER" in
    bedrock)
      export MONGODB_LOG_DIAG_BEDROCK_REGION="$BEDROCK_REGION"
      export MONGODB_LOG_DIAG_BEDROCK_MODEL_ID="$BEDROCK_MODEL_ID"
      ;;
    vertex)
      export MONGODB_LOG_DIAG_VERTEX_PROJECT="$GCP_PROJECT"
      export MONGODB_LOG_DIAG_VERTEX_LOCATION="$VERTEX_LOCATION"
      export MONGODB_LOG_DIAG_VERTEX_MODEL="$VERTEX_MODEL"
      ;;
    anthropic|claude_cli)
      note "The unchanged provider loader does not expose per-provider model overrides; using its default model."
      ;;
  esac
else
  unset MONGODB_LOG_DIAG_LLM_PROVIDER
fi

# Fail before any network operation if the generated secret is not keys-only.
python - <<'PYCODE'
import json
import os
from pathlib import Path
path = Path(os.environ["ATLAS_CONFIG_FILE"])
keys = set(json.loads(path.read_text(encoding="utf-8")))
if keys != {"atlas_public_key", "atlas_private_key"}:
    raise SystemExit("Generated Atlas credential file has unexpected fields")
print("Atlas credential file verified: keys-only")
PYCODE

show_atlas_storage_target
note "Downloading from Atlas into the selected bucket"
CMD=(python -m agent.handler --stage download); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
note "Extracting one node/log at a time"
CMD=(python -m agent.handler --stage extract); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  note "Collecting observability one node at a time"
  CMD=(python -m agent.handler --stage observability); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
fi
if [[ "$REPORT_PROVIDER" != none ]]; then
  note "Generating $REPORT_PROVIDER reports into the selected bucket"
  CMD=(python -m agent.handler --stage report); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
fi
[[ -n "$LOG_DATE" ]] || LOG_DATE="$(python - <<PY
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
print((datetime.now(ZoneInfo('$TIMEZONE'))-timedelta(days=1)).date())
PY
)"
show_atlas_storage_outputs "$LOG_DATE"
printf '\nTemporary keys-only credential file will be removed on exit.\n'
