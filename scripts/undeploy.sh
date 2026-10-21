#!/usr/bin/env bash
# undeploy.sh — tear down an OpenClaw deployment (CloudFormation stacks plus the
# SDK-owned AgentCore Runtime), asking for confirmation first.
#
# Usage:
#   ./scripts/undeploy.sh [--env NAME] [--all] [--delete-user-files-bucket] [--manifest PATH] [--yes]
#
# Order: TokenMonitoring/Cron/Router -> AgentCore Runtime (endpoints, then the
# runtime) -> AgentCore/Guardrails/Observability/Gateway -> wait for AgentCore
# ENIs -> Vpc -> (with --all) Security -> (with --delete-user-files-bucket) the
# user-files bucket.
#
# Kept unless noted: the user-files S3 bucket, DynamoDB tables (identity, token
# usage), KMS key, Cognito user pool, API access log group, VPC flow log group
# and the ECR image repository. These have RemovalPolicy RETAIN or are not
# owned by a stack.
#
# Runs on the macOS system bash (3.2): no bash 4+ features.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/undeploy.sh [--env NAME] [--all] [--delete-user-files-bucket] [--manifest PATH] [--yes]

  --env NAME                  Load .env.NAME and tear down the stacks suffixed with -NAME.
  --all                       Also delete the Security stack (secrets are scheduled for deletion;
                              the KMS key and Cognito user pool are retained).
  --delete-user-files-bucket  Also permanently delete the user-files bucket and every object version
                              in it (all users' files and workspace state). Asks separately.
  --manifest PATH             Runtime manifest (default .runtime/runtime-v2.json, or
                              .runtime/runtime-v2-NAME.json with --env NAME).
  --yes                       Do not prompt (for automation). Without it, a terminal is required.
  -h, --help                  Show this help.

Environment:
  DESTROY_TIMEOUT_SECONDS     Max seconds to wait for each deletion / ENI release (default 900)
  POLL_INTERVAL_SECONDS       Poll interval while waiting (default 15)
EOF
}

ENV_NAME="${OPENCLAW_ENV_NAME:-}"
DESTROY_SECURITY=false
DELETE_USER_FILES_BUCKET=false
ASSUME_YES=false
MANIFEST=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENV_NAME="${2:?--env requires a name}"; shift 2 ;;
    --env=*) ENV_NAME="${1#*=}"; [ -n "$ENV_NAME" ] || { echo "ERROR: --env requires a name" >&2; exit 1; }; shift ;;
    --all) DESTROY_SECURITY=true; shift ;;
    --delete-user-files-bucket) DELETE_USER_FILES_BUCKET=true; shift ;;
    --manifest) MANIFEST="${2:?--manifest requires a path}"; shift 2 ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option $1" >&2; usage >&2; exit 1 ;;
  esac
done

DESTROY_TIMEOUT_SECONDS="${DESTROY_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-15}"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/openclaw-env.sh"
load_project_env "$PROJECT_DIR" "$ENV_NAME"
apply_named_environment "$ENV_NAME"
OPENCLAW_ENV_SUFFIX="$(resolve_env_suffix "$PROJECT_DIR")"
export OPENCLAW_ENV_SUFFIX

for tool in aws python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: $tool is required." >&2
    exit 1
  fi
done

cd "$PROJECT_DIR"
REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-}}"
if [ -z "$REGION" ]; then
  REGION="$(python3 -c 'import json; print(json.load(open("cdk.json"))["context"].get("region", "") or "")')"
fi
if [ -z "$REGION" ]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [ -z "$REGION" ]; then
  echo "ERROR: set CDK_DEFAULT_REGION, the cdk.json region, or an AWS CLI default region." >&2
  exit 1
fi
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

