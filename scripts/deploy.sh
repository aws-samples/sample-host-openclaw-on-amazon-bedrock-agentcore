#!/usr/bin/env bash
# deploy.sh — Hybrid deployment: CDK + AgentCore Starter Toolkit.
#
# Three-phase deployment:
#   Phase 1: CDK deploys foundation (VPC, Security, AgentCore base, Observability)
#   Phase 2: Starter Toolkit deploys Runtime (ECR, Docker build, Runtime, Endpoint)
#   Phase 3: CDK deploys Router, the legacy Cron tombstone, and TokenMonitoring
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
#   CDK_DEFAULT_REGION  AWS region; must be exactly eu-west-1
#   AGENTCORE_CLI       Path to agentcore CLI (auto-detected)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Build mode ---
BUILD_MODE="${BUILD_MODE:-local-build}"
REQUIRED_REGION="eu-west-1"

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

# Resolve and validate every explicit region before the first AWS CLI call.
for region_variable in CDK_DEFAULT_REGION AWS_REGION AWS_DEFAULT_REGION; do
  configured_region="${!region_variable:-}"
  if [ -n "$configured_region" ] && [ "$configured_region" != "$REQUIRED_REGION" ]; then
    echo "ERROR: $region_variable must be exactly $REQUIRED_REGION; got $configured_region." >&2
    exit 1
  fi
done
REGION="${CDK_DEFAULT_REGION:-}"
if [ -z "$REGION" ]; then
  REGION=$(python3 -c "import json; r=json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('region',''); print(r)" 2>/dev/null || echo "")
fi
if [ -z "$REGION" ]; then
  REGION="$REQUIRED_REGION"
fi
if [ "$REGION" != "$REQUIRED_REGION" ]; then
  echo "ERROR: AWS region must be exactly $REQUIRED_REGION; got $REGION." >&2
  exit 1
fi

ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"

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

  WORKSPACE_SESSION_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAgentCore --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='WorkspaceSessionRoleArn'].OutputValue" \
    --output text)
  if [[ ! "$WORKSPACE_SESSION_ROLE_ARN" =~ ^arn:aws:iam::${ACCOUNT}:role/openclaw-workspace-session-role-eu-west-1$ ]]; then
    echo "ERROR: Invalid or missing WorkspaceSessionRoleArn output: $WORKSPACE_SESSION_ROLE_ARN" >&2
    exit 1
  fi

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

  CMK_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?contains(OutputKey,'SecretsCmk')].OutputValue" \
    --output text)

  # Read config values from cdk.json
  DEFAULT_MODEL_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('default_model_id','global.anthropic.claude-opus-4-6-v1'))")
  IMAGE_VERSION=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('image_version','1'))")
  WORKSPACE_SYNC_MS=$(python3 -c "import json; print(int(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('workspace_sync_interval_seconds',300))*1000)")
  SESSION_IDLE=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_idle_timeout',1800))")
  SESSION_MAX=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_max_lifetime',28800))")

  echo "  Execution Role: $EXECUTION_ROLE_ARN"
  echo "  Workspace Role: $WORKSPACE_SESSION_ROLE_ARN"
  echo "  Security Group: $SECURITY_GROUP_ID"
  echo "  Subnets:        $PRIVATE_SUBNET_IDS"
  echo "  S3 Bucket:      $USER_FILES_BUCKET"
}

