#!/usr/bin/env python3
"""Pick the extractor's JSON decoder: run each installed backend on the same log and compare.

Usage: python3 scripts/bench_json_backends.py LOG [LOG ...] [--slow-ms 100] [--runs 1]

For every backend in (stdlib, msgspec, orjson) that is importable, the extractor runs with
MONGODB_LOG_DIAG_JSON=<backend>. Output files must be byte-identical to the stdlib run; a backend
whose output differs is rejected regardless of speed. The recommendation follows the agreed rule:
use msgspec; use orjson only if msgspec is unavailable, differs, or is not faster than stdlib.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EXTRACTOR = Path(__file__).with_name("extract_mongodb_log.py")
OUTPUTS = ("extractionOccurence.json", "extractionshort.json", "handoff.md")
BACKENDS = ("stdlib", "msgspec", "orjson")


def available(backend: str) -> bool:
    return backend == "stdlib" or importlib.util.find_spec(backend) is not None


def run(backend: str, log: Path, slow_ms: float, out: Path) -> float:
    env = {**os.environ, "MONGODB_LOG_DIAG_JSON": backend}
    started = time.perf_counter()
    subprocess.run([sys.executable, str(EXTRACTOR), "--output", str(out), "--slow-ms", str(slow_ms), str(log)],
                   env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--slow-ms", type=float, default=100.0)
    parser.add_argument("--runs", type=int, default=1, help="best of N runs per backend")
    args = parser.parse_args(argv)
    backends = [b for b in BACKENDS if available(b)]
    missing = [b for b in BACKENDS if b not in backends]
    if missing:
        print(f"not installed (skipped): {', '.join(missing)}")
    totals = {b: 0.0 for b in backends}
    identical = {b: True for b in backends}
    with tempfile.TemporaryDirectory() as tmp:
        for log in args.logs:
            reference = None
            for backend in backends:
                out = Path(tmp) / f"{log.name}.{backend}"
                best = min(run(backend, log, args.slow_ms, out) for _ in range(max(1, args.runs)))
                files = {name: (out / name).read_bytes() for name in OUTPUTS}
                if reference is None:
                    reference = files
                same = files == reference
                identical[backend] &= same
                totals[backend] += best
                print(f"{log.name:30s} {backend:8s} {best:8.2f}s  output {'IDENTICAL' if same else 'DIFFERENT'}")
    print()
    base = totals["stdlib"]
    for backend in backends:
        print(f"total {backend:8s} {totals[backend]:8.2f}s  ({base / totals[backend]:.2f}x vs stdlib)"
              f"{'' if identical[backend] else '  REJECTED: output differs'}")

    def good(backend: str) -> bool:
        return backend in totals and identical[backend] and totals[backend] < base * 0.97

    choice = "msgspec" if good("msgspec") else "orjson" if good("orjson") else "stdlib"
    print(f"\nrecommended: MONGODB_LOG_DIAG_JSON={choice}"
          + ("  (default 'auto' already picks it)" if choice == next((b for b in ("msgspec", "orjson") if b in totals), "stdlib") else ""))
    return 0 if all(identical.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
