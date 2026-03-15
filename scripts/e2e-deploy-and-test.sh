#!/usr/bin/env bash
# e2e-deploy-and-test.sh
# Full E2E pipeline: deploy → reset sessions → stop runtime → run all E2E tests
# Usage: ./scripts/e2e-deploy-and-test.sh [--skip-deploy] [--test-filter PATTERN]
set -euo pipefail

REGION="ap-southeast-2"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_DEPLOY=false
TEST_FILTER="${2:-}"

for arg in "$@"; do
  case $arg in
    --skip-deploy) SKIP_DEPLOY=true ;;
    --test-filter) TEST_FILTER="${2:-}" ;;
  esac
done

# ── Telegram notification helper ────────────────────────────────────────────
TG_CHAT_ID="${E2E_TELEGRAM_CHAT_ID:?Set E2E_TELEGRAM_CHAT_ID}"
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
echo " OpenClaw E2E Deploy + Test Pipeline"
echo " Region: $REGION | Account: $ACCOUNT"
echo "================================================"

# ── Step 1: Deploy ───────────────────────────────────────────────────────────
if [ "$SKIP_DEPLOY" = false ]; then
  send_telegram "🚀 *E2E Pipeline started*
Step 1/4: Deploying to AWS..."

  echo ""
  echo "── Step 1: CDK Deploy ──────────────────────────"
  source .venv/bin/activate

  # Bump image_version
  CURRENT_VERSION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])")
  NEW_VERSION=$((CURRENT_VERSION + 1))
  python3 -c "
import json
with open('cdk.json') as f: cfg = json.load(f)
cfg['context']['image_version'] = $NEW_VERSION
with open('cdk.json', 'w') as f: json.dump(cfg, f, indent=2)
print(f'Bumped image_version: $CURRENT_VERSION → $NEW_VERSION')
"
  # CDK synth first (validates cdk-nag)
  echo "Running cdk synth..."
  cdk synth --quiet 2>&1 | tail -5

  echo "Deploying all stacks..."
  cdk deploy --all --require-approval never 2>&1 | tail -30

  echo ""
  echo "── Step 2: Build + Push Docker Image ──────────"
  ECR_URI=$(aws ecr describe-repositories \
    --repository-names openclaw-bridge \
    --region "$REGION" \
    --query "repositories[0].repositoryUri" --output text 2>/dev/null)

  aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR_URI" 2>/dev/null

  echo "Building ARM64 image v${NEW_VERSION}..."
  docker build --platform linux/arm64 -t "openclaw-bridge:v${NEW_VERSION}" bridge/ 2>&1 | tail -10

  docker tag "openclaw-bridge:v${NEW_VERSION}" "${ECR_URI}:v${NEW_VERSION}"
  echo "Pushing ${ECR_URI}:v${NEW_VERSION}..."
  docker push "${ECR_URI}:v${NEW_VERSION}" 2>&1 | tail -5

  send_telegram "✅ *Deploy complete*
Image v${NEW_VERSION} pushed to ECR.
Step 2/4: Resetting sessions..."
else
  echo "── Skipping deploy (--skip-deploy) ────────────"
fi

# ── Step 3: Reset sessions + stop old runtime ────────────────────────────────
echo ""
echo "── Step 3: Reset sessions + stop old runtime ──"

# Require E2E env vars
if [ -z "${E2E_TELEGRAM_USER_ID:-}" ] || [ -z "${E2E_TELEGRAM_CHAT_ID:-}" ]; then
  echo "ERROR: E2E_TELEGRAM_USER_ID and E2E_TELEGRAM_CHAT_ID must be set"
  echo "  export E2E_TELEGRAM_USER_ID=<YOUR_TELEGRAM_USER_ID>"
  echo "  export E2E_TELEGRAM_CHAT_ID=<YOUR_TELEGRAM_CHAT_ID>"
  exit 1
fi

cd "$PROJECT_DIR"
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from tests.e2e.config import load_config
from tests.e2e.session import reset_session, _stop_agentcore_session

cfg = load_config()
print(f"Stopping AgentCore session for E2E user...")
stopped = _stop_agentcore_session(cfg)
print(f"  Session stopped: {stopped}")

print(f"Resetting session record in DynamoDB (preserving user identity)...")
reset = reset_session(cfg)
print(f"  Session reset: {reset}")

print("Session reset complete — next message will trigger cold start with new image.")
EOF

send_telegram "✅ *Sessions reset*
Old container terminated. Cold start will pick up new image.
Step 3/4: Running E2E tests..."

# ── Step 4: E2E Tests ────────────────────────────────────────────────────────
echo ""
echo "── Step 4: E2E Tests ───────────────────────────"
echo "Waiting 10s for DynamoDB to settle..."
sleep 10

cd "$PROJECT_DIR"
source .venv/bin/activate

# Build pytest command — run all test classes documented in the E2E skill
PYTEST_CMD="python -m pytest tests/e2e/bot_test.py -v --tb=short"
if [ -n "$TEST_FILTER" ]; then
  PYTEST_CMD="$PYTEST_CMD -k $TEST_FILTER"
fi

echo "Running: $PYTEST_CMD"
echo ""

set +e
$PYTEST_CMD 2>&1 | tee /tmp/e2e-results.txt
E2E_EXIT=$?
set -e

# Parse results
PASSED=$(grep -c "PASSED" /tmp/e2e-results.txt || true)
FAILED=$(grep -c "FAILED" /tmp/e2e-results.txt || true)
SKIPPED=$(grep -c "SKIPPED" /tmp/e2e-results.txt || true)
ERRORS=$(grep -c "ERROR" /tmp/e2e-results.txt || true)

echo ""
echo "================================================"
echo " E2E Results: PASSED=$PASSED FAILED=$FAILED SKIPPED=$SKIPPED ERRORS=$ERRORS"
echo "================================================"

# ── Notify ───────────────────────────────────────────────────────────────────
if [ "$E2E_EXIT" -eq 0 ]; then
  STATUS="✅ All E2E tests passed"
  EMOJI="🎉"
else
  STATUS="❌ E2E tests failed"
  EMOJI="🚨"
  # Collect failed test names
  FAILURES=$(grep "FAILED" /tmp/e2e-results.txt | head -5 | sed 's/FAILED //' | tr '\n' '\n' || true)
fi

MSG="${EMOJI} *E2E Pipeline complete*
${STATUS}

Passed: ${PASSED} | Failed: ${FAILED} | Skipped: ${SKIPPED}
$([ -n "${FAILURES:-}" ] && echo "
*Failed tests:*
${FAILURES}")"

send_telegram "$MSG"

echo ""
echo "Full results: /tmp/e2e-results.txt"
exit "$E2E_EXIT"
