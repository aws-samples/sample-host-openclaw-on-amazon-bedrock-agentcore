#!/bin/bash
set -euo pipefail

# OpenClaw — Rotate gateway token
# Generates a new 64-character gateway token, updates Secrets Manager,
# and restarts the Fargate task so it picks up the new token.
#
# Usage: ./scripts/rotate-token.sh [--profile <aws-profile>]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AWS_PROFILE_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            AWS_PROFILE_ARG="--profile $2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

REGION=$(python3 -c "import json; print(json.load(open('${PROJECT_DIR}/cdk.json'))['context']['region'])")
SECRET_ID="openclaw/gateway-token"

echo "=========================================="
echo " OpenClaw — Gateway Token Rotation"
echo "=========================================="

# 1. Generate a new 64-character alphanumeric token
echo ""
echo "[1/4] Generating new gateway token..."
NEW_TOKEN=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(64)))")

# 2. Update the secret in Secrets Manager
echo "[2/4] Updating Secrets Manager..."
aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ID" \
    --secret-string "$NEW_TOKEN" \
    --region "$REGION" \
    $AWS_PROFILE_ARG

echo "  Secret updated: ${SECRET_ID}"

# 3. Restart the Fargate task (force new deployment)
echo "[3/4] Restarting Fargate service..."
CLUSTER_ARN=$(aws ecs list-clusters \
    --region "$REGION" \
    $AWS_PROFILE_ARG \
    --query 'clusterArns[?contains(@, `OpenClawFargate`)] | [0]' \
    --output text 2>/dev/null || echo "")

if [ -n "$CLUSTER_ARN" ] && [ "$CLUSTER_ARN" != "None" ]; then
    SERVICE_ARN=$(aws ecs list-services \
        --cluster "$CLUSTER_ARN" \
        --region "$REGION" \
        $AWS_PROFILE_ARG \
        --query 'serviceArns[0]' \
        --output text 2>/dev/null || echo "")

    if [ -n "$SERVICE_ARN" ] && [ "$SERVICE_ARN" != "None" ]; then
        aws ecs update-service \
            --cluster "$CLUSTER_ARN" \
            --service "$SERVICE_ARN" \
            --force-new-deployment \
            --region "$REGION" \
            $AWS_PROFILE_ARG \
            --query 'service.serviceName' \
            --output text
        echo "  Fargate service restarting with new deployment"
    else
        echo "  WARNING: Could not find Fargate service"
    fi
else
    echo "  WARNING: Could not find ECS cluster"
fi

# 4. Retrieve CloudFront URL and print new access URL
echo "[4/4] Retrieving CloudFront URL..."
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawEdge \
    --region "$REGION" \
    $AWS_PROFILE_ARG \
    --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontUrl`].OutputValue' \
    --output text 2>/dev/null || echo "N/A")

echo ""
echo "=========================================="
echo " Token Rotation Complete"
echo "=========================================="
echo ""
echo "  New Web UI URL: ${CLOUDFRONT_URL}?token=${NEW_TOKEN}"
echo ""
echo "  The Fargate task is restarting and will pick up the new"
echo "  token automatically. This may cause a brief service interruption."
echo ""
echo "  Old tokens are immediately invalidated."
echo ""
echo "=========================================="
