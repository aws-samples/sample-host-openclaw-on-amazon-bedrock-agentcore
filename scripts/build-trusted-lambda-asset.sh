#!/usr/bin/env bash
# Build and verify the shared deployment ZIP used by trusted Python Lambdas.
# MANIFEST.json is outside trusted-lambda.zip so Lambda never executes release
# metadata and CDK can authenticate the archive before registering an asset.

set -Eeuo pipefail

SCRIPT_DIR="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(unset CDPATH; cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly REQUIREMENTS_FILE="${REPO_ROOT}/lambda/requirements.txt"
readonly BUILD_DIR="${REPO_ROOT}/build"
readonly ASSET_DIR="${BUILD_DIR}/trusted-lambda"
readonly ARCHIVE_NAME="trusted-lambda.zip"
readonly PLATFORM="linux/arm64"
readonly BUILD_IMAGE="${TRUSTED_LAMBDA_BUILD_IMAGE:-}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/build-trusted-lambda-asset.sh build
  scripts/build-trusted-lambda-asset.sh verify

build   Create build/trusted-lambda/trusted-lambda.zip and its external v2
        manifest atomically in the AWS Lambda Python 3.13 ARM64 image, then
        verify the exact ZIP before publishing the artifact directory.
verify  Re-run ZIP inventory, dependency, architecture, and import checks in
        the exact immutable builder image recorded by the external manifest.

The script only pulls public images/packages. It does not deploy, contact AWS
APIs, mount ~/.aws, or pass host credentials into a container.

TRUSTED_LAMBDA_BUILD_IMAGE must name a reviewed immutable image such as
public.ecr.aws/lambda/python@sha256:<64 lowercase hex characters>. A mutable
tag is deliberately rejected.
USAGE
}

die() {
  printf 'trusted-lambda packaging: %s\n' "$*" >&2
  exit 1
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is required; refusing a host-native build"
  docker version >/dev/null 2>&1 || die "Docker daemon is unavailable"
}

validate_image_reference() {
  case "$1" in
    public.ecr.aws/lambda/python@sha256:*) ;;
    "") die "TRUSTED_LAMBDA_BUILD_IMAGE must be an immutable reviewed digest" ;;
    *) die "builder must be the immutable official AWS Lambda Python image" ;;
  esac
  [[ "$1" =~ ^public\.ecr\.aws/lambda/python@sha256:[0-9a-f]{64}$ ]] || \
    die "builder digest must contain exactly 64 lowercase hexadecimal characters"
}

pull_and_validate_image() {
  local image_ref="$1"
  local image_platform

  docker pull --platform "${PLATFORM}" "${image_ref}" >/dev/null
  image_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${image_ref}")"
  [[ "${image_platform}" == "${PLATFORM}" ]] || \
    die "builder resolved to ${image_platform}, expected ${PLATFORM}"

  docker run --rm --platform "${PLATFORM}" \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,size=32m \
    --env AWS_EC2_METADATA_DISABLED=true \
    --env AWS_ACCESS_KEY_ID=packaging-placeholder \
    --env AWS_SECRET_ACCESS_KEY=packaging-placeholder \
    --env AWS_REGION=eu-west-1 \
    --env IDENTITY_TABLE_NAME=packaging-placeholder \
    --env HOME=/tmp \
    --entrypoint /var/lang/bin/python3.13 \
    "${image_ref}" -c \
    'import pathlib, platform, sys
assert sys.version_info[:2] == (3, 13), sys.version
assert platform.machine() in {"aarch64", "arm64"}, platform.machine()
os_release = pathlib.Path("/etc/os-release").read_text(encoding="utf-8")
assert "ID=amzn" in os_release, os_release' \
    >/dev/null || die "Docker cannot execute the required Lambda ARM64 platform"
}

