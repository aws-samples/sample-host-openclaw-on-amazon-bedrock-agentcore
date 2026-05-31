#!/usr/bin/env bash
# undeploy.sh — CDK teardown for OpenClaw on Bedrock AgentCore.
#
# Usage:
#   ./scripts/undeploy.sh                               # destroy deployable stacks, keep security resources
#   ./scripts/undeploy.sh --all                         # also destroy OpenClawSecurity
#   ./scripts/undeploy.sh --delete-user-files-bucket    # also delete retained user-files bucket
#   ./scripts/undeploy.sh --all --delete-user-files-bucket

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

DESTROY_SECURITY=false
DELETE_USER_FILES_BUCKET=false
DESTROY_TIMEOUT_SECONDS="${DESTROY_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-15}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/undeploy.sh [--all] [--delete-user-files-bucket]

Options:
  --all                       Also destroy OpenClawSecurity.
  --delete-user-files-bucket  Delete the retained S3 user-files bucket after stack teardown.
  -h, --help                  Show this help text.

Environment:
  DESTROY_TIMEOUT_SECONDS     Max seconds to wait for AgentCore ENIs / stack deletion (default: 900)
  POLL_INTERVAL_SECONDS       Poll interval while waiting (default: 15)
EOF
}

for arg in "$@"; do
  case "$arg" in
    --all)
      DESTROY_SECURITY=true
      ;;
    --delete-user-files-bucket)
      DELETE_USER_FILES_BUCKET=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option '$arg'."
      usage
      exit 1
      ;;
  esac
done

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

preflight() {
  local errors=0

  if ! aws sts get-caller-identity &>/dev/null; then
    echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
    errors=$((errors + 1))
  fi

  if ! command -v cdk &>/dev/null; then
    echo "ERROR: AWS CDK CLI not found for Node $(node -v 2>/dev/null || echo unknown). Install with: npm install -g aws-cdk@latest"
    errors=$((errors + 1))
  fi

  if [ "$errors" -gt 0 ]; then
    echo ""
    echo "Fix the above errors and re-run."
    exit 1
  fi
}

activate_venv() {
  if [ -f "$VENV_ACTIVATE" ]; then
    # shellcheck disable=SC1091
    source "$VENV_ACTIVATE"
  fi
}

stack_exists() {
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$1" >/dev/null 2>&1
}

get_stack_resource_id() {
  local stack_name="$1"
  local query="$2"
  aws cloudformation describe-stack-resources \
    --region "$REGION" \
    --stack-name "$stack_name" \
    --query "$query" \
    --output text 2>/dev/null || true
}

get_stack_output_value() {
  local stack_name="$1"
  local output_key="$2"
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='$output_key'].OutputValue | [0]" \
    --output text 2>/dev/null || true
}

wait_for_stack_delete() {
  local stack_name="$1"
  local status=""
  local waited=0

  while [ "$waited" -lt "$DESTROY_TIMEOUT_SECONDS" ]; do
    status=$(aws cloudformation describe-stacks \
      --region "$REGION" \
      --stack-name "$stack_name" \
      --query 'Stacks[0].StackStatus' \
      --output text 2>/dev/null || true)

    if [ -z "$status" ]; then
      echo "Stack deleted: $stack_name"
      return 0
    fi

    case "$status" in
      DELETE_COMPLETE)
        echo "Stack deleted: $stack_name"
        return 0
        ;;
      DELETE_FAILED|ROLLBACK_COMPLETE|UPDATE_ROLLBACK_FAILED|UPDATE_ROLLBACK_COMPLETE)
        echo "ERROR: Stack $stack_name is in state $status"
        return 1
        ;;
    esac

    echo "Waiting for $stack_name ($status)..."
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done

  echo "ERROR: Timed out waiting for $stack_name to delete."
  return 1
}

