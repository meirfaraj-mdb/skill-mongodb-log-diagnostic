#!/usr/bin/env bash
# Query Shape Insights: per host first; if that fails or is empty, ONE cluster-wide call whose result is copied
# into every such node file (scope=cluster - the API gives no per-node attribution). Nodes whose
# query-stats.json already has shapes are skipped (MDIAG_REFRESH=1 forces). Needs ATLAS_CLUSTER.
# Role: Project Observability Viewer or Project Data Access Read Only (or higher). M10+, MongoDB 8.0+.
. "$(dirname "$0")/../_common.sh"
NOTE="Cluster-wide Query Shape Insights copied to this node because the per-host request returned no data. Summaries do not identify the process that ran them: do not attribute these shapes to this node."
base="/groups/$GROUP/clusters/${ATLAS_CLUSTER:?}/queryShapeInsights/summaries?nSummaries=100"
[ -n "${MDIAG_START_S:-}" ] && base="$base&since=${MDIAG_START_S}000&until=${MDIAG_END_S}000"
hosts | while read -r pid type dir; do
  case "${type:-}" in SHARD_CONFIG*) continue;; esac
  out="$DAY/$(host_dir "$pid" "${type:-}" "${dir:-}")/queryStats/query-stats.json"
  if [ -z "${MDIAG_REFRESH:-}" ] && grep -qs queryShapeHash "$out"; then echo "skip $out (has shapes)"; continue; fi
  # 1) per host, as before
  if api "$TMPF" "$base&processIds=$pid" && grep -q queryShapeHash "$TMPF"; then
    envelope "$out" atlas_query_shape_insights "${pid%:*}" log_day_window "$TMPF" "\"processId\":\"$pid\",\"scope\":\"process\""
    continue
  fi
  reason=no_reads_on_this_process; grep -qs '"summaries"' "$TMPF" || reason=per_process_request_failed
  case "${type:-}" in SHARD_MONGOS) ;; SHARD_*) [ $reason = no_reads_on_this_process ] && reason=collected_on_mongos_only;; esac
  # 2) all hosts: one cluster-wide call per run, also stored once under cluster/queryStats/
  if [ ! -s "$TMPF.cluster" ]; then
    api "$TMPF.cluster" "$base" || { echo "!! cluster-wide call failed: $(head -c 300 "$TMPF.cluster")" >&2; rm -f "$TMPF.cluster"; continue; }
    envelope "$DAY/cluster/queryStats/query-stats.json" atlas_query_shape_insights "" log_day_window "$TMPF.cluster" '"scope":"cluster"'
  fi
  if ! grep -q queryShapeHash "$TMPF.cluster"; then
    envelope "$out" atlas_query_shape_insights "${pid%:*}" log_day_window "$TMPF.cluster" \
      "\"processId\":\"$pid\",\"scope\":\"process\",\"diagnostics\":{\"reason\":\"no_shapes_in_cluster\"}"
    continue
  fi
  # 3) copy ("split") the cluster-wide list into this node file, marked as not node-attributed
  envelope "$out" atlas_query_shape_insights "${pid%:*}" log_day_window "$TMPF.cluster" \
    "\"processId\":null,\"requestedProcessId\":\"$pid\",\"scope\":\"cluster\",\"attribution_note\":\"$NOTE\",\"diagnostics\":{\"reason\":\"$reason\",\"fallback\":\"cluster_wide_copy\"}"
done
