"""Select cloud, load non-secret settings from env, and Atlas API keys from secret."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import skills
from .common import logger

CLOUD_DEFAULTS = {
    "aws": {"storage_provider": "s3", "llm_provider": "bedrock"},
    "gcp": {"storage_provider": "gcs", "llm_provider": "vertex"},
    "local": {"storage_provider": "local", "llm_provider": None},
}


def detect_cloud() -> str:
    explicit = os.environ.get("CLOUD_PROVIDER", "").strip().lower()
    if explicit:
        if explicit not in CLOUD_DEFAULTS:
            raise ValueError(f"CLOUD_PROVIDER must be aws|gcp|local, got {explicit!r}")
        return explicit
    if any(os.environ.get(x) for x in (
        "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV", "ECS_CONTAINER_METADATA_URI_V4"
    )):
        return "aws"
    if any(os.environ.get(x) for x in ("CLOUD_RUN_JOB", "K_SERVICE", "FUNCTION_TARGET")):
        return "gcp"
    secret = os.environ.get("ATLAS_SECRET_ID", "")
    if secret.startswith("arn:aws:"):
        return "aws"
    if secret.startswith("projects/"):
        return "gcp"
    if os.environ.get("ATLAS_CONFIG_FILE"):
        return "local"
    raise RuntimeError("Cannot detect cloud: set CLOUD_PROVIDER=aws|gcp|local")


# Non-secret settings only. Never accept these settings from the secret payload.
_ENV_FIELDS = {
    "input_mode": "INPUT_MODE", "group_id": "GROUP_ID", "cluster_name": "CLUSTER_NAME",
    "timezone": "TIMEZONE", "api_version": "API_VERSION", "log_names": "LOG_NAMES",
    "host_selector": "HOST_SELECTOR", "http_timeout_seconds": "HTTP_TIMEOUT_SECONDS",
    "max_retries": "MAX_RETRIES", "bucket": "BUCKET", "prefix": "PREFIX",
    "slow_ms": "SLOW_MS", "expected_node_count": "EXPECTED_NODE_COUNT",
    "report_max_tokens": "REPORT_MAX_TOKENS", "report_max_input_chars": "REPORT_MAX_INPUT_CHARS",
    "cluster_summary": "CLUSTER_SUMMARY", "llm_provider": "LLM_PROVIDER",
    "bedrock_model_id": "BEDROCK_MODEL_ID", "bedrock_region": "BEDROCK_REGION",
    "vertex_model": "VERTEX_MODEL", "vertex_location": "VERTEX_LOCATION",
    "vertex_project": "VERTEX_PROJECT", "storage_provider": "STORAGE_PROVIDER",
    "aws_region": "AWS_REGION", "gcp_project": "GCP_PROJECT",
}
_INT_FIELDS = {"http_timeout_seconds", "max_retries", "slow_ms", "expected_node_count",
               "report_max_tokens", "report_max_input_chars"}
_CREDENTIAL_FIELDS = {"atlas_public_key", "atlas_private_key"}


def _env_config() -> dict:
    result = {}
    for key, suffix in _ENV_FIELDS.items():
        name = "MONGODB_LOG_DIAG_" + suffix
        value = os.environ.get(name)
        if value is None or not value.strip():
            continue
        value = value.strip()
        if key in _INT_FIELDS:
            try:
                value = int(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
        elif key == "cluster_summary":
            if value.lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"{name} must be true or false")
            value = value.lower() in ("true", "1")
        elif key == "log_names":
            if value.startswith("["):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{name} must be a JSON list or comma-separated names") from exc
            else:
                value = [item.strip() for item in value.split(",") if item.strip()]
            if not isinstance(value, list) or not value or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ValueError(f"{name} must contain at least one log name")
        result[key] = value
    return result


def load_raw_config(cloud: str) -> dict:
    """Read only Atlas public/private keys, and only when Atlas API is used."""
    mode = os.environ.get("MONGODB_LOG_DIAG_INPUT_MODE", "atlas_api").strip().lower()
    if mode == "existing_bucket":
        return {}
    if mode != "atlas_api":
        raise ValueError("MONGODB_LOG_DIAG_INPUT_MODE must be atlas_api or existing_bucket")
    if os.environ.get("ATLAS_CONFIG_FILE"):
        raw = json.loads(Path(os.environ["ATLAS_CONFIG_FILE"]).read_text(encoding="utf-8"))
    else:
        secret_id = os.environ.get("ATLAS_SECRET_ID")
        if not secret_id:
            raise RuntimeError("Set ATLAS_SECRET_ID for atlas_api mode")
        if cloud == "aws":
            raw = skills.aws_storage().load_secret(secret_id)
        elif cloud == "gcp":
            raw = skills.gcp_storage().load_secret(secret_id)
        else:
            raise RuntimeError("local cloud requires ATLAS_CONFIG_FILE for atlas_api mode")
    if not isinstance(raw, dict):
        raise ValueError("Atlas secret must be a JSON object")
    unexpected = set(raw) - _CREDENTIAL_FIELDS
    if unexpected:
        raise ValueError("Atlas secret must contain only atlas_public_key and atlas_private_key; "
                         "move all non-secret settings to MONGODB_LOG_DIAG_* environment variables")
    if not all(isinstance(raw.get(key), str) and raw[key].strip() for key in _CREDENTIAL_FIELDS):
        raise ValueError("Atlas secret must contain non-empty atlas_public_key and atlas_private_key")
    return dict(raw)


def normalize_config(raw: dict, cloud: str) -> dict:
    # Keep non-sensitive settings env-only, even if normalize_config is called directly.
    if set(raw) - _CREDENTIAL_FIELDS:
        raise ValueError("Config payload must contain only Atlas API credentials")
    cfg = _env_config()
    cfg.update(raw)
    defaults = CLOUD_DEFAULTS[cloud]
    bucket = cfg.get("bucket")
    if not bucket:
        raise ValueError("Missing MONGODB_LOG_DIAG_BUCKET environment variable")
    storage = cfg.get("storage_provider")
    for scheme, provider in (("s3://", "s3"), ("gs://", "gcs"), ("file://", "local")):
        if bucket.startswith(scheme):
            bucket, storage = bucket[len(scheme):].rstrip("/"), storage or provider
    cfg["bucket"] = bucket
    cfg["storage_provider"] = storage or defaults["storage_provider"]
    cfg["prefix"] = (cfg.get("prefix") or "mongodb-atlas-logs").strip("/")
    cfg["llm_provider"] = cfg.get("llm_provider") or defaults["llm_provider"]
    cfg["input_mode"] = str(cfg.get("input_mode", "atlas_api")).strip().lower()
    if cfg["input_mode"] not in {"atlas_api", "existing_bucket"}:
        raise ValueError("input_mode must be atlas_api or existing_bucket")
    if cfg["input_mode"] == "atlas_api":
        skills.atlas_logs().validate_config(cfg)
    cfg.setdefault("cluster_name", "MongoDB cluster")
    cfg.setdefault("timezone", "UTC")
    if cfg["llm_provider"] == "vertex" and not (cfg.get("vertex_model") or os.environ.get("VERTEX_MODEL")):
        cfg["vertex_model"] = "claude-sonnet-5"
    cfg["cloud"] = cloud
    return cfg


def load_config() -> dict:
    cloud = detect_cloud()
    cfg = normalize_config(load_raw_config(cloud), cloud)
    logger.info("Config loaded cloud=%s input_mode=%s storage=%s bucket=%s prefix=%s llm=%s cluster=%s",
                cloud, cfg["input_mode"], cfg["storage_provider"], cfg["bucket"], cfg["prefix"],
                cfg["llm_provider"], cfg["cluster_name"])
    return cfg


class LocalStore:
    """Filesystem ObjectStore (dev/test/on-prem); same contract as S3Store/GCSStore."""
    scheme = "file"

    def __init__(self, root):
        self.root = Path(root)

    def _p(self, key):
        return self.root / key

    def uri(self, key):
        return f"file://{self._p(key)}"

    def exists(self, key):
        return self._p(key).is_file()

    def list_keys(self, prefix):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p.is_file() and not p.name.endswith(".meta.json")
                      and str(p.relative_to(self.root)).startswith(prefix))

    def download(self, key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self._p(key).read_bytes())

    def _write_meta(self, key, content_type, metadata):
        self._p(key + ".meta.json").write_text(json.dumps({"content_type": content_type, **(metadata or {})}))

    def upload(self, path, key, content_type, metadata=None):
        self._p(key).parent.mkdir(parents=True, exist_ok=True)
        self._p(key).write_bytes(Path(path).read_bytes())
        self._write_meta(key, content_type, metadata)
        return self.uri(key)

    def upload_stream(self, fileobj, key, content_type, metadata=None):
        target = self._p(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                             prefix=".atlas-upload-", delete=False) as out:
                name = out.name
                while True:
                    data = fileobj.read(8 * 1024 * 1024)
                    if not data:
                        break
                    out.write(data)
            os.replace(name, target)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
        self._write_meta(key, content_type, metadata)
        return self.uri(key)

    def put_text(self, key, text, content_type):
        self._p(key).parent.mkdir(parents=True, exist_ok=True)
        self._p(key).write_text(text, encoding="utf-8")
        return self.uri(key)

    def get_text(self, key):
        return self._p(key).read_text(encoding="utf-8")


def get_store(config: dict):
    provider = config["storage_provider"]
    if provider == "s3":
        return skills.aws_storage().S3Store(config["bucket"], region=config.get("aws_region"))
    if provider == "gcs":
        return skills.gcp_storage().GCSStore(config["bucket"], project=config.get("gcp_project"))
    if provider == "local":
        return LocalStore(config["bucket"])
    raise ValueError(f"Unknown storage_provider {provider!r}")


def get_llm(config: dict):
    from . import llm
    provider = config.get("llm_provider")
    if provider == "bedrock":
        return llm.BedrockLLM(config)
    if provider == "vertex":
        return llm.VertexLLM(config)
    if provider == "anthropic":
        return llm.AnthropicLLM(config)
    if provider == "claude_cli":
        if config.get("cloud") != "local":
            raise ValueError("claude_cli is for locally executed runs only")
        return llm.ClaudeCLILLM(config)
    raise ValueError(f"Unknown or missing llm_provider {provider!r} (bedrock|vertex|anthropic|claude_cli)")
