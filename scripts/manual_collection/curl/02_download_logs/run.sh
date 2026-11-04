#!/usr/bin/env bash
# Downloads mongod (or mongos) logs per host into <host_dir>/mongodb/<log>.gz. Skips files already present.
# Role: Project Data Access Read Only (or Project Cluster Log Viewer).
. "$(dirname "$0")/../_common.sh"
q=${MDIAG_START_S:+"?startDate=$MDIAG_START_S&endDate=$MDIAG_END_S"}     # default: last 24 h
hosts | while read -r pid type dir; do
  log=mongodb; [ "${type:-}" = SHARD_MONGOS ] && log=mongos
  out="$DAY/$(host_dir "$pid" "${type:-}" "${dir:-}")/mongodb/$log.gz"
  [ -s "$out" ] && { echo "skip $out (exists)"; continue; }
  api "$out" "/groups/$GROUP/clusters/${pid%:*}/logs/$log.gz$q" "application/vnd.atlas.2023-02-01+gzip" \
    || { echo "!! $pid: $(head -c 300 "$out" 2>/dev/null)" >&2; mv -f "$out" "$out.error.json" 2>/dev/null || true; }
done
