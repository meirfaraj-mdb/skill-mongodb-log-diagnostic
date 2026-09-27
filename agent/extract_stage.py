"""Stage 2 -- run skill-mongodb-log-diagnostic's extractor per node; push outputs to the bucket.

Parallelism: Step Functions Map (AWS) passes one log per invocation; Cloud Run Jobs (GCP) run N tasks
and each task takes logs[CLOUD_RUN_TASK_INDEX::CLOUD_RUN_TASK_COUNT].
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import EXTRACT_FILES, WORK_DIR, Layout, logger
from .skills import diagnostic_skill_dir

CONTENT_TYPES = {".json": "application/json", ".md": "text/markdown; charset=utf-8"}


def validate_skill_bundle(skill_dir: Path | None = None) -> Path:
    """SKILL.md 'File boundaries' check -- scripts must not have been overwritten by SKILL.md."""
    skill_dir = skill_dir or diagnostic_skill_dir()
    expected = {"scripts/extract_mongodb_log.py": "#!/usr/bin/env python3",
                "scripts/ftdc_decoder.py": "#!/usr/bin/env python3", "SKILL.md": "---"}
    for rel, first in expected.items():
        path = skill_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"Skill file missing: {path}")
        with path.open(encoding="utf-8") as fh:
            head = fh.readline().strip()
        if head != first:
            raise RuntimeError(f"Skill bundle corrupted: {rel} starts with {head!r}")
    for rel in ("scripts/driver_compatibility.py", "references/analysis-prompt.md", "references/extracted-signal-reference.md"):
        if not (skill_dir / rel).is_file():
            raise FileNotFoundError(f"Skill file missing: {skill_dir / rel}")
    return skill_dir


def extract_one(store, layout: Layout, log_date: str, entry: dict, slow_ms: float, skill_dir: Path,
                skip_existing: bool = True, timeout_s: int = 3 * 3600) -> dict:
    host_dir, log_name = entry["host_dir"], entry["log_name"]
    targets = {n: layout.extract(log_date, host_dir, log_name, n) for n in EXTRACT_FILES}
    # A completed canonical extraction means this node/log was already uploaded.
    # Process entries sequentially: download -> extract -> upload all outputs -> next node.
    if skip_existing and store.exists(targets["extractionOccurence.json"]):
        return {**entry, "status": "skipped_existing", "extract": targets}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="extract-", dir=WORK_DIR))
    try:
        local_log = work / "input" / f"{host_dir}__{log_date}__{log_name}.gz"
        store.download(entry["key"], local_log)
        out_dir = work / "out"
        cmd = [sys.executable, str(skill_dir / "scripts" / "extract_mongodb_log.py"),
               "--output", str(out_dir), "--slow-ms", str(slow_ms), str(local_log)]
        logger.info("Running extractor for %s/%s", host_dir, log_name)
        proc = subprocess.run(cmd, cwd=str(skill_dir), capture_output=True, text=True, timeout=timeout_s)
        if proc.returncode != 0:
            raise RuntimeError(f"Extractor failed rc={proc.returncode}: {proc.stderr[-2000:]}")
        uploaded = {}
        for name, key in targets.items():
            path = out_dir / name
            if not path.is_file():
                raise RuntimeError(f"Extractor did not produce {name}")
            uploaded[name] = store.upload(path, key, CONTENT_TYPES[path.suffix], {
                "host": entry.get("host", host_dir), "log-name": log_name, "log-date": log_date,
                "source-key": entry["key"], "slow-ms": slow_ms})
        return {**entry, "status": "extracted", "extract": targets, "uploaded": uploaded}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def discover_logs(store, layout: Layout, log_date: str) -> list[dict]:
    day = layout.day(log_date) + "/"
    found = []
    for key in store.list_keys(day):
        parts = key[len(day):].split("/")
        if len(parts) == 3 and parts[1] == "mongodb" and parts[2].endswith(".gz") and "audit" not in parts[2]:
            found.append({"host": parts[0], "host_dir": parts[0], "log_name": parts[2][:-3], "key": key})
    return sorted(found, key=lambda e: (e["host_dir"], e["log_name"]))


def _shard(items: list, index: int | None, count: int | None) -> list:
    index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", 0)) if index is None else index
    count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", 1)) if count is None else count
    return items[index::count] if count > 1 else items


def run(config: dict, log_date: str, logs: list[dict] | None = None, skip_existing: bool = True,
        store=None, shard_index: int | None = None, shard_count: int | None = None) -> dict:
    skill_dir = validate_skill_bundle()
    layout = Layout.from_config(config)
    if store is None:
        from .providers import get_store
        store = get_store(config)
    if logs is None:
        logs = discover_logs(store, layout, log_date)
    else:
        logs = [{**e, "key": e.get("key") or layout.raw_log(log_date, e["host_dir"], e["log_name"])} for e in logs]
    logs = _shard([e for e in logs if "audit" not in e["log_name"]], shard_index, shard_count)
    if not logs:
        logger.warning("No raw logs to extract for %s in this shard", log_date)
        return {"log_date": log_date, "extracts": [], "failed": []}
    slow_ms = float(config.get("slow_ms", 1000))
    results, failed = [], []
    for entry in logs:
        try:
            results.append(extract_one(store, layout, log_date, entry, slow_ms, skill_dir, skip_existing))
        except Exception as error:
            logger.exception("Extraction failed for %s", entry["key"])
            failed.append({**entry, "error": str(error)[:1000]})
    if not results:
        raise RuntimeError(f"All extractions failed: {failed}")
    return {"log_date": log_date, "extracts": results, "failed": failed}
