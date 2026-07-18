#!/usr/bin/env bash
# deploy.sh — guarded CDK deployment utilities.
#
# AgentCore runtime creation/update is deliberately unavailable in v0. The
# previous toolkit path deployed mutable source first and only replaced it with
# the reviewed image afterward, so it could not satisfy the release boundary.
#
# Usage:
#   ./scripts/deploy.sh --full           # disabled until immutable runtime provisioning exists
#   ./scripts/deploy.sh --cdk-only       # CDK stacks only (skip toolkit)
#   ./scripts/deploy.sh --runtime-only   # disabled until immutable runtime provisioning exists
#   ./scripts/deploy.sh --phase1         # Phase 1 only
#   ./scripts/deploy.sh --phase3         # Phase 3 only (assumes runtime already deployed)
#
# Environment variables:
#   BUILD_MODE          local-build (default) or codebuild
#                       local-build: builds ARM64 container locally with Docker (recommended)
#                       codebuild: builds in AWS CodeBuild (no Docker required, adds cost)
#   PERSONAL_OPERATOR_DEPLOY_ACCOUNT exact allowed AWS account ID
#   PERSONAL_OPERATOR_DEPLOY_COMMIT  exact clean Git commit being released
#   PERSONAL_OPERATOR_DEPLOY_CONFIRMATION exact deploy:<account>:eu-west-1
#   PERSONAL_OPERATOR_RUNTIME_IMAGE_URI exact private-ECR bridge image digest;
#                       the digest must carry tag commit-<candidate> in target ECR
#   TRUSTED_LAMBDA_BUILD_IMAGE immutable reviewed Lambda builder digest
#   CDK_DEFAULT_REGION  AWS region; must be exactly eu-west-1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_CONTEXT_FILE="$PROJECT_DIR/build/runtime-context.json"

# --- Build mode ---
BUILD_MODE="${BUILD_MODE:-local-build}"
REQUIRED_REGION="eu-west-1"
MODE="${1:---full}"

if [ "$#" -gt 1 ]; then
  echo "ERROR: exactly one deployment mode is allowed." >&2
  exit 2
fi
case "$MODE" in
  --full|--phase1|--runtime-only|--phase3|--cdk-only) ;;
  *)
    echo "ERROR: unknown deployment mode: $MODE" >&2
    exit 2
    ;;
esac
case "$BUILD_MODE" in
  local-build|codebuild) ;;
  *)
    echo "ERROR: BUILD_MODE must be exactly local-build or codebuild; got $BUILD_MODE." >&2
    exit 2
    ;;
esac

runtime_deployment_unavailable() {
  echo "ERROR: immutable AgentCore runtime deployment is not implemented; no cloud changes were made." >&2
  echo "Build and review a direct immutable-image create/update path before enabling --full or --runtime-only." >&2
  exit 1
}

# Fail before account validation, credential discovery, preflight, or any cloud
# mutation. A full deploy must never leave a partially deployed foundation when
# its reviewed runtime cannot be provisioned safely.
case "$MODE" in
  --full|--runtime-only)
    runtime_deployment_unavailable
    ;;
esac

