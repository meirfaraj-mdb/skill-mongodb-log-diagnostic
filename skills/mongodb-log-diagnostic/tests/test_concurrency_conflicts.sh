#!/usr/bin/env bash
# Parser 1.6/1.7 regression: write conflicts, transaction outcomes, lock waits, hot write target, minute timeline.
# Scenario: one document (items by `id`) is updated 3,600 times in six minutes with write conflicts,
# while 400 other documents receive background updates. The extractor must surface the single hot target.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTOR="$SCRIPT_DIR/extract_mongodb_log.py"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/out"

python3 - "$TMP_DIR/conflicts.log" <<'PY'
import json, sys
lines = []
def t(minute, second, cs):
    return {"$date": f"2026-03-10T15:{minute:02d}:{second:02d}.{cs:02d}0Z"}
def update(ts, doc_id, conflicts, dur, cpu, app="app-service-1"):
    attr = {"type": "command", "ns": "appdb.items", "appName": app,
            "command": {"findAndModify": "items", "query": {"id": doc_id}, "update": {"$inc": {"counter": 5}}, "new": True, "$db": "appdb"},
            "planSummary": "IXSCAN { id: 1 }", "keysExamined": 1, "docsExamined": 1, "nMatched": 1, "nModified": 1,
            "numYields": 0, "queryHash": "QH000001", "writeConflicts": conflicts, "cpuNanos": cpu, "durationMillis": dur,
            "locks": {"Global": {"acquireCount": {"w": 3}, "acquireWaitCount": {"w": 1 if conflicts else 0}, "timeAcquiringMicros": {"w": 4000 if conflicts else 0}}}}
    return {"t": ts, "s": "I", "c": "WRITE" if False else "COMMAND", "id": 51803, "ctx": "conn9", "msg": "Slow query", "attr": attr}
# Background: 400 distinct documents, 2 updates each, spread over 14:00-14:59 (no conflicts).
for i in range(800):
    lines.append(update({"$date": f"2026-03-10T14:{(i // 14) % 60:02d}:{i % 60:02d}.000Z"}, f"bg-{i % 400}", 0, 120, 2_000_000))
# Incident: one hot document, 600 updates/minute for 15:07-15:12 with conflicts.
for minute in range(7, 13):
    for k in range(600):
        lines.append(update(t(minute, k % 60, k % 100), "HOT-DOC-0001", 3, 250, 9_000_000))
# Transaction outcomes (TXN slow transaction records).
for k in range(40):
    lines.append({"t": t(9, k % 60, 0), "s": "I", "c": "TXN", "id": 51802, "ctx": "conn9", "msg": "transaction",
                  "attr": {"parameters": {"lsid": {"id": "x"}, "txnNumber": k, "autocommit": False},
                           "readTimestamp": "Timestamp(0, 0)", "terminationCause": "aborted" if k % 4 else "committed",
                           "abortCause": "WriteConflict" if k % 4 else None, "timeActiveMicros": 900, "timeInactiveMicros": 120000,
                           "numYields": 0, "wasPrepared": False, "durationMillis": 150}})
# One explicit conflict error and one non-slow record with a temporarily-unavailable counter.
lines.append({"t": t(10, 1, 0), "s": "W", "c": "COMMAND", "id": 20444, "ctx": "conn7", "msg": "Plan executor error during findAndModify",
              "attr": {"error": {"code": 112, "codeName": "WriteConflict", "errmsg": "WriteConflict error"}, "ns": "appdb.items"}})
lines.append({"t": t(10, 2, 0), "s": "I", "c": "WRITE", "id": 51803, "ctx": "conn7", "msg": "Slow query",
              "attr": {"type": "update", "ns": "appdb.accounts", "command": {"q": {"ownerId": {"$oid": "000000000000000000000001"}}, "u": {"$set": {"x": 1}}},
                       "temporarilyUnavailableErrors": 2, "durationMillis": 20, "cpuNanos": 100}})
