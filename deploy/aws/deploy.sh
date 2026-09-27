#!/usr/bin/env bash
# AWS deployment: ECR image -> Lambda (container) -> Step Functions -> EventBridge Scheduler (daily).
# Run from the project root after create-roles.sh. Re-running updates everything in place.
set -euo pipefail
REGION=${REGION:?}; SECRET_ARN=${SECRET_ARN:?}; LAMBDA_ROLE_ARN=${LAMBDA_ROLE_ARN:?}
SFN_ROLE_ARN=${SFN_ROLE_ARN:?}; SCHEDULER_ROLE_ARN=${SCHEDULER_ROLE_ARN:?}
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:?set BEDROCK_MODEL_ID (model or inference-profile id)}
BEDROCK_REGION=${BEDROCK_REGION:-$REGION}
TZ_NAME=${TZ_NAME:-Asia/Jerusalem}; SCHEDULE=${SCHEDULE:-cron(30 2 * * ? *)}
MEMORY=${MEMORY:-4096}; NAME=${NAME:-mongodb-log-diagnostic-agent}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
IMAGE="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${NAME}:latest"
ENV_VARS="Variables={CLOUD_PROVIDER=aws,ATLAS_SECRET_ID=${SECRET_ARN},BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID},BEDROCK_REGION=${BEDROCK_REGION}}"

# 1) Image (Lambda needs a Docker v2 single-arch manifest -> --provenance=false)
aws ecr describe-repositories --repository-names "$NAME" --region "$REGION" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$NAME" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
docker build --platform linux/amd64 --provenance=false -f deploy/aws/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

# 2) Lambda
if aws lambda get-function --function-name "$NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$NAME" --image-uri "$IMAGE" --region "$REGION" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$NAME" --region "$REGION" --role "$LAMBDA_ROLE_ARN" \
    --timeout 900 --memory-size "$MEMORY" --ephemeral-storage Size=10240 --environment "$ENV_VARS" >/dev/null
else
  aws lambda create-function --function-name "$NAME" --package-type Image --code "ImageUri=$IMAGE" \
    --role "$LAMBDA_ROLE_ARN" --timeout 900 --memory-size "$MEMORY" --ephemeral-storage Size=10240 \
    --region "$REGION" --environment "$ENV_VARS" >/dev/null
fi
aws lambda wait function-updated-v2 --function-name "$NAME" --region "$REGION"
FN_ARN=$(aws lambda get-function --function-name "$NAME" --region "$REGION" --query Configuration.FunctionArn --output text)

# 3) Step Functions
sed "s|\${AgentFunctionArn}|${FN_ARN}|g" deploy/aws/state-machine.asl.json > /tmp/asl.json
SM_ARN="arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:${NAME}"
if aws stepfunctions describe-state-machine --state-machine-arn "$SM_ARN" --region "$REGION" >/dev/null 2>&1; then
  aws stepfunctions update-state-machine --state-machine-arn "$SM_ARN" --definition file:///tmp/asl.json --role-arn "$SFN_ROLE_ARN" --region "$REGION" >/dev/null
else
  aws stepfunctions create-state-machine --name "$NAME" --definition file:///tmp/asl.json --role-arn "$SFN_ROLE_ARN" --region "$REGION" >/dev/null
fi

# 4) Daily schedule (timezone-aware)
TARGET="{\"Arn\":\"${SM_ARN}\",\"RoleArn\":\"${SCHEDULER_ROLE_ARN}\",\"Input\":\"{\\\"log_date\\\":null}\"}"
ACTION=create-schedule
aws scheduler get-schedule --name "${NAME}-daily" --region "$REGION" >/dev/null 2>&1 && ACTION=update-schedule
aws scheduler "$ACTION" --name "${NAME}-daily" --region "$REGION" --schedule-expression "$SCHEDULE" \
  --schedule-expression-timezone "$TZ_NAME" --flexible-time-window Mode=OFF --target "$TARGET" >/dev/null

echo "Deployed: $FN_ARN"
echo "State machine: $SM_ARN (schedule ${SCHEDULE} ${TZ_NAME})"
