# The production builder first retains and authenticates the exact local ARM64
# base images with `docker create --pull=never`, then supplies only a normalized
# Python root filesystem and the retained Node binary. The Dockerfile therefore
# resolves no registry name and has no network-resolved build stage.
FROM scratch

ARG PYTHON_BASE_ROOTFS_SHA256
ARG NODE_BASE_BINARY_SHA256
ADD base/python-rootfs.tar /
COPY --chmod=0755 base/node /usr/local/bin/node
LABEL personal.operator.python-base-rootfs-sha256=${PYTHON_BASE_ROOTFS_SHA256}
LABEL personal.operator.node-base-binary-sha256=${NODE_BASE_BINARY_SHA256}

ARG RUNTIME_BUILD_CLOSURE_MANIFEST_SHA256
ARG OPENCLAW_RUNTIME_MANIFEST_SHA256
ARG OPENCLAW_RUNTIME_ARCHIVE_SHA256
ARG OPENCLAW_SOURCE_COMMIT
ARG OPENCLAW_SOURCE_TREE
ARG OPENCLAW_LOCK_SHA256
ARG OPENCLAW_TOOLCHAIN_SHA256
ARG OPENCLAW_PACKAGE_MANAGER_ARTIFACT_SHA256
ARG OPENCLAW_PACKAGE_MANAGER_ARTIFACT_CONTRACT_SHA256
ARG OPENCLAW_PACKAGE_MANAGER_DISTRIBUTION_SHA512
ARG OPENCLAW_OUTPUT_SHA256
ARG BRIDGE_NODE_MODULES_MANIFEST_SHA256
ARG BRIDGE_NODE_MODULES_ARCHIVE_SHA256
ARG BRIDGE_LOCK_SHA256
ARG BRIDGE_TOOLCHAIN_SHA256
ARG BRIDGE_PACKAGE_MANAGER_ARTIFACT_SHA256
ARG BRIDGE_PACKAGE_MANAGER_ARTIFACT_CONTRACT_SHA256
ARG BRIDGE_OUTPUT_SHA256
ARG PERSONAL_OPERATOR_RELEASE_COMMIT
ARG PERSONAL_OPERATOR_RELEASE_TREE
ARG PERSONAL_OPERATOR_CATALOG_SOURCE_SHA256
ARG PERSONAL_OPERATOR_CAPABILITY_CATALOG_DIGEST
ARG SOURCE_DATE_EPOCH=0

COPY build-closure/runtime-build-closure.json /tmp/runtime-build-closure/runtime-build-closure.json
COPY build-closure/openclaw-runtime.manifest.json /tmp/runtime-build-closure/openclaw-runtime.manifest.json
COPY build-closure/openclaw-runtime.tar.gz /tmp/runtime-build-closure/openclaw-runtime.tar.gz
COPY build-closure/bridge-node-modules.manifest.json /tmp/runtime-build-closure/bridge-node-modules.manifest.json
COPY build-closure/bridge-node-modules.tar.gz /tmp/runtime-build-closure/bridge-node-modules.tar.gz
COPY package.json package-lock.json /tmp/bridge-source/

# Authenticate canonical manifests and every archive member before extracting
# into its one allowed root. Build arguments are plan-bound values; empty,
# malformed, substituted, traversing, linked, or incomplete material fails.
RUN --network=none python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile

ROOT = Path("/tmp/runtime-build-closure")
SHA = re.compile(r"[0-9a-f]{64}")
SHA512 = re.compile(r"[0-9a-f]{128}")
COMMIT = re.compile(r"[0-9a-f]{40}")
PYTHON_BASE = (
    "public.ecr.aws/docker/library/python:3.13-slim@sha256:"
    "7f6f057c60bb4b050500ab319f5fd13f842bf2367b038b7362d1b3e416fa3d9d"
)
NODE_BASE = (
    "public.ecr.aws/docker/library/node:24.15.0-slim@sha256:"
    "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
)
AUDITED_OPENCLAW_VERSION = "2026.7.2"
AUDITED_OPENCLAW_COMMIT = "4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"
AUDITED_OPENCLAW_TREE = "33ee4a213f9b97795ac592b74b82789c5120fab5"
RUNTIME_BUILD_EXECUTOR_SHA256 = (
    "17ba93e0d1cdf5b279444507ba61bfa4ce9a05d0b4b83858586707a803838ad4"
)

