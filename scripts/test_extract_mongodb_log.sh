#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
EXTRACTOR="$SCRIPT_DIR/extract_mongodb_log.py"
INPUTS_DIR="${INPUTS_DIR:-$ROOT_DIR/inputs}"
OUTPUTS_DIR="${OUTPUTS_DIR:-$ROOT_DIR/outputs}"
SLOW_MS="${SLOW_MS:-1000}"
export DEBUG_FTDC_JSON="${DEBUG_FTDC_JSON:-0}"
FTDC_DIR="${FTDC_DIR:-${DIAG_DIR:-}}"

if [ -z "$FTDC_DIR" ]; then
  for candidate in \
    "$ROOT_DIR/diagnostic.data" \
    "$ROOT_DIR/diag/diagnostic.data" \
    "$ROOT_DIR/diag" \
    "$INPUTS_DIR/diagnostic.data" \
    "$INPUTS_DIR/diag/diagnostic.data" \
    "$INPUTS_DIR/diag"; do
    if [ -d "$candidate" ] || [ -f "$candidate" ]; then
      FTDC_DIR="$candidate"
      break
    fi
  done
fi

if [ ! -d "$INPUTS_DIR" ]; then
  echo "Input directory does not exist: $INPUTS_DIR" >&2
  exit 1
fi

