#!/usr/bin/env bash
# Full current-product E2E gate: canonical deploy -> validate -> reset -> test.
# Usage: ./scripts/e2e-deploy-and-test.sh [--skip-deploy] [--test-filter PATTERN]
set -euo pipefail

REQUIRED_REGION="eu-west-1"
REQUIRED_MODEL_ID="eu.anthropic.claude-sonnet-4-6"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_DEPLOY=false
TEST_FILTER=""

usage() {
  echo "Usage: $0 [--skip-deploy] [--test-filter PATTERN]" >&2
}

while (( $# > 0 )); do
  case "$1" in
    --skip-deploy)
      SKIP_DEPLOY=true
      shift
      ;;
    --test-filter)
      if (( $# < 2 )) || [ -z "$2" ]; then
        echo "ERROR: --test-filter requires a non-empty pytest expression." >&2
        usage
        exit 2
      fi
      TEST_FILTER="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

# Resolve and reject every explicit region before the first AWS call.
for region_variable in CDK_DEFAULT_REGION AWS_REGION AWS_DEFAULT_REGION; do
  configured_region="${!region_variable:-}"
  if [ -n "$configured_region" ] && [ "$configured_region" != "$REQUIRED_REGION" ]; then
    echo "ERROR: $region_variable must be exactly $REQUIRED_REGION; got $configured_region." >&2
    exit 1
  fi
done
REGION="${CDK_DEFAULT_REGION:-}"
if [ -z "$REGION" ]; then
  REGION=$(python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('region',''))")
fi
if [ -z "$REGION" ]; then
  REGION="$REQUIRED_REGION"
fi
if [ "$REGION" != "$REQUIRED_REGION" ]; then
  echo "ERROR: AWS region must be exactly $REQUIRED_REGION; got $REGION." >&2
  exit 1
fi

if [ -z "${E2E_TELEGRAM_USER_ID:-}" ] || [ -z "${E2E_TELEGRAM_CHAT_ID:-}" ]; then
  echo "ERROR: E2E_TELEGRAM_USER_ID and E2E_TELEGRAM_CHAT_ID must be set." >&2
  exit 1
fi
TG_CHAT_ID="$E2E_TELEGRAM_CHAT_ID"

read_context() {
  local key="$1"
  python3 -c "import json; print(json.load(open('$PROJECT_DIR/cdk.json'))['context'].get('$key',''))"
}

stack_output() {
  local stack_name="$1"
  local query="$2"
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$REGION" \
    --query "$query" \
    --output text
}

require_equal() {
  local label="$1"
  local expected="$2"
  local actual="$3"
  if [ -z "$actual" ] || [ "$actual" = "None" ] || [ "$actual" != "$expected" ]; then
    echo "ERROR: $label mismatch; expected $expected, got ${actual:-<empty>}." >&2
    exit 1
  fi
}

validate_deployed_runtime() {
  echo "--- Validating deployed runtime contract ---"

  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  if [[ ! "$ACCOUNT" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: Could not resolve a canonical 12-digit AWS account; got $ACCOUNT." >&2
    exit 1
  fi

  WORKSPACE_SESSION_ROLE_ARN=$(stack_output \
    OpenClawAgentCore \
    "Stacks[0].Outputs[?OutputKey=='WorkspaceSessionRoleArn'].OutputValue")
  EXECUTION_ROLE_ARN=$(stack_output \
    OpenClawAgentCore \
    "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue")
  USER_FILES_BUCKET=$(stack_output \
    OpenClawAgentCore \
    "Stacks[0].Outputs[?OutputKey=='UserFilesBucketName'].OutputValue")
  CMK_ARN=$(stack_output \
    OpenClawSecurity \
    "Stacks[0].Outputs[?contains(OutputKey,'SecretsCmk')].OutputValue")

  require_equal \
    "workspace session role output" \
    "arn:aws:iam::${ACCOUNT}:role/openclaw-workspace-session-role-eu-west-1" \
    "$WORKSPACE_SESSION_ROLE_ARN"
  require_equal \
    "execution role output" \
    "arn:aws:iam::${ACCOUNT}:role/openclaw-agentcore-execution-role-eu-west-1" \
    "$EXECUTION_ROLE_ARN"
  if [ -z "$USER_FILES_BUCKET" ] || [ "$USER_FILES_BUCKET" = "None" ]; then
    echo "ERROR: UserFilesBucketName output is missing." >&2
    exit 1
  fi
  if [[ ! "$CMK_ARN" =~ ^arn:aws:kms:eu-west-1:${ACCOUNT}:key/[A-Za-z0-9-]+$ ]]; then
    echo "ERROR: SecretsCmk output is missing or outside the canonical account/region." >&2
    exit 1
  fi

  RUNTIME_ID=$(read_context runtime_id)
  RUNTIME_ENDPOINT_ID=$(read_context runtime_endpoint_id)
  RUNTIME_ARN=$(read_context runtime_arn)
  MODEL_ID=$(read_context default_model_id)
  if [[ ! "$RUNTIME_ID" =~ ^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$ ]]; then
    echo "ERROR: cdk.json does not contain a deployed runtime_id." >&2
    exit 1
  fi
  if [[ ! "$RUNTIME_ENDPOINT_ID" =~ ^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$ ]]; then
    echo "ERROR: cdk.json does not contain a deployed runtime_endpoint_id." >&2
    exit 1
  fi
  if [[ ! "$RUNTIME_ARN" =~ ^arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:agent/[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}$ ]]; then
    echo "ERROR: cdk.json does not contain a canonical deployed runtime_arn." >&2
    exit 1
  fi
  require_equal "configured model" "$REQUIRED_MODEL_ID" "$MODEL_ID"

  ACTUAL_RUNTIME_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query agentRuntimeArn --output text)
  ACTUAL_RUNTIME_ID=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query agentRuntimeId --output text)
  ACTUAL_RUNTIME_STATUS=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query status --output text)
  ACTUAL_EXECUTION_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query roleArn --output text)
  ACTUAL_WORKSPACE_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query environmentVariables.WORKSPACE_SESSION_ROLE_ARN --output text)
  ACTUAL_REGION=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query environmentVariables.AWS_REGION --output text)
  ACTUAL_MODEL_ID=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query environmentVariables.BEDROCK_MODEL_ID --output text)
  ACTUAL_BUCKET=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query environmentVariables.S3_USER_FILES_BUCKET --output text)
  ACTUAL_CMK_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query environmentVariables.CMK_ARN --output text)
  ACTUAL_ENDPOINT_ID=$(aws bedrock-agentcore-control list-agent-runtime-endpoints \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query "runtimeEndpoints[?id=='${RUNTIME_ENDPOINT_ID}'].id | [0]" \
    --output text)

  require_equal "runtime ID" "$RUNTIME_ID" "$ACTUAL_RUNTIME_ID"
  require_equal "runtime ARN" "$RUNTIME_ARN" "$ACTUAL_RUNTIME_ARN"
  require_equal "runtime status" "READY" "$ACTUAL_RUNTIME_STATUS"
  if [[ ! "$ACTUAL_RUNTIME_ARN" =~ ^arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:agent/[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}$ ]]; then
    echo "ERROR: AgentCore returned a runtime ARN outside the canonical account/region." >&2
    exit 1
  fi
  require_equal "runtime execution role" "$EXECUTION_ROLE_ARN" "$ACTUAL_EXECUTION_ROLE_ARN"
  require_equal "runtime workspace role" "$WORKSPACE_SESSION_ROLE_ARN" "$ACTUAL_WORKSPACE_ROLE_ARN"
  require_equal "runtime region" "$REGION" "$ACTUAL_REGION"
  require_equal "runtime model" "$MODEL_ID" "$ACTUAL_MODEL_ID"
  require_equal "runtime workspace bucket" "$USER_FILES_BUCKET" "$ACTUAL_BUCKET"
  require_equal "runtime CMK" "$CMK_ARN" "$ACTUAL_CMK_ARN"
  require_equal "runtime endpoint" "$RUNTIME_ENDPOINT_ID" "$ACTUAL_ENDPOINT_ID"

  # AgentCore invocation and IAM authorization intentionally use different
  # resource grammars. Validate the deployed Router against both so an
  # agent/<uuid>:<version> ARN can never leak into the IAM policy and the
  # runtime/<runtime-id> ARN can never leak into the invocation request.
  RUNTIME_IAM_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${RUNTIME_ID}"
  ROUTER_RUNTIME_ARN=$(aws lambda get-function-configuration \
    --function-name openclaw-router --region "$REGION" \
    --query Environment.Variables.AGENTCORE_RUNTIME_ARN --output text)
  require_equal "router invocation ARN" "$RUNTIME_ARN" "$ROUTER_RUNTIME_ARN"

  ROUTER_ROLE_ARN=$(aws lambda get-function-configuration \
    --function-name openclaw-router --region "$REGION" \
    --query Role --output text)
  if [[ ! "$ROUTER_ROLE_ARN" =~ ^arn:aws:iam::${ACCOUNT}:role/[A-Za-z0-9+=,.@_/-]+$ ]]; then
    echo "ERROR: Router Lambda role ARN is missing or outside the canonical account." >&2
    exit 1
  fi
  ROUTER_ROLE_NAME=${ROUTER_ROLE_ARN##*/}
  ROUTER_POLICY_NAMES=$(aws iam list-role-policies \
    --role-name "$ROUTER_ROLE_NAME" --region "$REGION" \
    --query 'PolicyNames' --output text)
  if [ -z "$ROUTER_POLICY_NAMES" ] || [ "$ROUTER_POLICY_NAMES" = "None" ]; then
    echo "ERROR: Router Lambda has no inline IAM policy." >&2
    exit 1
  fi

  ROUTER_AGENTCORE_RESOURCES=""
  for policy_name in $ROUTER_POLICY_NAMES; do
    policy_resources=$(aws iam get-role-policy \
      --role-name "$ROUTER_ROLE_NAME" --policy-name "$policy_name" \
      --region "$REGION" \
      --query "PolicyDocument.Statement[?contains(Action, 'bedrock-agentcore:InvokeAgentRuntime')].Resource[]" \
      --output text)
    if [ -n "$policy_resources" ] && [ "$policy_resources" != "None" ]; then
      ROUTER_AGENTCORE_RESOURCES="${ROUTER_AGENTCORE_RESOURCES} ${policy_resources}"
    fi
  done
  ROUTER_AGENTCORE_RESOURCES=$(printf '%s\n' "$ROUTER_AGENTCORE_RESOURCES" \
    | tr '\t\n' '  ' | awk '{$1=$1; print}')
  EXPECTED_ROUTER_IAM_RESOURCES="${RUNTIME_IAM_ARN} ${RUNTIME_IAM_ARN}/*"
  require_equal \
    "router IAM runtime resources" \
    "$EXPECTED_ROUTER_IAM_RESOURCES" \
    "$ROUTER_AGENTCORE_RESOURCES"

  export WORKSPACE_SESSION_ROLE_ARN
  echo "  Runtime contract validated: $RUNTIME_ID / $RUNTIME_ENDPOINT_ID"
  echo "  Router ARN boundaries validated: invocation + IAM"
}

