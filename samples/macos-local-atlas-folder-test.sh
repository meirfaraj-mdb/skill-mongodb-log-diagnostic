#!/usr/bin/env bash
# macOS: Atlas API -> local folder / S3 / GCS -> extraction -> optional reports
#        -> optional P0 Jira / P1 Slack notifications (each: off | json | create/send).
set -euo pipefail
umask 077  # credential file contains Atlas API keys

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
note() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
# Normalize a yes/no answer in place (accepts yes/no, y/n, true/false; any case).
yes_no() {
  local v; v="$(printf '%s' "${!1}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  case "$v" in
    yes|y|true) printf -v "$1" yes ;;
    no|n|false) printf -v "$1" no ;;
    *) die "$2: enter yes or no (got '${!1}')." ;;
  esac
}
# providers.py accepts only true/false for boolean MONGODB_LOG_DIAG_* settings.
yes_no_to_bool() { if [[ "$1" == yes ]]; then printf true; else printf false; fi; }
prompt() { local var="$1" label="$2" default="${3:-}" value; if [[ "$var" != LOG_DATE ]] && declare -f sample_default >/dev/null; then default="$(sample_default "$var" "$default")"; fi; read -r -p "$label${default:+ [$default]}: " value; printf -v "$var" '%s' "${value:-$default}"; }
[[ "$(uname -s)" == Darwin ]] || die "This script is for macOS."
command -v python3 >/dev/null || die "python3 is required."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 >= 3.9 is required (zoneinfo)."
# Stage-specific settings must come only from this script, not a stale shell
# environment (e.g. INDEX_STATS_ENABLED=yes would break every stage's config load).
unset MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED MONGODB_LOG_DIAG_QUERY_SHAPE_SOURCE \
  MONGODB_LOG_DIAG_QUERY_SHAPE_WINDOW_HOURS MONGODB_LOG_DIAG_INDEX_STATS_ENABLED \
  MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE MONGODB_LOG_DIAG_CLUSTER_SUMMARY_SOURCE
# Same for notification settings and credentials (a stale JIRA_MODE=create must never leak in).
for _var in $(compgen -e); do
  case "$_var" in
    MONGODB_LOG_DIAG_ENVIRONMENT|MONGODB_LOG_DIAG_JIRA_*|MONGODB_LOG_DIAG_SLACK_*|MONGODB_LOG_DIAG_NOTIFY_*|\
    NOTIFICATION_SECRET_ID|NOTIFICATION_CONFIG_FILE) unset "$_var" ;;
  esac
done
unset _var
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'; }

source "$PROJECT_ROOT/samples/local-atlas-storage.sh"
# Prompt to restore missing skill files from the repository before collecting credentials.
if [[ ! -f skills/mongodb-atlas-logs/scripts/atlas_logs.py ||
      ! -f skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py ||
      ! -f skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py ||
      ! -f skills/mongodb-log-diagnostic/scripts/driver_compatibility.py ]]; then
  read -r -p "Skill files missing. Restore both skills from GitHub now? (yes/no) [no]: " restore_skills
  restore_skills="${restore_skills:-no}"; yes_no restore_skills "Restore skills"
  if [[ "$restore_skills" == yes ]]; then
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

# Extra choices remembered by this runner (log date, Atlas public key, cluster-summary variant,
# non-secret notification settings). Kept in a separate 0600 JSON file so the shared
# ~/.localsample format is untouched. The Atlas private key, the Anthropic API key, and the
# Jira/Slack tokens and webhook URL are never written.
FOLDER_TEST_SAVED="${FOLDER_TEST_SAVED:-$HOME/.localsample.atlas-folder.json}"
saved_or() { local v; v="$(saved_value "$1")"; printf '%s' "${v:-$2}"; }  # saved_or KEY DEFAULT
# Non-secret notification answers remembered as notify_<name lower-case>.
NOTIFY_SAVED_KEYS=(ENABLE_NOTIFICATIONS NOTIFY_ENVIRONMENT JIRA_MODE JIRA_PROJECT_KEY JIRA_ISSUE_TYPE
  JIRA_PRIORITIES JIRA_DEPLOYMENT JIRA_ASSIGNEE JIRA_LABELS JIRA_COMPONENTS JIRA_PARENT_KEY JIRA_BASE_URL
  JIRA_DEDUPE_SCOPE JIRA_EMAIL SLACK_MODE SLACK_PRIORITIES SLACK_MENTIONS SLACK_REPEAT_AFTER_DAYS
  SLACK_SEND_VIA SLACK_CHANNEL)