LOG_FILES=()
for path in "$INPUTS_DIR"/*; do
  if [ -f "$path" ]; then
    LOG_FILES+=("$path")
  fi
done

if (( ${#LOG_FILES[@]} == 0 )); then
  echo "No log files found in $INPUTS_DIR" >&2
  exit 1
fi

mkdir -p "$OUTPUTS_DIR"
for path in "$OUTPUTS_DIR"/* "$OUTPUTS_DIR"/.[!.]* "$OUTPUTS_DIR"/..?*; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    rm -rf "$path"
  fi
done

DIAGNOSTIC_DUMP="${DIAGNOSTIC_DUMP:-0}"
RUN_TEMP_DIR=""
unset DIAGNOSTIC_DIR
if [ "$DIAGNOSTIC_DUMP" = "1" ]; then
  TEMP_ROOT="${TEMP_DIR:-./tmp}"
  RUN_TEMP_DIR="${TEMP_ROOT%/}/run-$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$RUN_TEMP_DIR"
  export DIAGNOSTIC_DIR="$RUN_TEMP_DIR"
  trap 'printf "Intermediate diagnostics: %s\n" "$RUN_TEMP_DIR" >&2' EXIT
  {
    printf 'inputs_dir=%s\n' "$INPUTS_DIR"
    printf 'outputs_dir=%s\n' "$OUTPUTS_DIR"
    printf 'ftdc_dir=%s\n' "${FTDC_DIR:-none}"
    printf 'debug_ftdc_json=%s\n' "$DEBUG_FTDC_JSON"
    printf 'diagnostic_dump=%s\n' "$DIAGNOSTIC_DUMP"
    printf 'started=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '\nlog_files=\n'
    printf '%s\n' "${LOG_FILES[@]}"
  } > "$RUN_TEMP_DIR/run-info.txt"
fi

EXTRACT_ARGS=(--output "$OUTPUTS_DIR" --slow-ms "$SLOW_MS")
if [ -n "$FTDC_DIR" ]; then
  EXTRACT_ARGS+=(--ftdc "$FTDC_DIR")
fi
if [ "$DEBUG_FTDC_JSON" != "0" ]; then
  EXTRACT_ARGS+=(--debug-ftdc-json)
fi
EXTRACT_ARGS+=("${LOG_FILES[@]}")

if [ "$DIAGNOSTIC_DUMP" = "1" ]; then
  if python3 "$EXTRACTOR" "${EXTRACT_ARGS[@]}" > "$RUN_TEMP_DIR/extractor.stdout.log" 2> "$RUN_TEMP_DIR/extractor.stderr.log"; then
    EXTRACTOR_STATUS=0
  else
    EXTRACTOR_STATUS=$?
  fi
  cat "$RUN_TEMP_DIR/extractor.stderr.log" >&2
  cat "$RUN_TEMP_DIR/extractor.stdout.log"
else
  if python3 "$EXTRACTOR" "${EXTRACT_ARGS[@]}"; then
    EXTRACTOR_STATUS=0
  else
    EXTRACTOR_STATUS=$?
  fi
fi
if [ "$EXTRACTOR_STATUS" -ne 0 ]; then
  exit "$EXTRACTOR_STATUS"
fi

if [ -n "$FTDC_DIR" ]; then
  echo "FTDC source selected: $FTDC_DIR"
else
  echo "FTDC source selected: none"
fi

[ -s "$OUTPUTS_DIR/extraction.json" ] || {
  echo "Missing or empty extraction.json" >&2
  exit 1
}
[ -s "$OUTPUTS_DIR/handoff.md" ] || {
  echo "Missing or empty handoff.md" >&2
  exit 1
}
if [ "$DIAGNOSTIC_DUMP" = "1" ]; then
  cp "$OUTPUTS_DIR/extraction.json" "$RUN_TEMP_DIR/extraction.json"
  cp "$OUTPUTS_DIR/handoff.md" "$RUN_TEMP_DIR/handoff.md"
fi

if [ "$DIAGNOSTIC_DUMP" = "1" ]; then
  VALIDATION_SINK=(tee "$RUN_TEMP_DIR/validation.log")
else
  VALIDATION_SINK=(cat)
fi
python3 - "$OUTPUTS_DIR/extraction.json" "${#LOG_FILES[@]}" <<'PYVALIDATE' | "${VALIDATION_SINK[@]}"
import json
import sys
import time

validation_started = time.perf_counter()
path, expected_count = sys.argv[1], int(sys.argv[2])
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)

ftdc = payload.get("ftdc", {})
print(f"FTDC status: {ftdc.get('status', 'not_provided')}")
print(f"FTDC timeline status: {ftdc.get('timeline_status', 'unknown')}")
print(f"FTDC log time range: {ftdc.get('log_time_range')}")
print(f"FTDC time range used: {ftdc.get('ftdc_time_range')}")
print(f"FTDC decoded records: {ftdc.get('records', 0)}")
print(f"FTDC resource samples used: {ftdc.get('resource_samples', 0)}")
print(f"FTDC records outside log timeline: {ftdc.get('filtered_outside_log_timeline', 0)}")
print(f"FTDC records without timestamp: {ftdc.get('filtered_without_timestamp', 0)}")
for item in ftdc.get("files", []):
    print(f"FTDC file loaded: {item.get('file')} status={item.get('status')} records={item.get('records', 0)} duration={item.get('duration_seconds', 'n/a')}s")

actual_count = len(payload.get("metadata", {}).get("input_files", []))
if actual_count != expected_count:
    raise SystemExit(f"Expected {expected_count} input files, extracted {actual_count}")
if "summary" not in payload or "issue_candidates" not in payload or "error_scan" not in payload:
    raise SystemExit("Extraction handoff is missing required sections")
for group in payload.get("slow_operations", []):
    if "sample_query_shape" not in group:
        raise SystemExit("Slow-operation group is missing sample_query_shape")
for group in payload.get("error_scan", {}).get("groups", []):
    sample = group.get("sample_log")
    if not isinstance(sample, dict) or not sample.get("raw_line"):
        raise SystemExit("Error group sample_log.raw_line is missing")
for group in payload.get("repeated_errors", []):
    for field in ("count", "first_seen", "last_seen", "sample_message", "sample_log", "operation_details"):
        if field not in group:
            raise SystemExit(f"Repeated-error group is missing {field}")
    if not group["sample_log"].get("raw_line"):
        raise SystemExit("Repeated-error group sample_log.raw_line is missing")
print(f"Validated {actual_count} input files -> {path}")
print(f"Validation: {time.perf_counter() - validation_started:.3f}s")
PYVALIDATE
