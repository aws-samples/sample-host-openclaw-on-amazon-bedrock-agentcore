#!/usr/bin/env bash
# Build and verify the shared deployment asset used by the trusted Python
# Lambdas. This script never deploys anything and never mounts or forwards
# host credentials into the build container.

set -Eeuo pipefail

SCRIPT_DIR="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(unset CDPATH; cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly REQUIREMENTS_FILE="${REPO_ROOT}/lambda/requirements.txt"
readonly BUILD_DIR="${REPO_ROOT}/build"
readonly ASSET_DIR="${BUILD_DIR}/trusted-lambda"
readonly PLATFORM="linux/arm64"
readonly BUILD_IMAGE="${TRUSTED_LAMBDA_BUILD_IMAGE:-}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/build-trusted-lambda-asset.sh build
  scripts/build-trusted-lambda-asset.sh verify

build   Create build/trusted-lambda atomically in the AWS Lambda Python 3.13
        ARM64 image, then verify it before publishing the directory.
verify  Re-run byte-inventory, dependency, architecture, and import checks in
        the exact immutable builder image recorded by the asset manifest.

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
physical = path.read_text(encoding="utf-8").splitlines()
logical: list[str] = []
pending = ""
for raw in physical:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    pending = f"{pending} {stripped}".strip()
    if pending.endswith("\\"):
        pending = pending[:-1].rstrip()
        continue
    logical.append(pending)
    pending = ""
if pending:
    raise SystemExit("unterminated requirement continuation")
if not logical:
    raise SystemExit("requirements file contains no packages")

exact = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*$"
)
hash_counts: list[int] = []
for line in logical:
    tokens = shlex.split(line)
    if not tokens or not exact.fullmatch(tokens[0]):
        raise SystemExit(f"requirement is not exactly version-pinned: {line}")
    hashes = tokens[1:]
    if any(not re.fullmatch(r"--hash=sha256:[0-9a-fA-F]{64}", item) for item in hashes):
        raise SystemExit(f"unsupported requirement option: {line}")
    hash_counts.append(len(hashes))

if not all(hash_counts):
    raise SystemExit("sha256 hash locking must cover every transitive requirement")
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

verify_asset_in_container() {
  local asset_dir="$1"
  local image_ref="$2"

  docker run --rm --interactive --platform "${PLATFORM}" \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --env AWS_EC2_METADATA_DISABLED=true \
    --env AWS_ACCESS_KEY_ID=packaging-placeholder \
    --env AWS_SECRET_ACCESS_KEY=packaging-placeholder \
    --env AWS_REGION=eu-west-1 \
    --env IDENTITY_TABLE_NAME=packaging-placeholder \
    --env HOME=/tmp \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --env PYTHONPATH=/asset \
    --volume "${asset_dir}:/asset:ro" \
    --entrypoint /bin/sh \
    "${image_ref}" -s <<'VERIFY_CONTAINER'
set -eu

/var/lang/bin/python3.13 - <<'PY'
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import pathlib
import platform
import re
import stat
import sys

asset = pathlib.Path("/asset")
manifest_path = asset / "MANIFEST.json"
sums_path = asset / "SHA256SUMS"
asset_digest_path = asset / "ASSET.sha256"
for required in (manifest_path, sums_path, asset_digest_path):
    if not required.is_file():
        raise SystemExit(f"missing inventory file: {required.name}")

manifest_bytes = manifest_path.read_bytes()
manifest = json.loads(manifest_bytes)
if manifest.get("schema") != "personal-operator.trusted-lambda-asset.v1":
    raise SystemExit("unrecognized asset manifest schema")
if manifest.get("platform") != "linux/arm64":
    raise SystemExit("asset manifest is not ARM64")
if manifest.get("python") != "3.13":
    raise SystemExit("asset manifest is not Python 3.13")
if manifest.get("requirementsMode") != "sha256-locked":
    raise SystemExit("asset requirements are not sha256 locked")
if not re.fullmatch(
    r"public\.ecr\.aws/lambda/python@sha256:[0-9a-f]{64}",
    manifest.get("builderImage", ""),
):
    raise SystemExit("asset builder image is not immutable")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("builderImageId", "")):
    raise SystemExit("asset builder image ID is invalid")
