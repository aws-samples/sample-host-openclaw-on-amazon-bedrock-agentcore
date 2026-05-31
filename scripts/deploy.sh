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
#   AGENTCORE_SUPPORTED_AZ_IDS
#                       Optional comma/space-separated list or JSON array of
#                       stable AZ IDs supported by AgentCore Runtime
#                       (for example: use1-az4,use1-az1,use1-az2). The deploy
#                       script resolves them to this account's AZ names and
#                       passes those names into CDK so the VPC matches
#                       AgentCore's subnet requirements.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_ACTIVATE="$VENV_DIR/bin/activate"
VENV_STAMP="$VENV_DIR/.requirements.sha256"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy.sh [--env <name>] [--cdk-only|--runtime-only|--phase1|--phase3]

Options:
  --env <name>       Load .env.<name> (for example .env.dev or .env.prod) and
                     require OPENCLAW_ENV_SUFFIX to match that name.
  --phase1           Deploy foundation stacks only.
  --runtime-only     Deploy the runtime stack only.
  --phase3           Deploy dependent stacks only.
  --cdk-only         Deploy all CDK-managed stacks.
  -h, --help         Show this help text.
EOF
}

OPENCLAW_ENV_NAME="${OPENCLAW_ENV_NAME:-}"
POSITIONAL_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --env)
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "ERROR: --env requires a value."
        usage
        exit 1
      fi
      OPENCLAW_ENV_NAME="$2"
      shift 2
      ;;
    --env=*)
      OPENCLAW_ENV_NAME="${1#*=}"
      if [ -z "$OPENCLAW_ENV_NAME" ]; then
        echo "ERROR: --env requires a value."
        usage
        exit 1
      fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${POSITIONAL_ARGS[@]}"
export OPENCLAW_ENV_NAME

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/openclaw-env.sh"
load_project_env "$PROJECT_DIR" "$OPENCLAW_ENV_NAME"
apply_named_environment "$OPENCLAW_ENV_NAME"

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

normalize_jsonish_list() {
  local raw="${1:-}"
  python3 - "$raw" <<'PY'
import json
import re
import sys

raw = (sys.argv[1] or "").strip()
if not raw or raw in {"[]", "None", "null"}:
    print("")
    raise SystemExit(0)

items = None
if raw.startswith("["):
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = parsed
    except json.JSONDecodeError:
        items = None

if items is None:
    items = re.split(r"[\s,]+", raw)

normalized = []
for item in items:
    value = str(item).strip().strip('"').strip("'")
    if value:
        normalized.append(value)

print(json.dumps(normalized))
PY
}

resolve_agentcore_availability_zones() {
  local supported_az_ids_raw="${AGENTCORE_SUPPORTED_AZ_IDS:-}"
  local supported_az_ids_json=""
  local az_catalog=""

  if [ -z "$supported_az_ids_raw" ]; then
    supported_az_ids_raw="$(context_value agentcore_supported_availability_zone_ids 2>/dev/null || echo "")"
  fi

  supported_az_ids_json="$(normalize_jsonish_list "$supported_az_ids_raw")"
  if [ -z "$supported_az_ids_json" ] || [ "$supported_az_ids_json" = "[]" ]; then
    RESOLVED_AVAILABILITY_ZONES_JSON=""
    return 0
  fi

  az_catalog="$(
    aws ec2 describe-availability-zones \
      --region "$REGION" \
      --all-availability-zones \
      --query 'AvailabilityZones[].[ZoneId,ZoneName,State]' \
      --output json
  )"

  RESOLVED_AVAILABILITY_ZONES_JSON="$(
    python3 - "$supported_az_ids_json" "$az_catalog" <<'PY'
import json
import sys

supported = json.loads(sys.argv[1])
catalog = json.loads(sys.argv[2])

zone_name_by_id = {}
state_by_id = {}
for zone_id, zone_name, state in catalog:
    zone_name_by_id[str(zone_id)] = str(zone_name)
    state_by_id[str(zone_id)] = str(state)

missing = [zone_id for zone_id in supported if zone_id not in zone_name_by_id]
if missing:
    raise SystemExit(
        "ERROR: Could not resolve AZ IDs in this account/region: " + ", ".join(missing)
    )

unavailable = [zone_id for zone_id in supported if state_by_id.get(zone_id) != "available"]
if unavailable:
    raise SystemExit(
        "ERROR: The following AZ IDs are not currently available in this account/region: "
        + ", ".join(f"{zone_id} ({state_by_id.get(zone_id, 'unknown')})" for zone_id in unavailable)
    )

resolved = [zone_name_by_id[zone_id] for zone_id in supported]
print(json.dumps(resolved))
PY
  )"

  echo "INFO: AgentCore-supported AZ IDs: $supported_az_ids_json"
  echo "INFO: Resolved VPC AZ names:      $RESOLVED_AVAILABILITY_ZONES_JSON"
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

