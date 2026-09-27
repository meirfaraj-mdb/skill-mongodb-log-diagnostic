#!/usr/bin/env bash
# Deploy Google ADK wrapper to Vertex AI Agent Engine.
# Prerequisites: gcloud + ADK CLI authenticated; an existing GCS bucket and Secret Manager secret.
set -euo pipefail
PROJECT=${PROJECT:?set PROJECT}
REGION=${REGION:-us-central1}
STAGING_BUCKET=${STAGING_BUCKET:?set STAGING_BUCKET as gs://...}
RUNTIME_SA=${RUNTIME_SA:?set RUNTIME_SA to the Agent Engine runtime service-account email}
SECRET_NAME=${SECRET_NAME:-atlas-log-agent}
DISPLAY_NAME=${DISPLAY_NAME:-mongodb-log-diagnostic}

# Run as the deployer: build the self-contained ADK source package.
./deploy/gcp/package_adk_agent.sh .build/adk-agent
# Secret name is not sensitive; it lets the runtime derive its Secret Manager resource path.
printf '\nATLAS_SECRET_NAME=%s\n' "$SECRET_NAME" >> .build/adk-agent/.env

gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com storage.googleapis.com --project "$PROJECT"
# The exact ADK CLI version determines the accepted spelling for service-account.
# If your installed `adk deploy agent_engine --help` uses a different flag, use its documented equivalent.
adk deploy agent_engine \
  --project="$PROJECT" \
  --region="$REGION" \
  --display_name="$DISPLAY_NAME" \
  --staging_bucket="$STAGING_BUCKET" \
  --requirements_file=.build/adk-agent/requirements.txt \
  --service_account="$RUNTIME_SA" \
  .build/adk-agent/google_adk_agent
