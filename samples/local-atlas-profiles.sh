#!/usr/bin/env bash
# Named profiles for samples/macos-local-atlas-folder-test.sh (--profile NAME).
# Source this file after defining note and die. Compatible with the macOS bash 3.2.
#
#   <dir>/<NAME>.json          every answer except secrets (0600)
#   <dir>/<NAME>.secrets.json  secrets, only when you choose to save them (0600, plain text)
#
# <dir> is $LOCALSAMPLE_PROFILES_DIR or ~/.localsample.profiles (0700). JSON is read as data and
# never sourced as shell code. Secrets are passed to Python through the environment, never argv.
LOCALSAMPLE_PROFILES_DIR="${LOCALSAMPLE_PROFILES_DIR:-$HOME/.localsample.profiles}"
PROFILE_NAME="${PROFILE_NAME:-}"
PROFILE_FILE=""; PROFILE_SECRETS_FILE=""
ASSUME_YES="${ASSUME_YES:-no}"
# Answers remembered in <NAME>.json (shell variable names of the prompts).
PROFILE_VALUE_KEYS="STORAGE_PROVIDER LOCAL_BUCKET BUCKET STORAGE_AWS_REGION STORAGE_AWS_PROFILE
  STORAGE_GCP_PROJECT PREFIX LOG_DATE TIMEZONE CLUSTER_NAME GROUP_ID ATLAS_PUBLIC_KEY LOG_NAMES
  ENABLE_OBSERVABILITY QUERY_SHAPE_SOURCE INDEX_STATS_ENABLED REPORT_PROVIDER BEDROCK_REGION
  BEDROCK_MODEL_ID AWS_PROFILE_NAME GCP_PROJECT VERTEX_LOCATION VERTEX_MODEL ANTHROPIC_MODEL
  ANTHROPIC_BASE_URL_OVERRIDE CLAUDE_CLI_MODEL CLUSTER_SUMMARY_SOURCE ENABLE_NOTIFICATIONS
  NOTIFY_ENVIRONMENT JIRA_MODE JIRA_PROJECT_KEY JIRA_ISSUE_TYPE JIRA_PRIORITIES JIRA_DEPLOYMENT
  JIRA_ASSIGNEE JIRA_LABELS JIRA_COMPONENTS JIRA_PARENT_KEY JIRA_BASE_URL JIRA_DEDUPE_SCOPE JIRA_EMAIL
  SLACK_MODE SLACK_PRIORITIES SLACK_MENTIONS SLACK_REPEAT_AFTER_DAYS SLACK_SEND_VIA SLACK_CHANNEL"
# Saved only in <NAME>.secrets.json, only after an explicit yes. The URI template can hold a password.
PROFILE_SECRET_KEYS="ATLAS_PRIVATE_KEY ANTHROPIC_API_KEY MONGODB_URI_TEMPLATE JIRA_API_TOKEN JIRA_PAT
  SLACK_WEBHOOK_URL SLACK_BOT_TOKEN"
PROFILE_KEYS_STR=" "    # keys present in the loaded profile
PROMPTED_VARS=" "       # answers given (or accepted) in this run; only these are written back

profile_usage() {
  cat <<'USAGE'
Usage: samples/macos-local-atlas-folder-test.sh [options]

  --profile NAME         use the answers saved in profile NAME as defaults; at the end you can
                         save this run's answers (and, if you choose, its secrets) to NAME
  --yes, -y              with --profile: accept every saved answer without asking. Secrets not
                         saved in the profile are still asked. Jira create / Slack send run
                         without a new confirmation only if the profile already has that mode.
  --list-profiles        list saved profiles and exit
  --delete-profile NAME  delete profile NAME and its saved secrets, then exit
  -h, --help             show this help

Profiles live in $LOCALSAMPLE_PROFILES_DIR (default ~/.localsample.profiles).
USAGE
}

_profile_valid_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ && "$1" != *..* ]]; }
_profile_is_secret() { [[ " $(printf '%s' "$PROFILE_SECRET_KEYS" | tr -s '[:space:]' ' ') " == *" $1 "* ]]; }

profile_select() {  # profile_select NAME -> sets PROFILE_FILE / PROFILE_SECRETS_FILE
  _profile_valid_name "$1" || die "Invalid profile name '$1': use letters, digits, '.', '_' or '-' (max 64)."
  PROFILE_NAME="$1"
  PROFILE_FILE="$LOCALSAMPLE_PROFILES_DIR/$1.json"
  PROFILE_SECRETS_FILE="$LOCALSAMPLE_PROFILES_DIR/$1.secrets.json"
}

