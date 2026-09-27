#!/usr/bin/env bash
# Creates a self-contained source directory for Vertex AI Agent Engine.
# The diagnostic skill is already vendored under skills/mongodb-log-diagnostic.
set -euo pipefail
OUT=${1:-.build/adk-agent}
ROOT_SKILL="skills/mongodb-log-diagnostic"
for file in "$ROOT_SKILL/SKILL.md" "$ROOT_SKILL/scripts/extract_mongodb_log.py" \
            "$ROOT_SKILL/references/analysis-prompt.md" "$ROOT_SKILL/references/extracted-signal-reference.md"; do
  test -f "$file" || { echo "Missing vendored skill file: $file" >&2; exit 1; }
done
rm -rf "$OUT"
mkdir -p "$OUT/skills"
cp -R google_adk_agent "$OUT/google_adk_agent"
cp -R agent "$OUT/agent"
cp -R skills/. "$OUT/skills/"
cp google_adk_agent/requirements.txt "$OUT/requirements.txt"
printf 'SKILLS_DIR=./skills\nDIAG_SKILL_DIR=./skills/mongodb-log-diagnostic\nCLOUD_PROVIDER=gcp\n' > "$OUT/.env"
echo "Prepared self-contained ADK package: $OUT"
