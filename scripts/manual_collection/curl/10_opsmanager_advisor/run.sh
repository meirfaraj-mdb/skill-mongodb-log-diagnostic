#!/usr/bin/env bash
# Ops Manager Performance Advisor suggested indexes per host -> <host_dir>/advisor/advisor.json.
# Hosts lines: "hostId hostname:port [host_dir]". Role: Project Read Only; field values in sample queries
# need Project Data Access Read Only (or higher). Ops Manager has no query-stats API: use 07 for $queryStats.
export MDIAG_TARGET=opsmanager
. "$(dirname "$0")/../_common.sh"
q=""; [ -n "${MDIAG_START_S:-}" ] && q="?since=${MDIAG_START_S}000&duration=$(( (MDIAG_END_S - MDIAG_START_S) * 1000 ))"
hosts | while read -r hid hp dir; do
  api "$TMPF" "/groups/$GROUP/hosts/$hid/performanceAdvisor/suggestedIndexes$q" || { echo "!! $hid: $(head -c 300 "$TMPF")" >&2; continue; }
  { printf '['; cat "$TMPF"; printf ']'; } > "$TMPF.data"
  envelope "$DAY/$(host_dir "$hp" "" "${dir:-}")/advisor/advisor.json" ops_manager_advisor "${hp%:*}" window "$TMPF.data" "\"hostId\":\"$hid\""
done
