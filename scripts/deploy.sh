#!/usr/bin/env bash
# deploy.sh — CDK deployment for OpenClaw on Bedrock AgentCore.
#
# Three-phase deployment:
#   Phase 1: CDK deploys foundation (VPC, Security, Guardrails, Observability)
#   Phase 2: CDK deploys AgentCore runtime (ECR asset, Runtime, Endpoint, Browser)
#   Phase 3: CDK deploys dependent stacks (Router, Cron, TokenMonitoring)
#
# Usage:
#   ./scripts/deploy.sh                  # full 3-phase deploy
#   ./scripts/deploy.sh --cdk-only       # all CDK phases only
#   ./scripts/deploy.sh --runtime-only   # runtime stack only (Phase 2)
#   ./scripts/deploy.sh --phase1         # Phase 1 only
#   ./scripts/deploy.sh --phase3         # Phase 3 only
#
# Environment variables:
#   BUILD_MODE          auto (default), local-build, or codebuild
#                       auto: uses local-build on ARM64 hosts, codebuild on amd64/x86_64 hosts
#                       local-build: builds ARM64 container locally with Docker
#                       codebuild: builds in AWS CodeBuild (no Docker required, adds cost)
#   CDK_DEFAULT_ACCOUNT AWS account ID (auto-detected if not set)
#   CDK_DEFAULT_REGION  AWS region (falls back to cdk.json, then aws configure)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_ACTIVATE="$VENV_DIR/bin/activate"
VENV_STAMP="$VENV_DIR/.requirements.sha256"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/openclaw-env.sh"
load_project_env "$PROJECT_DIR"

context_value() {
  local key="$1"
  python3 - "$PROJECT_DIR" "$key" <<'PY'
import json
import pathlib
import sys

project_dir = pathlib.Path(sys.argv[1])
key = sys.argv[2]
try:
    with open(project_dir / "cdk.json", encoding="utf-8") as fh:
        print(json.load(fh).get("context", {}).get(key, ""))
except FileNotFoundError:
    print("")
PY
}

sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  else
    shasum -a 256 "$file" | awk '{print $1}'
  fi
}

# --- Build mode ---
BUILD_MODE="${BUILD_MODE:-auto}"

supports_arm64_docker_build() {
  command -v docker &>/dev/null &&
    docker info &>/dev/null 2>&1 &&
    docker buildx ls 2>/dev/null | grep -q "linux/arm64"
}

resolve_build_mode() {
  case "$BUILD_MODE" in
    auto)
      if supports_arm64_docker_build; then
        BUILD_MODE="local-build"
      else
        BUILD_MODE="codebuild"
        echo "INFO: Docker buildx linux/arm64 support is unavailable; using BUILD_MODE=codebuild."
      fi
      ;;
    local-build)
      if ! supports_arm64_docker_build; then
        echo "ERROR: BUILD_MODE=local-build requires Docker buildx support for linux/arm64."
        echo "Either configure Docker buildx/QEMU for arm64 or use BUILD_MODE=codebuild."
        exit 1
      fi
      ;;
    codebuild)
      ;;
    *)
      echo "ERROR: Unsupported BUILD_MODE='$BUILD_MODE'. Use auto, local-build, or codebuild."
      exit 1
      ;;
  esac
}

activate_nvm() {
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090
    source "$NVM_DIR/nvm.sh"
    return 0
  fi
  return 1
}

use_project_node() {
  if [ ! -f "$PROJECT_DIR/.nvmrc" ]; then
    return 0
  fi

  if ! activate_nvm; then
    echo "WARNING: .nvmrc found but nvm is not available; continuing with system Node ($(node -v 2>/dev/null || echo unknown))."
    return 0
  fi

  if ! nvm use --silent >/dev/null 2>&1; then
    echo "ERROR: Node version $(tr -d '[:space:]' < "$PROJECT_DIR/.nvmrc") from .nvmrc is not installed in nvm."
    echo "Run: nvm install"
    exit 1
  fi

  hash -r
}

