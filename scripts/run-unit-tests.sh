#!/usr/bin/env bash
# Run the hermetic unit-test suites: fakes, loopback servers and synthetic CDK
# context only, so no AWS credentials or deployed stack are needed. E2E tests
# (tests/e2e) are never run. CI (.github/workflows/unit-tests.yml) calls this
# script one suite per step.
#
# Prerequisites (see docs/runtime-v2.md#offline-checks): Node 24, `npm ci
# --prefix bridge`, and a Python with requirements.txt, requirements-deploy.txt
# and pytest installed.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIRS=(bridge/skills/api-keys bridge/skills/eventbridge-cron bridge/skills/s3-user-files)
NODE_SUITES=(bridge skills gateway-tools)
PYTHON_SUITES=(router cron tests)

usage() {
  cat <<EOF
Usage: $0 [SUITE ...]

Suites:
  bridge         bridge/*.test.js
  skills         *.test.js in ${SKILL_DIRS[*]}
  gateway-tools  lambda/gateway_tools/*.test.js
  router         lambda/router (own Python process)
  cron           lambda/cron (own Python process)
  tests          tests/ without tests/e2e
Groups: node (${NODE_SUITES[*]}), python (${PYTHON_SUITES[*]}), all (default).

Router and Cron each import a module named 'index', so they always run in
separate Python processes. PYTHON selects the interpreter (default
.venv/bin/python when present, otherwise python3).
EOF
}

cd "$PROJECT_DIR"
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

check_node() {
  local version
  version="$(node --version)"
  case "$version" in
    v24.*) ;;
    *) echo "WARNING: Node $version found; the bridge, skills and gateway tools are tested on Node 24." >&2 ;;
  esac
}

require_bridge_modules() {
  if [ ! -d bridge/node_modules ]; then
    echo "ERROR: bridge/node_modules is missing; run: npm ci --prefix bridge" >&2
    return 1
  fi
}

run_bridge() {
  require_bridge_modules || return 1
  (cd bridge && node --test ./*.test.js)
}

run_skills() {
  local dir status=0
  require_bridge_modules || return 1
  for dir in "${SKILL_DIRS[@]}"; do
    echo "--- $dir"
    # The skills exit at load time without AWS_REGION; the tests make no AWS calls.
    (cd "$dir" && AWS_REGION="${AWS_REGION:-us-west-2}" node --test ./*.test.js) || status=1
  done
  return "$status"
}

run_gateway_tools() {
  node --test lambda/gateway_tools/*.test.js
}

run_router() {
  PYTHONPATH=lambda/shared/python:lambda/router "$PYTHON" -m pytest -q -ra lambda/router
}

run_cron() {
  PYTHONPATH=lambda/shared/python:lambda/cron "$PYTHON" -m pytest -q -ra lambda/cron
}

run_tests() {
  "$PYTHON" -m pytest -q -ra tests --ignore=tests/e2e
}

suites=()
[ "$#" -gt 0 ] || set -- all
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    all) suites+=("${NODE_SUITES[@]}" "${PYTHON_SUITES[@]}") ;;
    node) suites+=("${NODE_SUITES[@]}") ;;
    python) suites+=("${PYTHON_SUITES[@]}") ;;
    bridge|skills|gateway-tools|router|cron|tests) suites+=("$arg") ;;
    *) echo "ERROR: unknown suite '$arg'" >&2; usage >&2; exit 2 ;;
  esac
done

for suite in "${suites[@]}"; do
  case "$suite" in
    bridge|skills|gateway-tools) check_node; break ;;
  esac
done

failed=()
for suite in "${suites[@]}"; do
  echo "==> $suite"
  if ! "run_${suite//-/_}"; then
    failed+=("$suite")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "PASSED: ${suites[*]}"
