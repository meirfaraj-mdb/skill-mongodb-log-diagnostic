# Atlas Agent Engine wrapper (LangGraph)

This adds Atlas Agent Engine LangGraph orchestration around the existing
`agent.handler.run_pipeline` implementation. It does not replace or fork the
shared diagnostic pipeline, and does not modify the GCP ADK deployment.

## Files

- `src/mongodb_log_diagnostic_atlas/main.py` — Atlas `App`, LangGraph tool-calling
  graph, and pipeline tools.
- `src/mongodb_log_diagnostic_atlas/runtime.py` — maps Atlas runtime settings to
  the existing AWS/GCP/local pipeline configuration and safely adapts injected
  Atlas credential JSON to the existing `ATLAS_CONFIG_FILE` interface.
- `scripts/install_atlas_wrapper.py` — overlays the wrapper into an official
  Atlas Python LangGraph workspace, copies the shared `agent/` and `skills/`
  source, and adds required packages to its `pyproject.toml`.
- `requirements-atlas.txt` — dependency list added by the installer.
- `env.atlas.example` — non-secret configuration template.

## Prepare the Atlas workspace

Use the Atlas CLI to scaffold a Python LangGraph workspace first:

```bash
agentengine create --template hello-world-agent --name "mongodb-log-diagnostic-atlas"
```

The official scaffold includes `agent.yaml` and `pyproject.toml`. Run the
installer against the workspace directory and the **Python file path referenced
by that workspace's `agent.yaml`**. Example (adjust paths if the scaffold uses a
different module name):

```bash
python3 /path/to/atlas_langgraph_wrapper/scripts/install_atlas_wrapper.py \
  --repo-root /path/to/skill-mongodb-log-diagnostic-main \
  --workspace /path/to/mongodb-log-diagnostic-atlas/agents/mongodb-log-diagnostic-atlas \
  --entrypoint-file src/mongodb_log_diagnostic_atlas/main.py
```

The installer deliberately requires an initialized workspace instead of
inventing Atlas manifest fields. It writes the wrapper to the path you specify;
that path must match the Python entrypoint declared in `agent.yaml`. It copies
only `agent/` and `skills/` from the repository. It excludes `.env` files,
virtual environments, `.git`, compressed/raw logs, and Python cache files.

## Configure the model

The orchestration model is distinct from the report-generation model selected
by `MONGODB_LOG_DIAG_LLM_PROVIDER`.

Default AWS path (Bedrock through LangChain's AWS integration):

```text
ATLAS_AGENT_MODEL_PROVIDER=bedrock
ATLAS_AGENT_MODEL=YOUR_BEDROCK_MODEL_ID
ATLAS_AGENT_MODEL_REGION=us-east-1
```

The runtime must have an AWS credential source with least-privilege Bedrock
invoke access. Prefer a scoped runtime identity; do not paste AWS keys into the
source or local example file.

For an Anthropic-compatible endpoint, set
`ATLAS_AGENT_MODEL_PROVIDER=anthropic-compatible`,
`ATLAS_AGENT_MODEL`, `ATLAS_AGENT_MODEL_BASE_URL`, and store `LLM_API_KEY` with
the Atlas Agent Engine secret command. The endpoint must be reachable from the
agent's configured egress policy.

## Configure the shared pipeline

`CLOUD_PROVIDER` describes the existing pipeline's storage/secret backend; it
must remain `aws`, `gcp`, or `local`. **Do not set it to `atlas`.** Set
`MONGODB_LOG_DIAG_PIPELINE_CLOUD` explicitly when bucket names are unqualified,
or set `MONGODB_LOG_DIAG_STORAGE_PROVIDER` to `s3`, `gcs`, or `local`. The
wrapper sets `CLOUD_PROVIDER` immediately before calling the shared handler.

For existing bucket inputs, configure values such as:

```text
MONGODB_LOG_DIAG_INPUT_MODE=existing_bucket
MONGODB_LOG_DIAG_BUCKET=reports-result
MONGODB_LOG_DIAG_INPUT_BUCKET=log-test-mfaraj
MONGODB_LOG_DIAG_STORAGE_PROVIDER=s3
MONGODB_LOG_DIAG_PIPELINE_CLOUD=aws
MONGODB_LOG_DIAG_PREFIX=atlas-logs
```

Set the other `MONGODB_LOG_DIAG_*` values required by the shared config loader
as in the repository's `env.example`. The deployment workspace's generated
manifest/secret mechanism should hold runtime env values; `env.atlas.example`
is only a template.

For Atlas API collection, choose one of:

1. Existing AWS/GCP Secret Manager integration via `ATLAS_SECRET_ID`, with the
   runtime identity granted access; or
2. Store `MONGODB_LOG_DIAG_ATLAS_CREDENTIALS_JSON` as an Atlas Agent Engine
   secret. Its value must be JSON containing only `atlas_public_key` and
   `atlas_private_key`. The wrapper writes it to a mode-0600 temporary file,
   points the shared loader at it for the call, and removes the file afterward.

Never place secret values in `env.atlas.example`, source control, command-line
arguments, logs, or this ZIP.

## Test and deploy

From the generated Atlas workspace:

```bash
agentengine dev up
agentengine atlas setup
# Configure required model/pipeline secrets and non-secret runtime settings.
agentengine deploy --auto
agentengine invoke --session diagnostics-1 "Run diagnostics for 2026-10-13"
```

For Bedrock with direct AWS IAM authentication, ensure the runtime identity and
egress policy permit Bedrock invocation. For API-key model connections, store the
key with the CLI's secret facility; the exact provider and endpoint must also be
allowed by the workspace egress configuration.

## Verification boundary

The wrapper is syntax-checked and has local unit tests for backend selection and
TOML dependency updates. No live Atlas Agent Engine deployment, external model
call, AWS/GCP storage access, or production credentials were exercised here.