saved_value() {  # saved_value KEY -> prints the remembered value or nothing
  [[ -f "$FOLDER_TEST_SAVED" ]] || return 0
  python3 - "$FOLDER_TEST_SAVED" "$1" <<'PYSAVED' 2>/dev/null || true
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], "")
    print(value if isinstance(value, str) else "")
except Exception:
    pass
PYSAVED
}
save_folder_test_choices() {  # values go through the environment, not the command line
  local notify_env=() key
  for key in "${NOTIFY_SAVED_KEYS[@]}"; do notify_env+=("SAVE_NOTIFY_$key=${!key:-}"); done
  env "${notify_env[@]}" \
  SAVE_LOG_DATE="$LOG_DATE" SAVE_PUBLIC_KEY="$SAVED_PUBLIC_KEY" SAVE_SUMMARY="${CLUSTER_SUMMARY_SOURCE:-}" \
    python3 - "$FOLDER_TEST_SAVED" <<'PYSAVE' || printf 'warning: could not save choices to %s\n' "$FOLDER_TEST_SAVED" >&2
import json, os, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
data["log_date"] = os.environ["SAVE_LOG_DATE"]          # as typed: blank = yesterday, N = last N days
data["atlas_public_key"] = os.environ["SAVE_PUBLIC_KEY"]
if os.environ["SAVE_SUMMARY"]:
    data["cluster_summary_source"] = os.environ["SAVE_SUMMARY"]
if os.environ.get("SAVE_NOTIFY_ENABLE_NOTIFICATIONS"):
    for name, value in os.environ.items():
        if name.startswith("SAVE_NOTIFY_"):
            data["notify_" + name[len("SAVE_NOTIFY_"):].lower()] = value
for secret in ("atlas_private_key", "notify_jira_api_token", "notify_jira_pat",
               "notify_slack_webhook_url", "notify_slack_bot_token"):
    data.pop(secret, None)                              # never remembered
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(tmp, path)
os.chmod(path, 0o600)
PYSAVE
}

choose_atlas_storage
case "$STORAGE_PROVIDER" in
  s3) storage_module=skills/aws-storage/scripts/aws_storage.py ;;
  gcs) storage_module=skills/gcp-storage/scripts/gcp_storage.py ;;
  *) storage_module="" ;;
esac
if [[ -n "$storage_module" && ! -f "$storage_module" ]]; then
  read -r -p "Storage skill missing ($storage_module). Restore skills from GitHub now? (yes/no) [no]: " restore_storage
  restore_storage="${restore_storage:-no}"; yes_no restore_storage "Restore storage skill"
  if [[ "$restore_storage" == yes ]]; then
    "$PROJECT_ROOT/samples/restore-vendored-skills.sh"
  fi
  [[ -f "$storage_module" ]] || die "Missing vendored storage file: $storage_module"
fi
prompt LOG_DATE "Log date (YYYY-MM-DD, number of days ending yesterday, or 'yesterday')" "$(saved_value log_date)"
case "$(printf '%s' "$LOG_DATE" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  yesterday|y) LOG_DATE="" ;;   # back to yesterday when a fixed date was remembered
esac
prompt TIMEZONE "Timezone used for yesterday" "Asia/Jerusalem"
# Plan all dates once in the selected timezone, oldest first. 1 means yesterday.
if ! DATE_PLAN="$(LOG_DATE_INPUT="$LOG_DATE" LOG_TIMEZONE="$TIMEZONE" python3 - <<'PYDATES'
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import re

value = os.environ["LOG_DATE_INPUT"].strip()
try:
    yesterday = datetime.now(ZoneInfo(os.environ["LOG_TIMEZONE"])).date() - timedelta(days=1)
    if not value:
        days = [yesterday]
    elif re.fullmatch(r"[0-9]+", value):
        count = int(value)
        if not 1 <= count <= 365:
            raise ValueError("Day count must be between 1 and 365")
        days = [yesterday - timedelta(days=offset) for offset in range(count - 1, -1, -1)]
    else:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Enter YYYY-MM-DD or a positive day count")
        days = [date.fromisoformat(value)]
    print("\n".join(map(str, days)))
except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
    raise SystemExit(f"Invalid date, count, or timezone: {exc}")
PYDATES
)"; then
  die "Unable to plan log dates."
fi
LOG_DATES=()
while IFS= read -r day; do LOG_DATES+=("$day"); done <<< "$DATE_PLAN"
REPORT_DATE="${LOG_DATES[${#LOG_DATES[@]}-1]}"
if [[ "$LOG_DATE" =~ ^[0-9]+$ ]]; then
  note "Processing ${#LOG_DATES[@]} days through $REPORT_DATE; reporting only $REPORT_DATE"
else
  note "Processing and reporting $REPORT_DATE"
fi

