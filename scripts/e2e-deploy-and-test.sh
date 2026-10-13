#!/usr/bin/env bash
# Deploy and verify V2, then run the V2 smoke/lifecycle tests without resetting state.
# Selected tests remain responsible for their own V2 contract and side effects.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_PYTHON="${DEPLOY_PYTHON:-$PROJECT_DIR/.venv-deploy/bin/python}"
E2E_PYTHON="${E2E_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
SKIP_DEPLOY=false
MANIFEST=".runtime/runtime-v2.json"
TEST_NODES=()
DEPLOY_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-deploy) SKIP_DEPLOY=true; shift ;;
    --manifest) MANIFEST="${2:?--manifest requires a path}"; shift 2 ;;
    --image-uri|--endpoint-name) DEPLOY_ARGS+=("$1" "${2:?option requires a value}"); shift 2 ;;
    --test-node)
      node="${2:?--test-node requires a tests/e2e/path.py::test_name}"
      case "$node" in
        *..*|tests/e2e/bot_test.py::TestWarmupShim*)
          echo "ERROR: select V2 tests under tests/e2e/; the V1 warmup shim is unsupported." >&2
          exit 1 ;;
        tests/e2e/*.py::*) TEST_NODES+=("$node") ;;
        *)
          echo "ERROR: --test-node must name an individual test under tests/e2e/." >&2
          exit 1 ;;
      esac
      shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--test-node tests/e2e/path.py::test_name ...] [--skip-deploy] [--manifest PATH] [--image-uri URI] [--endpoint-name NAME]"
      echo "Default: V2 bot_test.py::TestSmoke and ::TestMessageLifecycle; --test-node replaces both."
      echo "V2 only. No session deletion, runtime stopping, or image-version rewrite."
      exit 0 ;;
    *) echo "ERROR: unknown option $1" >&2; exit 1 ;;
  esac
done
if [ "${RUNTIME_PLATFORM+x}" = x ]; then
  echo "ERROR: RUNTIME_PLATFORM is no longer supported; unset it for V2 deployment." >&2
  exit 1
fi
if [ "${#TEST_NODES[@]}" -eq 0 ]; then
  TEST_NODES=(tests/e2e/bot_test.py::TestSmoke tests/e2e/bot_test.py::TestMessageLifecycle)
fi
if ! command -v uv >/dev/null || [ ! -x "$DEPLOY_PYTHON" ] || [ ! -x "$E2E_PYTHON" ]; then
  echo "ERROR: install uv and prepare .venv-deploy and .venv (or set DEPLOY_PYTHON and E2E_PYTHON)." >&2
  exit 1
fi
cd "$PROJECT_DIR"
PYTHON=(uv run --no-project --python "$DEPLOY_PYTHON" python)
REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-}}"
if [ -z "$REGION" ]; then
  REGION=$("${PYTHON[@]}" -c 'import json; print(json.load(open("cdk.json"))["context"].get("region", ""))')
fi
if [ -z "$REGION" ]; then
  echo "ERROR: set CDK_DEFAULT_REGION or AWS_REGION." >&2
  exit 1
fi
export CDK_DEFAULT_REGION="$REGION"

TG_CHAT_ID="${E2E_TELEGRAM_CHAT_ID:-}"
RESULTS_FILE="${E2E_RESULTS_FILE:-/tmp/e2e-results.txt}"
send_telegram() {
  # Notifications are best effort and must not change the deployment/test result.
  [ -n "$TG_CHAT_ID" ] || return 0
  local msg="$1" token payload
  token=$(aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --region "$REGION" --query SecretString --output text 2>/dev/null) || return 0
  payload=$("${PYTHON[@]}" -c \
    'import json,sys; print(json.dumps({"chat_id": sys.argv[1], "text": sys.argv[2], "parse_mode": "Markdown"}))' \
    "$TG_CHAT_ID" "$msg") || return 0
  curl -sS --fail --connect-timeout 5 --max-time 10 -X POST \
    "https://api.telegram.org/bot${token}/sendMessage" \
    -H "Content-Type: application/json" -d "$payload" >/dev/null 2>&1 || true
}

send_telegram "🚀 *E2E Pipeline started*
Preparing AgentCore Runtime V2 and selected E2E tests..."

if [ "$SKIP_DEPLOY" = false ]; then
  bash "$PROJECT_DIR/scripts/deploy.sh" --manifest "$MANIFEST" ${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}
fi
"${PYTHON[@]}" "$PROJECT_DIR/scripts/runtime_deploy.py" verify --region "$REGION" \
  --manifest "$MANIFEST" --require-enrollment-ready

echo "Running: ${TEST_NODES[*]}"
set +e
uv run --no-project --python "$E2E_PYTHON" python -m pytest "${TEST_NODES[@]}" -v --tb=short \
  2>&1 | tee "$RESULTS_FILE"
E2E_EXIT=${PIPESTATUS[0]}
set -e

PASSED=$(grep -c "PASSED" "$RESULTS_FILE" 2>/dev/null || true)
FAILED=$(grep -c "FAILED" "$RESULTS_FILE" 2>/dev/null || true)
SKIPPED=$(grep -c "SKIPPED" "$RESULTS_FILE" 2>/dev/null || true)
ERRORS=$(grep -c "ERROR" "$RESULTS_FILE" 2>/dev/null || true)
PASSED=${PASSED:-0}
FAILED=${FAILED:-0}
SKIPPED=${SKIPPED:-0}
ERRORS=${ERRORS:-0}
echo "E2E Results: PASSED=$PASSED FAILED=$FAILED SKIPPED=$SKIPPED ERRORS=$ERRORS"

FAILURES=""
if [ "$E2E_EXIT" -eq 0 ]; then
  STATUS="✅ All E2E tests passed"
  EMOJI="🎉"
else
  STATUS="❌ E2E tests failed"
  EMOJI="🚨"
  FAILURES=$(grep "FAILED" "$RESULTS_FILE" 2>/dev/null | head -5 | sed 's/FAILED //' || true)
fi
MSG="${EMOJI} *E2E Pipeline complete*
${STATUS}

Passed: ${PASSED} | Failed: ${FAILED} | Skipped: ${SKIPPED} | Errors: ${ERRORS}"
if [ -n "$FAILURES" ]; then
  MSG="${MSG}

*Failed tests:*
${FAILURES}"
fi
send_telegram "$MSG"
echo "Full results: $RESULTS_FILE"
exit "$E2E_EXIT"
