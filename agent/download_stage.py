"""Stage 1 -- Atlas logs -> bucket, composed from skills:

mongodb-atlas-logs  (Atlas API download, cloud-agnostic)
+ aws-storage | gcp-storage  (streamed upload to S3 / GCS)
Keys: <prefix>/<date>/<host>/mongodb/<log_name>.gz.
Multiple raw log names can coexist per node.
"""
from __future__ import annotations

import time

from . import skills
from .common import Layout, logger

PROGRESS_BYTES = 10 * 1024 * 1024


class _ProgressReader:
    """Count bytes consumed from Atlas without buffering the response."""

    def __init__(self, source, location):
        self.source = source
        self.location = location
        self.total = 0
        self.next_report = PROGRESS_BYTES

    def read(self, size=-1):
        data = self.source.read(size)
        self.total += len(data)
        while self.total >= self.next_report:
            logger.info("Atlas upload progress target=%s transferred_mib=%d",
                        self.location, self.next_report // (1024 * 1024))
            self.next_report += PROGRESS_BYTES
        return data


def run(config: dict, log_date: str | None = None, skip_existing: bool = True, store=None, client=None) -> dict:
    atlas = skills.atlas_logs()
    layout = Layout.from_config(config)
    if store is None:
        from .providers import get_store
        store = get_store(config)

    def key_for(entry):
        return layout.raw_log(entry["log_date"], entry["host_dir"], entry["log_name"])

    def sink(entry, fileobj, metadata):
        key = key_for(entry)
        target = store.uri(key)
        # Log only safe, allowlisted response headers, never auth headers or URLs.
        headers = getattr(fileobj, "headers", None)
        header = (lambda name: headers.get(name) if headers is not None else None)
        length = header("Content-Length")
        metadata_shape = {str(k): len(str(v).encode("utf-8")) for k, v in metadata.items()}
        logger.info("Atlas stream ready target=%s status=%s content_length=%s content_type=%s "
                    "transfer_encoding=%s content_encoding=%s metadata_value_bytes=%s",
                    target, getattr(fileobj, "status", "?"), length,
                    header("Content-Type"), header("Transfer-Encoding"), header("Content-Encoding"),
                    metadata_shape)
        reader = _ProgressReader(fileobj, target)
        started = time.monotonic()
        try:
            location = store.upload_stream(reader, key, "application/gzip", metadata)
        except Exception:
            logger.exception("Atlas upload failed target=%s bytes_read=%d elapsed_seconds=%.1f",
                             target, reader.total, time.monotonic() - started)
            raise
        logger.info("Atlas upload complete target=%s bytes_read=%d expected_content_length=%s elapsed_seconds=%.1f",
                    target, reader.total, length, time.monotonic() - started)
        return location

    exists = (lambda entry: store.exists(key_for(entry))) if skip_existing else None
    result = atlas.archive_logs(config, log_date, sink=sink, exists=exists, client=client)
    for entry in result["logs"]:
        entry["key"] = key_for(entry)
        logger.info("Atlas log %s: %s", entry["status"], store.uri(entry["key"]))
    # The Atlas skill records individual failures. Stop before extraction if even
    # one Atlas download or bucket upload failed; successful nodes remain resumable.
    if result.get("failed"):
        failures = "; ".join(
            f"{item.get('host', '?')}/{item.get('log_name', '?')}: {item.get('error', 'unknown error')}"
            for item in result["failed"]
        )
        raise RuntimeError(f"Atlas download or {config.get('storage_provider', 'bucket')} upload failed: {failures}")
    return result
