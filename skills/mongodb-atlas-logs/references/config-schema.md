# Atlas logs configuration schema

| Key | Required | Example | Notes |
|---|---|---|---|
| `atlas_public_key` | yes | `abcdefgh` | Atlas API key with Project Data Access Read Only (or higher) |
| `atlas_private_key` | yes | `…` | Secret: never log |
| `group_id` | yes | 24-hex project id | |
| `cluster_name` | no | `my-cluster` | Only this cluster's nodes are selected. Empty = every cluster of the project (see below) |
| `timezone` | yes | `Asia/Jerusalem` | Defines the calendar day (D-1) |
| `api_version` | yes | `2025-03-12` | Atlas versioned API media type |
| `log_names` | no | `["auto"]` | `auto` = `mongodb` for mongod, `mongos` for routers |
| `host_selector` | no | `^my-cluster-` | Regex over `hostname userAlias` |
| `hostnames` | no | `["my-cluster-shard-00-00.x.mongodb.net"]` | Exact `userAlias` list; overrides selector |
| `atlas_base_url` | no | `https://cloud.mongodb.com/api/atlas/v2` | |
| `http_timeout_seconds` | no | `120` | |
| `max_retries` | no | `5` | Retries on 429/5xx and network errors |

Host matching: `hostnames` (exact list) overrides everything. Otherwise, when `cluster_name` is set, a project
process is kept only if it belongs to that cluster: its host/alias is in the cluster connection strings
(`GET /groups/{id}/clusters/{name}`), or its host label is `<prefix>-shard-NN-NN` / `<prefix>-config-NN-NN`, or its
replica set is `<prefix>-shard-N` / `<prefix>-config-N`, where `<prefix>` is the cluster name or an internal
`atlas-xxxx` prefix learned from the cluster's own hosts. `cluster-a` never matches `cluster-a-eu`. `host_selector`
only narrows that set further. Processes with `typeName == NO_DATA` are ignored.

Empty `cluster_name`: `list_clusters()` returns the project clusters (paused ones skipped) and the caller runs each
cluster separately. The CLI writes `<output-dir>/<cluster>/<date>/...`; the agent writes `<prefix>/<cluster>/<date>/...`.
With a cluster selected the layout is unchanged (`<prefix>/<date>/...`).
