#!/usr/bin/env bash
# deploy.sh — CDK foundation, AgentCore Runtime V2, then dependent stacks.
# Setup: uv venv .venv-deploy
#        uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt
# CDK uses .venv; the SDK controller uses .venv-deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_PYTHON="${DEPLOY_PYTHON:-$PROJECT_DIR/.venv-deploy/bin/python}"
MODE=full
SKIP_WARMUP=false
ENDPOINT_NAME=""
MANIFEST=".runtime/runtime-v2.json"
IMAGE_URI="${RUNTIME_IMAGE_URI:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --phase1|--phase3|--runtime-only|--cdk-only) MODE="$1"; shift ;;
    --manifest) MANIFEST="${2:?--manifest requires a path}"; shift 2 ;;
    --image-uri) IMAGE_URI="${2:?--image-uri requires a prebuilt ECR image}"; shift 2 ;;
    --skip-warmup) SKIP_WARMUP=true; shift ;;
    --endpoint-name) ENDPOINT_NAME="${2:?--endpoint-name requires a name}"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--phase1|--phase3|--runtime-only|--cdk-only] [--manifest PATH] [--image-uri URI] [--endpoint-name NAME] [--skip-warmup]"
      echo "Default: AgentCore Runtime V2, .runtime/runtime-v2.json, real billed short-chat warmup."
      echo "Without --image-uri: local ARM64 Docker build; ECR_REPOSITORY defaults to openclaw-bridge."
      echo "--skip-warmup is allowed only with --runtime-only to prepare an unverified artifact."
      echo "V2 releases keep existing endpoints fixed; use --endpoint-name release_2 for an update."
      exit 0 ;;
    *) echo "ERROR: unknown option $1" >&2; exit 1 ;;
  esac
done
if [ "${RUNTIME_PLATFORM+x}" = x ]; then
  echo "ERROR: RUNTIME_PLATFORM is no longer supported; unset it for V2 deployment." >&2
  exit 1
fi
if [ "$SKIP_WARMUP" = true ] && [ "$MODE" != --runtime-only ]; then
  echo "ERROR: --skip-warmup requires --runtime-only; enrollment requires successful chat warmup." >&2
  exit 1
fi
cd "$PROJECT_DIR"
if ! command -v uv >/dev/null; then
  echo "ERROR: install uv before running deploy.sh." >&2
  exit 1
fi
PYTHON_INTERPRETER="$DEPLOY_PYTHON"
if [ "$MODE" = --phase1 ]; then
  PYTHON_INTERPRETER="$PROJECT_DIR/.venv/bin/python"
elif [ ! -x "$DEPLOY_PYTHON" ]; then
  echo "ERROR: deployment Python is not executable: $DEPLOY_PYTHON" >&2
  echo "Setup: uv venv .venv-deploy" >&2
  echo "       uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt" >&2
  exit 1
fi
if [ "$MODE" != --runtime-only ]; then
  if [ ! -x "$PROJECT_DIR/.venv/bin/python" ] || ! command -v cdk >/dev/null; then
    echo "ERROR: install requirements.txt in .venv and install the AWS CDK CLI." >&2
    exit 1
  fi
fi
PYTHON=(uv run --no-project --python "$PYTHON_INTERPRETER" python)
REGION="${CDK_DEFAULT_REGION:-$("${PYTHON[@]}" -c 'import json; print(json.load(open("cdk.json"))["context"].get("region", ""))')}"
REGION="${REGION:-$(aws configure get region)}"
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
if [ -z "$REGION" ] || [ -z "$ACCOUNT" ]; then
  echo "ERROR: set CDK_DEFAULT_REGION and CDK_DEFAULT_ACCOUNT or configure AWS credentials." >&2
  exit 1
fi
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"

preflight() {
  if [ "$MODE" != --phase1 ]; then
    "${PYTHON[@]}" "$SCRIPT_DIR/runtime_deploy.py" preflight --region "$REGION"
  fi
  aws sts get-caller-identity >/dev/null
}

