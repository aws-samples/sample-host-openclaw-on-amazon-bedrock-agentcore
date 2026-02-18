#!/bin/bash
set -euo pipefail

# OpenClaw on AgentCore — Full deployment script
# Usage: ./scripts/deploy.sh [--profile <aws-profile>]

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

echo "=========================================="
echo " OpenClaw on AgentCore — Deployment"
echo "=========================================="

# 1. Install Python dependencies
echo ""
echo "[1/5] Installing CDK dependencies..."
cd "$PROJECT_DIR"
pip install -r requirements.txt -q

# 2. Synthesize (runs cdk-nag checks)
echo ""
echo "[2/5] Synthesizing CloudFormation templates (includes cdk-nag checks)..."
cdk synth $AWS_PROFILE_ARG 2>&1

# 3. Deploy all stacks
echo ""
echo "[3/5] Deploying all stacks..."
cdk deploy --all --require-approval never $AWS_PROFILE_ARG 2>&1

# 4. Retrieve outputs
echo ""
echo "[4/5] Retrieving deployment outputs..."

REGION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['region'])")

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

# 5. Print summary
echo ""
echo "[5/5] Deployment complete!"
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
echo "    3. To connect messaging channels, add bot tokens via the OpenClaw Web UI"
echo "       (tokens are stored in Secrets Manager under openclaw/channels/*)"
echo ""
echo "=========================================="
