#!/usr/bin/env bash
# macOS local test: Atlas API -> Amazon S3 -> extract -> optional Bedrock reports.
# Run from project root: ./samples/macos-local-aws-atlas-test.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }
prompt() { local __var="$1" label="$2" default="${3:-}" value; read -r -p "$label${default:+ [$default]}: " value; printf -v "$__var" '%s' "${value:-$default}"; }

[[ "$(uname -s)" == "Darwin" ]] || die "This bootstrap is for macOS."
need python3

# Fail before asking for credentials if the vendored runtime files are incomplete.
for required in \
  skills/mongodb-log-diagnostic/SKILL.md \
  skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py \
  skills/mongodb-log-diagnostic/scripts/driver_compatibility.py \
  skills/mongodb-log-diagnostic/references/analysis-prompt.md \
  skills/mongodb-log-diagnostic/references/extracted-signal-reference.md; do
  [[ -f "$required" ]] || die "Missing vendored skill file: $required"
done

note "Installing AWS CLI if needed"
if ! command -v aws >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || die "Install Homebrew (https://brew.sh), then rerun this script."
  brew install awscli
fi
aws --version

prompt AWS_PROFILE "AWS CLI profile (leave blank for default)" ""
AWS_ARGS=()
[[ -n "$AWS_PROFILE" ]] && AWS_ARGS+=(--profile "$AWS_PROFILE")
prompt AWS_REGION "AWS region" "$(aws "${AWS_ARGS[@]}" configure get region 2>/dev/null || printf "eu-west-1")"
[[ -n "$AWS_REGION" ]] || die "AWS region is required."
AWS_ARGS+=(--region "$AWS_REGION")

note "Checking AWS identity"
aws "${AWS_ARGS[@]}" sts get-caller-identity >/dev/null || die "AWS authentication failed. Run aws configure or aws sso login, then retry."

prompt CREATE_BUCKET "Create a new disposable S3 bucket? (y/n)" "y"
if [[ "$CREATE_BUCKET" =~ ^[Yy]$ ]]; then
  DEFAULT_BUCKET="mongodb-log-diag-${USER//[^a-zA-Z0-9-]/-}-$(date +%s)-$RANDOM"
  prompt BUCKET "New globally unique S3 bucket name" "$DEFAULT_BUCKET"
  note "Creating test bucket s3://$BUCKET"
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws "${AWS_ARGS[@]}" s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws "${AWS_ARGS[@]}" s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
  CREATED_BUCKET=true
else
  prompt BUCKET "Existing test S3 bucket name" ""
  [[ -n "$BUCKET" ]] || die "Bucket name is required."
  aws "${AWS_ARGS[@]}" s3api head-bucket --bucket "$BUCKET" >/dev/null || die "Cannot access s3://$BUCKET"
  CREATED_BUCKET=false
fi

