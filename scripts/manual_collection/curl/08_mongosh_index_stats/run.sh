#!/usr/bin/env bash
# $indexStats + index definitions per node via mongosh (no python). Run on each data-bearing mongod (not mongos).
# DB user needs listDatabases, listCollections, listIndexes, indexStats (built-in clusterMonitor has them).
set -eu
: "${MDIAG_OUT:=./observability-manual}" "${MDIAG_DATE:=$(date -u +%F)}" "${HOSTS_FILE:=./hosts.txt}" "${MDIAG_USER:?}"
read -r -a opts <<< "${MDIAG_MONGOSH_OPTS:-}"
js() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g"; }   # quote for a JS '...' string
script=$(cd "$(dirname "$0")" && pwd)/index_stats.js
while read -r hp type dir <&3; do                # hosts on fd 3: stdin stays free for the password prompt
  case "${type:-}" in SHARD_MONGOS|SHARD_CONFIG*) continue;; esac
  H=${hp%:*}; DIR="$MDIAG_OUT/$MDIAG_DATE/${dir:-${hp%:*}}"
  mongosh "mongodb://$hp/?directConnection=true&readPreference=secondaryPreferred" -u "$MDIAG_USER" \
    --authenticationDatabase "${MDIAG_AUTH_DB:-admin}" ${opts[@]+"${opts[@]}"} --quiet \
    --eval "var MDIAG_HOST='$(js "$H")', MDIAG_DIR='$(js "$DIR")'; load('$(js "$script")')" \
    || echo "!! $hp failed" >&2
done 3< <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS_FILE")
