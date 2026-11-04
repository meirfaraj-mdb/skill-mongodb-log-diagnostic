#!/usr/bin/env bash
# Ops Manager host measurements + one disk partition -> <host_dir>/metrics/metrics.json.
# Hosts file lines: "hostId hostname:port [host_dir]" (hostId from GET /groups/{G}/hosts).
# API key role: Project Read Only. Runs with MDIAG_TARGET=opsmanager.
export MDIAG_TARGET=opsmanager
. "$(dirname "$0")/../_common.sh"
w="granularity=${MDIAG_GRANULARITY:-PT5M}&period=P1D"; [ -n "${MDIAG_START:-}" ] && w="granularity=${MDIAG_GRANULARITY:-PT5M}&start=$MDIAG_START&end=$MDIAG_END"
part=${MDIAG_PARTITION:-data}
if [ "${1:-}" = --list ]; then api "$DAY/cluster/manifests/om-hosts.json" "/groups/$GROUP/hosts?itemsPerPage=500"; exit; fi
hosts | while read -r hid hp dir; do
  api "$TMPF.p" "/groups/$GROUP/hosts/$hid/measurements?$w" || { echo "!! $hid: $(head -c 300 "$TMPF.p")" >&2; continue; }
  api "$TMPF.d" "/groups/$GROUP/hosts/$hid/disks/$part/measurements?$w" || echo '{}' > "$TMPF.d"
  { printf '['; cat "$TMPF.p"; printf ']'; } > "$TMPF.data"
  envelope "$DAY/$(host_dir "$hp" "" "${dir:-}")/metrics/metrics.json" ops_manager_metrics "${hp%:*}" window "$TMPF.data" \
    "\"hostId\":\"$hid\",\"disk_measurements\":[{\"partitionName\":\"$part\",\"data\":[$(cat "$TMPF.d")]}]"
done
