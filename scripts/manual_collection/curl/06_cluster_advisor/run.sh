#!/usr/bin/env bash
# Cluster-level Performance Advisor: drop-index suggestions + schema advice -> cluster/advisor/cluster-advisor.json.
# Role: Project Read Only. Needs ATLAS_CLUSTER.
. "$(dirname "$0")/../_common.sh"
c="/groups/$GROUP/clusters/${ATLAS_CLUSTER:?}/performanceAdvisor"
api "$TMPF.d" "$c/dropIndexSuggestions" || { echo "!! drop: $(head -c 300 "$TMPF.d")" >&2; echo null > "$TMPF.d"; }
api "$TMPF.s" "$c/schemaAdvice"         || { echo "!! schema: $(head -c 300 "$TMPF.s")" >&2; echo null > "$TMPF.s"; }
{ printf '{"drop_index_suggestions":'; cat "$TMPF.d"; printf ',"schema_advice":'; cat "$TMPF.s"; printf '}'; } > "$TMPF.data"
envelope "$DAY/cluster/advisor/cluster-advisor.json" atlas_api_cluster_advisor "" current_snapshot "$TMPF.data" "\"cluster\":\"$ATLAS_CLUSTER\""
