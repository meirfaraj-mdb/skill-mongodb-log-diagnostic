#!/bin/sh
# Update an existing source-based Agent Engine; supply a gateway CA fingerprint
# to the startup diagnostic. This does not modify the runtime CA trust store.
set -eu
: "${PROJECT:?Set PROJECT}"
: "${REGION:?Set REGION}"
: "${ENGINE_ID:?Set ENGINE_ID}"
: "${GATEWAY:?Set GATEWAY to the bound egress gateway name}"
: "${DISPLAY_NAME:?Set DISPLAY_NAME}"
: "${ENV_FILE:?Set ENV_FILE to the non-secret runtime env file}"
: "${AGENT_DIR:=google_adk_agent}"
[ -f "$ENV_FILE" ] && [ -f "$AGENT_DIR/agent.py" ] && [ -f "$AGENT_DIR/tls_diagnostics.py" ] || {
  echo 'Run from repository root with the existing env and agent files' >&2; exit 1;
}
[ -f "$AGENT_DIR/requirements.txt" ] || { echo "Missing agent requirements.txt" >&2; exit 1; }
[ -d agent ] && [ -d skills ] || { echo 'Missing agent/ or skills/ directory' >&2; exit 1; }
command -v adk >/dev/null 2>&1 || { echo 'ADK CLI not found' >&2; exit 1; }
command -v gcloud >/dev/null 2>&1 || { echo 'gcloud CLI not found' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'python3 not found' >&2; exit 1; }
for legacy in "$AGENT_DIR/gateway-root.pem" "$AGENT_DIR/_gateway_root_generated.py" "$AGENT_DIR/tls_bootstrap.py"; do
  [ ! -e "$legacy" ] || { echo "Remove obsolete bootstrap file: $legacy" >&2; exit 1; }
done
# Read only the public gateway certificate from the control plane. Require a
# single root so a rotation/overlap cannot silently select the wrong one.
GATEWAY_JSON=$(mktemp "${TMPDIR:-/tmp}/mongodb-gateway.XXXXXXXX")
DEPLOY_ENV=$(mktemp "${TMPDIR:-/tmp}/mongodb-adk-env.XXXXXXXX")
chmod 600 "$GATEWAY_JSON" "$DEPLOY_ENV"
trap 'rm -f "$GATEWAY_JSON" "$DEPLOY_ENV"' EXIT HUP INT TERM
if ! gcloud network-services agent-gateways describe "$GATEWAY" \
  "--project=$PROJECT" "--location=$REGION" --format=json > "$GATEWAY_JSON"; then
  echo 'Unable to read gateway certificate; deployment cancelled' >&2; exit 1
fi
FINGERPRINT=$(python3 - "$GATEWAY_JSON" <<'PY'
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
# ADK reads --env_file at deploy time. Do not alter the user's original env file.
# These paths select the image's system CA bundle; they do not install a missing CA.
python3 - "$ENV_FILE" "$DEPLOY_ENV" "$FINGERPRINT" <<'ENV_GENERATOR'
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
ENV_GENERATOR
printf 'Deploying with gateway CA fingerprint %s (diagnostic only; no CA installed)\n' "$FINGERPRINT"
adk deploy agent_engine \
  "--project=$PROJECT" "--region=$REGION" "--agent_engine_id=$ENGINE_ID" \
  "--display_name=$DISPLAY_NAME" \
  "--env_file=$DEPLOY_ENV" \
  "--requirements_file=$AGENT_DIR/requirements.txt" \
  "$AGENT_DIR"
echo 'Redeployed. Invoke a session and inspect the startup TLS diagnostic.'
