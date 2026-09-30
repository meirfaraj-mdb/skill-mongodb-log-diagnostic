#!/usr/bin/env bash
# macOS: Atlas API -> local folder -> extraction -> optional Bedrock/Vertex/direct Claude reports.
# All logs, extracts, and reports stay local. Atlas and optional Bedrock are remote.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
prompt() { local var="$1" label="$2" default="${3:-}" value; read -r -p "$label${default:+ [$default]}: " value; printf -v "$var" '%s' "${value:-$default}"; }
[[ "$(uname -s)" == Darwin ]] || die "This script is for macOS."
command -v python3 >/dev/null || die "python3 is required."

for f in skills/mongodb-atlas-logs/scripts/atlas_logs.py \
  skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  skills/mongodb-log-diagnostic/references/analysis-prompt.md; do
  [[ -f "$f" ]] || die "Missing vendored skill file: $f"
done

note "Local bucket configuration"
prompt LOCAL_BUCKET "Local folder to emulate the bucket" "$HOME/Downloads/mongodb-log-local-bucket"
mkdir -p "$LOCAL_BUCKET"; LOCAL_BUCKET="$(cd "$LOCAL_BUCKET" && pwd)"
prompt PREFIX "Bucket prefix" "atlas-logs"
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
prompt REPORT_PROVIDER "Report provider (none/bedrock/vertex/anthropic)" "none"
REPORT_PROVIDER=$(printf '%s' "$REPORT_PROVIDER" | tr '[:upper:]' '[:lower:]')
[[ "$REPORT_PROVIDER" == none || "$REPORT_PROVIDER" == bedrock || "$REPORT_PROVIDER" == vertex || "$REPORT_PROVIDER" == anthropic ]] || die "Choose none, bedrock, vertex, or anthropic."
BEDROCK_REGION=""; BEDROCK_MODEL_ID=""; AWS_PROFILE_NAME=""
GCP_PROJECT=""; VERTEX_LOCATION=""; VERTEX_MODEL=""; ANTHROPIC_MODEL=""
case "$REPORT_PROVIDER" in
  bedrock)
    note "Amazon Bedrock configuration"
    prompt BEDROCK_REGION "AWS region" "eu-west-1"
    prompt BEDROCK_MODEL_ID "Bedrock model or inference-profile ID" "eu.anthropic.claude-sonnet-5"
    prompt AWS_PROFILE_NAME "AWS profile (blank uses default credentials)" ""
    ;;
  vertex)
    note "Vertex AI configuration"
    prompt GCP_PROJECT "GCP project ID" ""
    [[ -n "$GCP_PROJECT" ]] || die "A GCP project ID is required for Vertex reports."
    prompt VERTEX_LOCATION "Vertex location" "global"
    prompt VERTEX_MODEL "Vertex Claude model ID" "claude-sonnet-5"
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

note "Creating Python environment"
python3 -m venv .venv-local-atlas-folder
source .venv-local-atlas-folder/bin/activate
python -m pip install --upgrade pip
[[ ! -f skills/mongodb-log-diagnostic/requirements.txt ]] || python -m pip install -r skills/mongodb-log-diagnostic/requirements.txt
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  python -m pip install 'pymongo>=4.6' 'requests>=2.31'
fi
case "$REPORT_PROVIDER" in
  bedrock)
    python -m pip install -r requirements-aws.txt
    [[ -z "$AWS_PROFILE_NAME" ]] || export AWS_PROFILE="$AWS_PROFILE_NAME"
    export AWS_REGION="$BEDROCK_REGION"
    note "Checking AWS identity used for Bedrock"
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
    gcloud auth application-default login
    ;;
  anthropic)
    python -m pip install 'anthropic>=0.49'
    ;;
esac

CONFIG_FILE="$PROJECT_ROOT/.local-atlas-folder-test.json"
python - "$CONFIG_FILE" "$LOCAL_BUCKET" "$PREFIX" "$TIMEZONE" "$CLUSTER_NAME" "$GROUP_ID" "$ATLAS_PUBLIC_KEY" "$ATLAS_PRIVATE_KEY" "$LOG_NAMES" "$ENABLE_OBSERVABILITY" "$QUERY_SHAPE_SOURCE" "$INDEX_STATS_ENABLED" "$MONGODB_URI_TEMPLATE" "$REPORT_PROVIDER" "$BEDROCK_REGION" "$BEDROCK_MODEL_ID" "$GCP_PROJECT" "$VERTEX_LOCATION" "$VERTEX_MODEL" "$ANTHROPIC_MODEL" <<'PYCODE'
import json, sys
(path,bucket,prefix,tz,cluster,group,public,private,names,observability,query_source,index_stats,mongodb_uri,provider,region,bedrock_model,project,location,vertex_model,anthropic_model)=sys.argv[1:]
cfg={"input_mode":"atlas_api","storage_provider":"local","llm_provider":None,"bucket":bucket,"prefix":prefix,"timezone":tz,"cluster_name":cluster,"group_id":group,"atlas_public_key":public,"atlas_private_key":private,"api_version":"2025-03-12","log_names":[x.strip() for x in names.split(',') if x.strip()],"slow_ms":1000,"observability_enabled":observability == "yes", "deployment_type":"atlas", "query_shape_source":query_source, "query_shape_window_hours":24, "index_stats_enabled":index_stats == "yes"}
if mongodb_uri: cfg["mongodb_uri_template"] = mongodb_uri
if provider == "bedrock": cfg.update({"llm_provider":"bedrock","bedrock_region":region,"bedrock_model_id":bedrock_model})
if provider == "vertex": cfg.update({"llm_provider":"vertex","vertex_project":project,"vertex_location":location,"vertex_model":vertex_model})
if provider == "anthropic": cfg.update({"llm_provider":"anthropic","anthropic_model":anthropic_model})
with open(path,"w",encoding="utf-8") as f: json.dump(cfg,f,indent=2)
PYCODE
chmod 600 "$CONFIG_FILE"
export CLOUD_PROVIDER=local ATLAS_CONFIG_FILE="$CONFIG_FILE" SKILLS_DIR="$PROJECT_ROOT/skills" DIAG_SKILL_DIR="$PROJECT_ROOT/skills/mongodb-log-diagnostic"

note "Downloading from Atlas into the local bucket"
CMD=(python -m agent.handler --stage download); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
note "Extracting one node/log at a time"
CMD=(python -m agent.handler --stage extract); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  note "Collecting observability one node at a time"
  CMD=(python -m agent.handler --stage observability); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
fi
if [[ "$REPORT_PROVIDER" != none ]]; then
  note "Generating $REPORT_PROVIDER reports into the local bucket"
  CMD=(python -m agent.handler --stage report); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
fi
[[ -n "$LOG_DATE" ]] || LOG_DATE="$(python - <<PY
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
print((datetime.now(ZoneInfo('$TIMEZONE'))-timedelta(days=1)).date())
PY
)"
DAY_DIR="$LOCAL_BUCKET/$PREFIX/$LOG_DATE"
note "Files updated under $DAY_DIR"
find "$DAY_DIR" -type f \( -path '*/mongodb/*.gz' -o -path '*/extracts/*/*' -o -path '*/reports/*/*' -o -path '*/indexStats/*' -o -path '*/queryStats/*' -o -path '*/cluster/reports/*' \) -print 2>/dev/null || true
printf '\nLocal config (contains Atlas credentials; do not commit it): %s\n' "$CONFIG_FILE"
