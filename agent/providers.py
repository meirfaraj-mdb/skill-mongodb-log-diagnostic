"""Cloud selection: secret source, object storage and LLM backend.

    CLOUD_PROVIDER = aws | gcp | local   (auto-detected when unset)
    ATLAS_SECRET_ID                       AWS: ARN/name   GCP: projects/<p>/secrets/<name>[/versions/<v>]
    ATLAS_CONFIG_FILE                     local/dev: JSON file with the same keys

Secret keys are the lambda's keys plus optional generic ones:
    bucket | s3_bucket | gcs_bucket      prefix | s3_prefix | gcs_prefix
    storage_provider = s3 | gcs | local  llm_provider = bedrock | vertex
"""
from __future__ import annotations

import json
import os
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
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_EXECUTION_ENV") or os.environ.get("ECS_CONTAINER_METADATA_URI_V4"):
        return "aws"
    if os.environ.get("CLOUD_RUN_JOB") or os.environ.get("K_SERVICE") or os.environ.get("FUNCTION_TARGET"):
        return "gcp"
    secret = os.environ.get("ATLAS_SECRET_ID", "")
    if secret.startswith("arn:aws:"):
        return "aws"
    if secret.startswith("projects/"):
        return "gcp"
    if os.environ.get("ATLAS_CONFIG_FILE"):
        return "local"
    raise RuntimeError("Cannot detect cloud: set CLOUD_PROVIDER=aws|gcp|local")


def load_raw_config(cloud: str) -> dict:
    if os.environ.get("ATLAS_CONFIG_FILE"):
        return json.loads(Path(os.environ["ATLAS_CONFIG_FILE"]).read_text(encoding="utf-8"))
    secret_id = os.environ.get("ATLAS_SECRET_ID")
    if not secret_id:
        raise RuntimeError("Set ATLAS_SECRET_ID (or ATLAS_CONFIG_FILE for local runs)")
    if cloud == "aws":
        return skills.aws_storage().load_secret(secret_id)
    if cloud == "gcp":
        return skills.gcp_storage().load_secret(secret_id)
    raise RuntimeError("local cloud requires ATLAS_CONFIG_FILE")


def normalize_config(raw: dict, cloud: str) -> dict:
    cfg = dict(raw)
    defaults = CLOUD_DEFAULTS[cloud]
    bucket = cfg.get("bucket") or cfg.get("s3_bucket") or cfg.get("gcs_bucket")
    if not bucket:
        raise ValueError("Missing bucket: set bucket (or s3_bucket / gcs_bucket) in the secret")
    storage = cfg.get("storage_provider")
    for scheme, provider in (("s3://", "s3"), ("gs://", "gcs"), ("file://", "local")):
        if bucket.startswith(scheme):
            bucket, storage = bucket[len(scheme):].rstrip("/"), storage or provider
    cfg["bucket"] = bucket
    cfg["storage_provider"] = storage or defaults["storage_provider"]
    cfg["prefix"] = (cfg.get("prefix") or cfg.get("s3_prefix") or cfg.get("gcs_prefix") or "mongodb-atlas-logs").strip("/")
    cfg["llm_provider"] = cfg.get("llm_provider") or defaults["llm_provider"]
    cfg["input_mode"] = str(cfg.get("input_mode", "atlas_api")).strip().lower()
    if cfg["input_mode"] not in {"atlas_api", "existing_bucket"}:
        raise ValueError("input_mode must be atlas_api or existing_bucket")
    # Existing-bucket mode is intentionally usable with a minimal secret: it never
    # contacts Atlas and therefore must not require Atlas API credentials/config.
    if cfg["input_mode"] == "atlas_api":
        skills.atlas_logs().validate_config(cfg)
    cfg.setdefault("cluster_name", cfg.get("report_name") or "MongoDB cluster")
    cfg.setdefault("timezone", "UTC")
    if cfg["llm_provider"] == "vertex" and not (cfg.get("vertex_model") or os.environ.get("VERTEX_MODEL")):
        cfg["vertex_model"] = "claude-sonnet-5"
    cfg["cloud"] = cloud
    return cfg


def load_config() -> dict:
    cloud = detect_cloud()
    cfg = normalize_config(load_raw_config(cloud), cloud)
    logger.info("Config loaded cloud=%s input_mode=%s storage=%s bucket=%s prefix=%s llm=%s cluster=%s",
                cloud, cfg["input_mode"], cfg["storage_provider"], cfg["bucket"], cfg["prefix"], cfg["llm_provider"], cfg["cluster_name"])
    return cfg


class LocalStore:
    """Filesystem ObjectStore (dev/test/on-prem). Same contract as S3Store / GCSStore."""
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
                      if p.is_file() and not p.name.endswith(".meta.json") and str(p.relative_to(self.root)).startswith(prefix))

    def download(self, key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self._p(key).read_bytes())

    def _write_meta(self, key, content_type, metadata):
        self._p(key + ".meta.json").write_text(json.dumps({"content_type": content_type, **(metadata or {})}))

    def upload(self, path, key, content_type, metadata=None):
        self._p(key).parent.mkdir(parents=True, exist_ok=True)
        self._p(key).write_bytes(Path(path).read_bytes()); self._write_meta(key, content_type, metadata)
        return self.uri(key)

    def upload_stream(self, fileobj, key, content_type, metadata=None):
        import shutil
        self._p(key).parent.mkdir(parents=True, exist_ok=True)
        with self._p(key).open("wb") as out:
            shutil.copyfileobj(fileobj, out, 8 * 1024 * 1024)
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
    raise ValueError(f"Unknown or missing llm_provider {provider!r} (bedrock|vertex)")
