# Local samples

`samples/macos-local-atlas-folder-test.sh` is the **only** local runner. It replaces the former
Vertex, AWS and GCP test scripts, `local-atlas-storage.sh` and `restore-vendored-skills.sh`.

```bash
./samples/macos-local-atlas-folder-test.sh                 # asks for a mode
./samples/macos-local-atlas-folder-test.sh --mode atlas     # Atlas API download
./samples/macos-local-atlas-folder-test.sh --mode bucket    # logs already staged, no Atlas keys
./samples/macos-local-atlas-folder-test.sh --mode selftest  # offline tests, no credentials
./samples/macos-local-atlas-folder-test.sh --restore-skills [--source-dir DIR] [--branch BRANCH]
```

Run it from any directory; it switches to the repository root itself.

## Prerequisites

| Need | When |
|---|---|
| macOS, `python3` >= 3.9 | always (`selftest` also runs on Linux; set `ALLOW_NON_MACOS=1` to try the other modes) |
| Atlas API key pair with project read access | `atlas` mode |
| AWS credentials (`aws configure` / profile) | S3 storage or Bedrock reports |
| Google Cloud CLI + ADC login | GCS storage or Vertex reports |
| `ANTHROPIC_API_KEY` (asked hidden if unset); optional gateway/proxy base URL | `anthropic` reports |
| Claude Code CLI signed in (`claude`) | `claude_cli` reports |

The script creates `.venv-local-atlas-folder/` and installs only what the chosen options need.
Use `--no-install` on a machine without PyPI access.

## Modes

| Step | `atlas` | `bucket` | `selftest` |
|---|---|---|---|
| Storage: local folder / S3 / GCS | asked | asked | temporary local folder |
| Download (`--stage download`) | yes | skipped | – |
| Extract (`--stage extract`) | yes | yes | yes (synthetic log) |
| Observability (Atlas metrics, query shapes, indexStats) | optional | not available | – |
| Reports: `none` / `bedrock` / `vertex` / `anthropic` / `claude_cli` | optional | optional | – |
| Jira (P0) / Slack (P1): `off` / `json` / `create`·`send` | optional | optional | – |
| Unit tests + skill shell tests + `bash -n` + `compileall` | – | – | yes |

### Dates

Enter `YYYY-MM-DD`, a day count `N` (the last N days ending yesterday, oldest first), or leave it
blank/`yesterday`. Every day is downloaded and extracted. Reports and notifications run for the last
day only.

### `bucket` mode layout

Raw logs must be in `<bucket>/<prefix>/<YYYY-MM-DD>/<host>/mongodb/<name>.gz`, for example:

```text
~/Downloads/mongodb-log-local-bucket/atlas-logs/2026-11-14/node-0/mongodb/mongodb.gz
```

With local storage, the script can import a folder shaped `<dir>/<host>/*.gz` into that layout.
Outputs are written next to the raw logs: `<host>/extracts/<log>/`, `<host>/reports/<log>/`, and
`cluster/reports/` (cluster summary and `notifications.json`).

### Report models

| Provider | Prompted value | Passed to the pipeline as |
|---|---|---|
| `bedrock` | region, model/inference-profile ID, AWS profile | `MONGODB_LOG_DIAG_BEDROCK_REGION`, `MONGODB_LOG_DIAG_BEDROCK_MODEL_ID`, `AWS_PROFILE` |
| `vertex` | GCP project, location, model | `MONGODB_LOG_DIAG_VERTEX_PROJECT`, `_LOCATION`, `_MODEL` |
| `anthropic` | model, API key, base URL (blank = default) | `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`, `MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL` |
| `claude_cli` | model (blank = CLI default) | `CLAUDE_CLI_MODEL` |

#### Anthropic base URL override

With `anthropic`, the script also asks for the API base URL, for example an internal gateway or an
Anthropic-compatible proxy. Leave it blank to keep the value shown in brackets, or to use the
default `https://api.anthropic.com` when nothing is shown. Type `default` to drop a remembered or
inherited override.

- The prompt default comes from `MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL`, then `ANTHROPIC_BASE_URL`
  in your shell, then the last saved answer.
- The URL must be `https://host[:port][/path]` (`http` only for `localhost`) and must not contain
  credentials, a query string or a fragment.
- It is exported as `MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL`. When you choose the default, the script
  unsets both variables so a stale shell value cannot redirect the requests.
- The API key is still sent as `x-api-key`. Gateways that need a different auth header are not
  covered.

Non-interactive example:

```bash
export ANTHROPIC_API_KEY=...            # never saved
export MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL=https://llm-gateway.example.com/anthropic
./samples/macos-local-atlas-folder-test.sh   # choose provider: anthropic, press Enter at the URL prompt
```

A cluster summary is built when the day has at least two node reports. You can choose `extract`,
`node_reports` or `both` as its source.

### Notifications

See [docs/notifications.md](../docs/notifications.md). The `create` and `send` modes ask for a
confirmation before any real Jira or Slack call. If you decline, they fall back to `json`. When
reports are off, notifications use the reports already stored for the day. Credentials go to a
temporary 0600 file in the format of `secret.notifications.example.json`.

## What is stored, what is not

- `~/.localsample` stores non-secret defaults (storage, bucket, prefix, timezone, cluster, group,
  provider, models, mode). It is saved only when you answer `yes`.
- `~/.localsample.atlas-folder.json` stores the last log-date answer, the Atlas **public** key, the
  cluster-summary source, non-secret notification settings, and the Anthropic base URL override.
- The script never saves the Atlas private key, the MongoDB URI template, the Anthropic key, or
  Jira/Slack tokens and webhooks. The Atlas key pair is written to a temporary keys-only file.
  The pipeline rejects any other field in that file. The file is removed on exit.
- The script sets all non-secret pipeline settings as `MONGODB_LOG_DIAG_*` environment variables.
  Extra tuning variables you export yourself, such as `MONGODB_LOG_DIAG_REPORT_MAX_TOKENS`, are
  passed through. See `env.example`.

## Selftest

`--mode selftest` runs these steps and prints a PASS/FAIL summary:

1. `bash -n` on every shell script.
2. `python -m compileall` on `agent/`, `google_adk_agent/` and `skills/*/scripts`.
3. `python -m unittest discover -s tests`.
4. `skills/*/tests/*.sh`.
5. An existing-bucket extract on a generated log in a temporary folder.

It exits non-zero when any step fails.

## Restoring vendored skills

If skill files are missing, the script offers to restore all five skills. Restoring clones the
`observability-clean` branch by default. You can restore manually:

```bash
./samples/macos-local-atlas-folder-test.sh --restore-skills                 # clone observability-clean
./samples/macos-local-atlas-folder-test.sh --restore-skills --branch main
./samples/macos-local-atlas-folder-test.sh --restore-skills --source-dir ~/src/checkout
```

## Migrating from the removed scripts

| Removed | Use instead |
|---|---|
| `macos-local-aws-atlas-test.sh` | `--mode atlas`, storage `s3`, provider `bedrock` |
| `macos-local-atlas-vertex-test.sh` | `--mode atlas`, provider `vertex` (any storage) |
| `macos-local-gcp-test.sh` | `--mode bucket`, storage `local` or `gcs`, provider `vertex` or `none` |
| `macos-deploy-vertex-existing-bucket.sh` | Deployment, not a local test. See `README-deploy.md` |
| `restore-vendored-skills.sh` | `--restore-skills` |
| `local-atlas-storage.sh` | inlined |
| `secret.*.example.json` (mixed settings) | The pipeline accepts keys-only secrets. Use `../secret.example.json` and `MONGODB_LOG_DIAG_*` env vars |