STACK_VPC=$(with_suffix OpenClawVpc)
STACK_SECURITY=$(with_suffix OpenClawSecurity)
STACK_GUARDRAILS=$(with_suffix OpenClawGuardrails)
STACK_GATEWAY=$(with_suffix OpenClawGateway)
STACK_AGENTCORE=$(with_suffix OpenClawAgentCore)
STACK_OBSERVABILITY=$(with_suffix OpenClawObservability)
STACK_ROUTER=$(with_suffix OpenClawRouter)
STACK_CRON=$(with_suffix OpenClawCron)
STACK_TOKEN_MONITORING=$(with_suffix OpenClawTokenMonitoring)
RUNTIME_NAME=$(with_suffix openclaw_agent_v2 _)
USER_FILES_BUCKET=$(with_suffix "openclaw-user-files-${ACCOUNT}-${REGION}")
if [ -z "$MANIFEST" ]; then
  MANIFEST=".runtime/$(with_suffix runtime-v2).json"
fi
ENV_LABEL="${OPENCLAW_ENV_SUFFIX:-default}"

stack_status() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true
}

stack_exists() {
  local status
  status="$(stack_status "$1")"
  [ -n "$status" ] && [ "$status" != "DELETE_COMPLETE" ]
}

# The runtime is created by scripts/runtime_deploy.py, not by a stack. Prefer the
# ID recorded in the manifest; otherwise look it up by this environment's name.
RUNTIME_ID=""
if [ -f "$MANIFEST" ]; then
  RUNTIME_ID="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("agentRuntimeId", "") or "")' "$MANIFEST")"
fi
if [ -z "$RUNTIME_ID" ]; then
  RUNTIME_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeId | [0]" \
    --output text 2>/dev/null || true)"
  if [ "$RUNTIME_ID" = "None" ]; then RUNTIME_ID=""; fi
fi

PLAN_STACKS=()
for stack in "$STACK_TOKEN_MONITORING" "$STACK_CRON" "$STACK_ROUTER" "$STACK_AGENTCORE" \
             "$STACK_GATEWAY" "$STACK_GUARDRAILS" "$STACK_OBSERVABILITY" "$STACK_VPC"; do
  if stack_exists "$stack"; then PLAN_STACKS+=("$stack"); fi
done
if [ "$DESTROY_SECURITY" = true ] && stack_exists "$STACK_SECURITY"; then
  PLAN_STACKS+=("$STACK_SECURITY")
fi

echo "=== OpenClaw undeploy ==="
echo "  Account:      $ACCOUNT"
echo "  Region:       $REGION"
echo "  Environment:  $ENV_LABEL"
if [ -n "${OPENCLAW_SELECTED_ENV_FILE:-}" ] && [ -f "${OPENCLAW_SELECTED_ENV_FILE}" ]; then
  echo "  Env file:     $OPENCLAW_SELECTED_ENV_FILE"
fi
echo ""
echo "This will delete:"
if [ -n "$RUNTIME_ID" ]; then
  echo "  - AgentCore Runtime $RUNTIME_ID and its endpoints (all user sessions end)"
fi
if [ "${#PLAN_STACKS[@]}" -gt 0 ]; then
  for stack in "${PLAN_STACKS[@]}"; do echo "  - stack $stack"; done
fi
if [ "$DELETE_USER_FILES_BUCKET" = true ]; then
  echo "  - bucket $USER_FILES_BUCKET and EVERY object version in it (cannot be undone)"
fi
if [ -z "$RUNTIME_ID" ] && [ "${#PLAN_STACKS[@]}" -eq 0 ] && [ "$DELETE_USER_FILES_BUCKET" != true ]; then
  echo "  (nothing found for environment '$ENV_LABEL' in $REGION)"
  exit 0
fi
if [ "$DESTROY_SECURITY" != true ]; then
  echo "Kept: $STACK_SECURITY (use --all to delete it)."
fi
echo ""