send_telegram() {
  local msg="$1"
  local token
  token=$(aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --region "$REGION" --query SecretString --output text 2>/dev/null) || return 0
  curl -s -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"text\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$msg"), \"parse_mode\": \"Markdown\"}" \
    > /dev/null
}

cd "$PROJECT_DIR"
echo "================================================"
echo " Personal Operator E2E Deploy + Test Gate"
echo " Region: $REGION"
echo "================================================"

if [ "$SKIP_DEPLOY" = false ]; then
  echo ""
  echo "--- Canonical full deployment ---"
  "$PROJECT_DIR/scripts/deploy.sh"
else
  echo "--- Skipping deploy; validating the existing deployment ---"
fi

# Validation deliberately happens after canonical deployment. In skip mode it
# is the mandatory precondition for touching sessions or running tests.
validate_deployed_runtime

if [ "$SKIP_DEPLOY" = false ]; then
  send_telegram "✅ *Deploy and runtime validation complete*\nResetting the E2E session."
fi

echo ""
echo "--- Resetting E2E session ---"
source "$PROJECT_DIR/.venv/bin/activate"
python3 - <<'PY'
import sys
sys.path.insert(0, ".")
from tests.e2e.config import load_config
from tests.e2e.session import reset_session, _stop_agentcore_session

