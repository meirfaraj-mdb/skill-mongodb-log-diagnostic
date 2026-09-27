#!/usr/bin/env bash
# Creates the 3 IAM roles the agent needs, reusing bucket/prefix from your EXISTING lambda secret.
# Usage: REGION=eu-west-1 SECRET_ARN=arn:aws:secretsmanager:... ./deploy/aws/create-roles.sh
set -euo pipefail
REGION=${REGION:?set REGION (same region as the secret)}
SECRET_ARN=${SECRET_ARN:?set SECRET_ARN (the ATLAS_SECRET_ID of the old lambda)}
NAME=${NAME:-mongodb-log-diagnostic-agent}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# Read ONLY bucket/prefix from the secret (credentials are never printed)
read -r BUCKET PREFIX < <(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --region "$REGION" \
  --query SecretString --output text | python3 -c 'import json,sys
c=json.load(sys.stdin); print(c.get("bucket") or c["s3_bucket"], (c.get("prefix") or c.get("s3_prefix") or "mongodb-atlas-logs").strip("/"))')
KMS_KEY=$(aws secretsmanager describe-secret --secret-id "$SECRET_ARN" --region "$REGION" --query KmsKeyId --output text)
echo "Bucket: $BUCKET  Prefix: $PREFIX  KMS: ${KMS_KEY}"

trust() { printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"%s"},"Action":"sts:AssumeRole"}]}' "$1"; }
mkrole() { aws iam get-role --role-name "$1" >/dev/null 2>&1 || aws iam create-role --role-name "$1" --assume-role-policy-document "$(trust "$2")" >/dev/null; }

# 1) Lambda execution role
mkrole "${NAME}-lambda" lambda.amazonaws.com
sed -e "s|arn:aws:secretsmanager:<region>:<account>:secret:<name>\*|${SECRET_ARN}|" \
    -e "s|<bucket>|${BUCKET}|g" -e "s|<prefix>|${PREFIX}|g" deploy/aws/lambda-execution-policy.json > /tmp/agent-policy.json
if [ "$KMS_KEY" != "None" ] && [ -n "$KMS_KEY" ]; then   # customer-managed KMS key on the secret
  python3 - "$KMS_KEY" "$REGION" "$ACCOUNT" <<'PY'
import json,sys; key,region,acct=sys.argv[1:]
arn = key if key.startswith("arn:") else f"arn:aws:kms:{region}:{acct}:key/{key}"
p=json.load(open("/tmp/agent-policy.json")); p["Statement"].append({"Sid":"DecryptSecret","Effect":"Allow","Action":"kms:Decrypt","Resource":arn})
json.dump(p,open("/tmp/agent-policy.json","w"),indent=1)
PY
fi
aws iam put-role-policy --role-name "${NAME}-lambda" --policy-name agent --policy-document file:///tmp/agent-policy.json

# 2) Step Functions role (invoke the Lambda)
mkrole "${NAME}-sfn" states.amazonaws.com
aws iam put-role-policy --role-name "${NAME}-sfn" --policy-name invoke --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":[\"arn:aws:lambda:${REGION}:${ACCOUNT}:function:${NAME}\",\"arn:aws:lambda:${REGION}:${ACCOUNT}:function:${NAME}:*\"]}]}"

# 3) EventBridge Scheduler role (start the state machine)
mkrole "${NAME}-scheduler" scheduler.amazonaws.com
aws iam put-role-policy --role-name "${NAME}-scheduler" --policy-name start --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"states:StartExecution\",\"Resource\":\"arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:${NAME}\"}]}"

echo "Waiting 15s for IAM propagation..."; sleep 15
cat <<OUT
export REGION=${REGION}
export SECRET_ARN=${SECRET_ARN}
export LAMBDA_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/${NAME}-lambda
export SFN_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/${NAME}-sfn
export SCHEDULER_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/${NAME}-scheduler
OUT
