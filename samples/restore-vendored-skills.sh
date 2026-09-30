#!/usr/bin/env bash
# Restore the complete skill trees from the authoritative GitHub checkout.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO='https://github.com/meirfaraj-mdb/skill-mongodb-log-diagnostic.git'
SOURCE_DIR=''
if [[ "${1:-}" == '--source-dir' && -n "${2:-}" ]]; then
  SOURCE_DIR="$2"
elif [[ $# -ne 0 ]]; then
  printf 'Usage: %s [--source-dir /path/to/existing/checkout]\n' "$0" >&2; exit 2
fi
if [[ -z "$SOURCE_DIR" ]]; then
  command -v git >/dev/null || { echo 'git is required to restore skills' >&2; exit 1; }
  TEMP="$(mktemp -d)"
  trap 'rm -rf "$TEMP"' EXIT
  git clone --depth 1 --branch main "$REPO" "$TEMP/repo"
  SOURCE_DIR="$TEMP/repo"
fi
for skill in mongodb-log-diagnostic mongodb-atlas-logs aws-storage gcp-storage mongodb-observability; do
  [[ -d "$SOURCE_DIR/skills/$skill" ]] || { echo "Missing $skill in repository checkout" >&2; exit 1; }
done
for f in \
  mongodb-log-diagnostic/SKILL.md \
  mongodb-log-diagnostic/scripts/extract_mongodb_log.py \
  mongodb-log-diagnostic/scripts/ftdc_decoder.py \
  mongodb-log-diagnostic/scripts/driver_compatibility.py \
  mongodb-log-diagnostic/references/analysis-prompt.md \
  mongodb-log-diagnostic/references/extracted-signal-reference.md \
  mongodb-atlas-logs/SKILL.md \
  mongodb-atlas-logs/scripts/atlas_logs.py \
  aws-storage/scripts/aws_storage.py \
  gcp-storage/scripts/gcp_storage.py \
  mongodb-observability/scripts/observability.py; do
  [[ -s "$SOURCE_DIR/skills/$f" ]] || { echo "Missing/empty upstream file: $f" >&2; exit 1; }
done
python3 -m py_compile \
  "$SOURCE_DIR/skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py" \
  "$SOURCE_DIR/skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py" \
  "$SOURCE_DIR/skills/mongodb-log-diagnostic/scripts/driver_compatibility.py" \
  "$SOURCE_DIR/skills/mongodb-atlas-logs/scripts/atlas_logs.py" \
  "$SOURCE_DIR/skills/aws-storage/scripts/aws_storage.py" \
  "$SOURCE_DIR/skills/gcp-storage/scripts/gcp_storage.py" \
  "$SOURCE_DIR/skills/mongodb-observability/scripts/observability.py"
for skill in mongodb-log-diagnostic mongodb-atlas-logs aws-storage gcp-storage mongodb-observability; do
  mkdir -p "$ROOT/skills/$skill"
  # Copy the whole upstream tree, including references, config, and tests.
  cp -R "$SOURCE_DIR/skills/$skill/." "$ROOT/skills/$skill/"
done
printf 'Restored all five complete skills from %s\n' "$SOURCE_DIR"
