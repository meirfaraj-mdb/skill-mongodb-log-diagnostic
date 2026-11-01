#!/usr/bin/env bash
# $collStats for all non-system collections, per data-bearing node, via mongosh (no python, no curl API exists).
# -> <MDIAG_OUT>/<date>/<host_dir>/collStats/coll-stats.json. Password is prompted (never on the command line).
# DB user needs listDatabases (cluster) + listCollections, collStats on the user databases
# (built-in clusterMonitor has them, or the diagObservability custom role).
set -eu
: "${MDIAG_OUT:=./observability-manual}" "${MDIAG_DATE:=$(date -u +%F)}" "${HOSTS_FILE:=./hosts.txt}" "${MDIAG_USER:?}"
read -r -a opts <<< "${MDIAG_MONGOSH_OPTS:-}"
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS_FILE" | while read -r hp type dir; do
  case "${type:-}" in SHARD_MONGOS|SHARD_CONFIG*) continue;; esac     # stats are per mongod
  export H=${hp%:*} DIR="$MDIAG_OUT/$MDIAG_DATE/${dir:-${hp%:*}}"
  mongosh "mongodb://$hp/?directConnection=true&readPreference=secondaryPreferred" -u "$MDIAG_USER" \
    --authenticationDatabase "${MDIAG_AUTH_DB:-admin}" ${opts[@]+"${opts[@]}"} --quiet --file "$(dirname "$0")/coll_stats.js" </dev/tty \
    || echo "!! $hp failed" >&2
done
