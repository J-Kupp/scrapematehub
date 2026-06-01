#!/usr/bin/env bash
set -euo pipefail

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required." >&2
  exit 1
fi

AWS_REGION=${AWS_REGION:-eu-central-1}
AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID:-}
ECR_REPOSITORY=${ECR_REPOSITORY:-yourbarmate-scraper-worker}
ECS_CLUSTER=${ECS_CLUSTER:-yourbarmate-scraper-cluster}
TASK_FAMILY=${TASK_FAMILY:-yourbarmate-scraper-worker}
CONTAINER_NAME=${CONTAINER_NAME:-scraper-worker}
LOG_GROUP=${LOG_GROUP:-/ecs/yourbarmate-scraper-worker}
EXECUTION_ROLE_ARN=${EXECUTION_ROLE_ARN:-}
TASK_ROLE_ARN=${TASK_ROLE_ARN:-}
SECRETS_MANAGER_ID=${SECRETS_MANAGER_ID:-yourbarmate-suppliers-prod}

if [[ -z "$AWS_ACCOUNT_ID" || -z "$EXECUTION_ROLE_ARN" || -z "$TASK_ROLE_ARN" ]]; then
  echo "Set AWS_ACCOUNT_ID, EXECUTION_ROLE_ARN, and TASK_ROLE_ARN before running." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$ROOT_DIR/deploy/aws/ecs-task-definition.worker.json"
RENDERED="$(mktemp)"

aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" --region "$AWS_REGION" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$ECR_REPOSITORY" --region "$AWS_REGION" >/dev/null

aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" >/dev/null 2>&1 || \
  aws ecs create-cluster --cluster-name "$ECS_CLUSTER" --region "$AWS_REGION" >/dev/null

aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$AWS_REGION" | grep -q "$LOG_GROUP" || \
  aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION"

sed \
  -e "s|<ACCOUNT_ID>|$AWS_ACCOUNT_ID|g" \
  -e "s|<REGION>|$AWS_REGION|g" \
  -e "s|yourbarmate-scraper-worker|$TASK_FAMILY|g" \
  -e "s|scraper-worker|$CONTAINER_NAME|g" \
  -e "s|arn:aws:iam::<ACCOUNT_ID>:role/yourbarmate-ecs-task-execution|$EXECUTION_ROLE_ARN|g" \
  -e "s|arn:aws:iam::<ACCOUNT_ID>:role/yourbarmate-ecs-task-runtime|$TASK_ROLE_ARN|g" \
  -e "s|yourbarmate-scrapers-prod|$SECRETS_MANAGER_ID|g" \
  "$TEMPLATE" > "$RENDERED"

aws ecs register-task-definition \
  --cli-input-json "file://$RENDERED" \
  --region "$AWS_REGION"

echo "ECS worker prerequisites ensured."
echo "Cluster: $ECS_CLUSTER"
echo "Repository: $ECR_REPOSITORY"
echo "Task family: $TASK_FAMILY"