def env(name, pattern=SHA):
    value = os.environ.get(name, "")
    if pattern.fullmatch(value) is None:
        raise SystemExit(f"invalid required build argument: {name}")
    return value

def digest(payload):
    return hashlib.sha256(payload).hexdigest()

python_base_rootfs_sha256 = env("PYTHON_BASE_ROOTFS_SHA256")
node_base_binary_sha256 = env("NODE_BASE_BINARY_SHA256")
if digest(Path("/usr/local/bin/node").read_bytes()) != node_base_binary_sha256:
    raise SystemExit("retained Node base binary digest differs")

def canonical(path, expected):
    payload = path.read_bytes()
    if digest(payload) != expected:
        raise SystemExit(f"digest differs: {path.name}")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid JSON: {path.name}") from error
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if encoded != payload:
        raise SystemExit(f"noncanonical JSON: {path.name}")
    return value

closure = canonical(
    ROOT / "runtime-build-closure.json",
    env("RUNTIME_BUILD_CLOSURE_MANIFEST_SHA256"),
)
if closure != {
    "schema": "personal-operator.runtime-build-closure.v2",
    "platform": "linux/arm64",
    "pythonBaseImage": PYTHON_BASE,
    "nodeBaseImage": NODE_BASE,
    "releaseCommit": env("PERSONAL_OPERATOR_RELEASE_COMMIT", COMMIT),
    "releaseTree": env("PERSONAL_OPERATOR_RELEASE_TREE", COMMIT),
    "components": closure.get("components"),
}:
    raise SystemExit("runtime build closure identity differs")

expected_components = {
    "openclaw-runtime": {
        "manifest": "openclaw-runtime.manifest.json",
        "archive": "openclaw-runtime.tar.gz",
        "manifestSha": env("OPENCLAW_RUNTIME_MANIFEST_SHA256"),
        "archiveSha": env("OPENCLAW_RUNTIME_ARCHIVE_SHA256"),
        "sourceCommit": env("OPENCLAW_SOURCE_COMMIT", COMMIT),
        "sourceTree": env("OPENCLAW_SOURCE_TREE", COMMIT),
        "manager": "pnpm",
        "managerVersion": "11.2.2",
        "lockSha": env("OPENCLAW_LOCK_SHA256"),
        "toolchainSha": env("OPENCLAW_TOOLCHAIN_SHA256"),
        "packageManagerArtifactSha": env(
            "OPENCLAW_PACKAGE_MANAGER_ARTIFACT_SHA256"
        ),
        "packageManagerArtifactContractSha": env(
            "OPENCLAW_PACKAGE_MANAGER_ARTIFACT_CONTRACT_SHA256"
        ),
        "packageManagerDistributionSha512": env(
            "OPENCLAW_PACKAGE_MANAGER_DISTRIBUTION_SHA512", SHA512
        ),
        "outputSha": env("OPENCLAW_OUTPUT_SHA256"),
        "prefix": "opt/openclaw/",
    },
    "bridge-node-modules": {
        "manifest": "bridge-node-modules.manifest.json",
        "archive": "bridge-node-modules.tar.gz",
        "manifestSha": env("BRIDGE_NODE_MODULES_MANIFEST_SHA256"),
        "archiveSha": env("BRIDGE_NODE_MODULES_ARCHIVE_SHA256"),
        "sourceCommit": env("PERSONAL_OPERATOR_RELEASE_COMMIT", COMMIT),
        "sourceTree": env("PERSONAL_OPERATOR_RELEASE_TREE", COMMIT),
        "manager": "npm",
        "managerVersion": "11.12.1",
        "lockSha": env("BRIDGE_LOCK_SHA256"),
        "toolchainSha": env("BRIDGE_TOOLCHAIN_SHA256"),
        "packageManagerArtifactSha": env(
            "BRIDGE_PACKAGE_MANAGER_ARTIFACT_SHA256"
        ),
        "packageManagerArtifactContractSha": env(
            "BRIDGE_PACKAGE_MANAGER_ARTIFACT_CONTRACT_SHA256"
        ),
        "packageManagerDistributionSha512": "",
        "outputSha": env("BRIDGE_OUTPUT_SHA256"),
        "prefix": "app/node_modules/",
    },
}
bindings = closure.get("components")
if not isinstance(bindings, list) or len(bindings) != 2:
    raise SystemExit("runtime build closure component set differs")
