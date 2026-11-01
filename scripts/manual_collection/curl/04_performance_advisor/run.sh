#!/usr/bin/env bash
# Performance Advisor suggested indexes per host -> <host_dir>/advisor/advisor.json.
# Role: Project Read Only (values in sample queries are redacted unless Project Data Access Read Only or higher).
. "$(dirname "$0")/../_common.sh"
q=""; [ -n "${MDIAG_START_S:-}" ] && q="?since=${MDIAG_START_S}000&duration=$(( (MDIAG_END_S - MDIAG_START_S) * 1000 ))"
hosts | while read -r pid type dir; do
  [ "${type:-}" = SHARD_MONGOS ] && continue      # suggestions come from mongod processes
  api "$TMPF" "/groups/$GROUP/processes/$pid/performanceAdvisor/suggestedIndexes$q" || { echo "!! $pid: $(head -c 300 "$TMPF")" >&2; continue; }
  { printf '['; cat "$TMPF"; printf ']'; } > "$TMPF.data"
  envelope "$DAY/$(host_dir "$pid" "${type:-}" "${dir:-}")/advisor/advisor.json" atlas_api_advisor "${pid%:*}" window "$TMPF.data" "\"processId\":\"$pid\""
done
