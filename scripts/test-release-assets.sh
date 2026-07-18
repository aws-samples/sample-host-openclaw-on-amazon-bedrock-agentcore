#!/usr/bin/env bash
# Offline release-candidate gate. Builds the exact ARM64 Lambda payload and
# browser assets, then synthesizes every stack for a non-synthetic account.
# It never deploys and explicitly removes AWS credentials from synthesis.

set -Eeuo pipefail

SCRIPT_DIR="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(unset CDPATH; cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
readonly ACCOUNT="${PERSONAL_OPERATOR_RELEASE_ACCOUNT:-}"
readonly EXPECTED_COMMIT="${PERSONAL_OPERATOR_RELEASE_COMMIT:-}"
readonly REGION="eu-west-1"
readonly RUNTIME_CONTEXT_FILE="${PERSONAL_OPERATOR_RUNTIME_CONTEXT_FILE:-${REPO_ROOT}/build/runtime-context.json}"
readonly RUNTIME_IMAGE_URI="${PERSONAL_OPERATOR_RUNTIME_IMAGE_URI:-}"

# shellcheck source=hermetic-aws-env.sh
source "${SCRIPT_DIR}/hermetic-aws-env.sh"

die() {
  printf 'release asset gate: %s\n' "$*" >&2
  exit 1
}

[[ "${ACCOUNT}" =~ ^[0-9]{12}$ && "${ACCOUNT}" != "000000000000" ]] || \
  die "PERSONAL_OPERATOR_RELEASE_ACCOUNT must be a non-synthetic 12-digit account"
[[ "${EXPECTED_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || \
  die "PERSONAL_OPERATOR_RELEASE_COMMIT must be the reviewed exact Git commit"
[[ "${TRUSTED_LAMBDA_BUILD_IMAGE:-}" =~ ^public\.ecr\.aws/lambda/python@sha256:[0-9a-f]{64}$ ]] || \
  die "TRUSTED_LAMBDA_BUILD_IMAGE must be an immutable reviewed digest"
[[ -x "${PYTHON}" ]] || die "Python environment is missing at ${PYTHON}"
command -v docker >/dev/null 2>&1 || die "Docker is required"
docker version >/dev/null 2>&1 || die "Docker daemon is unavailable"
command -v npm >/dev/null 2>&1 || die "npm is required"

assert_exact_clean_commit() {
  local current_commit
  local ignored_input
  current_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD)" || \
    die "cannot resolve the release HEAD"
  [[ "${current_commit}" == "${EXPECTED_COMMIT}" ]] || \
    die "HEAD changed during release verification"
  [[ -z "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all)" ]] || \
    die "worktree changed during release verification"
  for ignored_input in "${REPO_ROOT}/web/.env" "${REPO_ROOT}"/web/.env.*; do
    [[ ! -e "${ignored_input}" && ! -L "${ignored_input}" ]] || \
      die "ignored web environment input is forbidden: ${ignored_input#"${REPO_ROOT}/"}"
  done
}

assert_exact_clean_commit
readonly actual_commit="${EXPECTED_COMMIT}"

CDK_CONTEXT_JSON="$(
  PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -m release_tools.release_assets \
    cdk-context \
    --config "${REPO_ROOT}/cdk.json" \
    --runtime-context "${RUNTIME_CONTEXT_FILE}" \
    --source-commit "${EXPECTED_COMMIT}" \
    --account "${ACCOUNT}" \
    --region "${REGION}" \
    --runtime-image-uri "${RUNTIME_IMAGE_URI}"
)"
readonly CDK_CONTEXT_JSON

cd "${REPO_ROOT}"
assert_exact_clean_commit
"${REPO_ROOT}/scripts/test-local.sh"
assert_exact_clean_commit

assert_exact_clean_commit
"${REPO_ROOT}/scripts/build-trusted-lambda-asset.sh" build
assert_exact_clean_commit

assert_exact_clean_commit
"${REPO_ROOT}/scripts/build-trusted-lambda-asset.sh" verify
assert_exact_clean_commit

release_work_dir="$(mktemp -d "${TMPDIR:-/tmp}/personal-operator-release-cdk.XXXXXX")"
outdir="${release_work_dir}/cdk.out"
aws_hermetic_home="${release_work_dir}/home"
trap 'rm -rf "${release_work_dir}"' EXIT INT TERM

assert_exact_clean_commit
run_with_hermetic_aws_env "${aws_hermetic_home}" env \
  AWS_REGION="${REGION}" \
  AWS_DEFAULT_REGION="${REGION}" \
  CDK_DEFAULT_ACCOUNT="${ACCOUNT}" \
  CDK_DEFAULT_REGION="${REGION}" \
  CDK_CONTEXT_JSON="${CDK_CONTEXT_JSON}" \
  CDK_OUTDIR="${outdir}" \
  "${PYTHON}" app.py
assert_exact_clean_commit

"${PYTHON}" - "${outdir}" <<'PY'
import csv
import json
import sys
from pathlib import Path

assembly = Path(sys.argv[1])
reports = sorted(assembly.glob("AwsSolutions--*-NagReport.csv"))
if not reports:
    raise SystemExit("release synthesis produced no cdk-nag reports")
findings = []
templates = sorted(assembly.glob("*.template.json"))
if not templates:
    raise SystemExit("release synthesis produced no CloudFormation templates")
for template in templates:
    rendered = json.dumps(json.loads(template.read_text(encoding="utf-8")))
    if '"PLACEHOLDER"' in rendered:
        raise SystemExit(f"{template.name} contains a placeholder runtime binding")
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

assert_exact_clean_commit
printf 'Lambda/web assets verified offline for commit %s and account-shaped synth %s. AgentCore runtime image was not built or attested by this gate.\n' \
  "${actual_commit}" "${ACCOUNT}"
