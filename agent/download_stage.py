"""Stage 1 -- Atlas logs -> bucket, composed from skills:
    mongodb-atlas-logs  (Atlas API download, cloud-agnostic)
  + aws-storage | gcp-storage  (streamed upload to S3 / GCS)
Keys: <prefix>/<date>/<host>/<log_name>.gz -- identical to the original lambda.
"""
from __future__ import annotations

from . import skills
from .common import Layout


def run(config: dict, log_date: str | None = None, skip_existing: bool = True, store=None, client=None) -> dict:
    atlas = skills.atlas_logs()
    layout = Layout.from_config(config)
    if store is None:
        from .providers import get_store
        store = get_store(config)

    def key_for(entry):
        return f"{layout.prefix}/{entry['relative_path']}"

    def sink(entry, fileobj, metadata):
        return store.upload_stream(fileobj, key_for(entry), "application/gzip", metadata)

    exists = (lambda entry: store.exists(key_for(entry))) if skip_existing else None
    result = atlas.archive_logs(config, log_date, sink=sink, exists=exists, client=client)
    for entry in result["logs"]:
        entry["key"] = key_for(entry)
    return result
