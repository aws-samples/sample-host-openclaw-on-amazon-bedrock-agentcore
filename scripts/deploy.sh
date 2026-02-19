#!/bin/bash
set -euo pipefail

# OpenClaw on AgentCore — Full deployment script
# Usage: ./scripts/deploy.sh [--profile <aws-profile>] [--skip-images]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AWS_PROFILE_ARG=""
SKIP_IMAGES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            AWS_PROFILE_ARG="--profile $2"
            shift 2
            ;;
        --skip-images)
            SKIP_IMAGES=true
            shift
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

REGION=$(python3 -c "import json; print(json.load(open('${PROJECT_DIR}/cdk.json'))['context']['region'])")
ACCOUNT=$(python3 -c "import json; print(json.load(open('${PROJECT_DIR}/cdk.json'))['context']['account'])")
ECR_REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "=========================================="
echo " OpenClaw on AgentCore — Deployment"
echo " Account: ${ACCOUNT}  Region: ${REGION}"
echo "=========================================="

# 1. Install Python dependencies
echo ""
echo "[1/7] Installing CDK dependencies..."
cd "$PROJECT_DIR"
pip install -r requirements.txt -q

# 2. Synthesize (runs cdk-nag checks)
echo ""
echo "[2/7] Synthesizing CloudFormation templates (includes cdk-nag checks)..."
cdk synth $AWS_PROFILE_ARG 2>&1

# 3. Deploy foundation stacks first (creates ECR repos)
echo ""
echo "[3/7] Deploying foundation stacks (VPC, Security, AgentCore, Fargate)..."
cdk deploy OpenClawVpc OpenClawSecurity OpenClawAgentCore OpenClawFargate \
    --require-approval never $AWS_PROFILE_ARG 2>&1

# 4. Build and push Docker images
if [ "$SKIP_IMAGES" = false ]; then
    echo ""
    echo "[4/7] Building and pushing Docker images to ECR..."

    # Authenticate Docker to ECR
    aws ecr get-login-password --region "$REGION" $AWS_PROFILE_ARG \
        | docker login --username AWS --password-stdin "$ECR_REGISTRY"

    # Build and push bridge image
    echo "  Building bridge image..."
    docker build -t openclaw-bridge "${PROJECT_DIR}/bridge/"
    docker tag openclaw-bridge:latest "${ECR_REGISTRY}/openclaw-bridge:latest"
    echo "  Pushing bridge image..."
    docker push "${ECR_REGISTRY}/openclaw-bridge:latest"

    # Build and push agent image
    echo "  Building agent image..."
    docker build -t openclaw-agent "${PROJECT_DIR}/agent/"
    docker tag openclaw-agent:latest "${ECR_REGISTRY}/openclaw-agent:latest"
    echo "  Pushing agent image..."
    docker push "${ECR_REGISTRY}/openclaw-agent:latest"

    # Force new ECS deployment to pick up latest image
    echo "  Forcing new ECS deployment..."
    CLUSTER_NAME=$(aws cloudformation describe-stacks \
        --stack-name OpenClawFargate \
        --region "$REGION" \
        $AWS_PROFILE_ARG \
        --query 'Stacks[0].Outputs[?OutputKey==`ClusterName`].OutputValue' \
        --output text 2>/dev/null || echo "")
    SERVICE_NAME=$(aws cloudformation describe-stacks \
        --stack-name OpenClawFargate \
        --region "$REGION" \
        $AWS_PROFILE_ARG \
        --query 'Stacks[0].Outputs[?OutputKey==`ServiceName`].OutputValue' \
        --output text 2>/dev/null || echo "")

    if [ -n "$CLUSTER_NAME" ] && [ -n "$SERVICE_NAME" ]; then
        aws ecs update-service --cluster "$CLUSTER_NAME" --service "$SERVICE_NAME" \
            --force-new-deployment --region "$REGION" $AWS_PROFILE_ARG > /dev/null
        echo "  ECS deployment triggered."
    else
        echo "  WARNING: Could not retrieve ECS cluster/service names from stack outputs."
        echo "  You may need to force a new deployment manually:"
        echo "    aws ecs update-service --cluster <CLUSTER> --service <SERVICE> --force-new-deployment"
    fi
else
    echo ""
    echo "[4/7] Skipping Docker image build (--skip-images)"
fi

# 5. Deploy remaining stacks
echo ""
echo "[5/7] Deploying remaining stacks (Edge, Observability, Token Monitoring)..."
cdk deploy OpenClawEdge OpenClawObservability OpenClawTokenMonitoring \
    --require-approval never $AWS_PROFILE_ARG 2>&1

# 6. Retrieve outputs
echo ""
echo "[6/7] Retrieving deployment outputs..."

CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawEdge \
    --region "$REGION" \
    $AWS_PROFILE_ARG \
    --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontUrl`].OutputValue' \
    --output text 2>/dev/null || echo "N/A")

GATEWAY_TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id "openclaw/gateway-token" \
    --region "$REGION" \
    $AWS_PROFILE_ARG \
    --query 'SecretString' \
    --output text 2>/dev/null || echo "N/A")

ALARM_TOPIC=$(aws cloudformation describe-stacks \
    --stack-name OpenClawObservability \
    --region "$REGION" \
    $AWS_PROFILE_ARG \
    --query 'Stacks[0].Outputs[?OutputKey==`AlarmTopicArn`].OutputValue' \
    --output text 2>/dev/null || echo "N/A")

# 7. Print summary
echo ""
echo "[7/7] Deployment complete!"
echo ""
echo "=========================================="
echo " Deployment Summary"
echo "=========================================="
echo ""
echo "  Web UI URL:      ${CLOUDFRONT_URL}?token=${GATEWAY_TOKEN}"
echo "  Gateway Token:   ${GATEWAY_TOKEN}"
echo "  Alarm Topic:     ${ALARM_TOPIC}"
echo ""
echo "  Dashboards:"
echo "    Operations:    https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=OpenClaw-Operations"
echo "    Token Analytics: https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=OpenClaw-Token-Analytics"
echo ""
echo "  Next steps:"
echo "    1. Subscribe an email to the alarm topic for notifications:"
echo "       aws sns subscribe --topic-arn ${ALARM_TOPIC} --protocol email --notification-endpoint your@email.com"
echo "    2. Open the Web UI URL above in your browser"
echo "    3. To connect messaging channels, add bot tokens via Secrets Manager:"
echo "       aws secretsmanager update-secret --secret-id openclaw/channels/telegram --secret-string 'BOT_TOKEN'"
echo "       Then force a new deployment to pick up the token."
echo ""
echo "  NOTE: OpenClaw takes ~4 minutes from container start to gateway listening."
echo ""
echo "=========================================="
