"""Select cloud, load non-secret settings from env, and Atlas API keys from secret."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import skills
from .common import logger
from agent.env_bool import parse_bool_env

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
    "max_retries": "MAX_RETRIES", "bucket": "BUCKET", "input_bucket": "INPUT_BUCKET", "prefix": "PREFIX",
    "slow_ms": "SLOW_MS", "expected_node_count": "EXPECTED_NODE_COUNT",
    "report_max_tokens": "REPORT_MAX_TOKENS", "report_max_input_chars": "REPORT_MAX_INPUT_CHARS",
    "cluster_summary": "CLUSTER_SUMMARY", "llm_provider": "LLM_PROVIDER",
    "bedrock_model_id": "BEDROCK_MODEL_ID", "bedrock_region": "BEDROCK_REGION",
    "vertex_model": "VERTEX_MODEL", "vertex_location": "VERTEX_LOCATION",
    "vertex_project": "VERTEX_PROJECT", "storage_provider": "STORAGE_PROVIDER",
    "aws_region": "AWS_REGION", "gcp_project": "GCP_PROJECT",
}
_ENV_FIELDS.update({
    "observability_enabled": "OBSERVABILITY_ENABLED", "deployment_type": "DEPLOYMENT_TYPE",
    "observability_hosts": "OBSERVABILITY_HOSTS", "observability_sources": "OBSERVABILITY_SOURCES",
    "mongodb_uri_template": "MONGODB_URI_TEMPLATE", "mongodb_timeout_ms": "MONGODB_TIMEOUT_MS",
    "mongodb_max_time_ms": "MONGODB_MAX_TIME_MS", "observability_max_rows": "OBSERVABILITY_MAX_ROWS",
    "index_stats_enabled": "INDEX_STATS_ENABLED", "index_stats_max_collections": "INDEX_STATS_MAX_COLLECTIONS",
    "query_shape_source": "QUERY_SHAPE_SOURCE", "query_shape_window_hours": "QUERY_SHAPE_WINDOW_HOURS",
    "query_stats_max_summaries": "QUERY_STATS_MAX_SUMMARIES", "atlas_process_ids": "ATLAS_PROCESS_IDS",
    "query_plan_requests": "QUERY_PLAN_REQUESTS", "profiler_enabled": "PROFILER_ENABLED",
    "atlas_base_url": "ATLAS_BASE_URL", "ops_manager_base_url": "OPS_MANAGER_BASE_URL",
    "ops_manager_host_ids": "OPS_MANAGER_HOST_IDS", "ops_manager_public_key": "OPS_MANAGER_PUBLIC_KEY",
    "ops_manager_private_key": "OPS_MANAGER_PRIVATE_KEY", "observability_api_max_pages": "OBSERVABILITY_API_MAX_PAGES",
    "metrics_granularity": "METRICS_GRANULARITY", "metrics_graphs_enabled": "METRICS_GRAPHS_ENABLED",
    "metrics_graph_groups": "METRICS_GRAPH_GROUPS", "metrics_graph_config": "METRICS_GRAPH_CONFIG",
    "resource_investigation_enabled": "RESOURCE_INVESTIGATION_ENABLED",
    "resource_thresholds": "RESOURCE_THRESHOLDS",
    "concurrency_findings_enabled": "CONCURRENCY_FINDINGS_ENABLED",
    "concurrency_graphs_enabled": "CONCURRENCY_GRAPHS_ENABLED",
    "concurrency_thresholds": "CONCURRENCY_THRESHOLDS",
    "advisor_appendix_enabled": "ADVISOR_APPENDIX_ENABLED",
    "trend_increase_pct": "TREND_INCREASE_PCT", "trend_min_abs_increase": "TREND_MIN_ABS_INCREASE",
    "trend_ratio_threshold": "TREND_RATIO_THRESHOLD",
    "query_stats_skip_existing": "QUERY_STATS_SKIP_EXISTING",
    "offline_cve_include_unreviewed": "OFFLINE_CVE_INCLUDE_UNREVIEWED",
    "query_shape_split_to_nodes": "QUERY_SHAPE_SPLIT_TO_NODES",
    "cluster_summary_source": "CLUSTER_SUMMARY_SOURCE",
})
# Per-environment notifications (agent/notifications.py). Tokens/webhooks are NOT here: they live in the
# separate NOTIFICATION_SECRET_ID secret (or NOTIFICATION_CONFIG_FILE locally).
_ENV_FIELDS.update({
    "environment": "ENVIRONMENT", "notify_source": "NOTIFY_SOURCE", "notify_after_report": "NOTIFY_AFTER_REPORT",
    "notify_env_overrides": "NOTIFY_ENV_OVERRIDES",
    "jira_mode": "JIRA_MODE", "jira_priorities": "JIRA_PRIORITIES", "jira_base_url": "JIRA_BASE_URL",
    "jira_deployment": "JIRA_DEPLOYMENT", "jira_project_key": "JIRA_PROJECT_KEY", "jira_issue_type": "JIRA_ISSUE_TYPE",
    "jira_assignee": "JIRA_ASSIGNEE", "jira_assignee_field": "JIRA_ASSIGNEE_FIELD", "jira_labels": "JIRA_LABELS",
    "jira_components": "JIRA_COMPONENTS", "jira_parent_key": "JIRA_PARENT_KEY", "jira_epic_key": "JIRA_EPIC_KEY",
    "jira_epic_link_field": "JIRA_EPIC_LINK_FIELD", "jira_priority_map": "JIRA_PRIORITY_MAP",
    "jira_extra_fields": "JIRA_EXTRA_FIELDS", "jira_dedupe_scope": "JIRA_DEDUPE_SCOPE",
    "jira_comment_on_existing": "JIRA_COMMENT_ON_EXISTING", "jira_max_issues": "JIRA_MAX_ISSUES",
    "jira_summary_template": "JIRA_SUMMARY_TEMPLATE",
    "slack_mode": "SLACK_MODE", "slack_priorities": "SLACK_PRIORITIES", "slack_channel": "SLACK_CHANNEL",
    "slack_mentions": "SLACK_MENTIONS", "slack_repeat_after_days": "SLACK_REPEAT_AFTER_DAYS",
    "slack_max_findings": "SLACK_MAX_FINDINGS",
})
_BOOL_FIELDS = {"cluster_summary", "offline_cve_include_unreviewed", "observability_enabled", "index_stats_enabled", "profiler_enabled",
                "metrics_graphs_enabled", "resource_investigation_enabled", "advisor_appendix_enabled",
                "concurrency_findings_enabled", "concurrency_graphs_enabled",
                "query_stats_skip_existing", "query_shape_split_to_nodes", "notify_after_report",
                "jira_comment_on_existing"}
_JSON_FIELDS = {"observability_sources": dict, "atlas_process_ids": dict, "ops_manager_host_ids": dict,
                "query_plan_requests": list, "metrics_graph_groups": list, "metrics_graph_config": dict,
                "resource_thresholds": dict, "concurrency_thresholds": dict, "notify_env_overrides": dict, "jira_priority_map": dict,
                "jira_extra_fields": dict}
_INT_FIELDS = {"http_timeout_seconds", "max_retries", "slow_ms", "expected_node_count",
               "report_max_tokens", "report_max_input_chars"}
_INT_FIELDS.update({"mongodb_timeout_ms", "mongodb_max_time_ms", "observability_max_rows",
                    "index_stats_max_collections", "query_shape_window_hours", "query_stats_max_summaries",
                    "observability_api_max_pages", "trend_increase_pct", "trend_min_abs_increase",
                    "trend_ratio_threshold", "jira_max_issues", "slack_repeat_after_days", "slack_max_findings"})
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
        elif key in _BOOL_FIELDS:
            value  = parse_bool_env(name, default=True)
        elif key in _JSON_FIELDS:
            value = json.loads(value)
            if not isinstance(value, _JSON_FIELDS[key]):
                raise ValueError(f"{name} must be a JSON {_JSON_FIELDS[key].__name__}")
        elif key in {"log_names", "observability_hosts"}:
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
    # The input bucket uses the same backend, credentials and object-key prefix.
    input_bucket = cfg.get("input_bucket") or bucket
    for scheme, provider in (("s3://", "s3"), ("gs://", "gcs"), ("file://", "local")):
        if input_bucket.startswith(scheme):
            if provider != cfg["storage_provider"]:
                raise ValueError("input_bucket and bucket must use the same storage provider")
            input_bucket = input_bucket[len(scheme):].rstrip("/")
            break
    if not input_bucket:
        raise ValueError("input_bucket must not be empty")
    if cfg["storage_provider"] != "local" and "/" in input_bucket:
        raise ValueError("input_bucket must be a bucket name; use prefix for object paths")
    cfg["input_bucket"] = input_bucket
    cfg["prefix"] = (cfg.get("prefix") or "mongodb-atlas-logs").strip("/")
    cfg["llm_provider"] = cfg.get("llm_provider") or defaults["llm_provider"]
    cfg["input_mode"] = str(cfg.get("input_mode", "atlas_api")).strip().lower()
    if cfg["input_mode"] not in {"atlas_api", "existing_bucket"}:
        raise ValueError("input_mode must be atlas_api or existing_bucket")
    if cfg["input_mode"] == "atlas_api":
        skills.atlas_logs().validate_config(cfg)
    # atlas_api: an empty cluster_name means "every cluster of the project" (agent.handler.run_all_clusters),
    # each stored under <prefix>/<cluster>/. existing_bucket only uses it as a report label.
    cfg["cluster_name"] = str(cfg.get("cluster_name") or "").strip()
    if cfg["input_mode"] != "atlas_api" and not cfg["cluster_name"]:
        cfg["cluster_name"] = "MongoDB cluster"
    cfg.setdefault("timezone", "UTC")
    if cfg["llm_provider"] == "vertex" and not (cfg.get("vertex_model") or os.environ.get("VERTEX_MODEL")):
        cfg["vertex_model"] = "claude-sonnet-5"
    cfg["cloud"] = cloud
    return cfg


def load_config() -> dict:
    cloud = detect_cloud()
    cfg = normalize_config(load_raw_config(cloud), cloud)
    from .safe_logging import log_config
    log_config(cfg)
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

    def open_read(self, key):
        """Binary reader over the stored object (streamed extraction input)."""
        return open(self._p(key), "rb")

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


def _get_single_store(config: dict):
    provider = config["storage_provider"]
    if provider == "s3":
        return skills.aws_storage().S3Store(config["bucket"], region=config.get("aws_region"))
    if provider == "gcs":
        return skills.gcp_storage().GCSStore(config["bucket"], project=config.get("gcp_project"))
    if provider == "local":
        return LocalStore(config["bucket"])
    raise ValueError(f"Unknown storage_provider {provider!r}")


def get_store(config: dict):
    """Route supplied inputs separately from extracts and reports."""
    from .bucket_routing import SplitBucketStore
    from .safe_logging import with_write_logging
    output = _get_single_store(config)
    input_bucket = config.get("input_bucket") or config["bucket"]
    if input_bucket == config["bucket"]:
        return with_write_logging(output)  # Preserve routing and return values.
    inputs = _get_single_store({**config, "bucket": input_bucket})
    return with_write_logging(SplitBucketStore(inputs, output, config["prefix"]))


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