validate_requirements() {
  [[ -f "${REQUIREMENTS_FILE}" ]] || die "missing lambda/requirements.txt"

  python3 - "${REQUIREMENTS_FILE}" <<'PY'
import pathlib
import re
import shlex
import sys

path = pathlib.Path(sys.argv[1])
logical = []
pending = ""
for raw in path.read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    pending = f"{pending} {stripped}".strip()
    if pending.endswith("\\"):
        pending = pending[:-1].rstrip()
        continue
    logical.append(pending)
    pending = ""
if pending or not logical:
    raise SystemExit("requirements lock is incomplete")
exact = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*$"
)
for line in logical:
    tokens = shlex.split(line)
    if not tokens or not exact.fullmatch(tokens[0]):
        raise SystemExit(f"requirement is not exactly version-pinned: {line}")
    if not tokens[1:] or any(
        re.fullmatch(r"--hash=sha256:[0-9a-fA-F]{64}", item) is None
        for item in tokens[1:]
    ):
        raise SystemExit(f"requirement is not sha256 locked: {line}")
print("sha256-locked")
PY
}

resolved_image_ref() {
  local image_ref="$1"
  local digest

  digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image_ref}" \
    | awk '/^public\.ecr\.aws\/lambda\/python@sha256:/{print; exit}')"
  [[ "${digest}" == public.ecr.aws/lambda/python@sha256:* ]] || \
    die "could not resolve the public builder tag to an immutable digest"
  printf '%s\n' "${digest}"
}

release_identity() {
  local source_commit
  local source_tree

  command -v git >/dev/null 2>&1 || die "git is required for release identity"
  source_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD)" || die "cannot resolve source commit"
  source_tree="$(git -C "${REPO_ROOT}" rev-parse "HEAD^{tree}")" || die "cannot resolve source tree"
  [[ "${source_commit}" =~ ^[0-9a-f]{40}$ ]] || die "source commit is not canonical"
  [[ "${source_tree}" =~ ^[0-9a-f]{40}$ ]] || die "source tree is not canonical"
  [[ -z "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all)" ]] || \
    die "trusted Lambda build requires a clean exact source tree"
  printf '%s\n%s\n' "${source_commit}" "${source_tree}"
}

verify_asset_in_container() {
  local asset_dir="$1"
  local image_ref="$2"
  local expected_commit="$3"
  local expected_tree="$4"

  docker run --rm --interactive --platform "${PLATFORM}" \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,size=512m \
    --env AWS_EC2_METADATA_DISABLED=true \
    --env AWS_ACCESS_KEY_ID=packaging-placeholder \
    --env AWS_SECRET_ACCESS_KEY=packaging-placeholder \
    --env AWS_REGION=eu-west-1 \
    --env IDENTITY_TABLE_NAME=packaging-placeholder \
    --env HOME=/tmp \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --volume "${asset_dir}:/asset:ro" \
    --volume "${REPO_ROOT}:/workspace:ro" \
    --entrypoint /bin/sh \
    "${image_ref}" -s -- "${expected_commit}" "${expected_tree}" <<'VERIFY_CONTAINER'
set -eu
expected_commit="$1"
expected_tree="$2"

PYTHONPATH=/workspace /var/lang/bin/python3.13 -m release_tools.lambda_asset verify \
  --artifact /asset \
  --source /workspace/lambda \
  --expected-commit "${expected_commit}" \
  --expected-tree "${expected_tree}" \
  --extract /tmp/trusted

export PYTHONPATH=/tmp/trusted
cd /tmp/trusted
/var/lang/bin/python3.13 - <<'PY'
import pathlib
import platform
import sys

assert sys.version_info[:2] == (3, 13), sys.version
assert platform.machine() in {"aarch64", "arm64"}, platform.machine()
assert pathlib.Path("workspace_broker/index.py").is_file()
# The authoritative external-manifest, source-inventory, capability-artifact,
# and deterministic-ZIP verification is performed by release_tools.lambda_asset
# (v2); this in-image step only confirms the extracted payload imports.
assert pathlib.Path("capabilities/gateway.py").is_file()
assert pathlib.Path("capabilities/artifacts/catalog-v1.json").is_file()
assert len(list(pathlib.Path("capabilities/artifacts/schemas").glob("*.json"))) == 20

import cryptography  # noqa: F401,E402
import googleapiclient  # noqa: F401,E402
import openai  # noqa: F401,E402
import boto3  # noqa: F401,E402

if "bedrock-agentcore" not in boto3.Session().get_available_services():
    raise SystemExit("bundled boto3 has no bedrock-agentcore service model")

import control.index  # noqa: F401,E402
import control.composition  # noqa: F401,E402
import router.index  # noqa: F401,E402
import web.index  # noqa: F401,E402
import web.composition  # noqa: F401,E402
import worker.index  # noqa: F401,E402
import workspace_broker.index  # noqa: F401,E402
import capabilities.gateway  # noqa: F401,E402
import capabilities.composition  # noqa: F401,E402
import capabilities.durable  # noqa: F401,E402
PY
/var/lang/bin/python3.13 -m pip check
VERIFY_CONTAINER
}

