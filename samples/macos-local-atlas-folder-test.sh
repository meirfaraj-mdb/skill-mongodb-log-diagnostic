#!/usr/bin/env bash
# macOS: Atlas API -> local folder -> sequential extraction -> optional Bedrock reports.
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
  note "Observability configuration"
  prompt OBS_HOSTS "Atlas node hostnames (comma-separated)" ""
  [[ -n "$OBS_HOSTS" ]] || die "At least one node hostname is required for observability."
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
prompt GENERATE_REPORTS "Generate local reports with Amazon Bedrock? (yes/no)" "no"
GENERATE_REPORTS=$(printf '%s' "$GENERATE_REPORTS" | tr '[:upper:]' '[:lower:]')
[[ "$GENERATE_REPORTS" == yes || "$GENERATE_REPORTS" == no ]] || die "Enter yes or no."
BEDROCK_REGION=""; BEDROCK_MODEL_ID=""; AWS_PROFILE_NAME=""
if [[ "$GENERATE_REPORTS" == yes ]]; then
  note "Amazon Bedrock configuration"
  prompt BEDROCK_REGION "AWS region for Bedrock" "eu-west-1"
  prompt BEDROCK_MODEL_ID "Bedrock model or inference-profile ID" "eu.anthropic.claude-sonnet-5"
  [[ -n "$BEDROCK_MODEL_ID" ]] || die "A Bedrock model or inference-profile ID is required."
  prompt AWS_PROFILE_NAME "AWS profile (blank uses default credentials)" ""
fi

note "Creating Python environment"
python3 -m venv .venv-local-atlas-folder
source .venv-local-atlas-folder/bin/activate
python -m pip install --upgrade pip
[[ ! -f skills/mongodb-log-diagnostic/requirements.txt ]] || python -m pip install -r skills/mongodb-log-diagnostic/requirements.txt
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  python -m pip install 'pymongo>=4.6' 'requests>=2.31'
fi
if [[ "$GENERATE_REPORTS" == yes ]]; then
  python -m pip install -r requirements-aws.txt
  [[ -z "$AWS_PROFILE_NAME" ]] || export AWS_PROFILE="$AWS_PROFILE_NAME"
  export AWS_REGION="$BEDROCK_REGION"
  note "Checking AWS identity used for Bedrock"
  python - <<'PY'
import boto3
try:
    print(boto3.client("sts").get_caller_identity()["Arn"])
except Exception as exc:
    raise SystemExit("AWS credentials are not usable: " + str(exc))
PY
fi

CONFIG_FILE="$PROJECT_ROOT/.local-atlas-folder-test.json"
python - "$CONFIG_FILE" "$LOCAL_BUCKET" "$PREFIX" "$TIMEZONE" "$CLUSTER_NAME" "$GROUP_ID" "$ATLAS_PUBLIC_KEY" "$ATLAS_PRIVATE_KEY" "$LOG_NAMES" "$ENABLE_OBSERVABILITY" "$OBS_HOSTS" "$QUERY_SHAPE_SOURCE" "$INDEX_STATS_ENABLED" "$MONGODB_URI_TEMPLATE" "$GENERATE_REPORTS" "$BEDROCK_REGION" "$BEDROCK_MODEL_ID" <<'PY'
import json, sys
(path,bucket,prefix,tz,cluster,group,public,private,names,observability,hosts,query_source,index_stats,mongodb_uri,reports,region,model)=sys.argv[1:]
cfg={"input_mode":"atlas_api","storage_provider":"local","llm_provider":None,"bucket":bucket,"prefix":prefix,"timezone":tz,"cluster_name":cluster,"group_id":group,"atlas_public_key":public,"atlas_private_key":private,"api_version":"2025-03-12","log_names":[x.strip() for x in names.split(',') if x.strip()],"slow_ms":1000,"observability_enabled":observability == "yes", "deployment_type":"atlas", "index_stats_hosts":[x.strip() for x in hosts.split(',') if x.strip()], "query_shape_source":query_source, "query_shape_window_hours":24, "index_stats_enabled":index_stats == "yes"}
if mongodb_uri: cfg["mongodb_uri_template"] = mongodb_uri
if reports == "yes": cfg.update({"llm_provider":"bedrock","bedrock_region":region,"bedrock_model_id":model})
open(path,"w",encoding="utf-8").write(json.dumps(cfg,indent=2))
PY
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
if [[ "$GENERATE_REPORTS" == yes ]]; then
  note "Generating Bedrock reports into the local bucket"
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
