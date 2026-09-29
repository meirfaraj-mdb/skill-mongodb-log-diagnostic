# MongoDB Observability Collection Skill

Collect optional, read-only diagnostics and upload them through the shared pipeline. Each node is completed before the next node starts.

## Outputs

```text
<prefix>/<date>/<node>/indexStats/index-stats.json
<prefix>/<date>/<node>/queryStats/query-stats.json
```

`indexStats` is collected directly from each node because `$indexStats` counters are node-local.

## Query-shape sources

Set `query_shape_source` to one of:

| Source | When to use it | Behavior |
|---|---|---|
| `atlas_api` | Atlas deployment | Calls the configured Atlas Query Shape Insights API for a rolling previous 24-hour UTC window. |
| `mongodb` | Ops Manager/self-managed or when direct access is preferred | Runs the read-only `$queryStats` aggregation against each configured node. It records a snapshot plus the requested 24-hour window. |
| `bucket` | A query-shape JSON was already uploaded | Makes no Atlas or MongoDB call; preserves/reuses `<node>/queryStats/query-stats.json`. |
| `auto` | Default | Uses `atlas_api` for `deployment_type: atlas`; otherwise uses `mongodb`. |
| `disabled` | Skip query shapes | Does not collect or replace query-shape data. |

In `input_mode: existing_bucket`, this skill never opens a MongoDB connection or calls Atlas. It only reports whether existing per-node query-shape files are present; reports consume those files automatically.

## Configuration

```json
{
  "observability_enabled": true,
  "deployment_type": "atlas",
  "mongodb_uri_template": "mongodb://diagnostic-reader:...@{host}:27017/?tls=true",
  "index_stats_hosts": ["hostname1", "hostname2"],
  "query_shape_source": "atlas_api",
  "query_shape_window_hours": 24,
  "atlas_query_stats_url": "https://cloud.mongodb.com/api/atlas/v2/groups/{group_id}/clusters/{cluster_name}/queryShapeInsights/summaries",
  "atlas_process_ids": {"hostname1": "hostname1:27017"}
}
```

The direct MongoDB account needs read-only permissions sufficient for `listDatabases`, `listCollections`, `$indexStats`, and `$queryStats`. Never place the URI in outputs. Atlas Query Shape Insights uses the existing Atlas project API key; `atlas_query_stats_url` can pin the organization-approved endpoint/API version.
