#!/usr/bin/env bash
# Run mongodb_collect_observability.js on every node listed in a hosts file (one host:port per line).
# Credentials are never placed on the command line: mongosh prompts for the password of each node,
# unless you authenticate with X.509 / Kerberos / OIDC through MDIAG_MONGOSH_OPTS.
#
#   MDIAG_USER=diag_reader MDIAG_TIMEZONE=Asia/Jerusalem MDIAG_OUT_DIR=./observability-manual \
#   MDIAG_MONGOSH_OPTS="--tls --tlsCAFile ca.pem" ./run_mongosh_all_nodes.sh hosts.txt
set -euo pipefail
HOSTS_FILE=${1:?usage: $0 hosts.txt}
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
: "${MDIAG_OUT_DIR:=./observability-manual}"; export MDIAG_OUT_DIR
read -r -a EXTRA <<< "${MDIAG_MONGOSH_OPTS:-}"
AUTH=()
[[ -n "${MDIAG_USER:-}" ]] && AUTH=(-u "$MDIAG_USER" --authenticationDatabase "${MDIAG_AUTH_DB:-admin}")
rc=0
while IFS= read -r host || [[ -n "$host" ]]; do
  host=${host%%#*}; host=$(echo "$host" | xargs)
  [[ -z "$host" ]] && continue
  echo "=== $host"
  if ! mongosh "mongodb://$host/?directConnection=true" "${AUTH[@]}" "${EXTRA[@]}" --quiet \
       --file "$SCRIPT_DIR/mongodb_collect_observability.js"; then
    echo "!!! $host failed" >&2; rc=2
  fi
done < "$HOSTS_FILE"
echo "Output: $MDIAG_OUT_DIR"
exit $rc