build_asset() {
  local immutable_image
  local image_id
  local identity
  local payload_dir
  local source_commit
  local source_tree
  local staging_dir

  validate_requirements >/dev/null
  identity="$(release_identity)"
  source_commit="$(printf '%s\n' "${identity}" | sed -n '1p')"
  source_tree="$(printf '%s\n' "${identity}" | sed -n '2p')"
  require_docker
  validate_image_reference "${BUILD_IMAGE}"
  pull_and_validate_image "${BUILD_IMAGE}"
  immutable_image="$(resolved_image_ref "${BUILD_IMAGE}")"
  image_id="$(docker image inspect --format '{{.Id}}' "${immutable_image}")"

  mkdir -p "${BUILD_DIR}"
  staging_dir="${BUILD_DIR}/.trusted-lambda.$$.tmp"
  payload_dir="${BUILD_DIR}/.trusted-lambda-payload.$$.tmp"
  rm -rf "${staging_dir}" "${payload_dir}"
  mkdir -p "${payload_dir}"
  trap 'rm -rf "${staging_dir:-}" "${payload_dir:-}"' EXIT INT TERM

  docker run --rm --interactive --platform "${PLATFORM}" \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,size=512m \
    --user "$(id -u):$(id -g)" \
    --env AWS_EC2_METADATA_DISABLED=true \
    --env HOME=/tmp \
    --env PIP_DISABLE_PIP_VERSION_CHECK=1 \
    --env PIP_NO_INPUT=1 \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --env SOURCE_DATE_EPOCH=0 \
    --env "SOURCE_COMMIT=${source_commit}" \
    --env "SOURCE_TREE=${source_tree}" \
    --env "BUILDER_IMAGE=${immutable_image}" \
    --env "BUILDER_IMAGE_ID=${image_id}" \
    --env AWS_ACCESS_KEY_ID=packaging-placeholder \
    --env AWS_SECRET_ACCESS_KEY=packaging-placeholder \
    --env AWS_REGION=eu-west-1 \
    --env IDENTITY_TABLE_NAME=packaging-placeholder \
    --volume "${REPO_ROOT}:/workspace:ro" \
    --volume "${payload_dir}:/payload:rw" \
    --volume "${BUILD_DIR}:/output-parent:rw" \
    --entrypoint /bin/sh \
    "${immutable_image}" -s -- "$(basename "${staging_dir}")" <<'BUILD_CONTAINER'
set -eu
output="/output-parent/$1"

cp -R /workspace/lambda/. /payload/
find /payload -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
find /payload -type f \( -name 'test_*.py' -o -name '*.pyc' -o -name '*.pyo' \) -delete
rm -f /payload/requirements.in
# The immutable capability catalog and its exact 20-schema set ship inside the
# trusted asset under capabilities/artifacts/; release_tools.lambda_asset then
# binds them as authenticated source inputs.
mkdir -p /payload/capabilities/artifacts
cp /workspace/specs/capabilities/catalog-v1.json /payload/capabilities/artifacts/catalog-v1.json
cp -R /workspace/specs/capabilities/schemas /payload/capabilities/artifacts/schemas
test "$(find /payload/capabilities/artifacts/schemas -type f -name '*.json' | wc -l | tr -d ' ')" = "20"
if find /payload -type f ! -name '*.py' ! -name requirements.txt \
  ! -path '/payload/capabilities/artifacts/catalog-v1.json' \
  ! -path '/payload/capabilities/artifacts/schemas/*.json' -print -quit | grep -q .; then
  echo "unsupported non-source file found under lambda/" >&2
  exit 1
fi

/var/lang/bin/python3.13 -m pip install \
  --isolated \
  --index-url https://pypi.org/simple \
  --requirement /workspace/lambda/requirements.txt \
  --target /payload \
  --only-binary=:all: \
  --no-compile \
  --no-cache-dir \
  --disable-pip-version-check \
  --require-hashes

rm -rf /payload/bin
find /payload -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
find /payload -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
if find /payload -type l -print -quit | grep -q .; then
  echo "symlinks are forbidden in the trusted Lambda asset" >&2
  exit 1
fi
find /payload -type d -exec chmod 0755 {} +
find /payload -type f -exec chmod 0644 {} +
find /payload -type f -name '*.so' -exec chmod 0755 {} +
find /payload -exec touch -h -d '@0' {} +

# The v2 helper enforces the 250 MiB unzipped limit, sorted inventory,
# deterministic ZIP metadata, dependencies, source bytes (including the packaged
# capability catalog and exact schema set), and the external manifest.
PYTHONPATH=/workspace /var/lang/bin/python3.13 -m release_tools.lambda_asset build \
  --payload /payload \
  --source /workspace/lambda \
  --output "${output}" \
  --source-commit "${SOURCE_COMMIT}" \
  --source-tree "${SOURCE_TREE}" \
  --builder-image "${BUILDER_IMAGE}" \
  --builder-image-id "${BUILDER_IMAGE_ID}"
BUILD_CONTAINER

  verify_asset_in_container "${staging_dir}" "${immutable_image}" \
    "${source_commit}" "${source_tree}"
  rm -rf "${payload_dir}"
  PYTHONPATH="${REPO_ROOT}" python3 -m release_tools.lambda_asset publish \
    --staging "${staging_dir}" \
    --destination "${ASSET_DIR}"
  trap - EXIT INT TERM
  printf 'trusted Lambda asset ready: %s/%s\n' "${ASSET_DIR}" "${ARCHIVE_NAME}"
  printf 'CDK code asset: build/trusted-lambda/trusted-lambda.zip\n'
}

