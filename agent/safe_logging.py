"""Allowlisted configuration logging and successful ObjectStore write receipts.

No credential dictionaries, query payloads, connection strings, file contents,
exception messages or environment dumps are logged by this module.
"""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .common import logger

_SCALARS = frozenset('cloud input_mode storage_provider llm_provider timezone slow_ms expected_node_count report_max_tokens report_max_input_chars cluster_summary http_timeout_seconds max_retries bedrock_region vertex_location aws_region observability_enabled deployment_type mongodb_timeout_ms mongodb_max_time_ms observability_max_rows index_stats_enabled index_stats_max_collections query_shape_source query_shape_window_hours query_stats_max_summaries profiler_enabled observability_api_max_pages'.split())
_LOCATORS = frozenset('bucket input_bucket prefix group_id cluster_name bedrock_model_id vertex_model vertex_project gcp_project'.split())
_SOURCES = {'bucket', 'mongodb', 'atlas_api', 'ops_manager', 'disabled'}


def safe_locator(value):
    """Strip URI credentials/query/fragment; refuse connection-string values."""
    text = str(value)
    if text.lower().startswith(('mongodb:', 'mongodb+srv:')):
        return '[REDACTED_CONNECTION_STRING]'
    if '://' in text:
        try:
            parsed = urlsplit(text)
            host = parsed.hostname or ''
            if parsed.port:
                host += ':' + str(parsed.port)
            return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
        except ValueError:
            return '[INVALID_URI]'
    # Plain locators must not encode credential-bearing URI components.
    if any(c in text for c in ('@', '?', '#', '\n', '\r')):
        return '[REDACTED_LOCATOR]'
    return text


def log_config(config):
    summary = {}
    for key in sorted(_SCALARS):
        value = config.get(key)
        if isinstance(value, (bool, int, float)) or (isinstance(value, str) and '://' not in value and not any(c in value for c in ('@', '?', '\n', '\r'))):
            summary[key] = value
    for key in sorted(_LOCATORS):
        if key in config:
            summary[key] = safe_locator(config[key])
    names = config.get('log_names')
    if isinstance(names, list):
        summary['log_names'] = [safe_locator(name) for name in names if isinstance(name, str)]
    sources = config.get('observability_sources')
    if isinstance(sources, dict):
        from .observability_contract import ARTIFACTS
        summary['observability_sources'] = {k: v for k, v in sources.items() if k in ARTIFACTS and isinstance(v, str) and v in _SOURCES}
    summary['input_bucket'] = safe_locator(config.get('input_bucket') or config.get('bucket', ''))
    summary['adk_model'] = safe_locator(__import__('os').environ.get('MONGODB_LOG_DIAG_ADK_MODEL', ''))
    logger.info('Configuration (allowlisted; credentials omitted): %s', json.dumps(summary, sort_keys=True))


def log_generated(destination, *, local_path=None, byte_count=None):
    uri = safe_locator(destination)
    parsed = urlsplit(uri)
    record = {'destination_uri': uri, 'destination_bucket': parsed.netloc if parsed.scheme in {'gs', 's3'} else None,
              'object_path': parsed.path.lstrip('/') if parsed.scheme else uri}
    if local_path is None and parsed.scheme == 'file':
        local_path = parsed.path
    if local_path is not None:
        record['generated_local_path'] = safe_locator(Path(local_path).absolute())
    if byte_count is not None:
        record['bytes'] = byte_count
    logger.info('Generated file: %s', json.dumps(record, sort_keys=True))


class LoggingStore:
    """Delegate unchanged; log only after successful writes, never on failure."""
    def __init__(self, store):
        self._delegate = store

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def upload(self, path, key, content_type, metadata=None):
        result = self._delegate.upload(path, key, content_type, metadata)
        log_generated(self._delegate.uri(key), local_path=path)
        return result

    def upload_stream(self, fileobj, key, content_type, metadata=None):
        result = self._delegate.upload_stream(fileobj, key, content_type, metadata)
        log_generated(self._delegate.uri(key))
        return result

    def put_text(self, key, text, content_type):
        result = self._delegate.put_text(key, text, content_type)
        log_generated(self._delegate.uri(key), byte_count=len(text.encode('utf-8')))
        return result


def with_write_logging(store):
    return store if isinstance(store, LoggingStore) else LoggingStore(store)