validate_runtime_metadata() {
  local runtime_metadata_json="$1"
  local candidate_runtime_id="$2"
  RUNTIME_METADATA_JSON="$runtime_metadata_json" python3 - \
    "$ACCOUNT" "$REGION" "$EXECUTION_ROLE_ARN" "$candidate_runtime_id" <<'PY'
import json
import os
import re
import sys

account, region, expected_role_arn, candidate_runtime_id = sys.argv[1:]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


try:
    runtime = json.loads(os.environ["RUNTIME_METADATA_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    fail(f"GetAgentRuntime returned invalid JSON: {error}")

runtime_id_pattern = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
runtime_arn_pattern = re.compile(
    rf"arn:aws:bedrock-agentcore:{re.escape(region)}:{re.escape(account)}:agent/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
)

actual_runtime_id = runtime.get("agentRuntimeId", "")
actual_runtime_arn = runtime.get("agentRuntimeArn", "")
if runtime_id_pattern.fullmatch(candidate_runtime_id) is None:
    fail(f"toolkit returned a noncanonical runtime ID: {candidate_runtime_id}")
if actual_runtime_id != candidate_runtime_id:
    fail(
        "GetAgentRuntime ID does not match the toolkit runtime ID: "
        f"{actual_runtime_id} != {candidate_runtime_id}"
    )
if runtime_arn_pattern.fullmatch(actual_runtime_arn) is None:
    fail("GetAgentRuntime returned a noncanonical runtime ARN")
if runtime.get("status") != "READY":
    fail(f"AgentCore runtime is not READY: {runtime.get('status', '<missing>')}")
if runtime.get("roleArn") != expected_role_arn:
    fail("AgentCore runtime execution role does not match the deployed role")

print(actual_runtime_id)
print(actual_runtime_arn)
PY
}

wait_for_runtime_ready() {
  local runtime_id="$1"
  local runtime_metadata=""
  local runtime_status=""
  local attempt

  for ((attempt = 1; attempt <= 60; attempt++)); do
    runtime_metadata=$(aws bedrock-agentcore-control get-agent-runtime \
      --agent-runtime-id "$runtime_id" \
      --region "$REGION" \
      --output json)
    runtime_status=$(RUNTIME_METADATA_JSON="$runtime_metadata" python3 -c \
      'import json, os; print(json.loads(os.environ["RUNTIME_METADATA_JSON"]).get("status", ""))')
    case "$runtime_status" in
      READY)
        printf '%s' "$runtime_metadata"
        return 0
        ;;
      CREATE_FAILED|UPDATE_FAILED|DELETING|"")
        echo "ERROR: AgentCore runtime entered terminal status ${runtime_status:-<missing>}." >&2
        return 1
        ;;
      *)
        echo "  Waiting for AgentCore runtime READY ($runtime_status, attempt $attempt/60)..." >&2
        sleep 5
        ;;
    esac
  done

  echo "ERROR: Timed out waiting for AgentCore runtime READY." >&2
  return 1
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
    --env "S3_USER_FILES_BUCKET=$USER_FILES_BUCKET" \
    --env "WORKSPACE_SYNC_INTERVAL_MS=$WORKSPACE_SYNC_MS" \
    --env "IMAGE_VERSION=$IMAGE_VERSION" \
    --env "WORKSPACE_SESSION_ROLE_ARN=$WORKSPACE_SESSION_ROLE_ARN" \
    --env "CMK_ARN=$CMK_ARN"

  # --- Configure session storage (not supported by agentcore CLI yet) ---
  echo "--- Configuring session storage ---"
  # Read runtime ID early for the update-agent-runtime call
  local _early_runtime_id
  _early_runtime_id=$(python3 -c "
import re
with open('$PROJECT_DIR/.bedrock_agentcore.yaml') as f:
    text = f.read()
m = re.search(r'agent_id:\s*(\S+)', text)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

  if [ -n "$_early_runtime_id" ]; then
    python3 -c "
import boto3, json, sys

client = boto3.client('bedrock-agentcore-control', region_name='$REGION')

# Get current runtime config to preserve all fields
rt = client.get_agent_runtime(agentRuntimeId='$_early_runtime_id')

# Check if session storage is already configured
existing_fs = rt.get('filesystemConfigurations', [])
has_session_storage = any(
    'sessionStorage' in fs for fs in existing_fs
)

if has_session_storage:
    print('  Session storage already configured — skipping.')
    sys.exit(0)

# Add session storage config (full replace — must include all fields)
print('  Adding filesystemConfigurations to runtime...')
client.update_agent_runtime(
    agentRuntimeId='$_early_runtime_id',
    agentRuntimeArtifact=rt['agentRuntimeArtifact'],
    roleArn=rt['roleArn'],
    networkConfiguration=rt['networkConfiguration'],
    environmentVariables=rt.get('environmentVariables', {}),
    filesystemConfigurations=[
        {'sessionStorage': {'mountPath': '/mnt/workspace'}}
    ],
)
print('  Session storage configured: /mnt/workspace')
" 2>&1 || echo "  WARNING: Failed to configure session storage (non-fatal)."
  else
    echo "  WARNING: Could not determine runtime ID — skipping session storage config."
  fi

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

  if [[ ! "$RUNTIME_ID" =~ ^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$ ]]; then
    echo "ERROR: Could not extract a canonical runtime_id from AgentCore Toolkit." >&2
    exit 1
  fi
  echo "  Runtime ID candidate: $RUNTIME_ID"

  # GetAgentRuntime is authoritative for runtime identity. The ARN contains an
  # opaque UUID and positive version and must never be synthesized from the ID.
  RUNTIME_METADATA_JSON=$(wait_for_runtime_ready "$RUNTIME_ID")
  VALIDATED_RUNTIME=$(validate_runtime_metadata "$RUNTIME_METADATA_JSON" "$RUNTIME_ID")
  VALIDATED_RUNTIME_ID=$(printf '%s\n' "$VALIDATED_RUNTIME" | sed -n '1p')
  RUNTIME_ARN=$(printf '%s\n' "$VALIDATED_RUNTIME" | sed -n '2p')
  if [ "$VALIDATED_RUNTIME_ID" != "$RUNTIME_ID" ] || [ -z "$RUNTIME_ARN" ]; then
    echo "ERROR: Validated AgentCore runtime metadata was incomplete." >&2
    exit 1
  fi
  echo "  Runtime ARN: $RUNTIME_ARN"

  # Get endpoint ID
  ENDPOINT_ID=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
    --agent-runtime-id "$RUNTIME_ID" \
    --region "$REGION" \
    --query "runtimeEndpoints[?name=='DEFAULT'].id | [0]" \
    --output text)
  ENDPOINT_STATUS=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
    --agent-runtime-id "$RUNTIME_ID" \
    --region "$REGION" \
    --query "runtimeEndpoints[?id=='${ENDPOINT_ID}'].status | [0]" \
    --output text)
  if [[ ! "$ENDPOINT_ID" =~ ^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$ ]]; then
    echo "ERROR: AgentCore DEFAULT endpoint ID is missing or noncanonical." >&2
    exit 1
  fi
  if [ "$ENDPOINT_STATUS" != "READY" ]; then
    echo "ERROR: AgentCore DEFAULT endpoint is not READY: $ENDPOINT_STATUS." >&2
    exit 1
  fi
  echo "  Endpoint ID: $ENDPOINT_ID"

  # Persist the exact API-returned ARN atomically with its ID and endpoint.
  echo "--- Updating cdk.json with runtime info ---"
  python3 -c "
import json
with open('$PROJECT_DIR/cdk.json') as f:
    cfg = json.load(f)
cfg['context']['runtime_id'] = '$RUNTIME_ID'
cfg['context']['runtime_endpoint_id'] = '$ENDPOINT_ID'
cfg['context']['runtime_arn'] = '$RUNTIME_ARN'
with open('$PROJECT_DIR/cdk.json', 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
"
  echo "  cdk.json updated."

  echo "  Phase 2 complete."
  echo ""
}

# --- Phase 3: CDK dependent stacks ---
phase3_cdk() {
  echo "=== Phase 3: CDK dependent stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  # AgentCoreStack validates that all exact runtime fields are present together.
  RUNTIME_ID=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('runtime_id',''))")
  RUNTIME_ARN=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('runtime_arn',''))")
  if [ -z "$RUNTIME_ID" ] || [ -z "$RUNTIME_ARN" ]; then
    echo "ERROR: exact runtime identity not set in cdk.json. Run Phase 2 first."
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
