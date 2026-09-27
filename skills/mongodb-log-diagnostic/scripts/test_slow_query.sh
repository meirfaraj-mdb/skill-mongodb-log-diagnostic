#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="$SCRIPT_DIR/extract_mongodb_log.py"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/out"

cat > "$TMP_DIR/slow.json" <<'JSON'
{"t":{"$date":"2026-09-01T10:00:00.000Z"},"s":"I","c":"COMMAND","id":51800,"ctx":"conn-test","msg":"Slow query","attr":{"type":"command","ns":"test.collection","command":{"find":"collection","filter":{"status":"active"},"$db":"test"},"planSummary":"COLLSCAN","keysExamined":0,"docsExamined":1500,"nreturned":2,"durationMillis":1250,"workingMillis":1240,"cpuNanos":5000000,"numYields":3,"appName":"slow-test","queryHash":"abc123","hasSortStage":true}}
{"t":{"$date":"2026-09-01T10:01:00.000Z"},"s":"I","c":"COMMAND","id":51801,"ctx":"conn-test","msg":"Slow query","attr":{"type":"command","ns":"test.collection","command":{"find":"collection","filter":{"status":"active"},"$db":"test"},"planSummary":"COLLSCAN","keysExamined":0,"docsExamined":1700,"nreturned":2,"durationMillis":1500,"workingMillis":1490,"cpuNanos":6000000,"numYields":4,"appName":"slow-test","queryHash":"abc123","hasSortStage":true}}
{"t":{"$date":"2026-09-01T10:06:07.158+00:00"},"s":"I","c":"COMMAND","id":51803,"ctx":"conn3635323","msg":"Slow query","attr":{"type":"command","isFromUserConnection":true,"ns":"aDb.aCollection","collectionType":"normal","appName":"some_service","command":{"getMore":1919470844742893814,"collection":"aCollection","batchSize":256,"$db":"aDb","lsid":{"id":{"$uuid":"ffdd5bf3-1a8b-43c1-bc08-1694d1c2abd7"}},"$clusterTime":{"clusterTime":{"$timestamp":{"t":1788257166,"i":2}},"signature":{"hash":{"$binary":{"base64":"redacted","subType":"0"}},"keyId":7654512125343170566}}},"originatingCommand":{"aggregate":"aCollection","pipeline":[{"$changeStream":{"fullDocument":"updateLookup","startAfter":{"_data":"redacted"}}},{"$match":{"operationType":"insert"}}],"cursor":{"batchSize":256},"$db":"aDb","$readPreference":{"mode":"secondaryPreferred","maxStalenessSeconds":90}},"planSummary":"COLLSCAN","cursorid":1919470844742893814,"keysExamined":0,"docsExamined":1443,"nBatches":1,"numYields":541,"nreturned":0,"planCacheShapeHash":"B8D1FBAC","queryHash":"B8D1FBAC","planCacheKey":"A315CA1B","queryFramework":"classic","reslen":314,"locks":{"Global":{"acquireCount":{"r":1971}}},"readConcern":{"level":"majority"},"writeConcern":{"w":"majority","wtimeout":0,"provenance":"customDefault"},"storage":{},"cpuNanos":120350711,"numInterruptChecks":5580,"queues":{"execution":{"admissions":1971},"ingress":{"admissions":1}},"workingMillis":111,"durationMillis":111}}
JSON

python3 "$EXTRACTOR" --output "$TMP_DIR/out" --slow-ms 1000 "$TMP_DIR/slow.json" >/dev/null

python3 - "$TMP_DIR/out/extractionOccurence.json" "$TMP_DIR/out/extractionshort.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
short = json.load(open(sys.argv[2], encoding="utf-8"))
slow = payload["slow"]
groups = slow["operations"]
assert payload["slow_operations"] == groups
assert len(groups) == 2, groups
group = next(item for item in groups if item["namespace"] == "test.collection")
assert group["count"] == 2, group
assert group["plan_summary"] == "COLLSCAN", group
assert group["query_hash"] == "abc123", group
assert group["app_names"] == ["slow-test"], group
assert group["duration_ms"]["min"] == 1250.0, group
assert group["duration_ms"]["max"] == 1500.0, group
assert group["keys_examined"]["max"] == 0.0, group
assert group["docs_examined"]["max"] == 1700.0, group
assert group["n_returned"]["max"] == 2.0, group
assert group["collscan_count"] == 2, group
assert group["examined_returned_ratio_max"] == 850.0, group
assert group["sample_query_shape"], group
occurrences = group["occurrence_timestamps"]
assert len(occurrences) == 1 and len(next(iter(occurrences.values()))) == 2, occurrences
first_detail = next(iter(occurrences.values()))[0]
metrics = next(iter(first_detail.values()))
for field in ("workingMillis", "cpuNanos", "numYields", "docs_examined", "n_returned", "has_sort_stage"):
    assert field in metrics, (field, metrics)

change = next(item for item in groups if item["namespace"] == "aDb.aCollection")
assert change["count"] == 1, change
assert change["query_hash"] == "B8D1FBAC", change
assert change["sample_query_shape"]["aggregate"] == "?", change
change_metrics = next(iter(next(iter(change["occurrence_timestamps"].values()))[0].values()))
for field in ("nBatches", "cursorid", "planCacheShapeHash", "planCacheKey", "queryFramework", "workingMillis", "cpuNanos"):
    assert field in change_metrics, (field, change_metrics)
assert slow["global_stats"]["count"] == 3, slow
assert slow["global_stats"]["collscan_count"] == 3, slow
assert slow["global_stats"]["change_stream_count"] == 1, slow
assert slow["global_stats"]["duration_ms"]["min"] == 111.0, slow
assert slow["global_stats"]["duration_ms"]["max"] == 1500.0, slow
assert slow["global_stats"]["cpuNanos"]["max"] == 120350711.0, slow

def contains_occurrences(value):
    if isinstance(value, dict):
        return "occurrence_timestamps" in value or any(contains_occurrences(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_occurrences(v) for v in value)
    return False
assert not contains_occurrences(short), "short output contains occurrence details"
print("slow query extraction test: PASS")
PY