_profile_dump() {  # prints KEY\0VALUE\0 pairs then __END__\0\0; non-zero exit on unsafe files
  PROFILE_ALLOWED="$PROFILE_VALUE_KEYS" PROFILE_SECRETS_ALLOWED="$PROFILE_SECRET_KEYS" \
    python3 - "$PROFILE_FILE" "$PROFILE_SECRETS_FILE" <<'PYPROFILE'
import json, os, re, stat, sys

def load(path, section, allowed, secret):
    if not os.path.lexists(path):
        return {}
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        sys.exit(f"Refusing to read {path}: not a regular file")
    if info.st_uid != os.getuid():
        sys.exit(f"Refusing to read {path}: owned by another user")
    if secret and info.st_mode & 0o077:
        sys.exit(f"Refusing to read {path}: readable by others. Run: chmod 600 '{path}'")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.exit(f"Cannot read {path}: {exc.__class__.__name__}")
    values = data.get(section) if isinstance(data, dict) else None
    if not isinstance(values, dict):
        sys.exit(f"Cannot read {path}: no '{section}' object")
    return {k: v for k, v in values.items()
            if k in allowed and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", k) and isinstance(v, str)
            and "\0" not in v}

values = load(sys.argv[1], "values", set(os.environ["PROFILE_ALLOWED"].split()), False)
values.update(load(sys.argv[2], "secrets", set(os.environ["PROFILE_SECRETS_ALLOWED"].split()), True))
out = sys.stdout.buffer
for key, value in values.items():
    out.write(key.encode() + b"\0" + value.encode("utf-8") + b"\0")
out.write(b"__END__\0\0")
PYPROFILE
}

profile_load() {  # loads the selected profile (if its files exist) into PROFILE_V__<KEY>
  local key value complete=no
  PROFILE_KEYS_STR=" "
  [[ -n "$PROFILE_FILE" ]] || return 0
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    if [[ "$key" == __END__ ]]; then complete=yes; break; fi
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    printf -v "PROFILE_V__$key" '%s' "$value"
    PROFILE_KEYS_STR+="$key "
  done < <(_profile_dump)
  [[ "$complete" == yes ]] || die "Could not load profile '$PROFILE_NAME' (see the message above)."
}

profile_lookup() {  # profile_lookup VAR -> 0 and PROFILE_VALUE when the profile has VAR
  PROFILE_VALUE=""
  [[ "$PROFILE_KEYS_STR" == *" $1 "* ]] || return 1
  local name="PROFILE_V__$1"
  PROFILE_VALUE="${!name}"
}

profile_has_secrets() { [[ -n "$PROFILE_SECRETS_FILE" && -f "$PROFILE_SECRETS_FILE" ]]; }

profile_mark() { [[ "$PROMPTED_VARS" == *" $1 "* ]] || PROMPTED_VARS+="$1 "; }  # remember VAR for saving

profile_describe() {
  local secrets=no
  if profile_has_secrets; then secrets=yes; fi
  if [[ -f "$PROFILE_FILE" ]]; then
    note "Profile $PROFILE_NAME ($PROFILE_FILE; saved secrets: $secrets)"
  else
    note "Profile $PROFILE_NAME is new: answer the questions, then choose to save them at the end"
    [[ "$ASSUME_YES" != yes ]] || die "--yes needs an existing profile; run once without --yes to create '$PROFILE_NAME'."
  fi
}

# prompt_secret VAR LABEL: hidden input; Enter keeps the value saved in the profile.
prompt_secret() {
  local var="$1" label="$2" value=""
  profile_mark "$var"
  if profile_lookup "$var" && [[ -n "$PROFILE_VALUE" ]]; then
    if [[ "$ASSUME_YES" == yes ]]; then
      printf '%s: [saved in profile %s]\n' "$label" "$PROFILE_NAME"
      printf -v "$var" '%s' "$PROFILE_VALUE"
      return 0
    fi
    read -r -s -p "$label (input hidden; Enter keeps the one saved in profile $PROFILE_NAME): " value
    printf '\n'
    printf -v "$var" '%s' "${value:-$PROFILE_VALUE}"
  else
    read -r -s -p "$label (input hidden): " value
    printf '\n'
    printf -v "$var" '%s' "$value"
  fi
}

# profile_preconfirmed VAR VALUE: --yes and the profile already holds VAR=VALUE (confirmed when saved).
profile_preconfirmed() { [[ "$ASSUME_YES" == yes ]] && profile_lookup "$1" && [[ "$PROFILE_VALUE" == "$2" ]]; }

_profile_write() {  # _profile_write yes|no  (secrets) -- values come from PROFILE_SAVE_* env vars
  local save_secrets="$1" key keys="" secret_keys=""
  for key in $PROMPTED_VARS; do
    if _profile_is_secret "$key"; then secret_keys+="$key "; else keys+="$key "; fi
  done
  (
    # export is a shell builtin: values never appear on a process command line.
    for key in $keys $secret_keys; do export "PROFILE_SAVE_$key=${!key-}"; done
    PROFILE_SAVE_KEYS="$keys" PROFILE_SAVE_SECRET_KEYS="$secret_keys" PROFILE_SAVE_SECRETS="$save_secrets" \
      PROFILE_SAVE_NAME="$PROFILE_NAME" \
      exec python3 - "$LOCALSAMPLE_PROFILES_DIR" "$PROFILE_FILE" "$PROFILE_SECRETS_FILE" <<'PYSAVE'
import json, os, stat, sys, tempfile
from datetime import datetime, timezone
directory, path, secrets_path = sys.argv[1:]
env = os.environ
os.makedirs(directory, mode=0o700, exist_ok=True)
info = os.lstat(directory)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
    sys.exit(f"Refusing to use {directory}: not a directory owned by you")
os.chmod(directory, 0o700)

def read(target, section):
    if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
        sys.exit(f"Refusing to replace a symlink or non-file: {target}")
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)[section]
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}