note "Atlas API configuration"
prompt CLUSTER_NAME "Atlas cluster name" ""
prompt GROUP_ID "Atlas project (group) ID" ""
prompt ATLAS_PUBLIC_KEY "Atlas API public key" "$(saved_value atlas_public_key)"
SAVED_PUBLIC_KEY="$ATLAS_PUBLIC_KEY"   # remembered; the private key below is not
read -r -s -p "Atlas API private key (input hidden): " ATLAS_PRIVATE_KEY; printf '\n'
[[ -n "$CLUSTER_NAME" && -n "$GROUP_ID" && -n "$ATLAS_PUBLIC_KEY" && -n "$ATLAS_PRIVATE_KEY" ]] || die "Atlas values are required."
prompt LOG_NAMES "Log names (comma-separated: auto,mongodb,mongos)" "auto"
prompt ENABLE_OBSERVABILITY "Collect observability for each selected day? (yes/no)" "no"
yes_no ENABLE_OBSERVABILITY "Collect observability"
OBS_HOSTS=""; QUERY_SHAPE_SOURCE="disabled"; INDEX_STATS_ENABLED="no"; MONGODB_URI_TEMPLATE=""
if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
  [[ -f skills/mongodb-observability/scripts/observability.py ]] || die "Missing observability skill. Run samples/restore-vendored-skills.sh"
  note "Observability configuration (nodes are discovered directly from Atlas)"
  prompt QUERY_SHAPE_SOURCE "Query-shape source (atlas_api/mongodb/bucket)" "atlas_api"
  [[ "$QUERY_SHAPE_SOURCE" == atlas_api || "$QUERY_SHAPE_SOURCE" == mongodb || "$QUERY_SHAPE_SOURCE" == bucket ]] || die "Use atlas_api, mongodb, or bucket."
  prompt INDEX_STATS_ENABLED "Also collect direct MongoDB indexStats? (yes/no)" "no"
  yes_no INDEX_STATS_ENABLED "Collect indexStats"
  if [[ "$INDEX_STATS_ENABLED" == yes || "$QUERY_SHAPE_SOURCE" == mongodb ]]; then
    prompt MONGODB_URI_TEMPLATE "MongoDB URI template ({host} is replaced; input hidden is not supported)" ""
    [[ -n "$MONGODB_URI_TEMPLATE" ]] || die "A MongoDB URI template is required for direct MongoDB collection."
  fi
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
      AWS_PROFILE_NAME="${STORAGE_AWS_PROFILE:-}"
    else
      prompt AWS_PROFILE_NAME "AWS profile (blank uses default credentials)" ""
    fi
    ;;
  vertex)
    note "Vertex AI configuration"
    if [[ "$STORAGE_PROVIDER" == gcs ]]; then
      GCP_PROJECT="${STORAGE_GCP_PROJECT:-}"
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
CLUSTER_SUMMARY_SOURCE=""
if [[ "$REPORT_PROVIDER" != none ]]; then
  note "Cluster summary (built when the day has at least 2 node reports)"
  printf '  extract      - cluster-summary.md (node reports + trend alerts + query shapes)\n'
  printf '  node_reports - cluster-summary.from-node-reports.md (node reports only)\n'
  printf '  both         - write both files to compare\n'
  summary_default="$(saved_value cluster_summary_source)"
  prompt CLUSTER_SUMMARY_SOURCE "Cluster summary (extract/node_reports/both)" "${summary_default:-extract}"
  CLUSTER_SUMMARY_SOURCE=$(printf '%s' "$CLUSTER_SUMMARY_SOURCE" | tr '[:upper:]-' '[:lower:]_' | tr -d '[:space:]')
  [[ "$CLUSTER_SUMMARY_SOURCE" == extract || "$CLUSTER_SUMMARY_SOURCE" == node_reports || "$CLUSTER_SUMMARY_SOURCE" == both ]] \
    || die "Choose extract, node_reports, or both."
fi

