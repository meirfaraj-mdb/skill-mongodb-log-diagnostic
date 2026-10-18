"""Runtime bridging Atlas Agent Engine configuration to the shared pipeline."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

VALID_PIPELINE_CLOUDS = {"aws", "gcp", "local"}
_STORAGE_TO_CLOUD = {"s3": "aws", "gcs": "gcp", "local": "local"}


def select_pipeline_cloud(env: Mapping[str, str]) -> str:
    """Select the shared pipeline's storage/secret backend, not the agent runtime."""
    requested = (env.get("MONGODB_LOG_DIAG_PIPELINE_CLOUD") or env.get("CLOUD_PROVIDER") or "").strip().lower()
    if requested in VALID_PIPELINE_CLOUDS:
        return requested

    storage = (env.get("MONGODB_LOG_DIAG_STORAGE_PROVIDER") or "").strip().lower()
    if storage in _STORAGE_TO_CLOUD:
        return _STORAGE_TO_CLOUD[storage]

    bucket = (env.get("MONGODB_LOG_DIAG_BUCKET") or "").strip().lower()
    for scheme, cloud in (("s3://", "aws"), ("gs://", "gcp"), ("file://", "local")):
        if bucket.startswith(scheme):
            return cloud

    secret_id = (env.get("ATLAS_SECRET_ID") or "").strip()
    if secret_id.startswith("arn:aws:"):
        return "aws"
    if secret_id.startswith("projects/"):
        return "gcp"

    raise ValueError(
        "Cannot infer the diagnostic pipeline backend. Set "
        "MONGODB_LOG_DIAG_PIPELINE_CLOUD=aws|gcp|local, or set "
        "MONGODB_LOG_DIAG_STORAGE_PROVIDER=s3|gcs|local."
    )


def configure_pipeline_runtime() -> str:
    """Set CLOUD_PROVIDER for the existing pipeline without using 'atlas'."""
    cloud = select_pipeline_cloud(os.environ)
    os.environ["CLOUD_PROVIDER"] = cloud
    return cloud


@contextmanager
def atlas_credentials_file() -> Iterator[None]:
    """Expose an injected Atlas credential JSON secret as a short-lived file.

    The shared pipeline already supports ATLAS_CONFIG_FILE. This adapter lets
    Atlas Agent Engine inject the JSON as a platform secret without changing the
    shared config loader. Secret contents are never logged or included in errors.
    """
    mode = os.environ.get("MONGODB_LOG_DIAG_INPUT_MODE", "atlas_api").strip().lower()
    secret_json = os.environ.get("MONGODB_LOG_DIAG_ATLAS_CREDENTIALS_JSON")
    if mode != "atlas_api" or not secret_json or os.environ.get("ATLAS_CONFIG_FILE"):
        yield
        return

    try:
        payload = json.loads(secret_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("MONGODB_LOG_DIAG_ATLAS_CREDENTIALS_JSON must be valid JSON") from exc
    allowed = {"atlas_public_key", "atlas_private_key"}
    if (not isinstance(payload, dict) or set(payload) != allowed
            or not all(isinstance(payload.get(key), str) and payload[key].strip() for key in allowed)):
        raise ValueError(
            "Atlas credentials secret must contain only non-empty atlas_public_key and atlas_private_key"
        )

    path: str | None = None
    previous = os.environ.get("ATLAS_CONFIG_FILE")
    try:
        fd, path = tempfile.mkstemp(prefix="atlas-agent-credentials-", suffix=".json")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.environ["ATLAS_CONFIG_FILE"] = path
        yield
    finally:
        if previous is None:
            os.environ.pop("ATLAS_CONFIG_FILE", None)
        else:
            os.environ["ATLAS_CONFIG_FILE"] = previous
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                # Avoid leaking secret values in cleanup errors.
                pass