def write(target, section, values):
    fd, tmp = tempfile.mkstemp(prefix=".profile-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"profile": env["PROFILE_SAVE_NAME"], "version": 1,
                       "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       section: dict(sorted(values.items()))}, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

# Merge: answers not asked in this run (e.g. another report provider) are kept.
values = read(path, "values")
values.update({k: env.get("PROFILE_SAVE_" + k, "") for k in env["PROFILE_SAVE_KEYS"].split()})
write(path, "values", values)
message = f"Saved profile to {path} (mode 0600)"
if env["PROFILE_SAVE_SECRETS"] == "yes":
    secrets = read(secrets_path, "secrets")
    for key in env["PROFILE_SAVE_SECRET_KEYS"].split():
        value = env.get("PROFILE_SAVE_" + key, "")
        if value:
            secrets[key] = value
        else:
            secrets.pop(key, None)  # declined or cleared in this run: do not keep an old one
    if secrets:
        write(secrets_path, "secrets", secrets)
        message += f"; secrets ({', '.join(sorted(secrets))}) in {secrets_path} (mode 0600, plain text)"
elif os.path.lexists(secrets_path):
    os.unlink(secrets_path)
    message += f"; removed saved secrets {secrets_path}"
print(message + ".")
PYSAVE
  )
}

profile_save_choices() {  # asks, then writes the profile (and optionally the secrets file)
  local answer default_secrets=no
  if [[ "$ASSUME_YES" == yes ]]; then
    note "Profile $PROFILE_NAME left unchanged (--yes)"
    return 0
  fi
  read -r -p "Save these answers to profile $PROFILE_NAME ($PROFILE_FILE)? (yes/no) [yes]: " answer
  answer="${answer:-yes}"; yes_no answer "Save profile"
  [[ "$answer" == yes ]] || return 0
  if profile_has_secrets; then default_secrets=yes; fi
  printf 'Secrets: Atlas private key, Anthropic API key, MongoDB URI template, Jira/Slack tokens and webhook.\n'
  printf 'They are stored in plain text in %s (mode 0600, only your user can read it).\n' "$PROFILE_SECRETS_FILE"
  read -r -p "Also save the secrets of this run to the profile? ('no' deletes saved ones) (yes/no) [$default_secrets]: " answer
  answer="${answer:-$default_secrets}"; yes_no answer "Save secrets"
  _profile_write "$answer" || printf 'warning: could not save profile %s\n' "$PROFILE_NAME" >&2
}

profile_list() {
  python3 - "$LOCALSAMPLE_PROFILES_DIR" <<'PYLIST'
import json, os, sys
directory = sys.argv[1]
names = sorted(f[:-5] for f in os.listdir(directory) if f.endswith(".json") and not f.endswith(".secrets.json")) \
    if os.path.isdir(directory) else []
if not names:
    print(f"No profiles in {directory}")
for name in names:
    try:
        with open(os.path.join(directory, name + ".json"), encoding="utf-8") as handle:
            data = json.load(handle)
        v = data.get("values", {})
        where = v.get("LOCAL_BUCKET") or v.get("BUCKET") or "?"
        detail = (f"{v.get('STORAGE_PROVIDER', '?')}:{where}/{v.get('PREFIX', '?')} "
                  f"cluster={v.get('CLUSTER_NAME') or '(all)'} report={v.get('REPORT_PROVIDER', '?')}")
        updated = data.get("updated", "?")
    except (OSError, ValueError, AttributeError):
        detail, updated = "(unreadable)", "?"
    secrets = "yes" if os.path.exists(os.path.join(directory, name + ".secrets.json")) else "no"
    print(f"{name:<20} secrets={secrets:<3} updated={updated}  {detail}")
PYLIST
}

profile_delete() {  # profile_delete NAME
  local answer
  profile_select "$1"
  [[ -e "$PROFILE_FILE" || -e "$PROFILE_SECRETS_FILE" ]] || die "No profile named '$1' in $LOCALSAMPLE_PROFILES_DIR."
  read -r -p "Delete profile $1 and its saved secrets? (yes/no) [no]: " answer
  answer="${answer:-no}"; yes_no answer "Delete profile"
  [[ "$answer" == yes ]] || return 0
  rm -f -- "$PROFILE_FILE" "$PROFILE_SECRETS_FILE"
  printf 'Deleted profile %s.\n' "$1"
}

profile_forget() {  # drop loaded profile values (incl. secrets) from shell memory once used
  local key
  for key in $PROFILE_KEYS_STR; do unset "PROFILE_V__$key"; done
  PROFILE_KEYS_STR=" "
}
