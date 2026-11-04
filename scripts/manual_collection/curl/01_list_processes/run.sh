#!/usr/bin/env bash
# Lists the project's processes and writes hosts.generated.txt ("processId typeName"). Role: Project Read Only.
. "$(dirname "$0")/../_common.sh"
out="$DAY/cluster/manifests/processes.json"
api "$out" "/groups/$GROUP/processes?itemsPerPage=500" || { cat "$out" >&2; exit 1; }
ids=$(grep -o '"id" *: *"[^"]*:[0-9]*"' "$out" | sed 's/.*"\([^"]*\)"$/\1/')
types=$(grep -o '"typeName" *: *"[^"]*"' "$out" | sed 's/.*"\([^"]*\)"$/\1/')
if [ "$(echo "$ids" | wc -l)" = "$(echo "$types" | wc -l)" ]; then paste -d' ' <(echo "$ids") <(echo "$types"); else echo "$ids"; fi \
  | { grep -E "${MDIAG_HOST_FILTER:-.}" || true; } > hosts.generated.txt
cat hosts.generated.txt; echo "-> review, keep this cluster's lines, save as $HOSTS_FILE" >&2