cfg = load_config()
print("Stopping AgentCore session for E2E user...")
stopped = _stop_agentcore_session(cfg)
print(f"  Session stopped: {stopped}")
print("Resetting session record in DynamoDB (preserving user identity)...")
reset = reset_session(cfg)
print(f"  Session reset: {reset}")
PY

send_telegram "✅ *E2E session reset*\nRunning the current-product gate."

echo ""
echo "--- Running current-product E2E tests ---"
sleep 10

# RH1 intentionally removed these capabilities. They remain as historical test
# classes but are excluded from the current product gate instead of being
# represented as supported behavior.
CURRENT_PRODUCT_FILTER="not TestSubagent and not TestApiKeyManagement and not TestSkillManagement"
if [ -n "$TEST_FILTER" ]; then
  EFFECTIVE_FILTER="(${CURRENT_PRODUCT_FILTER}) and (${TEST_FILTER})"
else
  EFFECTIVE_FILTER="$CURRENT_PRODUCT_FILTER"
fi
PYTEST_CMD=(
  python -m pytest tests/e2e/bot_test.py -v --tb=short
  -k "$EFFECTIVE_FILTER"
)

printf "Running:"
printf " %q" "${PYTEST_CMD[@]}"
echo ""

RESULTS_FILE="/tmp/e2e-results.txt"
set +e
"${PYTEST_CMD[@]}" 2>&1 | tee "$RESULTS_FILE"
E2E_EXIT=${PIPESTATUS[0]}
set -e

