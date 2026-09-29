#!/usr/bin/env bash
# macOS: Atlas API -> local folder (S3/GCS simulation) -> sequential extraction.
# This script never uses AWS, GCP, Bedrock, Vertex, reports, or MongoDB observability.
# Run from project root: ./samples/macos-local-atlas-folder-test.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }
prompt() { local __var="$1" label="$2" default="${3:-}" value; read -r -p "$label${default:+ [$default]}: " value; printf -v "$__var" '%s' "${value:-$default}"; }

[[ "$(uname -s)" == "Darwin" ]] || die "This bootstrap is for macOS."
need python3

# Make missing vendored files obvious before prompting for credentials.
for required in \
  skills/mongodb-atlas-logs/scripts/atlas_logs.py \
  skills/mongodb-log-diagnostic/SKILL.md \
  skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py \
  skills/mongodb-log-diagnostic/scripts/driver_compatibility.py \
  skills/mongodb-log-diagnostic/references/analysis-prompt.md \
  skills/mongodb-log-diagnostic/references/extracted-signal-reference.md; do
  [[ -f "$required" ]] || die "Missing vendored skill file: $required\nCopy the complete skill source into skills/ before running this test."
done

note "Local bucket configuration"
prompt LOCAL_BUCKET "Local folder to emulate the bucket" "$HOME/Downloads/mongodb-log-local-bucket"
LOCAL_BUCKET="${LOCAL_BUCKET/#\~/$HOME}"
mkdir -p "$LOCAL_BUCKET"
LOCAL_BUCKET="$(cd "$LOCAL_BUCKET" && pwd)"
prompt PREFIX "Bucket prefix" "atlas-logs"
prompt LOG_DATE "Log date (YYYY-MM-DD; blank means yesterday)" ""
[[ -z "$LOG_DATE" || "$LOG_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "Log date must be YYYY-MM-DD."
prompt TIMEZONE "Timezone used for yesterday" "Asia/Jerusalem"

note "Atlas API configuration"
prompt CLUSTER_NAME "Atlas cluster name" ""
prompt GROUP_ID "Atlas project (group) ID" ""
prompt ATLAS_PUBLIC_KEY "Atlas API public key" ""
read -r -s -p "Atlas API private key (input hidden): " ATLAS_PRIVATE_KEY; printf '\n'
[[ -n "$CLUSTER_NAME" && -n "$GROUP_ID" && -n "$ATLAS_PUBLIC_KEY" && -n "$ATLAS_PRIVATE_KEY" ]] \
  || die "Cluster name, group ID, and both Atlas API-key values are required."
prompt LOG_NAMES "Log names (comma-separated: auto,mongodb,mongos)" "auto"

note "Creating Python environment"
python3 -m venv .venv-local-atlas-folder
# shellcheck disable=SC1091
source .venv-local-atlas-folder/bin/activate
python -m pip install --upgrade pip
# Atlas download code is standard-library based; install only extractor dependencies when present.
if [[ -f skills/mongodb-log-diagnostic/requirements.txt ]]; then
  python -m pip install -r skills/mongodb-log-diagnostic/requirements.txt
elif [[ -f requirements.txt ]]; then
  python -m pip install -r requirements.txt
else
  note "No extractor requirements file found; continuing with the virtual environment."
fi

CONFIG_FILE="$PROJECT_ROOT/.local-atlas-folder-test.json"
python3 - "$CONFIG_FILE" "$LOCAL_BUCKET" "$PREFIX" "$TIMEZONE" "$CLUSTER_NAME" "$GROUP_ID" "$ATLAS_PUBLIC_KEY" "$ATLAS_PRIVATE_KEY" "$LOG_NAMES" <<'PY'
import json, sys
(path, bucket, prefix, timezone, cluster, group, public, private, log_names) = sys.argv[1:]
config = {
  "input_mode": "atlas_api", "storage_provider": "local", "llm_provider": None,
  "bucket": bucket, "prefix": prefix, "timezone": timezone, "cluster_name": cluster,
  "group_id": group, "atlas_public_key": public, "atlas_private_key": private,
  "api_version": "2025-03-12", "log_names": [x.strip() for x in log_names.split(",") if x.strip()],
  "slow_ms": 1000, "observability_enabled": False
}
with open(path, "w", encoding="utf-8") as fh:
  json.dump(config, fh, indent=2)
PY
chmod 600 "$CONFIG_FILE"

export CLOUD_PROVIDER=local
export ATLAS_CONFIG_FILE="$CONFIG_FILE"
export SKILLS_DIR="$PROJECT_ROOT/skills"
export DIAG_SKILL_DIR="$PROJECT_ROOT/skills/mongodb-log-diagnostic"

note "Downloading from Atlas into the local bucket"
CMD=(python -m agent.handler --stage download)
[[ -n "$LOG_DATE" ]] && CMD+=(--log-date "$LOG_DATE")
"${CMD[@]}"

note "Extracting one node/log at a time"
CMD=(python -m agent.handler --stage extract)
[[ -n "$LOG_DATE" ]] && CMD+=(--log-date "$LOG_DATE")
"${CMD[@]}"

if [[ -z "$LOG_DATE" ]]; then
  LOG_DATE="$(python - <<PY
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
print((datetime.now(ZoneInfo("$TIMEZONE")) - timedelta(days=1)).date())
PY
)"
fi
DAY_DIR="$LOCAL_BUCKET/$PREFIX/$LOG_DATE"
note "Files updated under $DAY_DIR"
printf '\nRaw logs:\n'
find "$DAY_DIR" -type f -path '*/mongodb/*.gz' -print 2>/dev/null || true
printf '\nExtracts:\n'
find "$DAY_DIR" -type f -path '*/extracts/*/*' -print 2>/dev/null || true
printf '\nNo reports were requested or generated.\n'
printf 'Local config (contains Atlas credentials; do not commit it): %s\n' "$CONFIG_FILE"