# --------------------------------------------------------------------------- notifications
# P0 -> Jira and P1 -> Slack, built from cluster/reports/ (cluster summary; node reports as fallback).
# Modes: off = nothing (default) | json = write jira-issues.json / slack-messages.json only |
#        create (Jira) / send (Slack) = real ticket / real message. Tokens are asked hidden,
#        written to a temporary 0600 file for this run only, and never remembered.
ENABLE_NOTIFICATIONS=no; NOTIFY_ENVIRONMENT=""; JIRA_MODE=off; SLACK_MODE=off
JIRA_PROJECT_KEY=""; JIRA_ISSUE_TYPE=""; JIRA_PRIORITIES=""; JIRA_DEPLOYMENT=""; JIRA_ASSIGNEE=""
JIRA_LABELS=""; JIRA_COMPONENTS=""; JIRA_PARENT_KEY=""; JIRA_BASE_URL=""; JIRA_DEDUPE_SCOPE=""; JIRA_EMAIL=""
SLACK_PRIORITIES=""; SLACK_MENTIONS=""; SLACK_REPEAT_AFTER_DAYS=""; SLACK_SEND_VIA=""; SLACK_CHANNEL=""
JIRA_API_TOKEN=""; JIRA_PAT=""; SLACK_WEBHOOK_URL=""; SLACK_BOT_TOKEN=""
confirm_real() {  # confirm_real WHAT -> 0 when the user confirms a real external action
  local answer; read -r -p "$1 Continue with real calls? (yes/no) [no]: " answer
  answer="${answer:-no}"; yes_no answer "Confirm"; [[ "$answer" == yes ]]
}
note "Notifications: Jira for P0, Slack for P1 (each off/json/create|send)"
if [[ "$REPORT_PROVIDER" == none ]]; then
  printf '  No report provider: notifications use the reports already stored for %s.\n' "$REPORT_DATE"
