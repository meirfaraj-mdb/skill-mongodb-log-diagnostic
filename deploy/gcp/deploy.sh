#!/usr/bin/env bash
# Google Cloud deployment: Artifact Registry image -> Cloud Run Job -> Workflows -> Cloud Scheduler (daily).
# Review and set the variables before running. Run from the project root.
set -euo pipefail
PROJECT=${PROJECT:?set PROJECT}
REGION=${REGION:-europe-west1}
REPO=${REPO:-mongodb-agents}
BUCKET=${BUCKET:?set BUCKET (GCS bucket name, no gs://)}
SECRET=${SECRET:-atlas-log-agent}
SA_NAME=${SA_NAME:-mongodb-log-agent}
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/mongodb-log-diagnostic-agent:latest"
TZ_NAME=${TZ_NAME:-Asia/Jerusalem}

gcloud services enable run.googleapis.com workflows.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com aiplatform.googleapis.com artifactregistry.googleapis.com --project "$PROJECT"

# Service account + least-privilege roles
gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" || true
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$SA" --role roles/storage.objectUser
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/aiplatform.user
# The same SA orchestrates: run jobs with overrides + invoke workflows
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/run.developer
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/workflows.invoker
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" --member "serviceAccount:$SA" --role roles/iam.serviceAccountUser

# Image
gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION" --project "$PROJECT" || true
gcloud builds submit --project "$PROJECT" --config deploy/gcp/cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" .
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "deploy/gcp/Dockerfile", "-t", "$IMAGE", "."]
images: ["$IMAGE"]
CFG

# Cloud Run Job (memory must cover the gz log + /tmp, which is in-memory on Cloud Run)
gcloud run jobs deploy mongodb-log-diagnostic-agent --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --service-account "$SA" --cpu 2 --memory 8Gi --task-timeout 6h --max-retries 1 \
  --set-env-vars "CLOUD_PROVIDER=gcp,GOOGLE_CLOUD_PROJECT=${PROJECT},ATLAS_SECRET_ID=projects/${PROJECT}/secrets/${SECRET}/versions/latest" \
  --args="--stage,all"

# Orchestration + schedule (daily 02:30 in the secret's timezone)
gcloud workflows deploy mongodb-log-diagnostic-agent --project "$PROJECT" --location "$REGION" \
  --source deploy/gcp/workflow.yaml --service-account "$SA"
gcloud scheduler jobs create http mongodb-log-diagnostic-daily --project "$PROJECT" --location "$REGION" \
  --schedule "30 2 * * *" --time-zone "$TZ_NAME" --http-method POST \
  --uri "https://workflowexecutions.googleapis.com/v1/projects/${PROJECT}/locations/${REGION}/workflows/mongodb-log-diagnostic-agent/executions" \
  --message-body "{\"argument\": \"{\\\"node_tasks\\\": 3, \\\"timezone\\\": \\\"${TZ_NAME}\\\"}\"}" --oauth-service-account-email "$SA"
echo "Deployed. Manual run: gcloud workflows run mongodb-log-diagnostic-agent --location $REGION --data '{}'"
