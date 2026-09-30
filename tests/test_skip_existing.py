"""Offline regression tests for per-log download/extraction resume semantics."""
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent import download_stage, extract_stage
from agent.common import EXTRACT_FILES, Layout
from agent.providers import LocalStore

DATE = "2026-09-29"
CFG = {"bucket": "local", "prefix": "atlas-logs", "group_id": "group",
       "cluster_name": "Cluster1", "timezone": "UTC", "api_version": "2025-03-12",
       "atlas_public_key": "p", "atlas_private_key": "s"}

class AtlasClient:
    def __init__(self):
        self.downloads = []
    def get_json(self, path, query=None):
        return {"results": [{"hostname": f"node-{i}", "typeName": "REPLICA_SECONDARY"}
                            for i in range(2)]}
    def download_log(self, group, host, log, start, end):
        self.downloads.append(host)
        return io.BytesIO(b"fake-gzip-data")

class TrackedStore(LocalStore):
    def __init__(self, root):
        super().__init__(root)
        self.operations = []
    def download(self, key, path):
        self.operations.append(("download", key))
        super().download(key, path)
    def upload(self, path, key, content_type, metadata=None):
        self.operations.append(("upload", key))
        return super().upload(path, key, content_type, metadata)

with tempfile.TemporaryDirectory() as tmp:
    store = TrackedStore(tmp)
    layout = Layout.from_config(CFG)
    client = AtlasClient()
    first = download_stage.run(CFG, DATE, store=store, client=client)
    assert client.downloads == ["node-0", "node-1"]
    assert all(x["status"] == "downloaded" for x in first["logs"])
    second = download_stage.run(CFG, DATE, store=store, client=client)
    assert client.downloads == ["node-0", "node-1"], "existing logs were downloaded again"
    assert all(x["status"] == "skipped_existing" for x in second["logs"])
    store._p(layout.raw_log(DATE, "node-1", "mongodb")).unlink()
    third = download_stage.run(CFG, DATE, store=store, client=client)
    assert client.downloads == ["node-0", "node-1", "node-1"]
    assert [x["status"] for x in third["logs"]] == ["skipped_existing", "downloaded"]

    # Seed completed node-0 and incomplete node-1: only node-1 should run.
    for name in EXTRACT_FILES:
        key = layout.extract(DATE, "node-0", "mongodb", name)
        store.put_text(key, "done", "text/plain")
    partial_key = layout.extract(DATE, "node-1", "mongodb", EXTRACT_FILES[0])
    store.put_text(partial_key, "partial", "text/plain")
    entries = extract_stage.discover_logs(store, layout, DATE)
    assert len(entries) == 2
    with patch.object(extract_stage, "validate_skill_bundle", side_effect=AssertionError("should not validate")):
        skipped = extract_stage.extract_one(store, layout, DATE, entries[0], 1000, Path(tmp), True)
    assert skipped["status"] == "skipped_existing"

    # Real minimal skill stub: subprocess produces all three required outputs.
    skill = Path(tmp) / "stub-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "references").mkdir()
    (skill / "SKILL.md").write_text("---\n")
    for name in ("extract_mongodb_log.py", "ftdc_decoder.py", "driver_compatibility.py"):
        (skill / "scripts" / name).write_text("#!/usr/bin/env python3\n")
    for name in ("analysis-prompt.md", "extracted-signal-reference.md"):
        (skill / "references" / name).write_text("reference\n")
    def run_extractor(cmd, **kwargs):
        out = Path(cmd[cmd.index("--output") + 1]); out.mkdir(parents=True)
        for name in EXTRACT_FILES:
            (out / name).write_text("generated")
        return type("Result", (), {"returncode": 0, "stderr": ""})()
    with patch.object(extract_stage.subprocess, "run", side_effect=run_extractor), \
         patch.object(extract_stage, "diagnostic_skill_dir", return_value=skill):
        result = extract_stage.run(CFG, DATE, store=store, shard_index=0, shard_count=1,
                                   logs=entries)
    assert [x["status"] for x in result["extracts"]] == ["skipped_existing", "extracted"]
    ops = store.operations
    assert [(typ, key.rsplit("/", 1)[-1]) for typ, key in ops if typ in ("download", "upload")] == [
        ("download", "mongodb.gz"),
        ("upload", "extractionOccurence.json"),
        ("upload", "extractionshort.json"),
        ("upload", "handoff.md"),
    ]
    with patch.object(extract_stage, "validate_skill_bundle", side_effect=AssertionError("should not validate")):
        repeat = extract_stage.run(CFG, DATE, store=store, logs=entries)
    assert all(x["status"] == "skipped_existing" for x in repeat["extracts"])
    assert store.operations == ops
print("Download and extraction skip/resume tests passed")
