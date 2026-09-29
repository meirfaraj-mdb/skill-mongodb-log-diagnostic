#!/usr/bin/env bash
# macOS: Atlas API -> local folder -> sequential extraction -> optional Vertex reports.
# All logs, extracts, and reports stay local. Atlas and optional Vertex AI are remote.
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
prompt GENERATE_REPORTS "Generate local reports with Vertex AI? (yes/no)" "no"
GENERATE_REPORTS=$(printf '%s' "$GENERATE_REPORTS" | tr '[:upper:]' '[:lower:]')
[[ "$GENERATE_REPORTS" == yes || "$GENERATE_REPORTS" == no ]] || die "Enter yes or no."
GCP_PROJECT=""; VERTEX_LOCATION=""; VERTEX_MODEL=""
if [[ "$GENERATE_REPORTS" == yes ]]; then
  note "Vertex AI configuration"
  prompt GCP_PROJECT "GCP project ID" ""
  [[ -n "$GCP_PROJECT" ]] || die "A GCP project ID is required for Vertex reports."
  prompt VERTEX_LOCATION "Vertex location" "global"
  prompt VERTEX_MODEL "Vertex Claude model ID" "claude-sonnet-5"
fi

note "Creating Python environment"
python3 -m venv .venv-local-atlas-vertex
source .venv-local-atlas-vertex/bin/activate
python -m pip install --upgrade pip
[[ ! -f skills/mongodb-log-diagnostic/requirements.txt ]] || python -m pip install -r skills/mongodb-log-diagnostic/requirements.txt
if [[ "$GENERATE_REPORTS" == yes ]]; then
  command -v gcloud >/dev/null || die "gcloud is required for Vertex reports. Install Google Cloud CLI, then rerun."
  python -m pip install 'google-auth>=2.27' 'requests>=2.31'
  note "Authenticating Application Default Credentials for Vertex"
  gcloud config set project "$GCP_PROJECT"
  gcloud auth application-default login
fi

CONFIG_FILE="$PROJECT_ROOT/.local-atlas-vertex-test.json"
python - "$CONFIG_FILE" "$LOCAL_BUCKET" "$PREFIX" "$TIMEZONE" "$CLUSTER_NAME" "$GROUP_ID" "$ATLAS_PUBLIC_KEY" "$ATLAS_PRIVATE_KEY" "$LOG_NAMES" "$GENERATE_REPORTS" "$GCP_PROJECT" "$VERTEX_LOCATION" "$VERTEX_MODEL" <<'PY'
import json, sys
(path,bucket,prefix,tz,cluster,group,public,private,names,reports,project,location,model)=sys.argv[1:]
cfg={"input_mode":"atlas_api","storage_provider":"local","llm_provider":None,"bucket":bucket,"prefix":prefix,"timezone":tz,"cluster_name":cluster,"group_id":group,"atlas_public_key":public,"atlas_private_key":private,"api_version":"2025-03-12","log_names":[x.strip() for x in names.split(',') if x.strip()],"slow_ms":1000,"observability_enabled":False}
if reports == "yes":
    cfg.update({"llm_provider":"vertex","vertex_project":project,"vertex_location":location,"vertex_model":model})
open(path,"w",encoding="utf-8").write(json.dumps(cfg,indent=2))
PY
chmod 600 "$CONFIG_FILE"
export CLOUD_PROVIDER=local ATLAS_CONFIG_FILE="$CONFIG_FILE" SKILLS_DIR="$PROJECT_ROOT/skills" DIAG_SKILL_DIR="$PROJECT_ROOT/skills/mongodb-log-diagnostic"
[[ -z "$GCP_PROJECT" ]] || export GOOGLE_CLOUD_PROJECT="$GCP_PROJECT"

note "Downloading from Atlas into the local bucket"
CMD=(python -m agent.handler --stage download); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
note "Extracting one node/log at a time"
CMD=(python -m agent.handler --stage extract); [[ -z "$LOG_DATE" ]] || CMD+=(--log-date "$LOG_DATE"); "${CMD[@]}"
if [[ "$GENERATE_REPORTS" == yes ]]; then
  note "Generating Vertex reports into the local bucket"
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
find "$DAY_DIR" -type f \( -path '*/mongodb/*.gz' -o -path '*/extracts/*/*' -o -path '*/reports/*/*' -o -path '*/cluster/reports/*' \) -print 2>/dev/null || true
printf '\nLocal config (contains Atlas credentials; do not commit it): %s\n' "$CONFIG_FILE"
