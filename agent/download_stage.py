"""Stage 1 -- Atlas logs -> bucket, composed from skills:
    mongodb-atlas-logs  (Atlas API download, cloud-agnostic)
  + aws-storage | gcp-storage  (streamed upload to S3 / GCS)
Keys: <prefix>/<date>/<host>/mongodb/<log_name>.gz. Multiple raw log names can coexist per node.
"""
from __future__ import annotations

from . import skills
from .common import Layout, logger


def run(config: dict, log_date: str | None = None, skip_existing: bool = True, store=None, client=None) -> dict:
    atlas = skills.atlas_logs()
    layout = Layout.from_config(config)
    if store is None:
        from .providers import get_store
        store = get_store(config)

    def key_for(entry):
        return layout.raw_log(entry["log_date"], entry["host_dir"], entry["log_name"])

    def sink(entry, fileobj, metadata):
        return store.upload_stream(fileobj, key_for(entry), "application/gzip", metadata)

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
