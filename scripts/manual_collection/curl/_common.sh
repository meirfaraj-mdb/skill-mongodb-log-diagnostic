# Sourced by every run.sh. Needs only bash + curl (no python, no jq).
#   Atlas:       ATLAS_GROUP_ID, ATLAS_CLUSTER, and ATLAS_PUBLIC_KEY/ATLAS_PRIVATE_KEY
#                (or ATLAS_CLIENT_ID/ATLAS_CLIENT_SECRET for a service account)
#   Ops Manager: OM_BASE_URL, OM_GROUP_ID, OM_PUBLIC_KEY/OM_PRIVATE_KEY  (set MDIAG_TARGET=opsmanager)
#   Common:      MDIAG_OUT (./observability-manual), MDIAG_DATE (today UTC), HOSTS_FILE (./hosts.txt)
#                MDIAG_START / MDIAG_END: optional window, ISO UTC (2026-10-29T22:00:00Z). Default: last 24 h.
set -eu
: "${MDIAG_OUT:=./observability-manual}" "${MDIAG_DATE:=$(date -u +%F)}" "${HOSTS_FILE:=./hosts.txt}"
DAY="$MDIAG_OUT/$MDIAG_DATE"
CURLCFG=$(mktemp); chmod 600 "$CURLCFG"; TMPF=$(mktemp); trap 'rm -f "$CURLCFG" "$TMPF" "$TMPF".*' EXIT
# Secrets go to a private curl config file (-K), never on the command line.
if [ "${MDIAG_TARGET:-atlas}" = opsmanager ]; then
  BASE="${OM_BASE_URL:?set OM_BASE_URL}/api/public/v1.0"; GROUP="${OM_GROUP_ID:?}"; ACCEPT=application/json
  printf 'digest\nuser = "%s:%s"\n' "${OM_PUBLIC_KEY:?}" "${OM_PRIVATE_KEY:?}" > "$CURLCFG"
  [ -n "${OM_CA_FILE:-}" ] && printf 'cacert = "%s"\n' "$OM_CA_FILE" >> "$CURLCFG"
else
  BASE="${ATLAS_BASE:-https://cloud.mongodb.com/api/atlas/v2}"; GROUP="${ATLAS_GROUP_ID:?}"
  ACCEPT="application/vnd.atlas.${ATLAS_API_VERSION:-2025-03-12}+json"
  if [ -n "${ATLAS_CLIENT_ID:-}" ]; then
    printf 'user = "%s:%s"\n' "$ATLAS_CLIENT_ID" "${ATLAS_CLIENT_SECRET:?}" > "$CURLCFG"
    TOKEN=$(curl -sS -K "$CURLCFG" -X POST -H 'Accept: application/json' -d grant_type=client_credentials \
      "${ATLAS_TOKEN_URL:-https://cloud.mongodb.com/api/oauth/token}" | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')
    [ -n "$TOKEN" ] || { echo "service-account token request failed" >&2; exit 1; }
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$CURLCFG"
  else
    printf 'digest\nuser = "%s:%s"\n' "${ATLAS_PUBLIC_KEY:?}" "${ATLAS_PRIVATE_KEY:?}" > "$CURLCFG"
  fi
fi
[ -n "${CURL_CA_FILE:-}" ] && printf 'cacert = "%s"\n' "$CURL_CA_FILE" >> "$CURLCFG"
if [ -n "${MDIAG_START:-}" ]; then          # GNU date; on macOS set MDIAG_START_S / MDIAG_END_S (epoch seconds)
  : "${MDIAG_START_S:=$(date -u -d "$MDIAG_START" +%s)}" "${MDIAG_END_S:=$(date -u -d "${MDIAG_END:?}" +%s)}"
fi

# api <out-file> <path?query> [accept]  -> 0 on HTTP 200; the body is kept even on error.
api() {
  mkdir -p "$(dirname "$1")"
  local code; code=$(curl -sS -K "$CURLCFG" -H "Accept: ${3:-$ACCEPT}" -o "$1" -w '%{http_code}' "$BASE$2") || code=000
  echo "  HTTP $code $2" >&2; [ "$code" = 200 ]
}
# envelope <out> <source> <host> <basis> <data-file> [extra json fields, no trailing comma]
envelope() {
  mkdir -p "$(dirname "$1")"
  { printf '{"schema_version":2,"source":"%s","host":"%s","temporal_basis":"%s","collector":"curl-sample","retrievedAt":"%s",%s"data":\n' \
      "$2" "$3" "$4" "$(date -u +%FT%TZ)" "${6:+$6,}"; cat "$5"; printf '}\n'; } > "$1"
  echo "  wrote $1" >&2
}
# hosts: lines "processId [typeName] [host_dir]"  (Ops Manager: "hostId hostname:port [host_dir]")
hosts() { sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS_FILE"; }
# host_dir: sanitized hostname as the log downloader uses; a co-located mongos gets hostname_port.
host_dir() { # <processId> <type> <override>
  local h=${1%:*} p=${1##*:}; [ -n "${3:-}" ] && { echo "$3"; return; }
  [ "${2:-}" = SHARD_MONGOS ] && h="${h}_$p"; echo "$h" | sed 's/[^A-Za-z0-9._-]/_/g'
}