open(sys.argv[1], "w").write("\n".join(json.dumps(x) for x in lines) + "\n")
PY

python3 "$EXTRACTOR" --output "$TMP_DIR/out" --slow-ms 100 "$TMP_DIR/conflicts.log" >/dev/null 2>"$TMP_DIR/err.txt"

python3 - "$TMP_DIR/out/extractionOccurence.json" "$TMP_DIR/out/extractionshort.json" <<'PY'
import json, sys
full = json.load(open(sys.argv[1], encoding="utf-8"))
short = json.load(open(sys.argv[2], encoding="utf-8"))
raw = open(sys.argv[1], encoding="utf-8").read()
assert full["schema_version"] == "1.7", full["schema_version"]

# 1. Literal document identifiers never leak.
assert "HOT-DOC-0001" not in raw and "bg-1" not in raw and "000000000000000000000001" not in raw

# 2. Global write-conflict and lock-wait evidence.
c = full["concurrency"]
wc = c["write_conflicts"]
assert wc["total"] == 3600 * 3, wc
assert wc["records_with_write_conflicts"] == 3600, wc
assert wc["by_namespace"][0] == ["appdb.items", 10800.0], wc
assert wc["by_app"][0][0] == "app-service-1", wc
assert wc["first_seen"].startswith("2026-03-10T15:07") and wc["last_seen"].startswith("2026-03-10T15:12"), wc
assert c["lock_wait"]["records"] == 3600 and c["lock_wait"]["micros_total"] == 3600 * 4000, c["lock_wait"]
assert c["temporarily_unavailable_errors_total"] == 2, c
assert c["conflict_errors"].get("WriteConflict") == 1, c["conflict_errors"]

# 3. Transaction outcomes.
tx = c["transactions"]
assert tx["records"] == 40 and tx["termination_causes"] == {"aborted": 30, "committed": 10}, tx
assert tx["abort_causes"][0] == ["WriteConflict", 30], tx

# 4. One hot write target dominates appdb.items.
hot = c["hot_write_targets"][0]
assert hot["namespace"] == "appdb.items" and hot["filter_fields"] == ["id"], hot
assert hot["count"] == 3600 and hot["write_conflicts"] == 10800, hot
assert hot["max_per_minute"] == 600 and hot["peak_minute"].endswith("15H07"), hot
assert hot["share_of_namespace_writes"] > 0.8, hot
assert len(hot["target_id"]) == 16, hot
assert sum(e[next(iter(e))]["occur"] for e in hot["occurrence_timestamps"]["20260310"]) == 3600
background = [t for t in c["hot_write_targets"] if t["target_id"] != hot["target_id"]]
assert background and max(t["count"] for t in background) == 2, background[:2]
cands = [x for x in full["issue_candidates"] if x["category"] == "hot_write_target"]
assert len(cands) == 1 and cands[0]["target_id"] == hot["target_id"], cands
assert any(x["category"] == "concurrency_conflict" for x in full["issue_candidates"])

# 5. Slow group carries conflict statistics and per-occurrence conflict metrics.
group = next(g for g in full["slow"]["operations"] if g["namespace"] == "appdb.items")
assert group["writeConflicts"]["total"] == 10800 and group["write_conflict_records"] == 3600, group
assert group["top_write_targets"][0] == {"target_id": hot["target_id"], "count": 3600}, group
assert group["distinct_write_targets"] == 401, group
details = [d for items in group["occurrence_timestamps"].values() for e in items for d in e.values()]
assert any(d.get("writeConflicts") == 3 and d.get("lockWaitMicros") == 4000 and d.get("target") == hot["target_id"] for d in details)
assert all("writeConflicts" not in d for d in details if d.get("target") != hot["target_id"]), "zero counters must be dropped"
assert full["slow"]["global_stats"]["writeConflicts"]["total"] == 10800
# 4,400 records exceed the 2,000-occurrence cap: the group must say so explicitly.
assert group["count"] == 4400 and group["occurrences_truncated"] is True and 0 < group["occurrences_recorded"] <= 2000, group["count"]
assert len(details) == group["occurrences_recorded"]
# 1.7: the sample is time-stratified, so every burst minute (15:07-15:12) keeps samples, not only the first ones.
assert group["occurrence_sampling"]["method"] == "time_stratified", group.get("occurrence_sampling")
kept_minutes = {k[:5] for items in group["occurrence_timestamps"].values() for e in items for k in e}
assert {"15:07", "15:08", "15:09", "15:10", "15:11", "15:12"} <= kept_minutes, sorted(kept_minutes)

