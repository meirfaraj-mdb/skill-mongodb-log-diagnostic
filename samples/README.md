# Local samples

`samples/macos-local-atlas-folder-test.sh` is the **only** local runner. It replaces the former
Vertex, AWS and GCP test scripts, `local-atlas-storage.sh` and `restore-vendored-skills.sh`.

```bash
./samples/macos-local-atlas-folder-test.sh                 # asks for a mode
./samples/macos-local-atlas-folder-test.sh --mode atlas     # Atlas API download
./samples/macos-local-atlas-folder-test.sh --mode bucket    # logs already staged, no Atlas keys
./samples/macos-local-atlas-folder-test.sh --mode selftest  # offline tests, no credentials
./samples/macos-local-atlas-folder-test.sh --restore-skills [--source-dir DIR] [--branch BRANCH]
./samples/macos-local-atlas-folder-test.sh --profile prod         # answers from profile "prod"
./samples/macos-local-atlas-folder-test.sh --profile prod --yes   # replay "prod" without questions
./samples/macos-local-atlas-folder-test.sh --list-profiles
./samples/macos-local-atlas-folder-test.sh --delete-profile prod
```

Run it from any directory; it switches to the repository root itself.

## Prerequisites

| Need | When |
|---|---|
| macOS, `python3` >= 3.9 | always (`selftest` also runs on Linux; set `ALLOW_NON_MACOS=1` to try the other modes) |
| Atlas API key pair with project read access | `atlas` mode |
| AWS credentials (`aws configure` / profile) | S3 storage or Bedrock reports |
| Google Cloud CLI + ADC login | GCS storage or Vertex reports |
| `ANTHROPIC_API_KEY` (asked hidden if unset); optional complete gateway/proxy Messages URL | `anthropic` reports |
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

Observability reruns reuse what is already stored: a node's `metrics/metrics.json` that covers the
complete day (same timezone, granularity and measurements, no 429/5xx error) and a query-stats file
with rows are not requested from Atlas again. A day collected while it was still in progress is
refreshed. Export `MONGODB_LOG_DIAG_METRICS_SKIP_EXISTING=false` (or run
`python -m agent.handler --stage observability --refresh-observability`) to force new calls.
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
| `anthropic` | model, API key, complete Messages URL (blank = default) | `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`, `MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL` |
| `claude_cli` | model (blank = CLI default) | `CLAUDE_CLI_MODEL` |

#### Anthropic Messages URL override

With `anthropic`, the script also asks for the **complete** Messages URL, for example an internal
gateway or an Anthropic-compatible proxy. The URL is used as-is: every request is POSTed to it and
nothing (no `/v1/messages`) is appended, so include the full path.

```text
Anthropic Messages URL, complete incl. /v1/messages (blank = default https://api.anthropic.com/v1/messages): https://llm-gateway.example.com/anthropic/v1/messages
==> Anthropic endpoint: https://llm-gateway.example.com/anthropic/v1/messages
```

- Leave it blank to keep the value shown in brackets, or to use the default
  `https://api.anthropic.com/v1/messages` (Anthropic SDK) when nothing is shown. Type `default` to
  drop a remembered or inherited override.
- The prompt default comes from `MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL` in your shell, then the last
  saved answer.
- The URL must be `https://host[:port]/path` (`http` only for `localhost`) and must not contain
  credentials, a query string or a fragment. A bare host such as `https://gateway.example.com` is
  rejected because it has no path. A URL with a path that does not end with `/messages` is accepted
  with a warning.
- LiteLLM proxies usually expose `https://<proxy>/v1/messages` (unified Anthropic-format endpoint)
  or `https://<proxy>/anthropic/v1/messages` (Anthropic pass-through). Ask your gateway owner
  which one is enabled.
- The override is sent with the Python standard library (`urllib`), so it does not need the
  `anthropic` or `httpx` packages. The default endpoint still uses the `anthropic` SDK.
- The override streams the answer (`"stream": true`, server-sent events), so a long report keeps
  bytes flowing and gateway idle timeouts (HTTP 504) do not cut it. Export
  `MONGODB_LOG_DIAG_ANTHROPIC_STREAM=false` only if your gateway cannot stream. If you still get a
  504 with streaming on, the gateway timed out before the first byte: ask its owner to raise the
  request timeout, or lower `MONGODB_LOG_DIAG_REPORT_MAX_INPUT_CHARS`.