for field in ("requirementsSha256", "requirementsInputSha256"):
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get(field, "")):
        raise SystemExit(f"asset {field} is invalid")
if sys.version_info[:2] != (3, 13):
    raise SystemExit(f"verification runtime drift: {sys.version}")
if platform.machine() not in {"aarch64", "arm64"}:
    raise SystemExit(f"verification architecture drift: {platform.machine()}")

excluded = {"ASSET.sha256", "MANIFEST.json", "SHA256SUMS"}
actual_files = []
for path in sorted(asset.rglob("*"), key=lambda item: item.relative_to(asset).as_posix()):
    if path.is_symlink():
        raise SystemExit(f"symlink is forbidden in asset: {path.relative_to(asset)}")
    if not path.is_file():
        continue
    relative = path.relative_to(asset).as_posix()
    if relative in excluded:
        continue
    payload = path.read_bytes()
    actual_files.append(
        {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "mode": format(stat.S_IMODE(path.stat().st_mode), "04o"),
        }
    )

if actual_files != manifest.get("files"):
    raise SystemExit("asset bytes, modes, or file set differ from MANIFEST.json")
if manifest.get("payloadBytes") != sum(item["size"] for item in actual_files):
    raise SystemExit("asset payload byte count differs from MANIFEST.json")
source_files = manifest.get("sourceFiles")
if not isinstance(source_files, list) or not source_files:
    raise SystemExit("asset source inventory is empty")
source_names = {item.get("path") for item in source_files if isinstance(item, dict)}
required_handlers = {
    "router/index.py",
    "worker/index.py",
    "web/index.py",
    "control/index.py",
    "workspace_broker/index.py",
    "capabilities/gateway.py",
    "capabilities/composition.py",
    "capabilities/durable.py",
    "capabilities/artifacts/catalog-v1.json",
}
if not required_handlers.issubset(source_names):
    raise SystemExit("asset source inventory is missing a handler")
schema_prefix = "capabilities/artifacts/schemas/"
if len([name for name in source_names if str(name).startswith(schema_prefix)]) != 20:
    raise SystemExit("asset source inventory is missing exact capability schemas")
actual_by_path = {item["path"]: item for item in actual_files}
for item in source_files:
    if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
        raise SystemExit("asset source inventory is malformed")
    actual = actual_by_path.get(item["path"])
    if actual is None or any(actual[key] != item[key] for key in ("sha256", "size")):
        raise SystemExit("asset source inventory differs from payload")
expected_sums = "".join(f'{item["sha256"]}  {item["path"]}\n' for item in actual_files)
if sums_path.read_text(encoding="utf-8") != expected_sums:
    raise SystemExit("SHA256SUMS differs from the manifest inventory")
expected_asset_digest = hashlib.sha256(manifest_bytes).hexdigest() + "\n"
if asset_digest_path.read_text(encoding="ascii") != expected_asset_digest:
    raise SystemExit("ASSET.sha256 does not authenticate MANIFEST.json")

dependencies = sorted(
    (
        {"name": dist.metadata["Name"], "version": dist.version}
        for dist in importlib.metadata.distributions(path=[str(asset)])
        if dist.metadata.get("Name")
    ),
    key=lambda item: (item["name"].lower(), item["version"]),
)
if dependencies != manifest.get("dependencies"):
    raise SystemExit("installed dependency metadata differs from MANIFEST.json")
dependency_names = {
    re.sub(r"[-_.]+", "-", item["name"].casefold()) for item in dependencies
}
required_dependencies = {
    "boto3",
    "cryptography",
    "google-api-python-client",
    "google-auth",
    "openai",
}
if not required_dependencies.issubset(dependency_names):
    raise SystemExit("asset dependency inventory is incomplete")

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

print(f'verified {len(actual_files)} files and {len(dependencies)} distributions')
PY

cd /asset
/var/lang/bin/python3.13 -m pip check
VERIFY_CONTAINER
}

