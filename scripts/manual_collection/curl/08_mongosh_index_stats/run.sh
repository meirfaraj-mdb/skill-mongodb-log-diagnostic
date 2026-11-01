#!/usr/bin/env bash
# $indexStats + index definitions per node via mongosh (no python). Run on each data-bearing mongod (not mongos).
# DB user needs listDatabases, listCollections, listIndexes, indexStats (built-in clusterMonitor has them).
set -eu
: "${MDIAG_OUT:=./observability-manual}" "${MDIAG_DATE:=$(date -u +%F)}" "${HOSTS_FILE:=./hosts.txt}" "${MDIAG_USER:?}"
read -r -a opts <<< "${MDIAG_MONGOSH_OPTS:-}"
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS_FILE" | while read -r hp type dir; do
  case "${type:-}" in SHARD_MONGOS|SHARD_CONFIG*) continue;; esac
  export H=${hp%:*} DIR="$MDIAG_OUT/$MDIAG_DATE/${dir:-${hp%:*}}"
  mongosh "mongodb://$hp/?directConnection=true&readPreference=secondaryPreferred" -u "$MDIAG_USER" \
    --authenticationDatabase "${MDIAG_AUTH_DB:-admin}" ${opts[@]+"${opts[@]}"} --quiet --file "$(dirname "$0")/index_stats.js" </dev/tty \
    || echo "!! $hp failed" >&2
done