confirm() {
  local prompt="$1" expected="$2" answer=""
  if [ "$ASSUME_YES" = true ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "ERROR: no terminal to confirm on; re-run with --yes to proceed without a prompt." >&2
    exit 1
  fi
  read -r -p "$prompt" answer
  if [ "$answer" != "$expected" ]; then
    echo "Aborted: nothing was deleted." >&2
    exit 1
  fi
}

confirm "Type '$ENV_LABEL' to delete the resources above: " "$ENV_LABEL"
if [ "$DELETE_USER_FILES_BUCKET" = true ]; then
  confirm "Type the bucket name to permanently delete it and all object versions: " "$USER_FILES_BUCKET"
fi

wait_for_stack_delete() {
  local stack_name="$1" status="" waited=0
  while [ "$waited" -lt "$DESTROY_TIMEOUT_SECONDS" ]; do
    status="$(stack_status "$stack_name")"
    case "$status" in
      ""|DELETE_COMPLETE) echo "Deleted stack $stack_name"; return 0 ;;
      DELETE_FAILED) echo "ERROR: stack $stack_name is DELETE_FAILED" >&2; return 1 ;;
    esac
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done
  echo "ERROR: timed out waiting for $stack_name to delete" >&2
  return 1
}

delete_stack() {
  local stack_name="$1"
  if ! stack_exists "$stack_name"; then
    return 0
  fi
  echo "Deleting stack $stack_name ..."
  aws cloudformation delete-stack --region "$REGION" --stack-name "$stack_name"
  wait_for_stack_delete "$stack_name"
}

delete_runtime() {
  local endpoints endpoint waited=0
  if [ -z "$RUNTIME_ID" ]; then
    return 0
  fi
  endpoints="$(aws bedrock-agentcore-control list-agent-runtime-endpoints --region "$REGION" \
    --agent-runtime-id "$RUNTIME_ID" --query "runtimeEndpoints[?name!='DEFAULT'].name" --output text)"
  for endpoint in $endpoints; do
    [ "$endpoint" != "None" ] || continue
    echo "Deleting AgentCore Runtime endpoint $endpoint ..."
    aws bedrock-agentcore-control delete-agent-runtime-endpoint --region "$REGION" \
      --agent-runtime-id "$RUNTIME_ID" --endpoint-name "$endpoint" >/dev/null
  done
  echo "Deleting AgentCore Runtime $RUNTIME_ID ..."
  aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
    --agent-runtime-id "$RUNTIME_ID" >/dev/null
  while [ "$waited" -lt "$DESTROY_TIMEOUT_SECONDS" ]; do
    if ! aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
        --agent-runtime-id "$RUNTIME_ID" --query status --output text >/dev/null 2>&1; then
      echo "Deleted AgentCore Runtime $RUNTIME_ID"
      if [ -f "$MANIFEST" ]; then
        mv "$MANIFEST" "$MANIFEST.deleted"
        echo "Moved $MANIFEST to $MANIFEST.deleted"
      fi
      return 0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done
  echo "ERROR: timed out waiting for AgentCore Runtime $RUNTIME_ID to delete" >&2
  return 1
}

# A failed AgentCore stack deletion is usually its security group, still held by
# runtime/browser ENIs that AgentCore releases late. Retry keeping the group.
delete_agentcore_stack() {
  local sg_logical_id=""
  if ! stack_exists "$STACK_AGENTCORE"; then
    return 0
  fi
  if delete_stack "$STACK_AGENTCORE"; then
    return 0
  fi
  sg_logical_id="$(aws cloudformation describe-stack-resources --region "$REGION" \
    --stack-name "$STACK_AGENTCORE" \
    --query "StackResources[?ResourceType=='AWS::EC2::SecurityGroup' && starts_with(LogicalResourceId, 'AgentRuntimeSecurityGroup')].LogicalResourceId | [0]" \
    --output text 2>/dev/null || true)"
  if [ -z "$sg_logical_id" ] || [ "$sg_logical_id" = "None" ]; then
    return 1
  fi
  echo "Retrying $STACK_AGENTCORE deletion, retaining security group $sg_logical_id (delete it once its ENIs are gone)."
  aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_AGENTCORE" \
    --retain-resources "$sg_logical_id"
  wait_for_stack_delete "$STACK_AGENTCORE"
}

