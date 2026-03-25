#!/usr/bin/env bash
# deploy.sh — Hybrid deployment: CDK + AgentCore Starter Toolkit.
#
# Three-phase deployment:
#   Phase 1: CDK deploys foundation (VPC, Security, AgentCore base, Observability)
#   Phase 2: Starter Toolkit deploys Runtime (ECR, Docker build, Runtime, Endpoint)
#   Phase 3: CDK deploys dependent stacks (Router, Cron, TokenMonitoring)
#
# Usage:
#   ./scripts/deploy.sh                  # full 3-phase deploy
#   ./scripts/deploy.sh --cdk-only       # CDK stacks only (skip toolkit)
#   ./scripts/deploy.sh --runtime-only   # toolkit deploy only (Phase 2)
#   ./scripts/deploy.sh --phase1         # Phase 1 only
#   ./scripts/deploy.sh --phase3         # Phase 3 only (assumes runtime already deployed)
#
# Environment variables:
#   BUILD_MODE          local-build (default) or codebuild
#                       local-build: builds ARM64 container locally with Docker (recommended)
#                       codebuild: builds in AWS CodeBuild (no Docker required, adds cost)
#   CDK_DEFAULT_ACCOUNT AWS account ID (auto-detected if not set)
#   CDK_DEFAULT_REGION  AWS region (falls back to cdk.json, then aws configure)
#   AGENTCORE_CLI       Path to agentcore CLI (auto-detected)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Build mode ---
BUILD_MODE="${BUILD_MODE:-local-build}"

