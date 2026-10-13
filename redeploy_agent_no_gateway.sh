#!/bin/sh
# Run from the repository root. Update an EXISTING no-gateway Agent Engine.
set -eu
: "${PROJECT:?Set PROJECT}"
: "${REGION:?Set REGION}"
: "${ENGINE_ID:?Set ENGINE_ID to the no-gateway engine}"
: "${DISPLAY_NAME:=MongoDB diagnostics — no gateway}"
: "${ENV_FILE:?Set ENV_FILE}"
: "${AGENT_DIR:=google_adk_agent}"
: "${ADK_BIN:=adk}"
for tool in "$ADK_BIN" python3 gcloud; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing command: $tool" >&2; exit 1; }
done
[ -f "$ENV_FILE" ] && [ -d "$AGENT_DIR" ] && [ -d agent ] && [ -d skills ] || {
  echo 'Run from repository root with env file, ADK package, agent/, and skills/.' >&2; exit 1;
}
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/mongodb-no-gateway.XXXXXXXX")
trap 'rm -rf "$TMP_ROOT"' 0
trap 'exit 130' INT
trap 'exit 143' HUP TERM
# Refuse to update an engine with a gateway. An empty config is NOT an unbind operation.
command -v curl >/dev/null 2>&1 || { echo "Missing command: curl" >&2; exit 1; }
curl -fsS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT}/locations/${REGION}/reasoningEngines/${ENGINE_ID}" > "$TMP_ROOT/engine.json"
python3 - "$TMP_ROOT/engine.json" <<'CHECK_ENGINE'
import json, sys
with open(sys.argv[1]) as f:
    engine = json.load(f)
if engine.get('spec', {}).get('deploymentSpec', {}).get('agentGatewayConfig'):
    raise SystemExit('Target engine has a gateway binding. Use the no-gateway engine ID.')
CHECK_ENGINE
# One source package for ADK 1.x; staging avoids agent.py/agent-directory collision.
STAGED_AGENT="$TMP_ROOT/$(basename "$AGENT_DIR")"
python3 - "$AGENT_DIR" "$STAGED_AGENT" <<'STAGE_SOURCE'
"""Stage shared dependencies inside ADK's single source package; no repo edits."""
import ast
from pathlib import Path
import shutil
import sys

source, target = map(Path, sys.argv[1:3])
repo = Path.cwd()
ignored = shutil.ignore_patterns('.git', '.venv*', '__pycache__', '*.pyc', '.adk', '.env*', '*.pem', '*.key', '*secret*', '*credentials*', '.gitignore', '.gcloudignore', '.ae_ignore')
shutil.copytree(source, target, ignore=ignored)
(target / '_shared').mkdir()
for name in ('agent', 'skills'):
    if (target / '_shared' / name).exists():
        raise SystemExit(f'Staging conflict: {target / name}')
    shutil.copytree(repo / name, target / "_shared" / name, ignore=ignored)
# Preserve the original entry point. Add import resolution before its imports,
# but after any module docstring and __future__ declarations.
entry = target / 'agent.py'
text = entry.read_text(encoding='utf-8')
tree = ast.parse(text)
line = 0
for i, node in enumerate(tree.body):
    if (i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)) or (isinstance(node, ast.ImportFrom) and node.module == '__future__'):
        line = node.end_lineno
    else:
        break
lines = text.splitlines(keepends=True)
bootstrap = '\n# Generated staging-only import path for bundled shared packages.\nimport sys as _stage_sys\nfrom pathlib import Path as _StagePath\n_stage_sys.path.insert(0, str(_StagePath(__file__).resolve().parent / "_shared"))\n'
lines.insert(line, bootstrap)
entry.write_text(''.join(lines), encoding='utf-8')
ast.parse(entry.read_text())
for required in ('__init__.py', 'agent.py', 'requirements.txt', '_shared/agent/handler.py'):
    if not (target / required).is_file():
        raise SystemExit(f'Missing staged file: {required}')
print('Staged ADK entry point, shared agent package, and skills (no PEM bootstrap).')
STAGE_SOURCE
# Fail clearly if an obsolete CA bootstrap still requires packaged certificates.
if [ -f "$STAGED_AGENT/tls_bootstrap.py" ]; then
  echo 'Remove the obsolete custom TLS bootstrap from the source before no-gateway deployment.' >&2
  exit 1
fi
# Do not read the default gateway config from the source package.
printf '{}\n' > "$TMP_ROOT/no-gateway.json"
# Keep the original env file unchanged; strip only our gateway diagnostic setting.
python3 - "$ENV_FILE" "$TMP_ROOT/deploy.env" <<'ENV_COPY'
import pathlib, re, sys
src, dst = map(pathlib.Path, sys.argv[1:])
lines = src.read_text().splitlines()
pattern = re.compile(r'^\s*(?:export\s+)?MONGODB_LOG_DIAG_GATEWAY_CA_SHA256\s*=')
dst.write_text('\n'.join(line for line in lines if not pattern.match(line))+'\n')
dst.chmod(0o600)
ENV_COPY
"$ADK_BIN" deploy agent_engine \
  "--project=$PROJECT" "--region=$REGION" "--agent_engine_id=$ENGINE_ID" \
  "--display_name=$DISPLAY_NAME" \
  "--env_file=$TMP_ROOT/deploy.env" \
  "--requirements_file=$STAGED_AGENT/requirements.txt" \
  "--agent_engine_config_file=$TMP_ROOT/no-gateway.json" \
  "$STAGED_AGENT"
echo 'Updated the no-gateway engine. Invoke a minimal session and inspect runtime logs.'