ensure_python_venv() {
  local requirements_hash
  local current_hash=""

  if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3 to bootstrap $VENV_DIR."
    exit 1
  fi

  if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "--- Creating Python virtualenv ---"
    python3 -m venv "$VENV_DIR"
  fi

  # shellcheck disable=SC1091
  source "$VENV_ACTIVATE"

  requirements_hash=$(sha256_file "$PROJECT_DIR/requirements.txt")
  if [ -f "$VENV_STAMP" ]; then
    current_hash=$(cat "$VENV_STAMP")
  fi

  if [ "$current_hash" != "$requirements_hash" ] || ! python -c "import aws_cdk, cdk_nag, constructs" &>/dev/null; then
    echo "--- Installing Python dependencies into $VENV_DIR ---"
    python -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"
    printf '%s\n' "$requirements_hash" > "$VENV_STAMP"
  fi
}

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
    echo "ERROR: AWS CDK CLI not found for Node $(node -v 2>/dev/null || echo unknown). Install with: npm install -g aws-cdk@latest"
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

  if [ "$errors" -gt 0 ]; then
    echo ""
    echo "Fix the above errors and re-run."
    exit 1
  fi
}

use_project_node
ensure_python_venv
resolve_build_mode
OPENCLAW_ENV_SUFFIX="$(resolve_env_suffix "$PROJECT_DIR")"
export OPENCLAW_ENV_SUFFIX
STACK_VPC="$(with_suffix OpenClawVpc)"
STACK_SECURITY="$(with_suffix OpenClawSecurity)"
STACK_GUARDRAILS="$(with_suffix OpenClawGuardrails)"
STACK_OBSERVABILITY="$(with_suffix OpenClawObservability)"
STACK_AGENTCORE="$(with_suffix OpenClawAgentCore)"
STACK_ROUTER="$(with_suffix OpenClawRouter)"
STACK_CRON="$(with_suffix OpenClawCron)"
STACK_TOKEN_MONITORING="$(with_suffix OpenClawTokenMonitoring)"
TELEGRAM_SECRET_ID="$(with_suffix 'openclaw/channels/telegram')"
WEBHOOK_SECRET_ID="$(with_suffix 'openclaw/webhook-secret')"
IDENTITY_TABLE_NAME="$(with_suffix 'openclaw-identity')"
telegram_setup_attempted=0
telegram_webhook_configured=0
telegram_allowlist_configured=0

# Resolve account and region
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-}}"
if [ -z "$REGION" ]; then
  REGION=$(context_value region 2>/dev/null || echo "")
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

# Run pre-flight checks
preflight

echo "=== OpenClaw CDK Deploy ==="
echo "  Account:    $ACCOUNT"
echo "  Region:     $REGION"
if [ -n "$OPENCLAW_ENV_SUFFIX" ]; then
  echo "  Env suffix: $OPENCLAW_ENV_SUFFIX"
else
  echo "  Env suffix: (none)"
fi
echo "  Build mode: $BUILD_MODE"
echo ""

MODE="${1:-full}"
CDK_DEPLOY_FLAGS=(--require-approval never)
if [ "$BUILD_MODE" = "codebuild" ]; then
  CDK_DEPLOY_FLAGS+=(--asset-publishing-codebuild)
fi

activate_venv() {
  if [ -f "$VENV_ACTIVATE" ]; then
    # shellcheck disable=SC1091
    source "$VENV_ACTIVATE"
  fi
}

update_telegram_secret() {
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    return 0
  fi

  echo "--- Updating Telegram bot token secret ---"
  aws secretsmanager update-secret \
    --secret-id "$TELEGRAM_SECRET_ID" \
    --secret-string "$TELEGRAM_BOT_TOKEN" \
    --region "$REGION" >/dev/null
}

register_telegram_webhook() {
  local telegram_token="$1"
  local api_url
  local webhook_secret
  local webhook_result

  api_url=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_ROUTER" \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text \
    --region "$REGION")

  webhook_secret=$(aws secretsmanager get-secret-value \
    --secret-id "$WEBHOOK_SECRET_ID" \
    --region "$REGION" \
    --query SecretString \
    --output text)

  webhook_result=$(curl -fsS \
    "https://api.telegram.org/bot${telegram_token}/setWebhook?url=${api_url}webhook/telegram&secret_token=${webhook_secret}")
  echo "Telegram webhook result: $webhook_result"

  if ! echo "$webhook_result" | grep -q '"ok":true'; then
    echo "ERROR: Telegram webhook registration failed."
    exit 1
  fi

  telegram_webhook_configured=1
}

