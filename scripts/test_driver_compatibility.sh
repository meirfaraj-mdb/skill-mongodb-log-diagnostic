#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="$SCRIPT_DIR/extract_mongodb_log.py"
LINES="${LINES:-100000}"
MAX_RSS_MB="${MAX_RSS_MB:-512}"
MODE="${MODE:-gzip}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/out"

python3 "$SCRIPT_DIR/test_driver_compatibility.py"

python3 - "$EXTRACTOR" <<'PYSTATIC'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text(encoding="utf-8")
main = source.split("def main()", 1)[1]
import re
assert not re.search(r"(?<![A-Za-z0-9_])events\.append", main), "whole-log event accumulation returned"
assert not re.search(r"(?<![A-Za-z0-9_])events\s*:\s*list", main), "main still declares a whole-log event list"
assert "analyzer.add(event)" in main and "driver.add(event)" in main
print("streaming source guard: PASS")
PYSTATIC

python3 - "$TMP_DIR" "$LINES" "$MODE" <<'PY'
import json
import gzip
import sys
from pathlib import Path

root = Path(sys.argv[1])
lines = int(sys.argv[2])
mode = sys.argv[3]
out = root / ("large.json.gz" if mode == "gzip" else "large.json")
handle = gzip.open(out, "wt", encoding="utf-8") if mode == "gzip" else out.open("w", encoding="utf-8")
with handle:
    build = {"msg": "buildInfo", "buildInfo": {"version": "8.0.0"}}
    handle.write(json.dumps(build) + "\n")
    for i in range(lines):
        event = {
            "t": {"$date": f"2026-09-01T10:{(i // 60) % 60:02d}:{i % 60:02d}.000Z"},
            "s": "E" if i % 10 == 0 else "I", "c": "QUERY", "id": i,
            "msg": "slow operation" if i % 10 == 0 else "connection observed",
            "attr": {
                "client": f"192.0.2.{(i % 20) + 1}:27017",
                "doc": {"application": {"name": f"app-{i % 4}"},
                        "driver": {"name": "mongo-csharp-driver", "version": "3.10.0"},
                        "platform": f".NET {i % 3 + 8}.0"},
                "ns": "db.collection", "commandName": "find",
                "durationMillis": 1500 if i % 10 == 0 else 2,
                "planSummary": "COLLSCAN" if i % 10 == 0 else "IXSCAN",
                "queryHash": "streaming-test", "docsExamined": 1000 if i % 10 == 0 else 1,
                "nreturned": 1, "appName": f"app-{i % 4}",
            },
        }
        handle.write(json.dumps(event) + "\n")
PY

RSS_FILE="$TMP_DIR/rss.txt"
INPUT_FILE="$TMP_DIR/large.json"
[ "$MODE" = "gzip" ] && INPUT_FILE="$TMP_DIR/large.json.gz"
if /usr/bin/time -f '%M' -o "$RSS_FILE" true 2>/dev/null; then
    /usr/bin/time -f '%M' -o "$RSS_FILE" python3 "$EXTRACTOR" --output "$TMP_DIR/out" "$INPUT_FILE" >/dev/null
else
    /usr/bin/time -l -o "$RSS_FILE" python3 "$EXTRACTOR" --output "$TMP_DIR/out" "$INPUT_FILE" >/dev/null
fi

if grep -Eq '^[0-9]+$' "$RSS_FILE"; then
    RSS_KB="$(awk 'NR==1 {print $1}' "$RSS_FILE")"
else
    RSS_KB="$(awk '/maximum resident set size/ {print $NF}' "$RSS_FILE")"
fi
RSS_KB="${RSS_KB:-0}"
if (( RSS_KB > MAX_RSS_MB * 1024 )); then
    echo "memory regression: maximum RSS ${RSS_KB} KB exceeds ${MAX_RSS_MB} MB" >&2
    exit 1
fi

python3 - "$TMP_DIR/out/extractionOccurence.json" "$LINES" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
expected = int(sys.argv[2]) + 1
assert payload["quality"]["parsed_records"] == expected, payload["quality"]
driver = payload["driverCompatibility"]
assert driver["count"] == int(sys.argv[2]), driver
assert driver["driver_log_count"] == int(sys.argv[2]), driver
assert driver["incompatible_count"] == 0, driver
versions = driver["distinct_compatible_drivers"]["mongo-csharp-driver"]
assert versions["3.10.0"]["distinctIps"], versions
assert versions["3.10.0"]["distinctAppNames"], versions
assert versions["3.10.0"]["distinctPlatforms"], versions
assert payload["slow_operations"], "slow-operation groups missing"
print(f"streaming driver compatibility test: PASS (records={expected})")
PY
printf 'maximum RSS: %s KB\n' "$RSS_KB"
