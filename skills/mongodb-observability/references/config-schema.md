# Optional observability configuration

| Key | Required | Meaning |
|---|---:|---|
| `observability_enabled` | no | Enables the stage, default `false`. |
| `deployment_type` | for `auto` | `atlas`, `ops_manager`, or self-managed deployment type. |
| `mongodb_uri_template` | index stats or MongoDB query shapes | Direct node URI template; `{host}` is substituted. |
| `index_stats_hosts` | when enabled | Node hostnames to collect from. |
| `index_stats_enabled` | no | Default `true`; set false to collect only query shapes. |
| `index_stats_max_collections` | no | Collection safety cap, default 1000. |
| `query_shape_source` | no | `auto`, `atlas_api`, `mongodb`, `bucket`, or `disabled`; default `auto`. |
| `query_shape_window_hours` | no | Atlas rolling-window size, default 24. |
| `query_stats_max_summaries` | no | Result safety cap, default 100. |
| `atlas_query_stats_url` | Atlas API | Approved Atlas Query Shape Insights endpoint. It may use `{group_id}`, `{cluster_name}`, `{host}` placeholders. |
| `atlas_process_ids` | no | Optional host-to-process-ID mapping for an Atlas API per-node filter. |
| `atlas_query_stats_start_param` | no | Atlas API window-start parameter, default `startDate`. |
| `atlas_query_stats_end_param` | no | Atlas API window-end parameter, default `endDate`. |
| `atlas_query_stats_process_id_param` | no | Atlas API process-ID parameter, default `processIds`. |

`input_mode: existing_bucket` never calls MongoDB or Atlas. It only uses already-uploaded `<node>/queryStats/query-stats.json` files.