phase1_cdk() (
  source "$PROJECT_DIR/.venv/bin/activate"
  local enable_gateway gateway=()
  enable_gateway=$("${PYTHON[@]}" -c 'import json; print(str(json.load(open("cdk.json"))["context"].get("enable_gateway", False)).lower())')
  if [ "$enable_gateway" = true ]; then
    gateway=(OpenClawGateway)
  fi
  cdk deploy OpenClawVpc OpenClawSecurity OpenClawGuardrails OpenClawAgentCore OpenClawObservability \
    ${gateway[@]+"${gateway[@]}"} -c runtime_manifest= --require-approval never
)

# --- Read CDK outputs for runtime config ---
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

  GATEWAY_TOKEN_SECRET_ID="openclaw/gateway-token"

  # OpenClawSecurity outputs use hand-chosen OutputKeys (stacks/security_stack.py).
  # Match them exactly — never with contains() on a CDK-generated ExportsOutput*
  # key, which embeds a logical-id hash and silently returns "" when it changes.
  COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
    --output text)

  COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoProxyClientId'].OutputValue" \
    --output text)

  COGNITO_PASSWORD_SECRET_ID="openclaw/cognito-password-secret"
  TELEGRAM_CHANNEL_SECRET_ID="openclaw/channels/telegram"

  CMK_ARN=$(aws cloudformation describe-stacks \
    --stack-name OpenClawSecurity --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='SecretsCmkArn'].OutputValue" \
    --output text)

  require_cdk_output EXECUTION_ROLE_ARN SECURITY_GROUP_ID PRIVATE_SUBNET_IDS USER_FILES_BUCKET \
    COGNITO_USER_POOL_ID COGNITO_CLIENT_ID CMK_ARN

  # Bedrock Guardrails (issue #100). The proxy only injects guardrailConfig when
  # BEDROCK_GUARDRAIL_ID is set on the runtime, so read the OpenClawGuardrails
  # outputs here and pass them through in Phase 2. enable_guardrails defaults
  # to true (stacks/guardrails_stack.py); when it is false the stack exports
  # nothing and we deliberately leave both values empty.
  ENABLE_GUARDRAILS=$("${PYTHON[@]}" -c "import json; print(str(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('enable_guardrails', True)).lower())")
  GUARDRAIL_ID=""
  GUARDRAIL_VERSION=""
  if [ "$ENABLE_GUARDRAILS" = "true" ]; then
    GUARDRAIL_ID=$(aws cloudformation describe-stacks \
      --stack-name OpenClawGuardrails --region "$REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" \
      --output text)
    GUARDRAIL_VERSION=$(aws cloudformation describe-stacks \
      --stack-name OpenClawGuardrails --region "$REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='GuardrailVersion'].OutputValue" \
      --output text)
    # An empty ID would silently deploy the runtime with guardrails OFF while
    # the stack and docs say they are on — refuse, same as COGNITO_CLIENT_ID.
    require_cdk_output GUARDRAIL_ID GUARDRAIL_VERSION
  else
    echo "  Guardrails:     disabled via enable_guardrails=false (BEDROCK_GUARDRAIL_ID not set)"
  fi

  # AgentCore Gateway MCP tools (prototype). When enable_gateway=true the
  # runtime gets AGENTCORE_GATEWAY_URL and the bridge adds mcp.servers.agentcore
  # to openclaw.json; an empty value would silently leave the tools off, so it
  # is required (same rule as COGNITO_CLIENT_ID / GUARDRAIL_ID). When the flag
  # is off the variable is not passed and the runtime behaves exactly as before.
  ENABLE_GATEWAY=$("${PYTHON[@]}" -c "import json; print(str(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('enable_gateway', False)).lower())")
  AGENTCORE_GATEWAY_URL=""
  if [ "$ENABLE_GATEWAY" = "true" ]; then
    AGENTCORE_GATEWAY_URL=$(aws cloudformation describe-stacks \
      --stack-name OpenClawGateway --region "$REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" \
      --output text)
    require_cdk_output AGENTCORE_GATEWAY_URL
    echo "  Gateway MCP:    $AGENTCORE_GATEWAY_URL"
  else
    echo "  Gateway MCP:    disabled via enable_gateway=false (AGENTCORE_GATEWAY_URL not set)"
  fi

  # An enabled browser requires its identifier; lookup failures must not disable it.
  ENABLE_BROWSER=$("${PYTHON[@]}" -c "import json; print(str(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('enable_browser', False)).lower())")
  BROWSER_IDENTIFIER=""
  if [ "$ENABLE_BROWSER" = "true" ]; then
    BROWSER_IDENTIFIER=$(aws cloudformation describe-stacks \
      --stack-name OpenClawAgentCore --region "$REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='BrowserIdentifier'].OutputValue" \
      --output text)
    require_cdk_output BROWSER_IDENTIFIER
    if [[ ! "$BROWSER_IDENTIFIER" =~ ^(aws\.browser\.v1|[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10})$ ]]; then
      echo "ERROR: BrowserIdentifier is not a valid AgentCore Browser ID." >&2
      exit 1
    fi
    echo "  Browser ID:     $BROWSER_IDENTIFIER"
  fi

  # Read config values from cdk.json
  DEFAULT_MODEL_ID=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('default_model_id','global.anthropic.claude-haiku-5-5'))")
  FALLBACK_MODEL_ID=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('fallback_model_id','global.anthropic.claude-sonnet-4-6'))")
  SUBAGENT_MODEL_ID=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('subagent_model_id',''))")
  # Per-user /model allowlist (validated; empty = feature disabled, runtime variable removed).
  USER_SELECTABLE_MODEL_IDS=$("${PYTHON[@]}" -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import model_selection; sys.exit(model_selection.main(['', '$PROJECT_DIR/cdk.json']))")
  IMAGE_VERSION=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('image_version','1'))")
  WORKSPACE_SYNC_MS=$("${PYTHON[@]}" -c "import json; print(int(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('workspace_sync_interval_seconds',300))*1000)")
  # How long the bridge waits for the S3 workspace restore before spawning the
  # OpenClaw gateway. The bridge default (45 s) is too short for a large state
  # (~1,200 files / 220 MB restores in ~100-115 s): the gateway would start before
  # the pre-2.0 sessions.json, memory and skills are on disk and the legacy
  # import would be skipped. 180 s covers that with margin; the wait ends as
  # soon as the restore settles, so small namespaces are not slowed down.
  WORKSPACE_RESTORE_WAIT_MS=$("${PYTHON[@]}" -c "import json; print(int(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('workspace_restore_wait_seconds',180))*1000)")
  CRON_LEAD_TIME=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('cron_lead_time_minutes',5))")
  SESSION_IDLE=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_idle_timeout',1800))")
  SESSION_MAX=$("${PYTHON[@]}" -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('session_max_lifetime',28800))")

  echo "  Execution Role: $EXECUTION_ROLE_ARN"
  echo "  Security Group: $SECURITY_GROUP_ID"
  echo "  Subnets:        $PRIVATE_SUBNET_IDS"
  echo "  S3 Bucket:      $USER_FILES_BUCKET"
  echo "  Cognito Pool:   $COGNITO_USER_POOL_ID"
  echo "  Cognito Client: $COGNITO_CLIENT_ID"
  echo "  Restore wait:   ${WORKSPACE_RESTORE_WAIT_MS} ms"
  if [ -n "$GUARDRAIL_ID" ]; then
    echo "  Guardrail:      $GUARDRAIL_ID v$GUARDRAIL_VERSION"
  fi
}