prompt PREFIX "Isolated test prefix" "local-atlas-test"
prompt LOG_DATE "Log date (YYYY-MM-DD; blank means yesterday in selected timezone)" ""
[[ -z "$LOG_DATE" || "$LOG_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "Log date must be YYYY-MM-DD."
prompt TIMEZONE "Timezone used for D-1" "Asia/Jerusalem"
prompt ENABLE_OBSERVABILITY "Collect Atlas Query Shape Insights for the last 24 hours? (y/n)" "n"
OBS_HOSTS=""; QUERY_SHAPE_SOURCE="disabled"; INDEX_STATS_ENABLED="n"; MONGODB_URI_TEMPLATE=""
if [[ "$ENABLE_OBSERVABILITY" =~ ^[Yy]$ ]]; then
  prompt OBS_HOSTS "Atlas node hostnames (comma-separated)" ""
  [[ -n "$OBS_HOSTS" ]] || die "At least one node hostname is required for observability."
  prompt QUERY_SHAPE_SOURCE "Query-shape source (atlas_api/mongodb/bucket)" "atlas_api"
  [[ "$QUERY_SHAPE_SOURCE" == atlas_api || "$QUERY_SHAPE_SOURCE" == mongodb || "$QUERY_SHAPE_SOURCE" == bucket ]] || die "Use atlas_api, mongodb, or bucket."
  prompt INDEX_STATS_ENABLED "Also collect direct MongoDB indexStats? (y/n)" "n"
  if [[ "$INDEX_STATS_ENABLED" =~ ^[Yy]$ || "$QUERY_SHAPE_SOURCE" == mongodb ]]; then
    prompt MONGODB_URI_TEMPLATE "MongoDB URI template ({host} is replaced)" ""
    [[ -n "$MONGODB_URI_TEMPLATE" ]] || die "A MongoDB URI template is required for direct MongoDB collection."
  fi
fi
prompt SECRET_MODE "Use existing Secrets Manager secret (e) or create a dedicated test secret (c)?" "e"

CONFIG_FILE="$PROJECT_ROOT/.local-aws-atlas-test-secret.json"
TEMP_SECRET_NAME=""
if [[ "$SECRET_MODE" == "e" ]]; then
  prompt ATLAS_SECRET_ID "Existing secret ARN or name" ""
  [[ -n "$ATLAS_SECRET_ID" ]] || die "Secret ARN/name is required."
  aws "${AWS_ARGS[@]}" secretsmanager get-secret-value --secret-id "$ATLAS_SECRET_ID" --query SecretString --output text >/dev/null \
    || die "Cannot read the secret. Check secretsmanager:GetSecretValue."
else
  prompt ATLAS_PUBLIC_KEY "Atlas API public key" ""
  read -r -s -p "Atlas API private key (input hidden): " ATLAS_PRIVATE_KEY; printf '\n'
  prompt GROUP_ID "Atlas project/group ID" ""
  prompt CLUSTER_NAME "Atlas cluster name" ""
  [[ -n "$ATLAS_PUBLIC_KEY" && -n "$ATLAS_PRIVATE_KEY" && -n "$GROUP_ID" && -n "$CLUSTER_NAME" ]] || die "All Atlas fields are required."
  TEMP_SECRET_NAME="mongodb-log-diag-local-${USER//[^a-zA-Z0-9-]/-}-$(date +%s)"
  python3 - "$CONFIG_FILE" "$BUCKET" "$PREFIX" "$TIMEZONE" "$ATLAS_PUBLIC_KEY" "$ATLAS_PRIVATE_KEY" "$GROUP_ID" "$CLUSTER_NAME" "$AWS_REGION" "$ENABLE_OBSERVABILITY" "$OBS_HOSTS" "$QUERY_SHAPE_SOURCE" "$INDEX_STATS_ENABLED" "$MONGODB_URI_TEMPLATE" <<'PY'
import json, sys
p, bucket, prefix, tz, public, private, group, cluster, region, observability, hosts, query_source, index_stats, mongodb_uri = sys.argv[1:]
json.dump({"input_mode":"atlas_api", "bucket":bucket, "prefix":prefix, "timezone":tz,
           "cluster_name":cluster, "atlas_public_key":public, "atlas_private_key":private,
           "group_id":group, "api_version":"2025-03-12", "log_names":["auto"],
           "storage_provider":"s3", "llm_provider":"bedrock", "bedrock_region":region,
           "observability_enabled":observability.lower() == "y", "deployment_type":"atlas", "index_stats_hosts":[x.strip() for x in hosts.split(',') if x.strip()], "query_shape_source":query_source, "query_shape_window_hours":24, "index_stats_enabled":index_stats.lower() == "y", **({"mongodb_uri_template":mongodb_uri} if mongodb_uri else {})}, open(p, "w"), indent=2)
PY
  chmod 600 "$CONFIG_FILE"
  ATLAS_SECRET_ID="$(aws "${AWS_ARGS[@]}" secretsmanager create-secret --name "$TEMP_SECRET_NAME" --secret-string "file://$CONFIG_FILE" --query ARN --output text)"
  rm -f "$CONFIG_FILE"
  note "Created dedicated test secret $ATLAS_SECRET_ID"
fi

prompt RUN_REPORTS "Also generate Bedrock reports? (y/n)" "n"
BEDROCK_MODEL_ID=""
if [[ "$RUN_REPORTS" =~ ^[Yy]$ ]]; then
  prompt BEDROCK_MODEL_ID "Bedrock inference profile/model ID" "eu.anthropic.claude-sonnet-5"
  [[ -n "$BEDROCK_MODEL_ID" ]] || die "A Bedrock model/inference-profile ID is required for reports."
fi

note "Creating Python environment"
python3 -m venv .venv-local-aws
# shellcheck disable=SC1091
source .venv-local-aws/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-aws.txt
if [[ "$ENABLE_OBSERVABILITY" =~ ^[Yy]$ ]]; then
  python -m pip install 'pymongo>=4.6' 'requests>=2.31'
  if [[ "$SECRET_MODE" == "e" ]]; then
    note "Existing-secret note"
    printf '%s\n' "The existing secret must already set observability_enabled, index_stats_hosts, and query_shape_source."
  fi
fi

export CLOUD_PROVIDER=aws
export ATLAS_SECRET_ID
export AWS_DEFAULT_REGION="$AWS_REGION"
export SKILLS_DIR="$PROJECT_ROOT/skills"
export DIAG_SKILL_DIR="$PROJECT_ROOT/skills/mongodb-log-diagnostic"
[[ -n "$AWS_PROFILE" ]] && export AWS_PROFILE
[[ -n "$BEDROCK_MODEL_ID" ]] && export BEDROCK_MODEL_ID

note "Running Atlas download and sequential extraction"
CMD=(python -m agent.handler --stage all)
# all requires Bedrock reports; otherwise run only download and extraction.
if [[ "$RUN_REPORTS" =~ ^[Yy]$ ]]; then
  : # --stage all is correct
else
  CMD=(python -m agent.handler --stage download)
  [[ -n "$LOG_DATE" ]] && CMD+=(--log-date "$LOG_DATE")
  "${CMD[@]}"
  CMD=(python -m agent.handler --stage extract)
fi
[[ -n "$LOG_DATE" ]] && CMD+=(--log-date "$LOG_DATE")
"${CMD[@]}"
if [[ "$ENABLE_OBSERVABILITY" =~ ^[Yy]$ ]]; then
  note "Collecting observability one node at a time"
  CMD=(python -m agent.handler --stage observability)
  [[ -n "$LOG_DATE" ]] && CMD+=(--log-date "$LOG_DATE")
  "${CMD[@]}"
fi

note "Objects written under s3://$BUCKET/$PREFIX/"
aws "${AWS_ARGS[@]}" s3 ls "s3://$BUCKET/$PREFIX/" --recursive

if [[ "$SECRET_MODE" == "c" ]]; then
  read -r -p "Delete dedicated test secret now? (y/N): " DELETE_SECRET
  if [[ "$DELETE_SECRET" =~ ^[Yy]$ ]]; then
    aws "${AWS_ARGS[@]}" secretsmanager delete-secret --secret-id "$ATLAS_SECRET_ID" --force-delete-without-recovery
  fi
fi
if [[ "$CREATED_BUCKET" == true ]]; then
  read -r -p "Delete all objects and the test bucket now? (y/N): " DELETE_BUCKET
  if [[ "$DELETE_BUCKET" =~ ^[Yy]$ ]]; then
    aws "${AWS_ARGS[@]}" s3 rm "s3://$BUCKET" --recursive
    aws "${AWS_ARGS[@]}" s3api delete-bucket --bucket "$BUCKET"
  fi
fi
printf '\nFinished. Bucket: s3://%s/%s/\n' "$BUCKET" "$PREFIX"
