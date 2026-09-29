# Optional observability configuration

| Key | Required | Meaning |
|---|---:|---|
| `observability_enabled` | no | Enables the stage, default false. Ignored in `existing_bucket` mode. |
| `deployment_type` | yes when enabled | `atlas` or `ops_manager`. Query stats only runs for `atlas`. |
| `mongodb_uri_template` | index stats only | Direct node URI template, with `{host}` substituted. |
| `index_stats_hosts` | index stats only | Hostnames to collect from. |
| `index_stats_max_collections` | no | Collection safety cap, default 1000. |
| `query_stats_max_summaries` | no | Atlas Query Shape Insights page size, default 100. |
| `atlas_query_stats_url` | no | Pin the current Atlas Query Shape Insights summaries endpoint when your approved Atlas API version uses a different route. |