# --- Pre-flight checks ---
preflight() {
  local errors=0

  # AWS credentials
  if ! aws sts get-caller-identity &>/dev/null; then
    echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
    errors=$((errors + 1))
  fi

  # CDK CLI
  if ! command -v cdk &>/dev/null; then
    echo "ERROR: AWS CDK CLI not found. Install with: npm install -g aws-cdk"
    errors=$((errors + 1))
  fi

  # Python venv
  if [ ! -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    echo "ERROR: Python venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    errors=$((errors + 1))
  fi

  # Docker (only for local-build)
  if [ "$BUILD_MODE" = "local-build" ]; then
    if ! command -v docker &>/dev/null; then
      echo "ERROR: Docker not found (required for BUILD_MODE=local-build). Install Docker or set BUILD_MODE=codebuild."
      errors=$((errors + 1))
    elif ! docker info &>/dev/null 2>&1; then
      echo "ERROR: Docker daemon not running. Start Docker or set BUILD_MODE=codebuild."
      errors=$((errors + 1))
    fi
  fi

  # Agentcore CLI
  if ! command -v "${AGENTCORE_CLI:-agentcore}" &>/dev/null && [ ! -x "$HOME/.local/bin/agentcore" ]; then
    echo "ERROR: agentcore CLI not found. Install with: pip install bedrock-agentcore-cli"
    errors=$((errors + 1))
  fi

  if [ "$errors" -gt 0 ]; then
    echo ""
    echo "Fix the above errors and re-run."
    exit 1
  fi
}

# Resolve account and region
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${CDK_DEFAULT_REGION:-}"
if [ -z "$REGION" ]; then
  REGION=$(python3 -c "import json; r=json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('region',''); print(r)" 2>/dev/null || echo "")
fi
if [ -z "$REGION" ]; then
  REGION=$(aws configure get region 2>/dev/null || echo "")
fi
if [ -z "$REGION" ]; then
  echo "ERROR: Could not determine AWS region. Set CDK_DEFAULT_REGION, configure region in cdk.json, or run 'aws configure'."
  exit 1
fi

if [ -z "$ACCOUNT" ]; then
  echo "ERROR: Could not determine AWS account. Set CDK_DEFAULT_ACCOUNT or configure AWS CLI."
  exit 1
fi

export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$REGION"

# Agentcore CLI path
AGENTCORE_CLI="${AGENTCORE_CLI:-agentcore}"
if ! command -v "$AGENTCORE_CLI" &>/dev/null; then
  AGENTCORE_CLI="$HOME/.local/bin/agentcore"
fi

# Run pre-flight checks
preflight

echo "=== OpenClaw Hybrid Deploy ==="
echo "  Account:    $ACCOUNT"
echo "  Region:     $REGION"
echo "  Build mode: $BUILD_MODE"
echo ""

MODE="${1:-full}"

activate_venv() {
  if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.venv/bin/activate"
  fi
}

# --- Phase 1: CDK foundation stacks ---
phase1_cdk() {
  echo "=== Phase 1: CDK foundation stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  cdk deploy \
    OpenClawVpc \
    OpenClawSecurity \
    OpenClawGuardrails \
    OpenClawAgentCore \
    OpenClawObservability \
    --require-approval never

  echo "  Phase 1 complete."
  echo ""
}

# --- Read CDK outputs for toolkit config ---
read_cdk_outputs() {
  echo "--- Reading CDK outputs ---"

  EXECUTION_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" \
    --output text)

  SECURITY_GROUP_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='SecurityGroupId'].OutputValue" \
    --output text)

  PRIVATE_SUBNET_IDS=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetIds'].OutputValue" \
    --output text)

  USER_FILES_BUCKET=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='UserFilesBucketName'].OutputValue" \
    --output text)

  GATEWAY_TOKEN_SECRET=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'GatewayTokenSecret')].OutputValue" \
    --output text)
  # Extract secret name from ARN (last segment after last colon, strip random suffix)
  GATEWAY_TOKEN_SECRET_ID="openclaw/gateway-token"

  COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'IdentityPoolEC8A1A0D')].OutputValue" \
    --output text)

  COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'IdentityPoolProxyClient')].OutputValue" \
    --output text)

  COGNITO_PASSWORD_SECRET_ID="openclaw/cognito-password-secret"
  TELEGRAM_CHANNEL_SECRET_ID="openclaw/channels/telegram"

  CMK_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'SecretsCmk')].OutputValue" \
    --output text)

  # Read config values from cdk.json
  DEFAULT_MODEL_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('default_model_id','global.anthropic.claude-opus-4-6-v1'))")
  SUBAGENT_MODEL_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('subagent_model_id',''))")
  IMAGE_VERSION=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('image_version','1'))")
  WORKSPACE_SYNC_MS=$(python3 -c "import json; print(int(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('workspace_sync_interval_seconds',300))*1000)")
  CRON_LEAD_TIME=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('cron_lead_time_minutes',5))")
  SESSION_IDLE=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_idle_timeout',1800))")
  SESSION_MAX=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_max_lifetime',28800))")

  echo "  Execution Role: $EXECUTION_ROLE_ARN"
  echo "  Security Group: $SECURITY_GROUP_ID"
  echo "  Subnets:        $PRIVATE_SUBNET_IDS"
  echo "  S3 Bucket:      $USER_FILES_BUCKET"
}

# --- Check ARM64 build capability (for local-build mode) ---
check_arm64_build() {
  local arch
  arch=$(uname -m)
  if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
    return 0  # native ARM64, no QEMU needed
  fi
  # x86 host — check for ARM64 emulation via buildx/QEMU
  if docker buildx ls 2>/dev/null | grep -q "linux/arm64"; then
    return 0
  fi
  echo "WARNING: ARM64 emulation not available. Attempting to register QEMU..."
  docker run --rm --privileged tonistiigi/binfmt --install arm64 || {
    echo "ERROR: Could not set up ARM64 emulation. Install QEMU or use BUILD_MODE=codebuild."
    exit 1
  }
}

