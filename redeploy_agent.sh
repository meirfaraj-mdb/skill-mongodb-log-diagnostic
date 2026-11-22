#!/bin/sh
# Run from the repository root. Update an EXISTING Agent Engine that is bound to an egress gateway.
# Packages the same source layout as redeploy_agent_no_gateway.sh (ADK entry point + bundled agent/
# and skills/ under _shared/) and passes the gateway CA fingerprint to the startup TLS diagnostic.
# This does not modify the runtime CA trust store.
set -eu
: "${PROJECT:?Set PROJECT}"
: "${REGION:?Set REGION}"
: "${ENGINE_ID:?Set ENGINE_ID to the gateway-bound engine}"
: "${GATEWAY:?Set GATEWAY to the bound egress gateway name}"
: "${DISPLAY_NAME:?Set DISPLAY_NAME}"
: "${ENV_FILE:?Set ENV_FILE to the non-secret runtime env file}"
: "${AGENT_DIR:=google_adk_agent}"
: "${ADK_BIN:=adk}"
for tool in "$ADK_BIN" python3 gcloud curl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing command: $tool" >&2; exit 1; }
done
[ -f "$ENV_FILE" ] && [ -f "$AGENT_DIR/agent.py" ] && [ -f "$AGENT_DIR/tls_diagnostics.py" ] \
  && [ -f "$AGENT_DIR/requirements.txt" ] && [ -d agent ] && [ -d skills ] || {
  echo 'Run from repository root with env file, ADK package (agent.py, tls_diagnostics.py, requirements.txt), agent/, and skills/.' >&2
  exit 1
}
for legacy in "$AGENT_DIR/gateway-root.pem" "$AGENT_DIR/_gateway_root_generated.py" "$AGENT_DIR/tls_bootstrap.py"; do
  [ ! -e "$legacy" ] || { echo "Remove obsolete bootstrap file: $legacy" >&2; exit 1; }
done
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/mongodb-gateway.XXXXXXXX")
chmod 700 "$TMP_ROOT"
trap 'rm -rf "$TMP_ROOT"' 0
trap 'exit 130' INT
trap 'exit 143' HUP TERM

# Refuse to update an engine WITHOUT a gateway binding (use redeploy_agent_no_gateway.sh for that one).
curl -fsS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT}/locations/${REGION}/reasoningEngines/${ENGINE_ID}" \
  > "$TMP_ROOT/engine.json"
python3 - "$TMP_ROOT/engine.json" <<'CHECK_ENGINE'
import json, sys
with open(sys.argv[1]) as f:
    engine = json.load(f)
if not engine.get('spec', {}).get('deploymentSpec', {}).get('agentGatewayConfig'):
    raise SystemExit('Target engine has no gateway binding. Use redeploy_agent_no_gateway.sh for this engine ID.')
CHECK_ENGINE

# Read only the public gateway certificate from the control plane. Require a
# single root so a rotation/overlap cannot silently select the wrong one.
if ! gcloud network-services agent-gateways describe "$GATEWAY" \
  "--project=$PROJECT" "--location=$REGION" --format=json > "$TMP_ROOT/gateway.json"; then
  echo 'Unable to read gateway certificate; deployment cancelled' >&2; exit 1
fi
FINGERPRINT=$(python3 - "$TMP_ROOT/gateway.json" <<'PY'
import hashlib, json, ssl, sys
with open(sys.argv[1], encoding='utf-8') as f:
    gateway = json.load(f)
roots = gateway.get('agentGatewayCard', {}).get('rootCertificates', [])
if len(roots) != 1:
    raise SystemExit('Expected exactly one gateway root certificate; deployment cancelled')
try:
    der = ssl.PEM_cert_to_DER_cert(roots[0])
except (ValueError, ssl.SSLError) as exc:
    raise SystemExit(f'Invalid gateway certificate: {type(exc).__name__}') from exc
print(hashlib.sha256(der).hexdigest().upper())
PY
)
# Optional independent pin prevents silently accepting a rotated/different CA.
if [ -n "${APPROVED_GATEWAY_CA_SHA256:-}" ]; then
  APPROVED=$(printf '%s' "$APPROVED_GATEWAY_CA_SHA256" | tr -d ':' | tr '[:lower:]' '[:upper:]')
  if [ "$APPROVED" != "$FINGERPRINT" ]; then
    echo 'Gateway CA fingerprint does not match the independently approved value' >&2; exit 1
  fi
fi

# One source package for ADK 1.x; staging avoids the agent.py/agent-directory collision and bundles
# the shared pipeline (agent/) and skills/ that the tools import at run time.
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
if (target / '_shared').exists():
    raise SystemExit(f'Staging conflict: {target / "_shared"}')
(target / '_shared').mkdir()
for name in ('agent', 'skills'):
    shutil.copytree(repo / name, target / '_shared' / name, ignore=ignored)
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
ast.parse(entry.read_text(encoding='utf-8'))
for required in ('__init__.py', 'agent.py', 'tls_diagnostics.py', 'requirements.txt', '_shared/agent/handler.py',
                 '_shared/skills/mongodb-log-diagnostic/SKILL.md'):
    if not (target / required).is_file():
        raise SystemExit(f'Missing staged file: {required}')
print('Staged ADK entry point, shared agent package, and skills.')
STAGE_SOURCE

# ADK reads --env_file at deploy time. Do not alter the user's original env file.
# These paths select the image's system CA bundle; they do not install a missing CA.
python3 - "$ENV_FILE" "$TMP_ROOT/deploy.env" "$FINGERPRINT" <<'ENV_GENERATOR'
import pathlib, re, sys
source, target = map(pathlib.Path, sys.argv[1:3])
lines = source.read_text(encoding='utf-8').splitlines()
settings = {
    'MONGODB_LOG_DIAG_GATEWAY_CA_SHA256': sys.argv[3],
    'SSL_CERT_FILE': '/etc/ssl/certs/ca-certificates.crt',
    'REQUESTS_CA_BUNDLE': '/etc/ssl/certs/ca-certificates.crt',
}
pattern = re.compile(r'^\s*(?:export\s+)?(' + '|'.join(settings) + r')\s*=')
clean = [line for line in lines if not pattern.match(line)]
target.write_text('\n'.join(clean + [f'{key}={value}' for key, value in settings.items()]) + '\n', encoding='utf-8')
target.chmod(0o600)
ENV_GENERATOR
printf 'Deploying with gateway CA fingerprint %s (diagnostic only; no CA installed)\n' "$FINGERPRINT"
"$ADK_BIN" deploy agent_engine \
  "--project=$PROJECT" "--region=$REGION" "--agent_engine_id=$ENGINE_ID" \
  "--display_name=$DISPLAY_NAME" \
  "--env_file=$TMP_ROOT/deploy.env" \
  "--requirements_file=$STAGED_AGENT/requirements.txt" \
  "$STAGED_AGENT"
echo 'Redeployed the gateway engine. Invoke a session and inspect the startup TLS diagnostic.'
