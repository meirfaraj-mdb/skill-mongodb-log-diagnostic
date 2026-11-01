#!/usr/bin/env bash
# $collStats for all non-system collections, per data-bearing node, via mongosh (no python, no curl API exists).
# -> <MDIAG_OUT>/<date>/<host_dir>/collStats/coll-stats.json. Password is prompted (never on the command line).
# DB user needs listDatabases (cluster) + listCollections, collStats on the user databases
# (built-in clusterMonitor has them, or the diagObservability custom role).
set -eu
: "${MDIAG_OUT:=./observability-manual}" "${MDIAG_DATE:=$(date -u +%F)}" "${HOSTS_FILE:=./hosts.txt}" "${MDIAG_USER:?}"
read -r -a opts <<< "${MDIAG_MONGOSH_OPTS:-}"
js() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g"; }   # quote for a JS '...' string
wt=false; [ -n "${MDIAG_WT_DETAILS:-}" ] && wt=true
script=$(cd "$(dirname "$0")" && pwd)/coll_stats.js
while read -r hp type dir <&3; do                # hosts on fd 3: stdin stays free for the password prompt
  case "${type:-}" in SHARD_MONGOS|SHARD_CONFIG*) continue;; esac     # stats are per mongod
  H=${hp%:*}; DIR="$MDIAG_OUT/$MDIAG_DATE/${dir:-${hp%:*}}"
  mongosh "mongodb://$hp/?directConnection=true&readPreference=secondaryPreferred" -u "$MDIAG_USER" \
    --authenticationDatabase "${MDIAG_AUTH_DB:-admin}" ${opts[@]+"${opts[@]}"} --quiet \
    --eval "var MDIAG_HOST='$(js "$H")', MDIAG_DIR='$(js "$DIR")', MDIAG_WT_DETAILS=$wt; load('$(js "$script")')" \
    || echo "!! $hp failed" >&2
done 3< <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS_FILE")
