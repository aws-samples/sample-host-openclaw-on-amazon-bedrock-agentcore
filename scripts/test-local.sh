#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
AWS_TEST_REGION="eu-west-1"
SYNTH_ACCOUNT="000000000000"
failures=0

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

run_check "Python unit tests" \
  env AWS_REGION="$AWS_TEST_REGION" AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  "$PYTHON" -m pytest lambda/router tests/test_product_configuration.py -v

run_check "Bridge Node tests (serialized)" \
  env AWS_REGION="$AWS_TEST_REGION" AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  npm --prefix bridge test

run_check "JavaScript syntax" \
  bash -c 'while IFS= read -r file; do node --check "$file" || exit 1; done < <(find bridge -type f -name "*.js" -not -path "*/node_modules/*" | sort)'

run_check "Python syntax" \
  "$PYTHON" -m compileall -q app.py stacks lambda tests

CDK_CONTEXT_JSON="$($PYTHON -c 'import json; print(json.dumps(json.load(open("cdk.json", encoding="utf-8"))["context"]))')"
SYNTH_OUT="$(mktemp -d "${TMPDIR:-/tmp}/personal-operator-cdk.XXXXXX")"
trap 'rm -rf "$SYNTH_OUT"' EXIT

run_check "CDK offline synthesis contract" \
  env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
  AWS_EC2_METADATA_DISABLED=true \
  AWS_REGION="$AWS_TEST_REGION" \
  AWS_DEFAULT_REGION="$AWS_TEST_REGION" \
  CDK_DEFAULT_ACCOUNT="$SYNTH_ACCOUNT" \
  CDK_DEFAULT_REGION="$AWS_TEST_REGION" \
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
for report in sorted(assembly.glob("AwsSolutions--*-NagReport.csv")):
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