# --- Pre-flight checks ---
preflight() {
  local errors=0
  local actual_commit=""
  local sts_account=""
  local expected_confirmation="deploy:${ACCOUNT}:${REGION}"
  local runtime_image_metadata=""
  local web_acl_arn=""

  if ! command -v git &>/dev/null; then
    echo "ERROR: git is required to bind deployment to a reviewed commit."
    errors=$((errors + 1))
  else
    actual_commit=$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)
    if [ -z "$PERSONAL_OPERATOR_DEPLOY_COMMIT" ] || [ "$PERSONAL_OPERATOR_DEPLOY_COMMIT" != "$actual_commit" ]; then
      echo "ERROR: PERSONAL_OPERATOR_DEPLOY_COMMIT must equal current HEAD ($actual_commit)."
      errors=$((errors + 1))
    fi
    if [ -n "$(git -C "$PROJECT_DIR" status --porcelain 2>/dev/null)" ]; then
      echo "ERROR: deployment requires a completely clean Git worktree, including untracked files."
      errors=$((errors + 1))
    fi
  fi

  if [ "$PERSONAL_OPERATOR_DEPLOY_CONFIRMATION" != "$expected_confirmation" ]; then
    echo "ERROR: PERSONAL_OPERATOR_DEPLOY_CONFIRMATION must be exactly $expected_confirmation."
    errors=$((errors + 1))
  fi

  if [[ ! "$TRUSTED_LAMBDA_BUILD_IMAGE" =~ ^public\.ecr\.aws/lambda/python@sha256:[0-9a-f]{64}$ ]]; then
    echo "ERROR: TRUSTED_LAMBDA_BUILD_IMAGE must be an immutable reviewed official image digest."
    errors=$((errors + 1))
  fi

  if [[ "$PERSONAL_OPERATOR_RUNTIME_IMAGE_URI" =~ ^${ACCOUNT}\.dkr\.ecr\.${REGION}\.amazonaws\.com/([a-z0-9]+([._/-][a-z0-9]+)*)@(sha256:[0-9a-f]{64})$ ]]; then
    RUNTIME_IMAGE_REPOSITORY="${BASH_REMATCH[1]}"
    RUNTIME_IMAGE_DIGEST="${BASH_REMATCH[3]}"
  else
    echo "ERROR: runtime image must be an immutable ECR digest in account $ACCOUNT and region $REGION."
    errors=$((errors + 1))
  fi

  web_acl_arn=$(python3 - "$PROJECT_DIR/cdk.json" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["context"].get(
    "cloudfront_web_acl_arn", ""
)
print(value if isinstance(value, str) else "")
PY
)
  if [[ ! "$web_acl_arn" =~ ^arn:aws:wafv2:us-east-1:${ACCOUNT}:global/webacl/[A-Za-z0-9_-]{1,128}/[0-9a-f-]{36}$ ]]; then
    echo "ERROR: cdk.json cloudfront_web_acl_arn must be an exact global us-east-1 Web ACL ARN in account $ACCOUNT."
    errors=$((errors + 1))
  fi

  # AWS credentials
  if ! command -v aws &>/dev/null; then
    echo "ERROR: AWS CLI is required."
    errors=$((errors + 1))
  elif ! sts_account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
    echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
    errors=$((errors + 1))
  elif [ "$sts_account" != "$ACCOUNT" ]; then
    echo "ERROR: PERSONAL_OPERATOR_DEPLOY_ACCOUNT must match the authenticated STS account; expected $ACCOUNT, got $sts_account."
    errors=$((errors + 1))
  fi

  if [ -n "$RUNTIME_IMAGE_REPOSITORY" ] && [ -n "$RUNTIME_IMAGE_DIGEST" ] && \
     [ -n "$PERSONAL_OPERATOR_DEPLOY_COMMIT" ] && [ "$sts_account" = "$ACCOUNT" ]; then
    if ! runtime_image_metadata=$(aws ecr describe-images \
      --repository-name "$RUNTIME_IMAGE_REPOSITORY" \
      --image-ids "imageDigest=$RUNTIME_IMAGE_DIGEST" \
      --region "$REGION" --output json 2>/dev/null); then
      echo "ERROR: immutable runtime image is unavailable in the exact target ECR repository."
      errors=$((errors + 1))
    elif ! RUNTIME_IMAGE_METADATA_JSON="$runtime_image_metadata" python3 - \
      "$PERSONAL_OPERATOR_DEPLOY_COMMIT" "$RUNTIME_IMAGE_DIGEST" <<'PY'
import json
import os
import sys

