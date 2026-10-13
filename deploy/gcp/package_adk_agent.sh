#!/usr/bin/env bash
# Creates a self-contained source directory for Vertex AI Agent Engine.
# The diagnostic skill is already vendored under skills/mongodb-log-diagnostic.
set -euo pipefail
OUT=${1:-.build/adk-agent}
APP="$OUT/google_adk_agent"
rm -rf "$OUT"
mkdir -p "$APP"
cp -R google_adk_agent/. "$APP/"
mkdir -p "$APP/_vendor/skills"
cp -R agent "$APP/_vendor/agent"
cp -R skills/. "$APP/_vendor/skills/"

cp google_adk_agent/requirements.txt "$OUT/requirements.txt"
printf 'SKILLS_DIR=./skills\nDIAG_SKILL_DIR=./skills/mongodb-log-diagnostic\nCLOUD_PROVIDER=gcp\n' > "$OUT/.env"
echo "Prepared self-contained ADK package: $OUT"