# Runtime and browser ENIs use the AgentCore stack's security group; AgentCore
# releases them some minutes after the runtime is deleted.
wait_for_agentcore_enis() {
  local vpc_id="$1" waited=0 enis=""
  if [ -z "$vpc_id" ] || [ "$vpc_id" = "None" ] || [ -z "$AGENT_SG_ID" ] || [ "$AGENT_SG_ID" = "None" ]; then
    return 0
  fi
  while [ "$waited" -lt "$DESTROY_TIMEOUT_SECONDS" ]; do
    enis="$(aws ec2 describe-network-interfaces --region "$REGION" \
      --filters "Name=vpc-id,Values=$vpc_id" "Name=group-id,Values=$AGENT_SG_ID" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || true)"
    if [ -z "$enis" ] || [ "$enis" = "None" ]; then
      return 0
    fi
    echo "Waiting for AgentCore to release ENIs in $vpc_id: $enis"
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done
  echo "ERROR: AgentCore ENIs still present in $vpc_id after ${DESTROY_TIMEOUT_SECONDS}s: $enis" >&2
  return 1
}

delete_user_files_bucket() {
  local key version_id
  if ! aws s3api head-bucket --bucket "$USER_FILES_BUCKET" >/dev/null 2>&1; then
    echo "User files bucket not found: $USER_FILES_BUCKET"
    return 0
  fi
  echo "Deleting every object version in $USER_FILES_BUCKET (one request per version; a large"
  echo "bucket is faster to empty with a lifecycle expiration rule first) ..."
  for listing in Versions DeleteMarkers; do
    while IFS=$'\t' read -r key version_id; do
      [ -n "$key" ] && [ "$key" != "None" ] || continue
      aws s3api delete-object --bucket "$USER_FILES_BUCKET" --key "$key" \
        --version-id "$version_id" >/dev/null
    done < <(aws s3api list-object-versions --bucket "$USER_FILES_BUCKET" \
               --query "${listing}[].[Key,VersionId]" --output text)
  done
  aws s3api delete-bucket --bucket "$USER_FILES_BUCKET" --region "$REGION"
  echo "Deleted bucket $USER_FILES_BUCKET"
}

AGENT_SG_ID=""
if stack_exists "$STACK_AGENTCORE"; then
  AGENT_SG_ID="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_AGENTCORE" \
    --query "Stacks[0].Outputs[?OutputKey=='SecurityGroupId'].OutputValue | [0]" --output text 2>/dev/null || true)"
fi
VPC_ID=""
if stack_exists "$STACK_VPC"; then
  VPC_ID="$(aws cloudformation describe-stack-resources --region "$REGION" --stack-name "$STACK_VPC" \
    --query "StackResources[?ResourceType=='AWS::EC2::VPC'].PhysicalResourceId | [0]" --output text)"
fi

# TokenMonitoring filters the Cron log group, so it goes first.
delete_stack "$STACK_TOKEN_MONITORING"
delete_stack "$STACK_CRON"
delete_stack "$STACK_ROUTER"
delete_runtime
delete_agentcore_stack
delete_stack "$STACK_GATEWAY"
delete_stack "$STACK_GUARDRAILS"
delete_stack "$STACK_OBSERVABILITY"
if stack_exists "$STACK_VPC"; then
  wait_for_agentcore_enis "$VPC_ID"
  delete_stack "$STACK_VPC"
fi
if [ "$DESTROY_SECURITY" = true ]; then
  delete_stack "$STACK_SECURITY"
fi
if [ "$DELETE_USER_FILES_BUCKET" = true ]; then
  delete_user_files_bucket
fi

echo ""
echo "Undeploy of environment '$ENV_LABEL' complete."
echo "Retained: DynamoDB tables, KMS key, Cognito user pool, API access and VPC flow log groups,"
echo "ECR repository $(with_suffix openclaw-bridge)$( [ "$DELETE_USER_FILES_BUCKET" = true ] || printf ', bucket %s' "$USER_FILES_BUCKET")."