allowlist_telegram_admin() {
  local channel_key
  local now_iso

  if ! [[ "${TELEGRAM_ADMIN_USER_ID:-}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: TELEGRAM_ADMIN_USER_ID must be numeric. Got: ${TELEGRAM_ADMIN_USER_ID:-}"
    exit 1
  fi

  channel_key="telegram:${TELEGRAM_ADMIN_USER_ID}"
  now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  aws dynamodb put-item \
    --table-name "$IDENTITY_TABLE_NAME" \
    --region "$REGION" \
    --item "{
      \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
      \"SK\": {\"S\": \"ALLOW\"},
      \"channelKey\": {\"S\": \"${channel_key}\"},
      \"addedAt\": {\"S\": \"${now_iso}\"}
    }" >/dev/null

  echo "Allowlisted $channel_key"
  telegram_allowlist_configured=1
}

configure_telegram_bootstrap() {
  local telegram_token=""

  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -z "${TELEGRAM_ADMIN_USER_ID:-}" ]; then
    return 0
  fi

  telegram_setup_attempted=1
  update_telegram_secret

  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    telegram_token="$TELEGRAM_BOT_TOKEN"
  else
    telegram_token=$(aws secretsmanager get-secret-value \
      --secret-id "$TELEGRAM_SECRET_ID" \
      --region "$REGION" \
      --query SecretString \
      --output text 2>/dev/null || true)
  fi

  if [ -n "$telegram_token" ]; then
    echo "--- Registering Telegram webhook ---"
    register_telegram_webhook "$telegram_token"
  else
    echo "WARNING: TELEGRAM_BOT_TOKEN not set and no stored token was found; skipping Telegram webhook registration."
  fi

  if [ -n "${TELEGRAM_ADMIN_USER_ID:-}" ]; then
    echo "--- Adding Telegram admin to allowlist ---"
    allowlist_telegram_admin
  else
    echo "INFO: TELEGRAM_ADMIN_USER_ID not set; skipping Telegram allowlist bootstrap."
  fi
}

# --- Phase 1: CDK foundation stacks ---
phase1_cdk() {
  echo "=== Phase 1: CDK foundation stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  cdk deploy \
    "$STACK_VPC" \
    "$STACK_SECURITY" \
    "$STACK_GUARDRAILS" \
    "$STACK_OBSERVABILITY" \
    "${CDK_DEPLOY_FLAGS[@]}"

  echo "  Phase 1 complete."
  echo ""
}

# --- Phase 2: CDK runtime deploy ---
phase2_runtime() {
  echo "=== Phase 2: AgentCore runtime stack ==="
  cd "$PROJECT_DIR"
  activate_venv

  cdk deploy \
    "$STACK_AGENTCORE" \
    "${CDK_DEPLOY_FLAGS[@]}"

  echo "  Phase 2 complete."
  echo ""
}

# --- Phase 3: CDK dependent stacks ---
phase3_cdk() {
  echo "=== Phase 3: CDK dependent stacks ==="
  cd "$PROJECT_DIR"
  activate_venv

  cdk deploy \
    "$STACK_ROUTER" \
    "$STACK_CRON" \
    "$STACK_TOKEN_MONITORING" \
    "${CDK_DEPLOY_FLAGS[@]}"

  configure_telegram_bootstrap

  echo "  Phase 3 complete."
  echo ""
}

case "$MODE" in
  --phase1)
    phase1_cdk
    ;;
  --runtime-only)
    phase2_runtime
    ;;
  --phase3)
    phase3_cdk
    ;;
  --cdk-only)
    phase1_cdk
    phase2_runtime
    phase3_cdk
    ;;
  *)
    phase1_cdk
    phase2_runtime
    phase3_cdk
    ;;
esac

echo "=== Deploy complete ==="
echo ""
if [ "$telegram_setup_attempted" -eq 1 ]; then
  echo "Telegram bootstrap:"
  if [ "$telegram_webhook_configured" -eq 1 ]; then
    echo "  - webhook configured"
  fi
  if [ "$telegram_allowlist_configured" -eq 1 ]; then
    echo "  - admin allowlisted"
  fi
else
  echo "Telegram bootstrap was skipped."
  echo "Add TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_USER_ID to .env to include it in deployment,"
  echo "or run ./scripts/setup-telegram.sh later."
fi