validate_required_settings() {
  local errors=0

  if [ -z "$REGION" ]; then
    echo "ERROR: CDK_DEFAULT_REGION/AWS_REGION is required."
    errors=$((errors + 1))
  fi

  if [ -n "${TELEGRAM_ADMIN_USER_ID:-}" ] && ! [[ "${TELEGRAM_ADMIN_USER_ID}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: TELEGRAM_ADMIN_USER_ID must be numeric. Got: ${TELEGRAM_ADMIN_USER_ID}"
    errors=$((errors + 1))
  fi

  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [[ "${TELEGRAM_BOT_TOKEN}" != *:* ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN does not look like a Telegram bot token (expected <bot_id>:<secret>)."
    errors=$((errors + 1))
  fi

  if [ -n "${RETAIN_STATEFUL_RESOURCES:-}" ]; then
    case "${RETAIN_STATEFUL_RESOURCES,,}" in
      1|0|true|false|yes|no|on|off)
        ;;
      *)
        echo "ERROR: RETAIN_STATEFUL_RESOURCES must be one of: true, false, 1, 0, yes, no, on, off."
        errors=$((errors + 1))
        ;;
    esac
  fi

  case "$MODE" in
    --phase1|--runtime-only)
      if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || [ -n "${TELEGRAM_ADMIN_USER_ID:-}" ]; then
        echo "ERROR: TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_USER_ID require a deployment mode that includes the Router stack."
        echo "Use ./scripts/deploy.sh, ./scripts/deploy.sh --cdk-only, or ./scripts/deploy.sh --phase3."
        errors=$((errors + 1))
      fi
      ;;
  esac

  if [ "$errors" -gt 0 ]; then
    echo ""
    echo "Fix the required configuration above and re-run."
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

RESOLVED_AVAILABILITY_ZONES_JSON=""
resolve_agentcore_availability_zones

# Run pre-flight checks
preflight

echo "=== OpenClaw CDK Deploy ==="
echo "  Account:    $ACCOUNT"
echo "  Region:     $REGION"
if [ -n "${OPENCLAW_ENV_NAME:-}" ]; then
  echo "  Env name:   $OPENCLAW_ENV_NAME"
fi
if [ -n "$OPENCLAW_ENV_SUFFIX" ]; then
  echo "  Env suffix: $OPENCLAW_ENV_SUFFIX"
else
  echo "  Env suffix: (none)"
fi
if [ -n "${OPENCLAW_SELECTED_ENV_FILE:-}" ] && [ -f "${OPENCLAW_SELECTED_ENV_FILE}" ]; then
  echo "  Env file:   $OPENCLAW_SELECTED_ENV_FILE"
fi
echo "  Build mode: $BUILD_MODE"
echo ""

MODE="full"
for arg in "$@"; do
  case "$arg" in
    --phase1|--runtime-only|--phase3|--cdk-only)
      if [ "$MODE" != "full" ]; then
        echo "ERROR: Specify only one deployment mode."
        usage
        exit 1
      fi
      MODE="$arg"
      ;;
    *)
      echo "ERROR: Unknown option '$arg'."
      usage
      exit 1
      ;;
  esac
done

CDK_DEPLOY_FLAGS=(--require-approval never)
if [ "$BUILD_MODE" = "codebuild" ]; then
  CDK_DEPLOY_FLAGS+=(--asset-publishing-codebuild)
fi
if [ -n "$RESOLVED_AVAILABILITY_ZONES_JSON" ]; then
  CDK_DEPLOY_FLAGS+=(-c "availability_zones=$RESOLVED_AVAILABILITY_ZONES_JSON")
fi
ROUTER_CDK_DEPLOY_FLAGS=("${CDK_DEPLOY_FLAGS[@]}")
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  telegram_setup_attempted=1
  ROUTER_CDK_DEPLOY_FLAGS+=(
    --parameters
    "${STACK_ROUTER}:TelegramBotToken=${TELEGRAM_BOT_TOKEN}"
  )
fi
if [ -n "${TELEGRAM_ADMIN_USER_ID:-}" ]; then
  telegram_setup_attempted=1
  ROUTER_CDK_DEPLOY_FLAGS+=(
    --parameters
    "${STACK_ROUTER}:TelegramAdminUserId=${TELEGRAM_ADMIN_USER_ID}"
  )
fi

activate_venv() {
  if [ -f "$VENV_ACTIVATE" ]; then
    # shellcheck disable=SC1091
    source "$VENV_ACTIVATE"
  fi
}

validate_required_settings

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
    "${ROUTER_CDK_DEPLOY_FLAGS[@]}"

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
  echo "Telegram bootstrap was requested via Router custom resource parameters."
else
  echo "Telegram bootstrap was skipped."
  echo "Add TELEGRAM_BOT_TOKEN and/or TELEGRAM_ADMIN_USER_ID to .env to include it in deployment."
fi