commit, expected_digest = sys.argv[1:]
try:
    value = json.loads(os.environ["RUNTIME_IMAGE_METADATA_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    raise SystemExit(f"runtime image metadata is invalid: {error}")
details = value.get("imageDetails") if isinstance(value, dict) else None
if not isinstance(details, list) or len(details) != 1:
    raise SystemExit("runtime image metadata must contain one exact image")
detail = details[0]
if not isinstance(detail, dict) or detail.get("imageDigest") != expected_digest:
    raise SystemExit("runtime image digest differs from the reviewed candidate")
tags = detail.get("imageTags")
expected_tag = f"commit-{commit}"
if not isinstance(tags, list) or expected_tag not in tags:
    raise SystemExit("runtime image is not tagged for the exact candidate commit")
PY
    then
      echo "ERROR: immutable runtime image is not bound to the exact candidate commit."
      errors=$((errors + 1))
    fi
  fi

  # CDK CLI
  if ! command -v cdk &>/dev/null; then
    echo "ERROR: AWS CDK CLI not found. Install with: npm install -g aws-cdk"
    errors=$((errors + 1))
  fi

  if ! command -v npm &>/dev/null; then
    echo "ERROR: npm is required to test and build the trusted browser asset."
    errors=$((errors + 1))
  fi

  # Python venv
  if [ ! -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    echo "ERROR: Python venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    errors=$((errors + 1))
  fi

  # Docker is always required for the trusted ARM64 Lambda asset. The runtime
  # image may use CodeBuild, but provider dependencies must still be built and
  # import-verified in the exact Lambda Python 3.13 ARM64 image first.
  if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker not found (required for the trusted Lambda asset)."
    errors=$((errors + 1))
  elif ! docker info &>/dev/null 2>&1; then
    echo "ERROR: Docker daemon not running; trusted Lambda packaging fails closed."
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

ACCOUNT="${PERSONAL_OPERATOR_DEPLOY_ACCOUNT:-}"
if [[ ! "$ACCOUNT" =~ ^[0-9]{12}$ ]]; then
  echo "ERROR: PERSONAL_OPERATOR_DEPLOY_ACCOUNT must be an explicit 12-digit account ID." >&2
  exit 1
fi
if [ -n "${CDK_DEFAULT_ACCOUNT:-}" ] && [ "$CDK_DEFAULT_ACCOUNT" != "$ACCOUNT" ]; then
  echo "ERROR: CDK_DEFAULT_ACCOUNT differs from PERSONAL_OPERATOR_DEPLOY_ACCOUNT." >&2
  exit 1
fi

PERSONAL_OPERATOR_DEPLOY_COMMIT="${PERSONAL_OPERATOR_DEPLOY_COMMIT:-}"
PERSONAL_OPERATOR_DEPLOY_CONFIRMATION="${PERSONAL_OPERATOR_DEPLOY_CONFIRMATION:-}"
PERSONAL_OPERATOR_RUNTIME_IMAGE_URI="${PERSONAL_OPERATOR_RUNTIME_IMAGE_URI:-}"
TRUSTED_LAMBDA_BUILD_IMAGE="${TRUSTED_LAMBDA_BUILD_IMAGE:-}"
RUNTIME_IMAGE_REPOSITORY=""
RUNTIME_IMAGE_DIGEST=""
export TRUSTED_LAMBDA_BUILD_IMAGE

export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$REGION"

# Run pre-flight checks
preflight

echo "=== OpenClaw Hybrid Deploy ==="
echo "  Account:    $ACCOUNT"
echo "  Region:     $REGION"
echo "  Build mode: $BUILD_MODE"
echo ""

activate_venv() {
  if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.venv/bin/activate"
  fi
}

prepare_trusted_assets() {
  echo "--- Building trusted browser and Lambda assets ---"
  cd "$PROJECT_DIR"
  npm --prefix web ci --ignore-scripts
  npm --prefix web test
  npm --prefix web run build
  "$PROJECT_DIR/scripts/build-trusted-lambda-asset.sh" build
  "$PROJECT_DIR/scripts/build-trusted-lambda-asset.sh" verify
}

assert_release_binding() {
  local actual_commit
  actual_commit=$(git -C "$PROJECT_DIR" rev-parse HEAD)
  if [ "$actual_commit" != "$PERSONAL_OPERATOR_DEPLOY_COMMIT" ]; then
    echo "ERROR: HEAD changed after deployment preflight." >&2
    exit 1
  fi
  if [ -n "$(git -C "$PROJECT_DIR" status --porcelain)" ]; then
    echo "ERROR: tracked or untracked source changed after deployment preflight." >&2
    exit 1
  fi
}

# --- Phase 1: CDK foundation stacks ---
phase1_cdk() {
  echo "=== Phase 1: CDK foundation stacks ==="
  cd "$PROJECT_DIR"
  activate_venv
  assert_release_binding
  prepare_trusted_assets

  cdk deploy \
    OpenClawVpc \
    OpenClawSecurity \
    OpenClawGuardrails \
    OpenClawAgentCore \
    OpenClawObservability \
    --require-approval broadening

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
  local expected_runtime_image_uri="$3"
  RUNTIME_METADATA_JSON="$runtime_metadata_json" python3 - \
    "$ACCOUNT" "$REGION" "$EXECUTION_ROLE_ARN" "$candidate_runtime_id" \
    "$expected_runtime_image_uri" <<'PY'
import json
import os
import re
import sys

(
    account,
    region,
    expected_role_arn,
    candidate_runtime_id,
    expected_runtime_image_uri,
) = sys.argv[1:]


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
actual_runtime_version = runtime.get("agentRuntimeVersion", "")
if runtime_id_pattern.fullmatch(candidate_runtime_id) is None:
    fail(f"toolkit returned a noncanonical runtime ID: {candidate_runtime_id}")
if actual_runtime_id != candidate_runtime_id:
    fail(
        "GetAgentRuntime ID does not match the toolkit runtime ID: "
        f"{actual_runtime_id} != {candidate_runtime_id}"
    )
if runtime_arn_pattern.fullmatch(actual_runtime_arn) is None:
    fail("GetAgentRuntime returned a noncanonical runtime ARN")
if re.fullmatch(r"[1-9][0-9]{0,4}", actual_runtime_version) is None:
    fail("GetAgentRuntime returned a noncanonical runtime version")
if not actual_runtime_arn.endswith(f":{actual_runtime_version}"):
    fail("GetAgentRuntime ARN is not bound to its reported version")
if runtime.get("status") != "READY":
    fail(f"AgentCore runtime is not READY: {runtime.get('status', '<missing>')}")
if runtime.get("roleArn") != expected_role_arn:
    fail("AgentCore runtime execution role does not match the deployed role")
expected_artifact = {
    "containerConfiguration": {"containerUri": expected_runtime_image_uri}
}
if runtime.get("agentRuntimeArtifact") != expected_artifact:
    fail("AgentCore runtime artifact is not the reviewed immutable image digest")

print(actual_runtime_id)
print(actual_runtime_arn)
print(actual_runtime_version)
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

# AgentCore runtime provisioning is intentionally absent. The only former
# implementation deployed mutable source before applying the reviewed digest.
# --full and --runtime-only fail above, before preflight or any cloud call.

# --- Phase 3: CDK dependent stacks ---
phase3_cdk() {
  echo "=== Phase 3: CDK dependent stacks ==="
  cd "$PROJECT_DIR"
  activate_venv
  assert_release_binding
  prepare_trusted_assets
  read_cdk_outputs

  # AgentCoreStack validates all exact runtime fields again. This parser also
  # refuses context emitted for any other source commit, account, or region.
  RUNTIME_CONTEXT=$(python3 - "$RUNTIME_CONTEXT_FILE" \
    "$PERSONAL_OPERATOR_DEPLOY_COMMIT" "$ACCOUNT" "$REGION" \
    "$PERSONAL_OPERATOR_RUNTIME_IMAGE_URI" <<'PY'
import json
import pathlib
import re
import sys

path, commit, account, region, expected_runtime_image_uri = sys.argv[1:]
try:
    value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"runtime context is unavailable: {type(error).__name__}")
if not isinstance(value, dict) or set(value) != {
    "schema", "sourceCommit", "account", "region", "runtimeId",
    "runtimeEndpointId", "runtimeEndpointName", "runtimeArn",
    "runtimeVersion", "runtimeImageUri",
}:
    raise SystemExit("runtime context has the wrong fields")
if value.get("schema") != "personal-operator.runtime-context.v3":
    raise SystemExit("runtime context schema is invalid")
if (value.get("sourceCommit"), value.get("account"), value.get("region")) != (
    commit, account, region
):
    raise SystemExit("runtime context is not bound to this release")
if value.get("runtimeImageUri") != expected_runtime_image_uri:
    raise SystemExit("runtime context is not bound to the reviewed release image")
identifier = r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}"
if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
    raise SystemExit("release commit is invalid")
if re.fullmatch(identifier, value.get("runtimeId", "")) is None:
    raise SystemExit("runtime ID is invalid")
if re.fullmatch(identifier, value.get("runtimeEndpointId", "")) is None:
    raise SystemExit("runtime endpoint ID is invalid")
if value.get("runtimeEndpointName") != f"release_{commit}":
    raise SystemExit("runtime endpoint name is not bound to the release commit")
runtime_version = value.get("runtimeVersion", "")
if re.fullmatch(r"[1-9][0-9]{0,4}", runtime_version) is None:
    raise SystemExit("runtime version is invalid")
arn = (
    rf"arn:aws:bedrock-agentcore:{re.escape(region)}:{re.escape(account)}:agent/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
)
if re.fullmatch(arn, value.get("runtimeArn", "")) is None:
    raise SystemExit("runtime ARN is invalid")
if value.get("runtimeArn", "").rsplit(":", 1)[-1] != runtime_version:
    raise SystemExit("runtime context ARN is not bound to its runtime version")
image_uri = (
    rf"{re.escape(account)}\.dkr\.ecr\."
    rf"{re.escape(region)}\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*"
    r"@sha256:[0-9a-f]{64}"
)
if re.fullmatch(image_uri, value.get("runtimeImageUri", "")) is None:
    raise SystemExit("runtime image URI is invalid")
print(value["runtimeId"])
print(value["runtimeEndpointId"])
print(value["runtimeEndpointName"])
print(value["runtimeVersion"])
print(value["runtimeArn"])
print(value["runtimeImageUri"])
PY
)
  RUNTIME_ID=$(printf '%s\n' "$RUNTIME_CONTEXT" | sed -n '1p')
  RUNTIME_ENDPOINT_ID=$(printf '%s\n' "$RUNTIME_CONTEXT" | sed -n '2p')
  RUNTIME_ENDPOINT_NAME=$(printf '%s\n' "$RUNTIME_CONTEXT" | sed -n '3p')
  RUNTIME_VERSION=$(printf '%s\n' "$RUNTIME_CONTEXT" | sed -n '4p')
  RUNTIME_ARN=$(printf '%s\n' "$RUNTIME_CONTEXT" | sed -n '5p')
  RUNTIME_IMAGE_URI=$(printf '%s\n' "$RUNTIME_CONTEXT" | sed -n '6p')

  # Re-fetch the exact immutable version before CDK can wire any consumer.
  # The release-specific endpoint may coexist with newer runtime versions, but
  # its ID, name, liveVersion, and targetVersion must remain on this version.
  if [ "$RUNTIME_VERSION" != "${RUNTIME_ARN##*:}" ]; then
    echo "ERROR: runtime context version differs from its exact ARN." >&2
    exit 1
  fi
  RUNTIME_METADATA_JSON=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" \
    --agent-runtime-version "$RUNTIME_VERSION" \
    --region "$REGION" --output json)
  validate_runtime_metadata \
    "$RUNTIME_METADATA_JSON" "$RUNTIME_ID" "$RUNTIME_IMAGE_URI" >/dev/null
  python3 "$PROJECT_DIR/scripts/verify-agentcore-storage.py" \
    --runtime-id "$RUNTIME_ID" \
    --endpoint-id "$RUNTIME_ENDPOINT_ID" \
    --endpoint-name "$RUNTIME_ENDPOINT_NAME" \
    --runtime-arn "$RUNTIME_ARN" \
    --execution-role-arn "$EXECUTION_ROLE_ARN" \
    --bucket "$USER_FILES_BUCKET" \
    --kms-key-arn "$CMK_ARN"

  cdk deploy \
    OpenClawRouter \
    PersonalOperatorWeb \
    OpenClawCron \
    -c "runtime_source_commit=$PERSONAL_OPERATOR_DEPLOY_COMMIT" \
    -c "runtime_id=$RUNTIME_ID" \
    -c "runtime_endpoint_id=$RUNTIME_ENDPOINT_ID" \
    -c "runtime_endpoint_name=$RUNTIME_ENDPOINT_NAME" \
    -c "runtime_version=$RUNTIME_VERSION" \
    -c "runtime_arn=$RUNTIME_ARN" \
    -c "runtime_image_uri=$RUNTIME_IMAGE_URI" \
    --require-approval broadening

  echo "  Phase 3 complete."
  echo ""
}

case "$MODE" in
  --full)
    runtime_deployment_unavailable
    ;;
  --phase1)
    phase1_cdk
    ;;
  --runtime-only)
    runtime_deployment_unavailable
    ;;
  --phase3)
    phase3_cdk
    ;;
  --cdk-only)
    phase1_cdk
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
