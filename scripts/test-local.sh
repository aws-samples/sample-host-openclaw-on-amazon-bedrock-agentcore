#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
AWS_TEST_REGION="eu-west-1"
SYNTH_ACCOUNT="000000000000"
failures=0

# shellcheck source=hermetic-aws-env.sh
source "$ROOT_DIR/scripts/hermetic-aws-env.sh"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: Python environment not found at $PYTHON" >&2
  echo "Create it and install requirements.txt before running this script." >&2
  exit 2
fi

run_check() {
  local label="$1"
  shift

  echo
  echo "==> $label"
  if "$@"; then
    echo "PASS: $label"
  else
    local status=$?
    echo "FAIL ($status): $label" >&2
    failures=$((failures + 1))
  fi
}

cd "$ROOT_DIR"

run_check "Node.js 24.15 or newer" \
  node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 24 || (major === 24 && minor < 15)) { console.error(`Node ${process.versions.node} is too old; require >=24.15.0`); process.exit(1); }'

run_check "Bridge hash-locked dependencies" \
  npm --prefix bridge ci --ignore-scripts

run_check "Web hash-locked dependencies" \
  npm --prefix web ci --ignore-scripts

run_check "Python unit tests" \
  env AWS_REGION="$AWS_TEST_REGION" AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  PYTHONPATH="$ROOT_DIR/lambda/router:$ROOT_DIR/lambda" \
  "$PYTHON" -m pytest lambda/router lambda/worker lambda/workflows \
  lambda/actions lambda/capabilities lambda/control lambda/portable lambda/web \
  lambda/cron lambda/compute lambda/connectors lambda/browser lambda/scheduler \
  lambda/workspace_broker lambda/observability \
  tests/test_capability_stack.py \
  tests/test_compute_stack.py \
  tests/test_browser_stack.py \
  tests/test_scheduler_stack.py \
  release_tools \
  tests/test_product_configuration.py \
  tests/test_telegram_queue_infrastructure.py \
  tests/test_deploy_safety.py \
  tests/test_trusted_lambda_packaging.py \
  tests/test_verify_agentcore_storage.py \
  tests/test_web_stack.py \
  tests/security tests/integration -v

run_check "E2E session-control unit tests" \
  env AWS_REGION="$AWS_TEST_REGION" AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  "$PYTHON" -m pytest tests/e2e/test_session_control.py \
  tests/e2e/test_log_tailer_privacy.py -v

run_check "Bridge Node tests (serialized)" \
  env AWS_REGION="$AWS_TEST_REGION" AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  npm --prefix bridge test

run_check "Web UI tests" \
  npm --prefix web test

run_check "Web UI production build" \
  npm --prefix web run build

run_check "JavaScript syntax" \
  bash -c 'while IFS= read -r file; do node --check "$file" || exit 1; done < <(find bridge -type f -name "*.js" -not -path "*/node_modules/*" | sort)'

run_check "Python syntax" \
  "$PYTHON" -m compileall -q app.py stacks lambda release_tools scripts tests

run_check "Repository whitespace contract" \
  git diff --check

CDK_CONTEXT_JSON="$($PYTHON -c 'import json; print(json.dumps(json.load(open("cdk.json", encoding="utf-8"))["context"]))')"
SYNTH_WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/personal-operator-cdk.XXXXXX")"
SYNTH_OUT="$SYNTH_WORK_DIR/cdk.out"
AWS_HERMETIC_HOME="$SYNTH_WORK_DIR/home"
trap 'rm -rf "$SYNTH_WORK_DIR"' EXIT

run_check "CDK offline synthesis contract" \
  run_with_hermetic_aws_env "$AWS_HERMETIC_HOME" env \
  AWS_REGION="$AWS_TEST_REGION" \
  AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  CDK_DEFAULT_ACCOUNT="$SYNTH_ACCOUNT" \
  CDK_DEFAULT_REGION="$AWS_TEST_REGION" \
  PERSONAL_OPERATOR_SYNTH_SOURCE_ASSET=1 \
  CDK_CONTEXT_JSON="$CDK_CONTEXT_JSON" \
  CDK_OUTDIR="$SYNTH_OUT" \
  "$PYTHON" app.py

check_cdk_nag() {
  "$PYTHON" - "$SYNTH_OUT" <<'PY'
import csv
import sys
from pathlib import Path

assembly = Path(sys.argv[1])
findings = []
reports = sorted(assembly.glob("AwsSolutions--*-NagReport.csv"))
if not reports:
    print(
        f"No AwsSolutions NagReport CSV files found in {assembly}",
        file=sys.stderr,
    )
    raise SystemExit(1)

for report in reports:
    with report.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["Compliance"] == "Non-Compliant":
                findings.append(
                    (report.name, row["Rule ID"], row["Resource ID"], row["Rule Info"])
                )

for report, rule, resource, info in findings:
    print(f"{report}: {rule} {resource}: {info}", file=sys.stderr)

raise SystemExit(1 if findings else 0)
PY
}

run_check "CDK cdk-nag contract" check_cdk_nag

echo
if ((failures > 0)); then
  echo "$failures local check(s) failed." >&2
  exit 1
fi

echo "All local checks passed."