# Fail loudly if a required CDK output resolved empty or to "None" (the AWS CLI
# prints "None" for a query that matched nothing). An empty value here would
# otherwise be passed to the runtime as an env var and silently disable a
# feature — e.g. an empty COGNITO_CLIENT_ID turns off per-user scoped credentials,
# and an empty BEDROCK_GUARDRAIL_ID turns off Bedrock Guardrails in the proxy.
require_cdk_output() {
  local name value missing=""
  for name in "$@"; do
    eval "value=\${$name:-}"
    if [ -z "$value" ] || [ "$value" = "None" ]; then
      missing="$missing $name"
    fi
  done
  if [ -n "$missing" ]; then
    echo "ERROR: required CDK stack output(s) resolved empty:$missing" >&2
    echo "       Check 'aws cloudformation describe-stacks --stack-name OpenClawSecurity / OpenClawAgentCore / OpenClawGuardrails / OpenClawGateway --region $REGION'" >&2
    echo "       and confirm the OutputKeys queried in scripts/deploy.sh exist in the deployed stacks." >&2
    echo "       Refusing to configure the runtime with empty values." >&2
    exit 1
  fi
}

# --- Phase 2: AgentCore Runtime controller ---
phase2_runtime() {
  read_cdk_outputs
  mkdir -p .runtime
  local runtime_args=() config_file image
  if [ "$SKIP_WARMUP" = true ]; then runtime_args+=(--skip-warmup); fi
  if [ -n "$ENDPOINT_NAME" ]; then runtime_args+=(--endpoint-name "$ENDPOINT_NAME"); fi
  image="$IMAGE_URI"
  if [ -z "$image" ]; then
    if [ "${BUILD_MODE:-local-build}" != local-build ]; then
      echo "ERROR: supply --image-uri from your external build for BUILD_MODE=${BUILD_MODE}; the SDK controller never invokes a global agentcore CLI." >&2
      exit 1
    fi
    local repository_uri
    repository_uri=$("${PYTHON[@]}" "$SCRIPT_DIR/runtime_image.py" --region "$REGION" \
      --name "${ECR_REPOSITORY:-openclaw-bridge}")
    image="${repository_uri}:v${IMAGE_VERSION}"
    bash "$SCRIPT_DIR/build_and_push.sh" "$image" "$REGION"
  fi

  export EXECUTION_ROLE_ARN SECURITY_GROUP_ID PRIVATE_SUBNET_IDS USER_FILES_BUCKET
  export GATEWAY_TOKEN_SECRET_ID COGNITO_USER_POOL_ID COGNITO_CLIENT_ID COGNITO_PASSWORD_SECRET_ID
  export CMK_ARN DEFAULT_MODEL_ID FALLBACK_MODEL_ID SUBAGENT_MODEL_ID USER_SELECTABLE_MODEL_IDS
  export IMAGE_VERSION WORKSPACE_SYNC_MS WORKSPACE_RESTORE_WAIT_MS
  export CRON_LEAD_TIME SESSION_IDLE SESSION_MAX TELEGRAM_CHANNEL_SECRET_ID
  export GUARDRAIL_ID GUARDRAIL_VERSION BROWSER_IDENTIFIER AGENTCORE_GATEWAY_URL REGION ACCOUNT
  config_file=$(mktemp "$PROJECT_DIR/.runtime/config.XXXXXX")
  "${PYTHON[@]}" "$SCRIPT_DIR/runtime_config.py" > "$config_file"
  local result=0
  "${PYTHON[@]}" "$SCRIPT_DIR/runtime_deploy.py" deploy \
    --region "$REGION" --manifest "$MANIFEST" \
    --image-uri "$image" --config "$config_file" ${runtime_args[@]+"${runtime_args[@]}"} || result=$?
  rm -f "$config_file"
  if [ "$result" -ne 0 ]; then return "$result"; fi
  echo "AgentCore Runtime manifest: $MANIFEST"
}

# --- Phase 3: dependent stacks ---
phase3_cdk() (
  "${PYTHON[@]}" "$SCRIPT_DIR/runtime_deploy.py" verify --region "$REGION" \
    --manifest "$MANIFEST" --require-enrollment-ready --check-existing-callers
  source "$PROJECT_DIR/.venv/bin/activate"
  cdk deploy OpenClawRouter OpenClawCron OpenClawTokenMonitoring \
    -c "runtime_manifest=$MANIFEST" --require-approval never
)

preflight
case "$MODE" in
  --phase1) phase1_cdk ;;
  --runtime-only) phase2_runtime ;;
  --phase3) phase3_cdk ;;
  --cdk-only) phase1_cdk; phase3_cdk ;;
  full) phase1_cdk; phase2_runtime; phase3_cdk ;;
esac