fi
prompt ENABLE_NOTIFICATIONS "Configure Jira/Slack notifications? (yes/no)" "$(saved_or notify_enable_notifications no)"
yes_no ENABLE_NOTIFICATIONS "Configure notifications"
if [[ "$ENABLE_NOTIFICATIONS" == yes ]]; then
  prompt NOTIFY_ENVIRONMENT "Environment tag (dev/staging/production)" "$(saved_or notify_notify_environment dev)"
  NOTIFY_ENVIRONMENT="$(lower "$NOTIFY_ENVIRONMENT")"
  [[ "$NOTIFY_ENVIRONMENT" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]] || die "Environment must be a short slug such as dev, staging or production."

  # ---- Jira (P0)
  prompt JIRA_MODE "Jira for P0 (off/json/create)" "$(saved_or notify_jira_mode off)"
  JIRA_MODE="$(lower "$JIRA_MODE")"
  [[ "$JIRA_MODE" == off || "$JIRA_MODE" == json || "$JIRA_MODE" == create ]] || die "Choose off, json, or create."
  if [[ "$JIRA_MODE" != off ]]; then
    prompt JIRA_PROJECT_KEY "Jira project key" "$(saved_value notify_jira_project_key)"
    JIRA_PROJECT_KEY="$(printf '%s' "$JIRA_PROJECT_KEY" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')"
    [[ "$JIRA_PROJECT_KEY" =~ ^[A-Z][A-Z0-9_]{0,19}$ ]] || die "Enter a Jira project key such as OPS."
    prompt JIRA_ISSUE_TYPE "Jira issue type" "$(saved_or notify_jira_issue_type Bug)"
    prompt JIRA_PRIORITIES "Priorities that open a Jira issue (comma list of P0..P3)" "$(saved_or notify_jira_priorities P0)"
    JIRA_PRIORITIES="$(printf '%s' "$JIRA_PRIORITIES" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')"
    [[ "$JIRA_PRIORITIES" =~ ^P[0-3](,P[0-3])*$ ]] || die "Jira priorities must look like P0 or P0,P1."
    prompt JIRA_DEPLOYMENT "Jira deployment (cloud/datacenter)" "$(saved_or notify_jira_deployment cloud)"
    JIRA_DEPLOYMENT="$(lower "$JIRA_DEPLOYMENT")"
    [[ "$JIRA_DEPLOYMENT" == cloud || "$JIRA_DEPLOYMENT" == datacenter ]] || die "Choose cloud or datacenter."
    if [[ "$JIRA_DEPLOYMENT" == cloud ]]; then
      prompt JIRA_ASSIGNEE "Assignee Atlassian accountId (blank = unassigned)" "$(saved_value notify_jira_assignee)"
    else
      prompt JIRA_ASSIGNEE "Assignee username (blank = unassigned)" "$(saved_value notify_jira_assignee)"
    fi
    prompt JIRA_LABELS "Extra labels, comma-separated, no spaces (blank = none)" "$(saved_value notify_jira_labels)"
    [[ "$JIRA_LABELS" != *" "* ]] || die "Jira labels cannot contain spaces."
    prompt JIRA_COMPONENTS "Components, comma-separated (blank = none)" "$(saved_value notify_jira_components)"
    prompt JIRA_PARENT_KEY "Parent/epic issue key (blank = none)" "$(saved_value notify_jira_parent_key)"
    if [[ "$JIRA_MODE" == create ]]; then
      prompt JIRA_BASE_URL "Jira base URL (https://...)" "$(saved_value notify_jira_base_url)"
      JIRA_BASE_URL="${JIRA_BASE_URL%/}"
      [[ "$JIRA_BASE_URL" == https://* ]] || die "The Jira base URL must start with https://"
      prompt JIRA_DEDUPE_SCOPE "Skip creation when a ticket exists: open tickets only or any (open/all)" "$(saved_or notify_jira_dedupe_scope open)"
      JIRA_DEDUPE_SCOPE="$(lower "$JIRA_DEDUPE_SCOPE")"
      [[ "$JIRA_DEDUPE_SCOPE" == open || "$JIRA_DEDUPE_SCOPE" == all ]] || die "Choose open or all."
      if [[ "$JIRA_DEPLOYMENT" == cloud ]]; then
        prompt JIRA_EMAIL "Jira account email (for the API token)" "$(saved_value notify_jira_email)"
        [[ -n "$JIRA_EMAIL" ]] || die "A Jira account email is required for Jira Cloud."
        read -r -s -p "Jira API token (input hidden): " JIRA_API_TOKEN; printf '\n'
        [[ -n "$JIRA_API_TOKEN" ]] || die "A Jira API token is required for JIRA_MODE=create."
      else
        read -r -s -p "Jira personal access token (input hidden): " JIRA_PAT; printf '\n'
        [[ -n "$JIRA_PAT" ]] || die "A Jira personal access token is required for JIRA_MODE=create."
      fi
      if ! confirm_real "Jira create mode opens real issues in $JIRA_PROJECT_KEY for $NOTIFY_ENVIRONMENT (existing ones are skipped)."; then
        note "Jira switched to json mode (jira-issues.json only)"
        JIRA_MODE=json; JIRA_API_TOKEN=""; JIRA_PAT=""
      fi
    fi
  fi

  # ---- Slack (P1)
  prompt SLACK_MODE "Slack for P1 (off/json/send)" "$(saved_or notify_slack_mode off)"
  SLACK_MODE="$(lower "$SLACK_MODE")"
  [[ "$SLACK_MODE" == off || "$SLACK_MODE" == json || "$SLACK_MODE" == send ]] || die "Choose off, json, or send."
  if [[ "$SLACK_MODE" != off ]]; then
    prompt SLACK_PRIORITIES "Priorities that are sent to Slack (comma list of P0..P3)" "$(saved_or notify_slack_priorities P1)"
    SLACK_PRIORITIES="$(printf '%s' "$SLACK_PRIORITIES" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')"
    [[ "$SLACK_PRIORITIES" =~ ^P[0-3](,P[0-3])*$ ]] || die "Slack priorities must look like P1 or P0,P1."
    prompt SLACK_MENTIONS "Mentions, comma-separated (e.g. <!subteam^ID>,<@UID>; blank = none)" "$(saved_value notify_slack_mentions)"
    prompt SLACK_REPEAT_AFTER_DAYS "Resend the same item at most every N days (0 = every day it is present)" "$(saved_or notify_slack_repeat_after_days 0)"
    [[ "$SLACK_REPEAT_AFTER_DAYS" =~ ^[0-9]+$ ]] || die "Enter a whole number of days."
    if [[ "$SLACK_MODE" == send ]]; then
      prompt SLACK_SEND_VIA "Send via incoming webhook or bot token (webhook/bot)" "$(saved_or notify_slack_send_via webhook)"
      SLACK_SEND_VIA="$(lower "$SLACK_SEND_VIA")"
      if [[ "$SLACK_SEND_VIA" == webhook ]]; then
        read -r -s -p "Slack incoming webhook URL (input hidden): " SLACK_WEBHOOK_URL; printf '\n'
        [[ "$SLACK_WEBHOOK_URL" == https://hooks.slack.com/* ]] || die "The webhook URL must start with https://hooks.slack.com/"
      elif [[ "$SLACK_SEND_VIA" == bot ]]; then
        prompt SLACK_CHANNEL "Slack channel ID or #name" "$(saved_value notify_slack_channel)"
        [[ -n "$SLACK_CHANNEL" ]] || die "A channel is required with a bot token."
        read -r -s -p "Slack bot token (input hidden): " SLACK_BOT_TOKEN; printf '\n'
        [[ "$SLACK_BOT_TOKEN" == xoxb-* ]] || die "A Slack bot token (xoxb-...) is required."
      else
        die "Choose webhook or bot."
      fi
      if ! confirm_real "Slack send mode posts a real message for $NOTIFY_ENVIRONMENT."; then
        note "Slack switched to json mode (slack-messages.json only)"
        SLACK_MODE=json; SLACK_WEBHOOK_URL=""; SLACK_BOT_TOKEN=""
      fi
    else
      prompt SLACK_CHANNEL "Channel written into the JSON payload (blank = none)" "$(saved_value notify_slack_channel)"
    fi
  fi
  if [[ "$JIRA_MODE" == off && "$SLACK_MODE" == off ]]; then
    note "Jira and Slack are both off: notifications disabled"
    ENABLE_NOTIFICATIONS=no
  fi
fi

# Persist selections before installing dependencies or checking S3/GCS access.
# Failed cloud authentication must not discard a newly selected S3 bucket.
save_atlas_sample_defaults
save_folder_test_choices

note "Creating Python environment"
python3 -m venv .venv-local-atlas-folder
source .venv-local-atlas-folder/bin/activate
python -m pip install --upgrade pip
# Faster log decoding for the extractor (optional; results are identical without it).
python -m pip install 'msgspec>=0.18' || note "msgspec unavailable; extractor uses orjson or stdlib json"
python - <<'PYCODE'
import sys; sys.path.insert(0, "skills/mongodb-log-diagnostic/scripts")
import extract_mongodb_log as m
print(f"Extractor parser {m.VERSION}, JSON reader: {m.JSON_BACKEND}")
PYCODE
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
NOTIFY_FILE=""
cleanup_credentials() { rm -f -- "$CONFIG_FILE"; if [[ -n "$NOTIFY_FILE" ]]; then rm -f -- "$NOTIFY_FILE"; fi; }
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
# Notification credentials go to their own temporary file (the Atlas file must stay keys-only).
if [[ -n "$JIRA_API_TOKEN$JIRA_PAT$SLACK_WEBHOOK_URL$SLACK_BOT_TOKEN" ]]; then
  NOTIFY_FILE="$(mktemp "${TMPDIR:-/tmp}/mongodb-notification-keys.XXXXXXXX")"
  N_JIRA_EMAIL="$JIRA_EMAIL" N_JIRA_API_TOKEN="$JIRA_API_TOKEN" N_JIRA_PAT="$JIRA_PAT" \
  N_SLACK_WEBHOOK_URL="$SLACK_WEBHOOK_URL" N_SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN" \
    python - "$NOTIFY_FILE" <<'PYCODE'
import json
import os
import sys
from pathlib import Path
names = {"jira_email": "N_JIRA_EMAIL", "jira_api_token": "N_JIRA_API_TOKEN", "jira_pat": "N_JIRA_PAT",
         "slack_webhook_url": "N_SLACK_WEBHOOK_URL", "slack_bot_token": "N_SLACK_BOT_TOKEN"}
data = {key: os.environ[env] for key, env in names.items() if os.environ.get(env)}
if "jira_api_token" not in data and "jira_pat" not in data:
    data.pop("jira_email", None)  # an email alone is not a credential
Path(sys.argv[1]).write_text(json.dumps(data) + "\n", encoding="utf-8")
PYCODE
  chmod 600 "$NOTIFY_FILE"
fi
unset JIRA_API_TOKEN JIRA_PAT SLACK_WEBHOOK_URL SLACK_BOT_TOKEN
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
if [[ -n "${STORAGE_AWS_REGION:-}" ]]; then export MONGODB_LOG_DIAG_AWS_REGION="$STORAGE_AWS_REGION"; fi
if [[ -n "${STORAGE_GCP_PROJECT:-}" ]]; then export MONGODB_LOG_DIAG_GCP_PROJECT="$STORAGE_GCP_PROJECT"; fi
if [[ "$REPORT_PROVIDER" != none ]]; then
  export MONGODB_LOG_DIAG_LLM_PROVIDER="$REPORT_PROVIDER"
  export MONGODB_LOG_DIAG_CLUSTER_SUMMARY_SOURCE="$CLUSTER_SUMMARY_SOURCE"
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
  unset MONGODB_LOG_DIAG_LLM_PROVIDER MONGODB_LOG_DIAG_CLUSTER_SUMMARY_SOURCE
fi
export_if() { if [[ -n "$2" ]]; then export "$1=$2"; fi; }
if [[ "$ENABLE_NOTIFICATIONS" == yes ]]; then
  export MONGODB_LOG_DIAG_ENVIRONMENT="$NOTIFY_ENVIRONMENT"
  export MONGODB_LOG_DIAG_JIRA_MODE="$JIRA_MODE" MONGODB_LOG_DIAG_SLACK_MODE="$SLACK_MODE"
  export MONGODB_LOG_DIAG_NOTIFY_SOURCE=auto
  if [[ "$JIRA_MODE" != off ]]; then
    export MONGODB_LOG_DIAG_JIRA_PROJECT_KEY="$JIRA_PROJECT_KEY" MONGODB_LOG_DIAG_JIRA_ISSUE_TYPE="$JIRA_ISSUE_TYPE"
    export MONGODB_LOG_DIAG_JIRA_PRIORITIES="$JIRA_PRIORITIES" MONGODB_LOG_DIAG_JIRA_DEPLOYMENT="$JIRA_DEPLOYMENT"
    export_if MONGODB_LOG_DIAG_JIRA_ASSIGNEE "$JIRA_ASSIGNEE"
    export_if MONGODB_LOG_DIAG_JIRA_LABELS "$JIRA_LABELS"
    export_if MONGODB_LOG_DIAG_JIRA_COMPONENTS "$JIRA_COMPONENTS"
    export_if MONGODB_LOG_DIAG_JIRA_PARENT_KEY "$JIRA_PARENT_KEY"
    export_if MONGODB_LOG_DIAG_JIRA_BASE_URL "$JIRA_BASE_URL"
    export_if MONGODB_LOG_DIAG_JIRA_DEDUPE_SCOPE "$JIRA_DEDUPE_SCOPE"
  fi
  if [[ "$SLACK_MODE" != off ]]; then
    export MONGODB_LOG_DIAG_SLACK_PRIORITIES="$SLACK_PRIORITIES"
    export MONGODB_LOG_DIAG_SLACK_REPEAT_AFTER_DAYS="$SLACK_REPEAT_AFTER_DAYS"
    export_if MONGODB_LOG_DIAG_SLACK_MENTIONS "$SLACK_MENTIONS"
    export_if MONGODB_LOG_DIAG_SLACK_CHANNEL "$SLACK_CHANNEL"
  fi
  export_if NOTIFICATION_CONFIG_FILE "$NOTIFY_FILE"
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
if [[ "$ENABLE_NOTIFICATIONS" == yes ]]; then
  # Same validation as the pipeline, before any download: bad settings fail here, not after the report.
  python - <<'PYCODE'
import json
from agent import notifications
from agent.providers import _env_config
cfg = _env_config()
s = notifications.settings(cfg)
secrets = notifications.load_secrets(cfg, "local")
if s["jira_mode"] == "create" and not (secrets.get("jira_pat") or
                                      (secrets.get("jira_email") and secrets.get("jira_api_token"))):
    raise SystemExit("JIRA_MODE=create needs Jira credentials")
if s["slack_mode"] == "send" and not (secrets.get("slack_webhook_url") or secrets.get("slack_bot_token")):
    raise SystemExit("SLACK_MODE=send needs a Slack webhook URL or bot token")
print("Notifications verified: " + json.dumps({
    "environment": s["environment"], "jira_mode": s["jira_mode"], "jira_priorities": s["jira_priorities"],
    "jira_project": s["jira"]["project_key"] or None, "slack_mode": s["slack_mode"],
    "slack_priorities": s["slack_priorities"], "credentials": sorted(secrets)}))
PYCODE
fi

show_atlas_storage_target
for CURRENT_DATE in "${LOG_DATES[@]}"; do
  note "Processing $CURRENT_DATE"
  note "Downloading from Atlas into the selected bucket"
  python -m agent.handler --stage download --log-date "$CURRENT_DATE"
  note "Extracting one node/log at a time"
  python -m agent.handler --stage extract --log-date "$CURRENT_DATE"
  if [[ "$ENABLE_OBSERVABILITY" == yes ]]; then
    note "Collecting observability one node at a time"
    # The unchanged provider loads general settings and keys-only Atlas credentials.
    # These stage-specific choices are local to this invocation, not put in the secret.
    export MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED=true
    export MONGODB_LOG_DIAG_QUERY_SHAPE_SOURCE="$QUERY_SHAPE_SOURCE"
    export MONGODB_LOG_DIAG_QUERY_SHAPE_WINDOW_HOURS=24
    # providers.py requires true/false here; the prompt answer is yes/no.
    export MONGODB_LOG_DIAG_INDEX_STATS_ENABLED="$(yes_no_to_bool "$INDEX_STATS_ENABLED")"
    if [[ -n "$MONGODB_URI_TEMPLATE" ]]; then
      export MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE="$MONGODB_URI_TEMPLATE"
    fi
    python - "$CURRENT_DATE" <<'PYCODE'
import json
import os
import sys
from agent.handler import run_pipeline
from agent.providers import load_config
from agent import skills

cfg = load_config()
source = os.environ["MONGODB_LOG_DIAG_QUERY_SHAPE_SOURCE"]
index_stats = os.environ["MONGODB_LOG_DIAG_INDEX_STATS_ENABLED"] == "true"
if cfg["input_mode"] != "atlas_api":
    raise SystemExit("Live observability requires atlas_api mode in this sample")
if source not in {"atlas_api", "mongodb", "bucket"}:
    raise SystemExit("Unsupported query-shape source")
if (index_stats or source == "mongodb") and not os.environ.get("MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE"):
    raise SystemExit("Direct MongoDB collection requires a URI template")
cfg.update({
    "observability_enabled": True,
    "deployment_type": "atlas",
    "query_shape_source": source,
    "query_shape_window_hours": int(os.environ["MONGODB_LOG_DIAG_QUERY_SHAPE_WINDOW_HOURS"]),
    "index_stats_enabled": index_stats,
})
if os.environ.get("MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE"):
    cfg["mongodb_uri_template"] = os.environ["MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE"]
log_date = sys.argv[1] or skills.atlas_logs().previous_day(cfg["timezone"])
result = run_pipeline({"stage": "observability", "log_date": log_date}, config=cfg)["observability"]
# Do not print MongoDB URI, Atlas keys, or per-node exception text.
nodes = result.get("nodes", [])
failed = sum(bool(node.get("errors")) for node in nodes)
print(json.dumps({"status": result.get("status"), "nodes": len(nodes), "nodes_with_errors": failed}))
if result.get("status") != "completed" or failed:
    raise SystemExit("Observability did not complete successfully; inspect stage logs")
PYCODE
    unset MONGODB_LOG_DIAG_OBSERVABILITY_ENABLED MONGODB_LOG_DIAG_QUERY_SHAPE_SOURCE \
      MONGODB_LOG_DIAG_QUERY_SHAPE_WINDOW_HOURS MONGODB_LOG_DIAG_INDEX_STATS_ENABLED \
      MONGODB_LOG_DIAG_MONGODB_URI_TEMPLATE
  fi
done
if [[ "$REPORT_PROVIDER" != none ]]; then
  note "Generating $REPORT_PROVIDER reports into the selected bucket"
  python -m agent.handler --stage report --log-date "$REPORT_DATE"
  case "$CLUSTER_SUMMARY_SOURCE" in
    extract) note "Cluster summary: cluster/reports/cluster-summary.md" ;;
    node_reports) note "Cluster summary: cluster/reports/cluster-summary.from-node-reports.md" ;;
    both) note "Cluster summaries: cluster/reports/cluster-summary.md and cluster-summary.from-node-reports.md" ;;
  esac
fi
notification_summary() {  # prints notifications.json; exit 3 when it does not exist for the day
  python - "$1" <<'PYCODE'
import json
import sys
from agent.common import Layout
from agent.notifications import RESULT_FILE
from agent.providers import get_store, load_config
cfg = load_config()
store, layout = get_store(cfg), Layout.from_config(cfg)
key = layout.cluster_report(sys.argv[1], RESULT_FILE)
if not store.exists(key):
    sys.exit(3)
r = json.loads(store.get_text(key))
jira, slack = r.get("jira") or {}, r.get("slack") or {}
print(json.dumps({
    "status": r.get("status"), "environment": r.get("environment"), "sources": r.get("sources"),
    "findings": len(r.get("findings") or []),
    "jira": {"mode": r.get("jira_mode"), "created": [c.get("key") for c in jira.get("created", [])],
             "existing": [e.get("key") or e.get("fingerprint") for e in jira.get("existing", [])],
             "exported": len(jira.get("exported", [])), "file": jira.get("file"),
             "errors": len(jira.get("errors", []))},
    "slack": {"mode": r.get("slack_mode"), "sent": slack.get("sent"), "items": len(slack.get("items", [])),
              "suppressed": len(slack.get("suppressed", [])), "file": slack.get("file"),
              "errors": len(slack.get("errors", []))},
    "result": store.uri(key), "ledger": r.get("ledger")}, indent=2))
PYCODE
}
if [[ "$ENABLE_NOTIFICATIONS" == yes ]]; then
  # The report stage notifies after a cluster summary (>= 2 nodes). Otherwise (one node, or no
  # report provider) run the notify stage on the stored reports; it is idempotent (ledger + Jira JQL).
  if [[ "$REPORT_PROVIDER" == none ]] || ! notification_summary "$REPORT_DATE" >/dev/null 2>&1; then
    note "Running notifications (P0 Jira: $JIRA_MODE, P1 Slack: $SLACK_MODE) for $REPORT_DATE"
    python -m agent.handler --stage notify --log-date "$REPORT_DATE" >/dev/null
  fi
  note "Notification result ($NOTIFY_ENVIRONMENT)"
  notification_summary "$REPORT_DATE" || printf 'No notifications.json was written for %s.\n' "$REPORT_DATE"
fi
show_atlas_storage_outputs "$REPORT_DATE"
printf '\nTemporary keys-only credential file(s) will be removed on exit.\n' 
