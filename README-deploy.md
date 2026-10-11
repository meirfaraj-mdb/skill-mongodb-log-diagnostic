# Managed Agent Engine container CA diagnostics

Replace `google_adk_agent/agent.py` and `google_adk_agent/tls_diagnostics.py` in your checkout. The agent entry point is based on the last supplied ADK-model variant; review the diff against your current checkout. No PEM is shipped, generated, or installed by these files, and TLS verification is unchanged.

Set the **non-secret** `MONGODB_LOG_DIAG_GATEWAY_CA_SHA256` in your deployment env file to the fingerprint independently verified from this environment's gateway `agentGatewayCard.rootCertificates` (64 hexadecimal characters; colons optional). The diagnostic explicitly reports `not_checked` if omitted. Do not put a private key into the env file. Keep `MONGODB_LOG_DIAG_ADK_MODEL` as your orchestration model setting.

Run the normal redeploy command; `redeploy_agent.sh` is provided as a source-only optional helper. From repository root, for the already-bound existing engine:

```sh
PROJECT=meir-faraj REGION=europe-west1 ENGINE_ID=800270742183739392 \
DISPLAY_NAME='MongoDB Atlas daily diagnostics' \
ENV_FILE="$(pwd)/mongodb_log_diagnostic_deploy.env" sh ./redeploy_agent.sh
```

Inspect the `Gateway TLS trust diagnostic` startup record after invoking one minimal session:

- `gateway_ca_in_python_default_context`: does Python's default `ssl` context contain the specified DER SHA-256 certificate fingerprint?
- `container_ca_file_scan.gateway_ca_found_in_scanned_files` and `matching_paths`: was that exact certificate found in common **running container** CA bundle files or certificate directories (including `certifi` if installed)? This scan is bounded and non-exhaustive.
- `default_verify_paths`, `ca_override_paths`, `certifi_bundle_path`: paths useful for spotting which trust store a client might use. The diagnostic logs paths but no certificate contents.
- `grpc_effective_roots_checked: false`: gRPC may use separate roots. This diagnostic does not prove what ADK's managed `aiohttp` or gRPC clients actually loaded.

If the scan finds the CA but Python does not, investigate the effective trust-store path. If neither finds it, investigate source-image CA injection with Google Cloud support, but do not infer absence from the entire image: only documented common locations were scanned. The managed Agent Engine container is not a locally inspectable Docker image. Keep TLS verification enabled.

Checks: `python3 -m unittest discover -s tests -v` (if running these files from this ZIP's root); `python3 -m compileall -q google_adk_agent` in your repository checkout.