PASSED=$(grep -c "PASSED" "$RESULTS_FILE" || true)
FAILED=$(grep -c "FAILED" "$RESULTS_FILE" || true)
SKIPPED=$(grep -c "SKIPPED" "$RESULTS_FILE" || true)

echo ""
echo "================================================"
echo " E2E Results: PASSED=$PASSED FAILED=$FAILED SKIPPED=$SKIPPED"
echo "================================================"

if [ "$E2E_EXIT" -eq 0 ]; then
  STATUS="✅ Current-product E2E gate passed"
  EMOJI="🎉"
  FAILURES=""
else
  STATUS="❌ Current-product E2E gate failed"
  EMOJI="🚨"
  FAILURES=$(grep "FAILED" "$RESULTS_FILE" | head -5 | sed 's/FAILED //' || true)
fi

FAILURE_SECTION=""
if [ -n "$FAILURES" ]; then
  FAILURE_SECTION=$(printf '\n*Failed tests:*\n%s' "$FAILURES")
fi

MSG="${EMOJI} *E2E gate complete*
${STATUS}

Passed: ${PASSED} | Failed: ${FAILED} | Skipped: ${SKIPPED}
${FAILURE_SECTION}"
send_telegram "$MSG"

echo "Full results: $RESULTS_FILE"
exit "$E2E_EXIT"
