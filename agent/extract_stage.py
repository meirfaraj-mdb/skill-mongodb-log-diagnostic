"""Extract all raw MongoDB logs for each node/day in one extractor invocation.

Node groups, not individual files, are assigned to Cloud Run task shards.
Outputs retain the canonical extracts/mongodb layout used by reporting.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import threading
import sys
import tempfile
from pathlib import Path

from .common import EXTRACT_FILES, WORK_DIR, Layout, logger
from .skills import diagnostic_skill_dir

CONTENT_TYPES = {".json": "application/json", ".md": "text/markdown; charset=utf-8"}
PART = re.compile(r"^(.*)\.part-(\d+)(?:\.gz)?$")
MANIFEST = "source-files.json"


def validate_skill_bundle(skill_dir: Path | None = None) -> Path:
    """Check that required skill files exist and have not been overwritten."""
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
    for rel in ("scripts/driver_compatibility.py", "references/analysis-prompt.md",
                "references/extracted-signal-reference.md"):
        if not (skill_dir / rel).is_file():
            raise FileNotFoundError(f"Skill file missing: {skill_dir / rel}")
    return skill_dir


def _natural(value):
    return tuple((1, int(piece)) if piece.isdigit() else (0, piece.lower())
                 for piece in re.split(r"(\d+)", value))


def _allowed(name):
    lower = name.lower()
    return "audit" not in lower and (
        lower.endswith((".gz", ".log", ".txt")) or lower in ("mongodb", "mongod", "mongos")
        or PART.fullmatch(name) is not None)


def discover_logs(store, layout: Layout, log_date: str) -> list[dict]:
    day = layout.day(log_date) + "/"
    found = []
    for key in store.list_keys(day):
        if not key.startswith(day):
            continue
        parts = key[len(day):].split("/")
        if len(parts) == 3 and parts[1] == "mongodb" and _allowed(parts[2]):
            found.append({"host": parts[0], "host_dir": parts[0],
                          "log_name": "mongodb", "key": key})
    return sorted(found, key=lambda e: _natural(e["key"]))


def group_logs(logs):
    groups = {}
    for item in logs:
        keys = item.get("source_keys") or [item["key"]]
        for key in keys:
            if "audit" in Path(key).name.lower():
                continue
            host = item["host_dir"]
            group = groups.setdefault(host, {"host": item.get("host", host),
                                            "host_dir": host, "log_name": "mongodb",
                                            "source_keys": []})
            if key not in group["source_keys"]:
                group["source_keys"].append(key)
    for group in groups.values():
        group["source_keys"].sort(key=_natural)
        group["key"] = group["source_keys"][0]  # Legacy manifest consumers.
    return [groups[key] for key in sorted(groups, key=_natural)]


def _streams(keys):
    """Group byte-split pieces; do not insert newlines inside a split sequence."""
    sequences = {}
    names = {Path(key).name for key in keys}
    for key in keys:
        name = Path(key).name
        match = PART.fullmatch(name)
        base = match.group(1) if match else name
        token = (str(Path(key).parent), base, bool(match))
        sequences.setdefault(token, []).append((int(match.group(2)) if match else 0, key))
    result = []
    for (_, base, split), pieces in sequences.items():
        pieces.sort()
        if split:
            numbers = [n for n, _ in pieces]
            if numbers != list(range(1, len(pieces) + 1)):
                raise ValueError(f"Split sequence {base}: missing or duplicate numbered part")
            if base in names or base + ".gz" in names:
                raise ValueError(f"Split sequence {base}: original and parts both supplied; remove one copy")
        result.append((base, [key for _, key in pieces]))
    return [pieces for _, pieces in sorted(result, key=lambda pair: _natural(pair[0]))]


BLOCK = 1024 * 1024


def _open_source(store, key, work):
    """Yield a binary reader over one source object's *decompressed* bytes.

