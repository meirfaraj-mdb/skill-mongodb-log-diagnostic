#!/usr/bin/env python3
"""Create a NEW gateway-bound Agent Runtime using the native Python SDK.

Run from the repository root. Does not update or delete the existing engine.
Use --dry-run to inspect the plan; --deploy is required to create a resource.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import inspect
import typing
from importlib.metadata import version


def load_env(path: Path) -> dict[str, str]:
    # Parse a simple, non-secret ADK env file without executing shell code.
    parsed: dict[str, str] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Invalid env assignment on line {n}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid env name on line {n}")
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if key in parsed:
            raise ValueError(f"Duplicate env name: {key}")
        parsed[key] = value
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--gateway", required=True, help="Existing Agent-to-Anywhere gateway name")
    parser.add_argument("--staging-bucket", required=True, help="Writable gs:// bucket for SDK staging")
    parser.add_argument("--env-file", default="mongodb_log_diagnostic_deploy.env")
    parser.add_argument("--requirements-file", default="requirements-sdk-gateway-test.txt")
    parser.add_argument("--display-name", default="MongoDB Atlas daily diagnostics - SDK gateway test")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--deploy", action="store_true")
    args = parser.parse_args()

    for name in ("google_adk_agent/__init__.py", "google_adk_agent/agent.py", "google_adk_agent/runtime.py", "agent", "skills"):
        if not Path(name).exists():
            parser.error(f"Run from repository root; missing {name}")
    if not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", args.staging_bucket) or "YOUR_" in args.staging_bucket.upper():
        parser.error("--staging-bucket must be a real, writable gs:// bucket name (not a placeholder)")
    env = load_env(Path(args.env_file))
    if env.get("MONGODB_LOG_DIAG_INPUT_MODE") != "existing_bucket":
        parser.error("Expected MONGODB_LOG_DIAG_INPUT_MODE=existing_bucket")
    # No secrets or developer-machine CA paths are forwarded into the new runtime.
    rejected = [k for k in env if any(s in k.upper() for s in ("PRIVATE_KEY", "PUBLIC_KEY", "SECRET", "PASSWORD"))]
    if rejected:
        parser.error("Remove secret-related fields from env file: " + ", ".join(rejected))
    env_vars = {k: v for k, v in env.items()
                if k.startswith("MONGODB_LOG_DIAG_") or k in ("CLOUD_PROVIDER", "GOOGLE_GENAI_USE_ENTERPRISE")}
    # A fingerprint only enables diagnostics; it never installs a CA.
    if "MONGODB_LOG_DIAG_GATEWAY_CA_SHA256" in env_vars and not re.fullmatch(
        r"[0-9A-Fa-f]{64}", env_vars["MONGODB_LOG_DIAG_GATEWAY_CA_SHA256"].replace(":", "")
    ):
        parser.error("Remove invalid MONGODB_LOG_DIAG_GATEWAY_CA_SHA256 (e.g. True)")
    for k, v in env_vars.items():
        os.environ[k] = v  # Set before importing root_agent locally.
    requirements = []
    for line in Path(args.requirements_file).read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()  # Remove human comments before SDK serialization.
        if line and not line.startswith("#"):
            if line.startswith(("-", ".", "git+")):
                parser.error("Review non-package requirement: " + line)
            requirements.append(line)
    if any(r.lower().startswith("google-cloud-agentplatform") for r in requirements):
        parser.error("This is a 1.x vertexai SDK test; remove google-cloud-agentplatform")
    gateway = f"projects/{args.project}/locations/{args.region}/agentGateways/{args.gateway}"
    plan = {"mode": "NEW ENGINE; old engine unchanged", "project": args.project,
            "region": args.region, "gateway": gateway, "staging_bucket": args.staging_bucket,
            "identity_type": "AGENT_IDENTITY", "env_names": sorted(env_vars),
            "requirements": requirements, "extra_packages": ["google_adk_agent", "agent", "skills"]}
    print(json.dumps(plan, indent=2))
    if args.dry_run:
        return
    # Import SDK and local agent only after the non-secret env is loaded.
    import vertexai
    from vertexai.agent_engines import AdkApp

    sdk_version = version("google-cloud-aiplatform")
    if not sdk_version.startswith("1."):
        parser.error(f"Expected google-cloud-aiplatform 1.x, found {sdk_version}")
    client = vertexai.Client(project=args.project, location=args.region)
    create = client.agent_engines.create
    # Inspect the installed SDK's public config annotation. Do not silently
    # discard gateway settings when running an older SDK that lacks them.
    try:
        annotation = typing.get_type_hints(create).get("config")
    except (NameError, TypeError):
        annotation = inspect.signature(create).parameters["config"].annotation
    def config_models(annotation):
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            yield annotation
        for argument in typing.get_args(annotation):
            yield from config_models(argument)
    models = list(config_models(annotation))
    supported = next((model for model in models
                      if {"identity_type", "agent_gateway_config"}
                      <= set(model.model_fields)), None)
    if supported is None:
        raise RuntimeError(
            "The installed 1.x SDK does not expose a config model with both "
            "identity_type and agent_gateway_config. No engine was created. "
            "Use a gateway-capable SDK; changing the import alone is insufficient."
        )
    config = supported.model_validate({
        "display_name": args.display_name,
        "identity_type": "AGENT_IDENTITY",
        "agent_gateway_config": {
            "agent_to_anywhere_config": {"agent_gateway": gateway}},
        "staging_bucket": args.staging_bucket,
        "requirements": requirements,
        "extra_packages": ["google_adk_agent", "agent", "skills"],
        "env_vars": env_vars,
    })
    # Ensure every supplied setting exists rather than relying on Pydantic's
    # default behavior of ignoring unknown fields.
    fields = set(supported.model_fields)
    required = {"staging_bucket", "requirements", "extra_packages", "env_vars"}
    if not required <= fields:
        raise RuntimeError("SDK config lacks packaging fields: " +
                           ", ".join(sorted(required - fields)))
    from google_adk_agent.agent import root_agent
    remote = create(agent=AdkApp(agent=root_agent), config=config)
    print("NEW_ENGINE=", remote.api_resource.name)
    print("EFFECTIVE_IDENTITY=", remote.api_resource.spec.effective_identity)
    print("Do not migrate callers until build warning, TLS diagnostic and minimal session are verified.")


if __name__ == "__main__":
    main()