# --- Phase 2: Starter Toolkit deploy ---
phase2_toolkit() {
  echo "=== Phase 2: Starter Toolkit deploy ==="
  cd "$PROJECT_DIR"

  read_cdk_outputs

  # Configure the agent (creates/updates .bedrock_agentcore.yaml)
  echo "--- Configuring agent ---"
  "$AGENTCORE_CLI" configure \
    --name openclaw_agent \
    --entrypoint bridge/agentcore-contract.js \
    --execution-role "$EXECUTION_ROLE_ARN" \
    --region "$REGION" \
    --vpc \
    --subnets "$PRIVATE_SUBNET_IDS" \
    --security-groups "$SECURITY_GROUP_ID" \
    --idle-timeout "$SESSION_IDLE" \
    --max-lifetime "$SESSION_MAX" \
    --deployment-type container \
    --language typescript \
    --non-interactive

  # Fix: agentcore configure expands source_path to project root, but our
  # Dockerfile COPY commands expect paths relative to bridge/. Patch it back.
  local yaml_file="$PROJECT_DIR/.bedrock_agentcore.yaml"
  if grep -q "source_path:.*$PROJECT_DIR$" "$yaml_file" 2>/dev/null; then
    local tmp_file="${yaml_file}.tmp"
    sed "s|source_path: $PROJECT_DIR$|source_path: $PROJECT_DIR/bridge|" "$yaml_file" > "$tmp_file" && mv "$tmp_file" "$yaml_file"
    echo "  (patched source_path -> bridge/)"
  fi

  # Ensure the generated Dockerfile matches our actual Dockerfile
  local gen_dockerfile="$PROJECT_DIR/.bedrock_agentcore/openclaw_agent/Dockerfile"
  if [ -f "$gen_dockerfile" ] && [ -f "$PROJECT_DIR/bridge/Dockerfile" ]; then
    cp "$PROJECT_DIR/bridge/Dockerfile" "$gen_dockerfile"
    echo "  (synced Dockerfile from bridge/)"
  fi

  # Build deploy command based on BUILD_MODE
  echo "--- Deploying runtime (mode: $BUILD_MODE) ---"
  local deploy_flags=()
  if [ "$BUILD_MODE" = "local-build" ]; then
    check_arm64_build
    deploy_flags+=("--local-build")
  fi
  # codebuild mode: no extra flags (default behavior)

  "$AGENTCORE_CLI" deploy \
    --agent openclaw_agent \
    --auto-update-on-conflict \
    "${deploy_flags[@]}" \
    --env "AWS_REGION=$REGION" \
    --env "BEDROCK_MODEL_ID=$DEFAULT_MODEL_ID" \
    --env "GATEWAY_TOKEN_SECRET_ID=$GATEWAY_TOKEN_SECRET_ID" \
    --env "COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID" \
    --env "COGNITO_CLIENT_ID=$COGNITO_CLIENT_ID" \
    --env "COGNITO_PASSWORD_SECRET_ID=$COGNITO_PASSWORD_SECRET_ID" \
    --env "S3_USER_FILES_BUCKET=$USER_FILES_BUCKET" \
    --env "WORKSPACE_SYNC_INTERVAL_MS=$WORKSPACE_SYNC_MS" \
    --env "IMAGE_VERSION=$IMAGE_VERSION" \
    --env "EXECUTION_ROLE_ARN=$EXECUTION_ROLE_ARN" \
    --env "CMK_ARN=$CMK_ARN" \
    --env "EVENTBRIDGE_SCHEDULE_GROUP=openclaw-cron" \
    --env "CRON_LAMBDA_ARN=arn:aws:lambda:${REGION}:${ACCOUNT}:function:openclaw-cron-executor" \
    --env "EVENTBRIDGE_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/openclaw-cron-scheduler-role-${REGION}" \
    --env "IDENTITY_TABLE_NAME=openclaw-identity" \
    --env "CRON_LEAD_TIME_MINUTES=$CRON_LEAD_TIME" \
    --env "SUBAGENT_BEDROCK_MODEL_ID=$SUBAGENT_MODEL_ID" \
    --env "TELEGRAM_CHANNEL_SECRET_ID=$TELEGRAM_CHANNEL_SECRET_ID"

  # Read runtime ID and endpoint ID from toolkit
  echo "--- Reading runtime info ---"
  TOOLKIT_STATUS=$("$AGENTCORE_CLI" status --agent openclaw_agent --verbose 2>&1 || true)

  # Extract runtime_id from status output (handles non-JSON prefix lines from warnings)
  RUNTIME_ID=$(echo "$TOOLKIT_STATUS" | python3 -c "
import sys, re, json
text = sys.stdin.read()
# Try to find JSON object in the output
m = re.search(r'\{.*\}', text, re.DOTALL)
if m:
    try:
        data = json.loads(m.group())
        # Navigate nested structure: {config: {agent_id: ...}} or flat {agent_id: ...}
        cfg = data.get('config', data)
        rid = cfg.get('agent_id', cfg.get('runtime_id', ''))
        if rid:
            print(rid)
            sys.exit(0)
    except json.JSONDecodeError:
        pass
# Regex fallback
m = re.search(r'\"agent_id\"\s*:\s*\"([a-zA-Z0-9_-]+)\"', text)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

  # Fallback: read from .bedrock_agentcore.yaml (uses simple text parsing, no yaml dep)
  if [ -z "$RUNTIME_ID" ]; then
    RUNTIME_ID=$(python3 -c "
import re
with open('$PROJECT_DIR/.bedrock_agentcore.yaml') as f:
    text = f.read()
m = re.search(r'agent_id:\s*(\S+)', text)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")
  fi

  if [ -z "$RUNTIME_ID" ]; then
    echo "WARNING: Could not extract runtime_id from toolkit. You may need to set it manually in cdk.json."
  else
    echo "  Runtime ID: $RUNTIME_ID"
  fi

  # Get endpoint ID
  ENDPOINT_ID=""
  if [ -n "$RUNTIME_ID" ]; then
    ENDPOINT_ID=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
      --agent-runtime-id "$RUNTIME_ID" \
      --region "$REGION" \
      --query "runtimeEndpoints[?name=='DEFAULT'].id | [0]" \
      --output text 2>/dev/null || echo "")
    echo "  Endpoint ID: $ENDPOINT_ID"
  fi

  # Update cdk.json with runtime info
  if [ -n "$RUNTIME_ID" ] && [ -n "$ENDPOINT_ID" ]; then
    echo "--- Updating cdk.json with runtime info ---"
    python3 -c "
import json
with open('$PROJECT_DIR/cdk.json') as f:
    cfg = json.load(f)
cfg['context']['runtime_id'] = '$RUNTIME_ID'
cfg['context']['runtime_endpoint_id'] = '$ENDPOINT_ID'
with open('$PROJECT_DIR/cdk.json', 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
"
    echo "  cdk.json updated."
  fi

  echo "  Phase 2 complete."
  echo ""
}

# --- Phase 3: CDK dependent stacks ---
phase3_cdk() {
  echo "=== Phase 3: CDK dependent stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  # Verify runtime_id is set
  RUNTIME_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('runtime_id',''))")
  if [ -z "$RUNTIME_ID" ] || [ "$RUNTIME_ID" = "PLACEHOLDER" ]; then
    echo "ERROR: runtime_id not set in cdk.json. Run Phase 2 first."
    exit 1
  fi

  cdk deploy \
    OpenClawRouter \
    OpenClawCron \
    OpenClawTokenMonitoring \
    --require-approval never

  echo "  Phase 3 complete."
  echo ""
}

case "$MODE" in
  --phase1)
    phase1_cdk
    ;;
  --runtime-only)
    phase2_toolkit
    ;;
  --phase3)
    phase3_cdk
    ;;
  --cdk-only)
    phase1_cdk
    phase3_cdk
    ;;
  *)
    phase1_cdk
    phase2_toolkit
    phase3_cdk
    ;;
esac

echo "=== Deploy complete ==="
echo ""
echo "Next steps:"
echo "  1. Store your Telegram bot token:"
echo "     aws secretsmanager update-secret --secret-id openclaw/channels/telegram \\"
echo "       --secret-string 'YOUR_BOT_TOKEN' --region $REGION"
echo ""
echo "  2. Set up webhook:"
echo "     ./scripts/setup-telegram.sh"