- It is exported as `MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL`. The script always unsets the SDK's own
  `ANTHROPIC_BASE_URL`, which is only a base that the SDK appends `/v1/messages` to.
- Requests send `x-api-key: $ANTHROPIC_API_KEY` and `anthropic-version: 2023-06-01`. Gateways that
  need a different auth header are not covered.

Non-interactive example:

```bash
export ANTHROPIC_API_KEY=...            # never saved
export MONGODB_LOG_DIAG_ANTHROPIC_BASE_URL=https://llm-gateway.example.com/anthropic/v1/messages
./samples/macos-local-atlas-folder-test.sh   # choose provider: anthropic, press Enter at the URL prompt
```

A cluster summary is built when the day has at least two node reports. You can choose `extract`,
`node_reports` or `both` as its source.

### Notifications

See [docs/notifications.md](../docs/notifications.md). The `create` and `send` modes ask for a
confirmation before any real Jira or Slack call. If you decline, they fall back to `json`. When
reports are off, notifications use the reports already stored for the day. Credentials go to a
temporary 0600 file in the format of `secret.notifications.example.json`.

## Profiles

`--profile NAME` keeps one complete set of answers per environment, for example `dev` and `prod`.

1. **First run** with a new name: answer the questions as usual. Before any install or cloud check
   the script asks:
   - `Save these answers to profile NAME?` (default `yes`). Saves every answer, including the
     storage, cluster, report provider, Anthropic URL, notification settings and log date.
   - `Also save the secrets of this run to the profile?` (default `no` for a new profile). Saves the
     Atlas private key, the Anthropic API key, the MongoDB URI template, and the Jira/Slack tokens
     and webhook URL. Answering `no` deletes secrets saved earlier.
2. **Next runs** with `--profile NAME`: every question shows the saved answer. Press Enter to keep
   it, or type `-` to answer blank (for example a blank cluster name = every cluster). Secret
   questions say `Enter keeps the one saved in profile NAME` and never show the value.
3. **`--yes`** uses every saved answer and secret without asking. It only asks for secrets the
   profile does not have, and for questions it has no answer for. Jira `create` and Slack `send` run
   without a new confirmation only when the profile already has that mode (it was confirmed when it
   was saved). Without `--yes`, real Jira/Slack calls are always confirmed again. `--yes` does not
   change the profile.

Files, in `~/.localsample.profiles/` (0700; override with `LOCALSAMPLE_PROFILES_DIR`):

| File | Content | Mode |
|---|---|---|
| `NAME.json` | all non-secret answers | 0600 |
| `NAME.secrets.json` | secrets, only if you chose to save them, **plain text** | 0600 |

- The script refuses a secrets file that another user owns, or that group or others can read. Fix it with
  `chmod 600 ~/.localsample.profiles/NAME.secrets.json`.
- Files are read as JSON data, never sourced as shell code. Secrets reach the Python helpers through
  the environment, never the command line.
- An `ANTHROPIC_API_KEY` exported in your shell still wins over the profile. It is saved only
  if you choose to save secrets.
- With `--profile`, `~/.localsample` and `~/.localsample.atlas-folder.json` are only read, as
  defaults for answers the profile does not have yet. They are not updated.
- The saved log date is replayed as is. Save a day count (for example `1` = yesterday) rather than
  a fixed date if you use `--yes` every day.

## What is stored, what is not

- `~/.localsample` stores non-secret defaults (storage, bucket, prefix, timezone, cluster, group,
  provider, models, mode). It is saved only when you answer `yes`.
- `~/.localsample.atlas-folder.json` stores the last log-date answer, the Atlas **public** key, the
  cluster-summary source, non-secret notification settings, and the Anthropic Messages URL override.
- Without `--profile`, the script never saves the Atlas private key, the MongoDB URI template, the
  Anthropic key, or Jira/Slack tokens and webhooks. With `--profile`, they are saved only to
  `NAME.secrets.json` and only if you say so (see [Profiles](#profiles)). The Atlas key pair is
  written to a temporary keys-only file.
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