Uses ``store.open_read(key)`` when the store offers it (no local copy);
otherwise downloads to ``source.tmp`` (one source on disk at a time)."""
    opener = getattr(store, "open_read", None)
    gz = key.lower().endswith(".gz")
    raw = None
    if callable(opener):
        try:
            raw = opener(key)
        except (AttributeError, NotImplementedError):
            raw = None  # wrapped store without streaming support
    if raw is not None:
        try:
            yield gzip.GzipFile(fileobj=raw, mode="rb") if gz else raw
        finally:
            raw.close()
        return
    local = work / "source.tmp"
    store.download(key, local)
    try:
        with (gzip.open if gz else open)(local, "rb") as incoming:
            yield incoming
    finally:
        local.unlink(missing_ok=True)


def iter_input_blocks(store, keys, work):
    """Decompressed input as byte blocks; preserve records cut by byte splitting.

Independent files are separated by a newline if needed; pieces of one split
sequence are concatenated with nothing inserted. Memory stays bounded."""
    last = b""
    for sequence in _streams(keys):
        if last and last != b"\n":
            yield b"\n"
            last = b"\n"
        for key in sequence:
            for incoming in _open_source(store, key, work):
                while True:
                    block = incoming.read(BLOCK)
                    if not block:
                        break
                    last = block[-1:]
                    yield block


def assemble_input(store, keys, work):
    """File fallback: write all input to one gzip (level 1: it is read once, locally)."""
    target = work / "node-input.gz"
    with gzip.open(target, "wb", compresslevel=1) as combined:
        for block in iter_input_blocks(store, keys, work):
            combined.write(block)
    return target


def _stream_enabled() -> bool:
    return os.environ.get("MONGODB_LOG_DIAG_EXTRACT_STREAM", "1").strip().lower() not in {"0", "false", "no", "off"}


def _run_streaming(cmd, cwd, blocks, timeout_s, work):
    """Run the extractor reading stdin; feed it blocks; enforce the timeout. Returns (rc, stderr)."""
    err_path = work / "extractor.stderr"
    with open(err_path, "wb") as err, open(os.devnull, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err)
        expired = threading.Event()

        def _expire():
            expired.set()
            proc.kill()
        timer = threading.Timer(timeout_s, _expire)
        timer.daemon = True
        timer.start()
        try:
            try:
                for block in blocks:
                    proc.stdin.write(block)
            except BrokenPipeError:
                pass  # extractor exited early; its return code and stderr explain why
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            rc = proc.wait()
        except BaseException:
            proc.kill(); proc.wait()
            raise
        finally:
            timer.cancel()
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout_s)
    return rc, err_path.read_text(encoding="utf-8", errors="replace")


def extract_one(store, layout: Layout, log_date: str, entry: dict, slow_ms: float,
                skill_dir: Path, skip_existing: bool = True,
                timeout_s: int = 3 * 3600) -> dict:
    entry = group_logs([entry])[0]
    host_dir, log_name = entry["host_dir"], entry["log_name"]
    keys = entry["source_keys"]
    targets = {n: layout.extract(log_date, host_dir, log_name, n) for n in EXTRACT_FILES}
    manifest_key = layout.extract(log_date, host_dir, log_name, MANIFEST)
    # parser_version: an extract written by an older extractor (e.g. pre-1.4 "$db: ?" shapes) is redone.
    expected = {"version": 1, "parser_version": extractor_version(skill_dir), "source_keys": keys}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="extract-", dir=WORK_DIR))
    try:
        if skip_existing and all(store.exists(key) for key in targets.values()) and store.exists(manifest_key):
            try:
                saved = work / MANIFEST
                store.download(manifest_key, saved)
                if json.loads(saved.read_text(encoding="utf-8")) == expected:
                    logger.info("Skipping completed node extraction for %s (%s files)", host_dir, len(keys))
                    return {**entry, "status": "skipped_existing", "extract": targets}
            except (OSError, ValueError):
                logger.warning("Unreadable source manifest for %s; re-extracting", host_dir)
        validate_skill_bundle(skill_dir)
        out_dir = work / "out"
        cmd = [sys.executable, str(skill_dir / "scripts" / "extract_mongodb_log.py"),
               "--output", str(out_dir), "--slow-ms", str(slow_ms)]
        logger.info("Running one extractor for %s/%s (%s source files)", host_dir, log_name, len(keys))
        if _stream_enabled():
            # Decompressed bytes go straight to the extractor: no node-input.gz re-compression.
            # --input-name keeps the recorded input file name identical to the file path below.
            rc, stderr = _run_streaming(cmd + ["--input-name", "node-input.gz", "-"], str(skill_dir),
                                        iter_input_blocks(store, keys, work), timeout_s, work)
        else:
            local_log = assemble_input(store, keys, work)
            proc = subprocess.run(cmd + [str(local_log)], cwd=str(skill_dir), capture_output=True,
                                  text=True, timeout=timeout_s)
            rc, stderr = proc.returncode, proc.stderr
        if rc != 0:
            raise RuntimeError(f"Extractor failed rc={rc}: {stderr[-2000:]}")
        timings = stderr
        # Make the speed-up visible: parser version, JSON reader, input mode and the extractor's own timings.
        logger.info("Extractor %s for %s/%s: parser=%s input=%s %s", "finished", host_dir, log_name,
                    extractor_version(skill_dir), "stream" if _stream_enabled() else "file(gzip-1)",
                    " | ".join(line.strip() for line in timings.splitlines()
                               if line.strip().startswith(("JSON reader", "Loading log", "Writing output", "Total extraction"))))
        for name in targets:
            if not (out_dir / name).is_file():
                raise RuntimeError(f"Extractor did not produce {name}")
        uploaded = {}
        metadata = {"host": entry.get("host", host_dir), "log-name": log_name,
                    "log-date": log_date, "source-file-count": str(len(keys)), "slow-ms": slow_ms}
        for name, key in targets.items():
            path = out_dir / name
            uploaded[name] = store.upload(path, key, CONTENT_TYPES[path.suffix], metadata)
        saved = work / MANIFEST
        saved.write_text(json.dumps(expected, indent=2), encoding="utf-8")
        store.upload(saved, manifest_key, "application/json", metadata)
        return {**entry, "status": "extracted", "extract": targets, "uploaded": uploaded}
    finally:
        shutil.rmtree(work, ignore_errors=True)


_VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.M)


def extractor_version(skill_dir: Path) -> str | None:
    """The bundled extractor's VERSION constant (read from source; the script is not imported)."""
    try:
        match = _VERSION_RE.search((Path(skill_dir) / "scripts" / "extract_mongodb_log.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _shard(items: list, index: int | None, count: int | None) -> list:
    index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", 0)) if index is None else index
    count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", 1)) if count is None else count
    if count < 1 or not 0 <= index < count:
        raise ValueError("Invalid extraction shard index/count")
    return items[index::count]


def run(config: dict, log_date: str, logs: list[dict] | None = None,
        skip_existing: bool = True, store=None, shard_index: int | None = None,
        shard_count: int | None = None) -> dict:
    skill_dir = diagnostic_skill_dir()
    layout = Layout.from_config(config)
    if store is None:
        from .providers import get_store
        store = get_store(config)
    discovered = discover_logs(store, layout, log_date)
    if logs is None:
        logs = discovered
    else:
        logs = [{**e, "key": e.get("key") or layout.raw_log(log_date, e["host_dir"], e["log_name"])}
                for e in logs]
        # Explicit selections select nodes; include every staged file for those nodes.
        selected = {e["host_dir"] for e in logs}
        logs += [e for e in discovered if e["host_dir"] in selected]
    nodes = _shard(group_logs(logs), shard_index, shard_count)
    if not nodes:
        logger.warning("No raw logs to extract for %s in this shard", log_date)
        return {"log_date": log_date, "extracts": [], "failed": []}
    slow_ms = float(config.get("slow_ms", 1000))
    results, failed = [], []
    for entry in nodes:
        try:
            results.append(extract_one(store, layout, log_date, entry, slow_ms, skill_dir, skip_existing))
        except Exception as error:
            logger.exception("Extraction failed for node %s", entry["host_dir"])
            failed.append({**entry, "error": str(error)[:1000]})
    if not results:
        raise RuntimeError(f"All extractions failed: {failed}")
    return {"log_date": log_date, "extracts": results, "failed": failed}
