#!/usr/bin/env bash
# $queryStats is a database command, not an API: this one uses mongosh (still no python).
# DB user needs queryStatsRead (built-in clusterMonitor has it). MongoDB 7.0.12+/8.0+.
# Hosts file: first column host:port. mongosh prompts for the password (never on the command line).
# Skips nodes whose query-stats.json already has shapes (e.g. from 05); MDIAG_REFRESH=1 forces.
set -eu
: "${MDIAG_OUT:=./observability-manual}" "${MDIAG_DATE:=$(date -u +%F)}" "${HOSTS_FILE:=./hosts.txt}" "${MDIAG_USER:?}"
read -r -a opts <<< "${MDIAG_MONGOSH_OPTS:-}"
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS_FILE" | while read -r hp type dir; do
  [ "${type:-}" = SHARD_MONGOS ] && dir=${dir:-${hp/:/_}}
  export H=${hp%:*} OUT="$MDIAG_OUT/$MDIAG_DATE/${dir:-${hp%:*}}/queryStats/query-stats.json"
  if [ -z "${MDIAG_REFRESH:-}" ] && grep -qs queryShapeHash "$OUT"; then echo "skip $OUT (has shapes)"; continue; fi
  mongosh "mongodb://$hp/?directConnection=true" -u "$MDIAG_USER" --authenticationDatabase "${MDIAG_AUTH_DB:-admin}" \
    ${opts[@]+"${opts[@]}"} --quiet --file "$(dirname "$0")/query_stats.js" </dev/tty || echo "!! $hp failed" >&2
done