list_agentcore_enis() {
  local vpc_id="$1"
  aws ec2 describe-network-interfaces \
    --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" "Name=interface-type,Values=agentic_ai" \
    --query 'NetworkInterfaces[].[NetworkInterfaceId,Status,PrivateIpAddress,SubnetId,Groups[0].GroupId]' \
    --output text 2>/dev/null || true
}

print_agentcore_enis() {
  local vpc_id="$1"
  aws ec2 describe-network-interfaces \
    --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" "Name=interface-type,Values=agentic_ai" \
    --query 'NetworkInterfaces[].[NetworkInterfaceId,Status,PrivateIpAddress,SubnetId,Groups[0].GroupId]' \
    --output table 2>/dev/null || true
}

wait_for_agentcore_enis() {
  local vpc_id="$1"
  local waited=0
  local enis=""

  if [ -z "$vpc_id" ] || [ "$vpc_id" = "None" ]; then
    return 0
  fi

  while [ "$waited" -lt "$DESTROY_TIMEOUT_SECONDS" ]; do
    enis="$(list_agentcore_enis "$vpc_id")"
    if [ -z "$enis" ]; then
      echo "No AgentCore-managed ENIs remain in $vpc_id."
      return 0
    fi

    echo "Waiting for AgentCore-managed ENIs to be released before deleting OpenClawVpc..."
    print_agentcore_enis "$vpc_id"
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done

  echo "ERROR: Timed out waiting for AgentCore-managed ENIs to clear from $vpc_id."
  echo "These ENIs are still present:"
  print_agentcore_enis "$vpc_id"
  return 1
}

stop_agentcore_runtime_sessions() {
  local runtime_arn=""
  local qualifier=""
  local list_help=""
  local list_output=""
  local extract_status=0
  local session_count=0
  local stopped_count=0
  local session_id=""
  local output=""
  local python_bin=""
  local -a list_args=()
  local -a session_ids=()
  local stop_args=()

  if ! stack_exists "$STACK_AGENTCORE"; then
    return 0
  fi

  runtime_arn="$(get_stack_output_value "$STACK_AGENTCORE" "RuntimeArn")"
  qualifier="$(get_stack_output_value "$STACK_AGENTCORE" "RuntimeEndpointId")"

  if [ -z "$runtime_arn" ] || [ "$runtime_arn" = "None" ]; then
    echo "Skipping AgentCore session shutdown: RuntimeArn output not found."
    return 0
  fi

  list_help="$(aws --no-cli-pager bedrock-agentcore list-runtime-sessions help 2>&1 || true)"
  if printf '%s' "$list_help" | grep -q "Found invalid choice 'list-runtime-sessions'"; then
    echo "Skipping AgentCore session shutdown: current AWS CLI does not expose list-runtime-sessions for runtime-scoped discovery."
    echo "If AgentCore sessions are still active, stop them manually or wait for the idle timeout before tearing down the VPC."
    return 0
  fi

  list_args=(
    --agent-runtime-arn "$runtime_arn"
    --region "$REGION"
    --output json
  )
  if [ -n "$qualifier" ] && [ "$qualifier" != "None" ]; then
    list_args+=(--qualifier "$qualifier")
  fi

  list_output="$(
    aws bedrock-agentcore list-runtime-sessions "${list_args[@]}" 2>&1
  )" || true

  if [ -z "$list_output" ]; then
    echo "No AgentCore runtime sessions returned for $runtime_arn."
    return 0
  fi

  if printf '%s' "$list_output" | grep -qE 'Exception|UnknownOperationException|ValidationException|Error'; then
    echo "Skipping AgentCore session shutdown: failed to list runtime sessions for $runtime_arn."
    echo "$list_output"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    python_bin="python"
  else
    echo "Skipping AgentCore session shutdown: Python is required to parse the runtime session list response."
    return 0
  fi

  mapfile -t session_ids < <(
    printf '%s' "$list_output" | "$python_bin" - <<'PY'
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)

found = []