by_component = {item.get("component"): item for item in bindings if isinstance(item, dict)}
if set(by_component) != set(expected_components):
    raise SystemExit("runtime build closure component identities differ")

def expected_recipe(component, expected):
    return {
        "schema": "personal-operator.runtime-build-recipe.v1",
        "component": component,
        "sourceSubdirectory": (
            "." if component == "openclaw-runtime" else "bridge"
        ),
        "packagePath": "package.json",
        "lockPath": (
            "pnpm-lock.yaml"
            if component == "openclaw-runtime"
            else "package-lock.json"
        ),
        "packageManager": expected["manager"],
        "packageManagerVersion": expected["managerVersion"],
        "installMode": "offline-frozen-production",
        "buildMode": (
            "source-production" if component == "openclaw-runtime" else "none"
        ),
        "outputContract": "personal-operator.runtime-file-inventory.v1",
        "executorSha256": RUNTIME_BUILD_EXECUTOR_SHA256,
        "sourceDateEpoch": 0,
    }

for component, expected in expected_components.items():
    if (
        component == "openclaw-runtime"
        and expected["sourceCommit"] != AUDITED_OPENCLAW_COMMIT
    ):
        raise SystemExit("OpenClaw source is not the audited commit")
    if (
        component == "openclaw-runtime"
        and expected["sourceTree"] != AUDITED_OPENCLAW_TREE
    ):
        raise SystemExit("OpenClaw source is not the audited tree")
    binding = by_component[component]
    manifest = canonical(ROOT / expected["manifest"], expected["manifestSha"])
    if not isinstance(manifest, dict):
        raise SystemExit(f"component manifest is not an object: {component}")
    if binding != {
        "component": component,
        "manifestName": expected["manifest"],
        "manifestSha256": expected["manifestSha"],
        "archiveName": expected["archive"],
        "archiveSha256": expected["archiveSha"],
        "sourceArchiveSha256": manifest.get("sourceArchiveSha256"),
        "buildRecipeSha256": manifest.get("buildRecipeSha256"),
        "packageManagerArtifactSha256": expected[
            "packageManagerArtifactSha"
        ],
        "packageManagerArtifactContractSha256": expected[
            "packageManagerArtifactContractSha"
        ],
        "packageManagerDistributionSha512": expected[
            "packageManagerDistributionSha512"
        ],
        "outputSha256": manifest.get("outputSha256"),
    }:
        raise SystemExit(f"closure binding differs: {component}")
    archive_path = ROOT / expected["archive"]
    archive_payload = archive_path.read_bytes()
    if digest(archive_payload) != expected["archiveSha"]:
        raise SystemExit(f"archive digest differs: {component}")
    required = {
        "schema",
        "component",
        "sourceCommit",
        "sourceTree",
        "sourceArchiveSha256",
        "sourceArchiveSize",
        "packagePath",
        "packageSha256",
        "lockPath",
        "lockSha256",
        "buildRecipe",
        "buildRecipeSha256",
        "toolchain",
        "toolchainSha256",
        "dependencyMode",
        "outputSha256",
        "files",
        "archiveName",
        "archiveSha256",
        "archiveSize",
    }
    if set(manifest) != required:
        raise SystemExit(f"component fields differ: {component}")
    if (
        manifest["schema"] != "personal-operator.runtime-build-component.v2"
        or manifest["component"] != component
        or manifest["sourceCommit"] != expected["sourceCommit"]
        or manifest["sourceTree"] != expected["sourceTree"]
        or manifest["lockSha256"] != expected["lockSha"]
        or manifest["toolchainSha256"] != expected["toolchainSha"]
        or manifest["outputSha256"] != expected["outputSha"]
        or manifest["dependencyMode"] != "production"
        or manifest["archiveName"] != expected["archive"]
        or manifest["archiveSha256"] != expected["archiveSha"]
        or manifest["archiveSize"] != len(archive_payload)
        or manifest["packagePath"] != "package.json"
        or manifest["lockPath"]
        != (
            "pnpm-lock.yaml"
            if component == "openclaw-runtime"
            else "package-lock.json"
        )
        or not isinstance(manifest["sourceArchiveSize"], int)
        or isinstance(manifest["sourceArchiveSize"], bool)
        or not 1 <= manifest["sourceArchiveSize"] <= 64 * 1024 * 1024
    ):
        raise SystemExit(f"component identity differs: {component}")
    for field in (
        "sourceArchiveSha256",
        "packageSha256",
        "lockSha256",
        "buildRecipeSha256",
        "toolchainSha256",
        "outputSha256",
    ):
        if not isinstance(manifest[field], str) or SHA.fullmatch(manifest[field]) is None:
            raise SystemExit(f"component digest is invalid: {component}:{field}")
    recipe = expected_recipe(component, expected)
    if (
        manifest["buildRecipe"] != recipe
        or digest(json.dumps(
            recipe,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()) != manifest["buildRecipeSha256"]
    ):
        raise SystemExit(f"build recipe differs: {component}")
    expected_toolchain = {
        "platform": "linux/arm64",
        "builderImage": NODE_BASE,
        "nodeBaseImage": NODE_BASE,
        "pythonBaseImage": PYTHON_BASE,
        "nodeVersion": "24.15.0",
        "packageManager": expected["manager"],
        "packageManagerVersion": expected["managerVersion"],
        "packageManagerArtifactSha256": expected[
            "packageManagerArtifactSha"
        ],
        "packageManagerArtifactContractSha256": expected[
            "packageManagerArtifactContractSha"
        ],
        "packageManagerDistributionSha512": expected[
            "packageManagerDistributionSha512"
        ],
        "networkMode": "none",
        "noCache": True,
        "pull": False,
        "sourceDateEpoch": 0,
    }
    if (
        not isinstance(expected_toolchain["packageManagerArtifactSha256"], str)
        or SHA.fullmatch(expected_toolchain["packageManagerArtifactSha256"])
        is None
        or manifest["toolchain"] != expected_toolchain
    ):
        raise SystemExit(f"toolchain identity differs: {component}")
    if digest(json.dumps(
        manifest["toolchain"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()) != expected["toolchainSha"]:
        raise SystemExit(f"toolchain digest differs: {component}")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise SystemExit(f"empty component inventory: {component}")
    if digest(json.dumps(
        files,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()) != expected["outputSha"]:
        raise SystemExit(f"output inventory digest differs: {component}")
    inventory = {item["path"]: item for item in files}
    if len(inventory) != len(files):
        raise SystemExit(f"duplicate component inventory: {component}")
    observed = set()
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (
                not member.isreg()
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or not member.name.startswith(expected["prefix"])
                or member.uid != 0
                or member.gid != 0
                or member.mtime != 0
            ):
                raise SystemExit(f"unsafe archive member: {member.name}")
            relative = member.name.removeprefix("opt/openclaw/")
            if component == "bridge-node-modules":
                relative = "node_modules/" + member.name.removeprefix(
                    "app/node_modules/"
                )
            item = inventory.get(relative)
            source = archive.extractfile(member)
            payload = source.read() if source is not None else b""
            if (
                item is None
                or item != {
                    "path": relative,
                    "sha256": digest(payload),
                    "size": len(payload),
                    "mode": format(member.mode & 0o777, "04o"),
                }
            ):
                raise SystemExit(f"archive inventory differs: {member.name}")
            if component == "openclaw-runtime" and relative == "package.json":
                try:
                    package = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise SystemExit("OpenClaw package is invalid") from error
                if (
                    not isinstance(package, dict)
                    or package.get("version") != AUDITED_OPENCLAW_VERSION
                ):
                    raise SystemExit("OpenClaw version is not audited")
            target = Path("/") / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise SystemExit(f"archive target already exists: {member.name}")
            target.write_bytes(payload)
            target.chmod(member.mode & 0o777)
            observed.add(relative)
    expected_archive_files = set(inventory)
    if component == "bridge-node-modules":
        expected_archive_files = {
            path for path in inventory if path.startswith("node_modules/")
        }
    if observed != expected_archive_files:
        raise SystemExit(f"archive inventory incomplete: {component}")
    lock = inventory.get(manifest["lockPath"])
    package_file = inventory.get(manifest["packagePath"])
    if (
        lock is None
        or lock["sha256"] != expected["lockSha"]
        or package_file is None
        or package_file["sha256"] != manifest["packageSha256"]
    ):
        raise SystemExit(f"package or lock inventory differs: {component}")
    if component == "bridge-node-modules":
        bridge_lock = Path("/tmp/bridge-source/package-lock.json").read_bytes()
        bridge_package = Path("/tmp/bridge-source/package.json").read_bytes()
        if digest(bridge_lock) != expected["lockSha"]:
            raise SystemExit("bridge lock bytes differ from offline build")
        if digest(bridge_package) != inventory["package.json"]["sha256"]:
            raise SystemExit("bridge package bytes differ from offline build")

# Remove installer/build packages inherited from the Python base while keeping
# the requested runtime interpreter and CA bundle.
for path in Path("/usr/local/bin").glob("pip*"):
    path.unlink()
site = Path("/usr/local/lib/python3.13/site-packages")
for pattern in ("pip*", "setuptools*", "wheel*"):
    for path in site.glob(pattern):
        shutil.rmtree(path) if path.is_dir() else path.unlink()
for runtime_bootstrap in (
    "/usr/local/lib/python3.13/ensurepip/_bundled",
    "/usr/local/lib/python3.13/ensurepip",
    "/usr/local/lib/python3.13/venv",
):
    shutil.rmtree(runtime_bootstrap, ignore_errors=True)
for command in (
    "ar", "as", "apt", "apt-cache", "apt-cdrom", "apt-config", "apt-get",
    "apt-key", "apt-mark", "c++", "cc", "cmake", "corepack", "cpp",
    "dpkg", "dpkg-deb", "dpkg-divert", "dpkg-maintscript-helper",
    "dpkg-query", "dpkg-realpath", "dpkg-split", "dpkg-statoverride",
    "dpkg-trigger", "g++", "gcc", "git", "ld", "make", "ninja", "npm",
    "npx", "objcopy", "pip", "pip3", "pnpm", "ranlib", "strip",
    "update-alternatives", "yarn",
):
    for directory in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        path = Path(directory) / command
        if path.is_file() or path.is_symlink():
            path.unlink()
for state in ("/etc/apt", "/var/cache/apt", "/var/lib/apt", "/var/lib/dpkg"):
    shutil.rmtree(state, ignore_errors=True)
for log in ("/var/log/alternatives.log", "/var/log/dpkg.log"):
    path = Path(log)
    if path.exists():
        path.unlink()
shutil.rmtree(ROOT)
shutil.rmtree("/tmp/bridge-source")
PY

RUN --network=none mkdir -p /app /home/node /run/personal-operator/home /mnt/workspace /opt/personal-operator/seed/workspace && \
    ln -s /opt/openclaw/openclaw.mjs /usr/local/bin/openclaw && \
    ln -s /opt/openclaw /app/node_modules/openclaw && \
    chown 1000:1000 /run/personal-operator /run/personal-operator/home && \
    chmod 0700 /run/personal-operator /run/personal-operator/home && \
    chmod -R a-w /opt/openclaw && \
    chmod -R a-w /opt/personal-operator/seed

WORKDIR /app
ENV NODE_PATH=/app/node_modules
ENV AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1
ENV HOME=/run/personal-operator/home TMPDIR=/tmp

COPY package.json /app/package.json
COPY agentcore-contract.js /app/agentcore-contract.js
COPY agentcore-proxy.js /app/agentcore-proxy.js
COPY lightweight-agent.js /app/lightweight-agent.js
COPY runtime-policy.js /app/runtime-policy.js
COPY capability-catalog.js /app/capability-catalog.js
COPY capability-relay.js /app/capability-relay.js
COPY capabilities/catalog-v1.json /app/capabilities/catalog-v1.json
COPY capabilities/schemas /app/capabilities/schemas
COPY gateway-invocation.js /app/gateway-invocation.js
COPY session-binding.js /app/session-binding.js
COPY invocation-handler.js /app/invocation-handler.js
COPY workspace-path-policy.js /app/workspace-path-policy.js
COPY workspace-manifest.js /app/workspace-manifest.js
COPY sqlite-snapshot.js /app/sqlite-snapshot.js
COPY workspace-sync.js /app/workspace-sync.js
COPY workspace-lifecycle.js /app/workspace-lifecycle.js
COPY workspace-s3-client.js /app/workspace-s3-client.js
COPY cloudwatch-logger.js /app/cloudwatch-logger.js
COPY scoped-credentials.js /app/scoped-credentials.js
COPY force-ipv4.js /app/force-ipv4.js
COPY plugins/personal-operator /app/plugins/personal-operator
COPY entrypoint.sh /app/entrypoint.sh

RUN --network=none python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re

commit = os.environ.get("PERSONAL_OPERATOR_RELEASE_COMMIT", "")
catalog_source = os.environ.get("PERSONAL_OPERATOR_CATALOG_SOURCE_SHA256", "")
catalog_digest = os.environ.get("PERSONAL_OPERATOR_CAPABILITY_CATALOG_DIGEST", "")
if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
    raise SystemExit("release commit is invalid")
if re.fullmatch(r"[0-9a-f]{64}", catalog_source) is None:
    raise SystemExit("catalog source digest is invalid")
if re.fullmatch(r"[0-9a-f]{64}", catalog_digest) is None:
    raise SystemExit("compiled catalog digest is invalid")
catalog_path = Path("/app/capabilities/catalog-v1.json")
if hashlib.sha256(catalog_path.read_bytes()).hexdigest() != catalog_source:
    raise SystemExit("catalog source bytes differ")
Path("/app/capabilities/release-v1.json").write_bytes(
    json.dumps(
        {
            "schema": "personal-operator.capability-release.v1",
            "releaseCommit": commit,
            "catalogDigest": catalog_digest,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"
)
PY

RUN --network=none chmod 0755 /app/entrypoint.sh && \
    chown -R 0:0 /app /opt/openclaw /opt/personal-operator/seed /home && \
    chmod -R a-w /app /opt/openclaw /opt/personal-operator/seed /home && \
    chmod a-w /var/tmp && \
    node -e 'require("./capability-catalog").loadRuntimeCapabilityRelease()' && \
    test "$(node --version)" = "v24.15.0" && \
    test -f /etc/ssl/certs/ca-certificates.crt && \
    ! python3 -m ensurepip --version && \
    ! python3 -m pip --version && \
    for command in ar as apt apt-cache apt-cdrom apt-config apt-get apt-key apt-mark c++ cc cmake corepack cpp dpkg dpkg-deb dpkg-divert dpkg-maintscript-helper dpkg-query dpkg-realpath dpkg-split dpkg-statoverride dpkg-trigger g++ gcc git ld make ninja npm npx objcopy pip pip3 pnpm ranlib strip update-alternatives yarn; do \
        if command -v "$command" >/dev/null 2>&1; then exit 1; fi; \
    done

EXPOSE 8080
USER 1000:1000
ENTRYPOINT ["/app/entrypoint.sh"]
