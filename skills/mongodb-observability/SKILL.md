# MongoDB Observability Collection Skill

Collect optional, read-only diagnostics and upload them through the shared pipeline.

* `index_stats`: connects directly to each configured MongoDB node and runs `$indexStats` for every non-system collection. `$indexStats` is node-local, so collect it per node.
* `query_stats`: Atlas-only. Uses Atlas Query Shape Insights API, not a MongoDB connection. It is disabled for Ops Manager and other self-managed deployments.
* `existing_bucket` mode: never invokes this skill, opens no MongoDB connection, and makes no Atlas API request.

Output paths:

```text
<prefix>/<date>/<node>/indexStats/index-stats.json
<prefix>/<date>/<node>/queryStats/query-stats.json
```

Configuration (only non-bucket mode):

```json
{
  "observability_enabled": true,
  "deployment_type": "atlas",
  "mongodb_uri_template": "mongodb://diagnostic-reader:...@{host}:27017/?tls=true",
  "index_stats_hosts": ["hostname1", "hostname2"],
  "index_stats_max_collections": 1000,
  "query_stats_max_summaries": 100
}
```

The direct MongoDB account needs read-only privileges sufficient for `listDatabases`, `listCollections`, and `$indexStats` (MongoDB documents `clusterMonitor` as sufficient for `$indexStats`). Never place this URI in output artifacts. Atlas Query Shape Insights requires an Atlas project API credential in the existing secret and an Atlas project role that can retrieve query-shape summaries.