# 6. Uncapped minute timeline (independent of the 2,000-occurrence group cap).
tl = full["timeline"]
minutes = {k: v for e in tl["occurrence_timestamps"]["20260310"] for k, v in e.items()}
for m in ("15H07", "15H08", "15H12"):
    assert minutes[m]["slow"] == 600 and minutes[m]["writeConflicts"] == 1800, (m, minutes[m])
assert minutes["15H09"]["txnAborted"] == 30 and minutes["15H09"]["txnCommitted"] == 10, minutes["15H09"]
assert tl["peak_minutes"]["writeConflicts"][0]["writeConflicts"] == 1800
assert sum(v.get("slow", 0) for v in minutes.values()) == full["slow"]["global_stats"]["count"]

# 7. Trends carry write-conflict counts; short file drops only occurrence arrays.
assert sum(b.get("write_conflicts", 0) for b in full["trends"]) == 10800
assert "occurrence_timestamps" not in json.dumps(short)
assert short["concurrency"]["hot_write_targets"][0]["count"] == 3600
assert short["timeline"]["peak_minutes"] == tl["peak_minutes"]
print("concurrency conflict extraction test: PASS")
PY

# FTDC: serverStatus.metrics.operation.writeConflicts and transactions.* were excluded by the generic
# resource filter ("operation" token). 1.6 reports them per minute under ftdc.concurrency.
python3 - "$TMP_DIR/ftdc.jsonl" <<'PY'
import json, sys
rows = []
for i in range(12):  # one sample per minute 15:04..15:15, counters cumulative
    burst = 7 <= 4 + i <= 12
    rows.append({"start": f"2026-03-10T15:{4 + i:02d}:00Z", "serverStatus": {
        "metrics": {"operation": {"writeConflicts": 1000 + i * 10 + (i - 2) * 1800 * burst + (1800 * 6 if 4 + i > 12 else 0)}},
        "transactions": {"totalAborted": 50 + i, "totalCommitted": 900 + i * 30, "currentActive": 40 if burst else 2},
        "system": {"cpu": {"user": 10 + i}}}})
open(sys.argv[1], "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
python3 "$EXTRACTOR" --output "$TMP_DIR/out2" --slow-ms 100 --ftdc "$TMP_DIR/ftdc.jsonl" "$TMP_DIR/conflicts.log" >/dev/null 2>"$TMP_DIR/err2.txt"
python3 - "$TMP_DIR/out2/extractionOccurence.json" <<'PY'
import json, sys
f = json.load(open(sys.argv[1]))["ftdc"]
cc = f["concurrency"]
assert "metrics.operation.writeConflicts" in cc["metrics_found"], cc["metrics_found"]
assert "transactions.totalAborted" in cc["metrics_found"], cc
mins = {k: v for e in cc["occurrence_timestamps"]["20260310"] for k, v in e.items()}
assert mins["15H08"]["metrics.operation.writeConflicts"] >= 1800, mins["15H08"]
assert mins["15H05"]["metrics.operation.writeConflicts"] == 10, mins["15H05"]
assert mins["15H08"]["transactions.currentActive"] == 40, mins["15H08"]
assert cc["peaks"]["transactions.currentActive"]["kind"] == "gauge"
print("ftdc concurrency extraction test: PASS")
PY