def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("runtimeSessionId", "sessionId") and isinstance(child, str):
                found.append(child)
            walk(child)
    elif isinstance(value, list):
        for item in value:
            walk(item)

walk(payload)

seen = set()
for session_id in found:
    if session_id and session_id not in seen:
        seen.add(session_id)
        print(session_id)
PY
  )
  extract_status=$?

  if [ "$extract_status" -ne 0 ]; then
    echo "Skipping AgentCore session shutdown: could not parse the runtime session list response."
    return 0
  fi

  if [ "${#session_ids[@]}" -eq 0 ]; then
    echo "No active AgentCore runtime sessions found for $runtime_arn."
    return 0
  fi

  echo "Stopping AgentCore runtime sessions before teardown..."
  for session_id in "${session_ids[@]}"; do
    [ -n "${session_id:-}" ] || continue
    [ "$session_id" != "None" ] || continue
    session_count=$((session_count + 1))

    stop_args=(
      --agent-runtime-arn "$runtime_arn"
      --runtime-session-id "$session_id"
      --region "$REGION"
    )
    if [ -n "$qualifier" ] && [ "$qualifier" != "None" ]; then
      stop_args+=(--qualifier "$qualifier")
    fi

    output="$(
      aws bedrock-agentcore stop-runtime-session "${stop_args[@]}" 2>&1
    )" || true

    if [ -z "$output" ] || ! printf '%s' "$output" | grep -qE 'Exception|Error'; then
      stopped_count=$((stopped_count + 1))
      echo "  stopped ${session_id}"
      continue
    fi

    if printf '%s' "$output" | grep -q 'ResourceNotFoundException'; then
      echo "  already stopped ${session_id}"
      continue
    fi

    echo "ERROR: Failed to stop runtime session $session_id"
    echo "$output"
    return 1
  done

  echo "Stopped $stopped_count of $session_count AgentCore runtime sessions."
}

destroy_stack_group() {
  local stacks=()
  local stack_name

  for stack_name in "$@"; do
    if stack_exists "$stack_name"; then
      stacks+=("$stack_name")
    fi
  done

  if [ "${#stacks[@]}" -eq 0 ]; then
    return 0
  fi

  echo "Destroying stacks: ${stacks[*]}"
  cd "$PROJECT_DIR"
  activate_venv
  cdk destroy "${stacks[@]}" --force --exclusively
}

destroy_agentcore_stack() {
  local status=""
  local sg_logical_id=""

  if ! stack_exists "$STACK_AGENTCORE"; then
    return 0
  fi

  echo "Destroying stack: $STACK_AGENTCORE"
  cd "$PROJECT_DIR"
  activate_venv

  set +e
  cdk destroy "$STACK_AGENTCORE" --force --exclusively
  local destroy_exit=$?
  set -e

  if [ "$destroy_exit" -eq 0 ]; then
    return 0
  fi

  if ! stack_exists "$STACK_AGENTCORE"; then
    return 0
  fi

  status=$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_AGENTCORE" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || true)

  if [ "$status" = "DELETE_FAILED" ]; then
    sg_logical_id=$(get_stack_resource_id \
      "$STACK_AGENTCORE" \
      "StackResources[?ResourceType=='AWS::EC2::SecurityGroup' && contains(LogicalResourceId, 'AgentRuntimeSecurityGroup')].LogicalResourceId | [0]")

    if [ -n "$sg_logical_id" ] && [ "$sg_logical_id" != "None" ]; then
      echo "$STACK_AGENTCORE is stuck on AgentCore security group cleanup; retaining $sg_logical_id and retrying stack deletion."
      aws cloudformation delete-stack \
        --region "$REGION" \
        --stack-name "$STACK_AGENTCORE" \
        --retain-resources "$sg_logical_id"
      wait_for_stack_delete "$STACK_AGENTCORE"
      return 0
    fi
  fi

  return "$destroy_exit"
}

