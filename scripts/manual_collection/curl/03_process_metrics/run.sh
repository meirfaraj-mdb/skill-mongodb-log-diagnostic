#!/usr/bin/env bash
# Process measurements (all metrics) + one disk partition per host -> <host_dir>/metrics/metrics.json.
# Role: Project Read Only. MDIAG_GRANULARITY (PT5M), MDIAG_PARTITION (data; see /processes/{id}/disks).
. "$(dirname "$0")/../_common.sh"
w="granularity=${MDIAG_GRANULARITY:-PT5M}&period=P1D"; [ -n "${MDIAG_START:-}" ] && w="granularity=${MDIAG_GRANULARITY:-PT5M}&start=$MDIAG_START&end=$MDIAG_END"
part=${MDIAG_PARTITION:-data}
hosts | while read -r pid type dir; do
  api "$TMPF.p" "/groups/$GROUP/processes/$pid/measurements?$w" || { echo "!! $pid: $(head -c 300 "$TMPF.p")" >&2; continue; }
  api "$TMPF.d" "/groups/$GROUP/processes/$pid/disks/$part/measurements?$w" || echo '{}' > "$TMPF.d"
  { printf '['; cat "$TMPF.p"; printf ']'; } > "$TMPF.data"
  envelope "$DAY/$(host_dir "$pid" "${type:-}" "${dir:-}")/metrics/metrics.json" atlas_api_metrics "${pid%:*}" window "$TMPF.data" \
    "\"processId\":\"$pid\",\"disk_measurements\":[{\"partitionName\":\"$part\",\"data\":[$(cat "$TMPF.d")]}]"
done