verify_asset() {
  local identity
  local recorded
  local recorded_image
  local source_commit
  local source_tree

  [[ -f "${ASSET_DIR}/MANIFEST.json" ]] || \
    die "missing build/trusted-lambda/MANIFEST.json; run build first"
  recorded="$(python3 - "${ASSET_DIR}/MANIFEST.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for field in ("builderImage", "sourceCommit", "sourceTree"):
    value = manifest.get(field)
    if not isinstance(value, str):
        raise SystemExit(f"manifest has no {field}")
    print(value)
PY
)"
  recorded_image="$(printf '%s\n' "${recorded}" | sed -n '1p')"
  identity="$(release_identity)"
  source_commit="$(printf '%s\n' "${identity}" | sed -n '1p')"
  source_tree="$(printf '%s\n' "${identity}" | sed -n '2p')"
  [[ "$(printf '%s\n' "${recorded}" | sed -n '2p')" == "${source_commit}" ]] || \
    die "manifest source commit differs from HEAD"
  [[ "$(printf '%s\n' "${recorded}" | sed -n '3p')" == "${source_tree}" ]] || \
    die "manifest source tree differs from HEAD"
  require_docker
  validate_image_reference "${recorded_image}"
  pull_and_validate_image "${recorded_image}"
  verify_asset_in_container "${ASSET_DIR}" "${recorded_image}" \
    "${source_commit}" "${source_tree}"
}

case "${1:-build}" in
  build) build_asset ;;
  verify) verify_asset ;;
  -h | --help | help) usage ;;
  *) usage >&2; die "unknown mode: ${1}" ;;
esac