delete_user_files_bucket() {
  local bucket_name
  bucket_name="$(with_suffix "openclaw-user-files-${ACCOUNT}-${REGION}")"
  local key
  local version_id

  if ! aws s3api head-bucket --bucket "$bucket_name" 2>/dev/null; then
    echo "User files bucket not found: $bucket_name"
    return 0
  fi

  echo "Deleting retained user files bucket: $bucket_name"

  while IFS=$'\t' read -r key version_id; do
    [ -n "$key" ] || continue
    if [ -n "${version_id:-}" ] && [ "$version_id" != "None" ]; then
      aws s3api delete-object --bucket "$bucket_name" --key "$key" --version-id "$version_id" >/dev/null
    else
      aws s3api delete-object --bucket "$bucket_name" --key "$key" >/dev/null
    fi
  done < <(
    aws s3api list-object-versions \
      --bucket "$bucket_name" \
      --query 'Versions[].[Key,VersionId]' \
      --output text 2>/dev/null
  )

  while IFS=$'\t' read -r key version_id; do
    [ -n "$key" ] || continue
    if [ -n "${version_id:-}" ] && [ "$version_id" != "None" ]; then
      aws s3api delete-object --bucket "$bucket_name" --key "$key" --version-id "$version_id" >/dev/null
    else
      aws s3api delete-object --bucket "$bucket_name" --key "$key" >/dev/null
    fi
  done < <(
    aws s3api list-object-versions \
      --bucket "$bucket_name" \
      --query 'DeleteMarkers[].[Key,VersionId]' \
      --output text 2>/dev/null
  )

  aws s3 rm "s3://$bucket_name" --recursive >/dev/null 2>&1 || true
  aws s3api delete-bucket --bucket "$bucket_name" --region "$REGION"
}

use_project_node
ensure_python_venv
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

ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${CDK_DEFAULT_REGION:-}"
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

preflight

VPC_ID=""
if stack_exists "$STACK_VPC"; then
  VPC_ID=$(get_stack_resource_id "$STACK_VPC" "StackResources[?ResourceType=='AWS::EC2::VPC'].PhysicalResourceId | [0]")
fi

echo "=== OpenClaw CDK Undeploy ==="
echo "  Account:                  $ACCOUNT"
echo "  Region:                   $REGION"
if [ -n "$OPENCLAW_ENV_SUFFIX" ]; then
  echo "  Env suffix:               $OPENCLAW_ENV_SUFFIX"
else
  echo "  Env suffix:               (none)"
fi
echo "  Destroy security stack:   $DESTROY_SECURITY"
echo "  Delete user files bucket: $DELETE_USER_FILES_BUCKET"
echo "  Destroy timeout seconds:  $DESTROY_TIMEOUT_SECONDS"
echo ""

if ! aws cloudformation describe-stacks \
  --region "$REGION" \
  --query "Stacks[?starts_with(StackName, 'OpenClaw')].StackName" \
  --output text >/dev/null 2>&1; then
  echo "No matching OpenClaw stacks found in $REGION."
else
  stop_agentcore_runtime_sessions
  destroy_stack_group "$STACK_ROUTER" "$STACK_CRON" "$STACK_TOKEN_MONITORING"
  destroy_agentcore_stack
  destroy_stack_group "$STACK_GUARDRAILS" "$STACK_OBSERVABILITY"

  if stack_exists "$STACK_VPC"; then
    wait_for_agentcore_enis "$VPC_ID"
    destroy_stack_group "$STACK_VPC"
  fi

  if [ "$DESTROY_SECURITY" = true ]; then
    destroy_stack_group "$STACK_SECURITY"
  fi
fi

if [ "$DELETE_USER_FILES_BUCKET" = true ]; then
  delete_user_files_bucket
fi

echo ""
echo "Remaining OpenClaw stacks:"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --query "Stacks[?starts_with(StackName, 'OpenClaw')].[StackName,StackStatus]" \
  --output table
