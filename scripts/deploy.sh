#!/usr/bin/env bash
# deploy.sh — Build, push, and deploy OpenClaw on AgentCore Runtime.
#
# Handles the ECR chicken-and-egg problem: the CDK stack creates the ECR repo,
# but CfnRuntime needs an image in it. On first deploy, this script:
#   1. Creates the ECR repo via AWS CLI (idempotent)
#   2. Builds and pushes the ARM64 bridge image
#   3. Deploys all CDK stacks
#
# On subsequent deploys, it rebuilds/pushes the image and redeploys.
#
# Usage:
#   ./scripts/deploy.sh              # full deploy (build + push + cdk deploy)
#   ./scripts/deploy.sh --cdk-only   # skip docker build, just cdk deploy
#   ./scripts/deploy.sh --image-only # just build and push the image

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve account and region
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${CDK_DEFAULT_REGION:-us-west-2}"

if [ -z "$ACCOUNT" ]; then
  echo "ERROR: Could not determine AWS account. Set CDK_DEFAULT_ACCOUNT or configure AWS CLI."
  exit 1
fi

ECR_URI="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
REPO_NAME="openclaw-bridge"
IMAGE_URI="$ECR_URI/$REPO_NAME:latest"

echo "=== OpenClaw Deploy ==="
echo "  Account: $ACCOUNT"
echo "  Region:  $REGION"
echo "  Image:   $IMAGE_URI"
echo ""

MODE="${1:-full}"

build_and_push() {
  echo "--- Step 1: Ensure ECR repository exists ---"
  aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION" >/dev/null
  echo "  ECR repo: $REPO_NAME ✓"

  echo "--- Step 2: Build ARM64 image ---"
  docker build --platform linux/arm64 -t "$REPO_NAME" "$PROJECT_DIR/bridge/"

  echo "--- Step 3: Push to ECR ---"
  aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR_URI" 2>/dev/null
  docker tag "$REPO_NAME:latest" "$IMAGE_URI"
  docker push "$IMAGE_URI"
  echo "  Pushed: $IMAGE_URI ✓"
}

cdk_deploy() {
  echo "--- Step 4: CDK deploy ---"
  cd "$PROJECT_DIR"

  # Activate venv if present
  if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi

  export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
  export CDK_DEFAULT_REGION="$REGION"

  cdk deploy --all --require-approval never
  echo "  CDK deploy complete ✓"
}

case "$MODE" in
  --image-only)
    build_and_push
    ;;
  --cdk-only)
    cdk_deploy
    ;;
  *)
    build_and_push
    echo ""
    cdk_deploy
    ;;
esac

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Next steps:"
echo "  1. Store your Telegram bot token:"
echo "     aws secretsmanager update-secret --secret-id openclaw/channels/telegram \\"
echo "       --secret-string 'YOUR_BOT_TOKEN' --region $REGION"
echo ""
echo "  2. Get the Router Lambda Function URL:"
echo "     FUNCTION_URL=\$(aws cloudformation describe-stacks --stack-name OpenClawRouter \\"
echo "       --query \"Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue\" --output text --region $REGION)"
echo ""
echo "  3. Set up Telegram webhook:"
echo "     TELEGRAM_TOKEN=\$(aws secretsmanager get-secret-value --secret-id openclaw/channels/telegram \\"
echo "       --region $REGION --query SecretString --output text)"
echo "     curl \"https://api.telegram.org/bot\${TELEGRAM_TOKEN}/setWebhook?url=\${FUNCTION_URL}webhook/telegram\""
