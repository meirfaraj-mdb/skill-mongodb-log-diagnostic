# Atlas logs configuration schema

| Key | Required | Example | Notes |
|---|---|---|---|
| `atlas_public_key` | yes | `abcdefgh` | Atlas API key with Project Data Access Read Only (or higher) |
| `atlas_private_key` | yes | `…` | Secret: never log |
| `group_id` | yes | 24-hex project id | |
| `cluster_name` | yes | `my-cluster` | Used for host matching when no selector is given |
| `timezone` | yes | `Asia/Jerusalem` | Defines the calendar day (D-1) |
| `api_version` | yes | `2025-03-12` | Atlas versioned API media type |
| `log_names` | no | `["auto"]` | `auto` = `mongodb` for mongod, `mongos` for routers |
| `host_selector` | no | `^my-cluster-` | Regex over `hostname userAlias` |
| `hostnames` | no | `["my-cluster-shard-00-00.x.mongodb.net"]` | Exact `userAlias` list; overrides selector |
| `atlas_base_url` | no | `https://cloud.mongodb.com/api/atlas/v2` | |
| `http_timeout_seconds` | no | `120` | |
| `max_retries` | no | `5` | Retries on 429/5xx and network errors |

Host matching priority is: `hostnames`, then `host_selector`, then `cluster_name` (equal or `cluster_name-` prefix).
Processes with `typeName == NO_DATA` are ignored.