build_asset() {
  local requirement_mode
  local immutable_image
  local image_id
  local requirements_input_sha
  local requirements_sha
  local staging_dir

  requirement_mode="$(validate_requirements)"
  require_docker
  validate_image_reference "${BUILD_IMAGE}"
  pull_and_validate_image "${BUILD_IMAGE}"
  immutable_image="$(resolved_image_ref "${BUILD_IMAGE}")"
  image_id="$(docker image inspect --format '{{.Id}}' "${immutable_image}")"
  requirements_sha="$(shasum -a 256 "${REQUIREMENTS_FILE}" | awk '{print $1}')"
  requirements_input_sha="$(shasum -a 256 "${REPO_ROOT}/lambda/requirements.in" | awk '{print $1}')"

  mkdir -p "${BUILD_DIR}"
  staging_dir="${BUILD_DIR}/.trusted-lambda.$$.tmp"
  rm -rf "${staging_dir}"
  mkdir -p "${staging_dir}"
  trap 'rm -rf "${staging_dir:-}"' EXIT INT TERM

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
    --env "BUILDER_IMAGE=${immutable_image}" \
    --env "BUILDER_IMAGE_ID=${image_id}" \
    --env "REQUIREMENTS_MODE=${requirement_mode}" \
    --env "REQUIREMENTS_SHA256=${requirements_sha}" \
    --env "REQUIREMENTS_INPUT_SHA256=${requirements_input_sha}" \
    --env AWS_ACCESS_KEY_ID=packaging-placeholder \
    --env AWS_SECRET_ACCESS_KEY=packaging-placeholder \
    --env AWS_REGION=eu-west-1 \
    --env IDENTITY_TABLE_NAME=packaging-placeholder \
    --volume "${REPO_ROOT}:/workspace:ro" \
    --volume "${staging_dir}:/asset:rw" \
    --entrypoint /bin/sh \
    "${immutable_image}" -s <<'BUILD_CONTAINER'
set -eu

cp -R /workspace/lambda/. /asset/
find /asset -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
find /asset -type f \( -name 'test_*.py' -o -name '*.pyc' -o -name '*.pyo' \) -delete
rm -f /asset/requirements.in
mkdir -p /asset/capabilities/artifacts
cp /workspace/specs/capabilities/catalog-v1.json /asset/capabilities/artifacts/catalog-v1.json
cp -R /workspace/specs/capabilities/schemas /asset/capabilities/artifacts/schemas
test "$(find /asset/capabilities/artifacts/schemas -type f -name '*.json' | wc -l | tr -d ' ')" = "20"
if find /asset -type f ! -name '*.py' ! -name requirements.txt \
  ! -path '/asset/capabilities/artifacts/catalog-v1.json' \
  ! -path '/asset/capabilities/artifacts/schemas/*.json' -print -quit | grep -q .; then
  echo "unsupported non-source file found under lambda/" >&2
  exit 1
fi

test "${REQUIREMENTS_MODE}" = "sha256-locked"
/var/lang/bin/python3.13 -m pip install \
  --isolated \
  --index-url https://pypi.org/simple \
  --requirement /workspace/lambda/requirements.txt \
  --target /asset \
  --only-binary=:all: \
  --no-compile \
  --no-cache-dir \
  --disable-pip-version-check \
  --require-hashes

rm -rf /asset/bin
find /asset -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
find /asset -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
if find /asset -type l -print -quit | grep -q .; then
  echo "symlinks are forbidden in the trusted Lambda asset" >&2
  exit 1
fi

find /asset -type d -exec chmod 0755 {} +
find /asset -type f -exec chmod 0644 {} +
find /asset -type f -name '*.so' -exec chmod 0755 {} +
find /asset -exec touch -h -d '@0' {} +

/var/lang/bin/python3.13 - <<'PY'
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import pathlib
import stat

asset = pathlib.Path("/asset")
excluded = {"ASSET.sha256", "MANIFEST.json", "SHA256SUMS"}
files = []
for path in sorted(asset.rglob("*"), key=lambda item: item.relative_to(asset).as_posix()):
    if not path.is_file():
        continue
    relative = path.relative_to(asset).as_posix()
    if relative in excluded:
        continue
    payload = path.read_bytes()
    files.append(
        {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "mode": format(stat.S_IMODE(path.stat().st_mode), "04o"),
        }
    )

dependencies = sorted(
    (
        {"name": dist.metadata["Name"], "version": dist.version}
        for dist in importlib.metadata.distributions(path=[str(asset)])
        if dist.metadata.get("Name")
    ),
    key=lambda item: (item["name"].lower(), item["version"]),
)
lambda_source = pathlib.Path("/workspace/lambda")
capability_source = pathlib.Path("/workspace/specs/capabilities")
source_inputs = [
        (path, path.relative_to(lambda_source).as_posix())
        for path in lambda_source.rglob("*.py")
        if not path.name.startswith("test_")
        and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
    ]
source_inputs.append((lambda_source / "requirements.txt", "requirements.txt"))
source_inputs.append(
    (
        capability_source / "catalog-v1.json",
        "capabilities/artifacts/catalog-v1.json",
    )
)
source_inputs.extend(
    (path, f"capabilities/artifacts/schemas/{path.name}")
    for path in sorted((capability_source / "schemas").glob("*.json"))
)
source_inputs.sort(key=lambda item: item[1])
source_files = []
for source_path, relative in source_inputs:
    source_payload = source_path.read_bytes()
    asset_payload = (asset / relative).read_bytes()
    if asset_payload != source_payload:
        raise SystemExit(f"packaged source differs from repository source: {relative}")
    source_files.append(
        {
            "path": relative,
            "sha256": hashlib.sha256(source_payload).hexdigest(),
            "size": len(source_payload),
        }
    )
manifest = {
    "schema": "personal-operator.trusted-lambda-asset.v1",
    "platform": "linux/arm64",
    "python": "3.13",
    "builderImage": os.environ["BUILDER_IMAGE"],
    "builderImageId": os.environ["BUILDER_IMAGE_ID"],
    "requirementsMode": os.environ["REQUIREMENTS_MODE"],
    "requirementsSha256": os.environ["REQUIREMENTS_SHA256"],
    "requirementsInputSha256": os.environ["REQUIREMENTS_INPUT_SHA256"],
    "sourceDateEpoch": 0,
    "payloadBytes": sum(item["size"] for item in files),
    "dependencies": dependencies,
    "sourceFiles": source_files,
    "files": files,
}
if manifest["payloadBytes"] > 250 * 1024 * 1024:
    raise SystemExit("trusted Lambda asset exceeds Lambda's 250 MiB unzipped limit")
manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
(asset / "MANIFEST.json").write_bytes(manifest_bytes)
(asset / "SHA256SUMS").write_text(
    "".join(f'{item["sha256"]}  {item["path"]}\n' for item in files),
    encoding="utf-8",
)
(asset / "ASSET.sha256").write_text(
    hashlib.sha256(manifest_bytes).hexdigest() + "\n",
    encoding="ascii",
)
for inventory in ("MANIFEST.json", "SHA256SUMS", "ASSET.sha256"):
    path = asset / inventory
    path.chmod(0o644)
    os.utime(path, (0, 0), follow_symlinks=False)
PY
find /asset -type d -exec touch -h -d '@0' {} +
BUILD_CONTAINER

  verify_asset_in_container "${staging_dir}" "${immutable_image}"
  rm -rf "${ASSET_DIR}"
  mv "${staging_dir}" "${ASSET_DIR}"
  trap - EXIT INT TERM
  printf 'trusted Lambda asset ready: %s\n' "${ASSET_DIR}"
  printf 'CDK code asset root: build/trusted-lambda\n'
}

verify_asset() {
  local recorded_image

  [[ -f "${ASSET_DIR}/MANIFEST.json" ]] || \
    die "missing build/trusted-lambda/MANIFEST.json; run build first"
  recorded_image="$(python3 - "${ASSET_DIR}/MANIFEST.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
value = manifest.get("builderImage")
if not isinstance(value, str):
    raise SystemExit("manifest has no builderImage")
print(value)
PY
)"
  require_docker
  validate_image_reference "${recorded_image}"
  pull_and_validate_image "${recorded_image}"
  verify_asset_in_container "${ASSET_DIR}" "${recorded_image}"
}

case "${1:-build}" in
  build) build_asset ;;
  verify) verify_asset ;;
  -h | --help | help) usage ;;
  *) usage >&2; die "unknown mode: ${1}" ;;
esac
