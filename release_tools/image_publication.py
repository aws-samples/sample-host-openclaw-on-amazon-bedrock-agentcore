"""Plan-bound, injected runtime-image production and ECR publication.

This module deliberately owns no network, SDK, or credential construction.
Callers inject an exact Git object exporter, two fresh OCI builds, a hermetic
runtime probe, and the ECR mutation API.  The resulting publication plan and
one-effect private request artifacts are immutable before registry mutation.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass, replace
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tarfile
from typing import (
    Any,
    BinaryIO,
    Iterator,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)
import zlib

from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AwsAuthorityError,
)
from release_tools.contracts import (
    ContractError,
    ReleasePlanV2,
    StagingTransactionV2,
    VerifiedPrivateMutationV2,
    _completed_prefix_sha256,
    _release_operation_sha256,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)


REPOSITORY_NAME = "personal-operator/bridge"
REQUIRED_REGION = "eu-west-1"
PLATFORM = "linux/arm64"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
    }
)
OCI_EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
SBOM_ARTIFACT_TYPE = "application/spdx+json"
PROVENANCE_ARTIFACT_TYPE = "application/vnd.in-toto+json"
PROVENANCE_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PROVENANCE_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BRIDGE_BUILD_TYPE = "https://personal-operator.invalid/build/bridge-v2"
MAX_GIT_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_BUILD_CONTEXT_BYTES = 64 * 1024 * 1024
MAX_BLOB_BYTES = 4 * 1024 * 1024 * 1024
MAX_PRIVATE_EFFECT_BYTES = 8 * 1024 * 1024 * 1024
IMAGE_EFFECT_MAGIC = b"PO-IMAGE-EFFECT-V1\0"
PYTHON_RUNTIME_BASE = (
    "public.ecr.aws/docker/library/python:3.13-slim@sha256:"
    "7f6f057c60bb4b050500ab319f5fd13f842bf2367b038b7362d1b3e416fa3d9d"
)
NODE_RUNTIME_BASE = (
    "public.ecr.aws/docker/library/node:24.15.0-slim@sha256:"
    "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
)
OPENCLAW_RUNTIME_VERSION = "2026.7.2"
OPENCLAW_RUNTIME_COMMIT = "4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"
OPENCLAW_RUNTIME_TREE = "33ee4a213f9b97795ac592b74b82789c5120fab5"
RUNTIME_BUILD_BUILDER_IMAGE = NODE_RUNTIME_BASE
REVIEWED_RUNTIME_DOCKERFILE_SHA256 = (
    "1a0bcc888465b4c6a11ed8bc0edf5ce4d1a9a7e52c4f3b8a5475cd9759d74161"
)
RUNTIME_PACKAGE_MANAGERS = {
    "openclaw-runtime": ("pnpm", "11.2.2"),
    "bridge-node-modules": ("npm", "11.12.1"),
}
OPENCLAW_PACKAGE_MANAGER_IDENTITY = (
    "pnpm@11.2.2+sha512."
    "36e6621fad506178936455e70247b8808ef4ec25797a9f437a93281a020484e2"
    "607f6a469a22e982987c3dbb8866e3071514ab10a4a1749e06edcd1ec118436f"
)
OPENCLAW_PNPM_DISTRIBUTION_SHA512 = OPENCLAW_PACKAGE_MANAGER_IDENTITY.rsplit(
    ".", 1
)[1]
BRIDGE_NPM_CLI = "/usr/local/lib/node_modules/npm/bin/npm-cli.js"
# Release-blocking review inputs.  A production closure cannot be prepared
# until the exact offline artifacts have been independently reviewed and their
# digests are committed here.  ``None`` is deliberately not a wildcard.
REVIEWED_RUNTIME_PACKAGE_MANAGER_ARTIFACT_SHA256: dict[str, str | None] = {
    "openclaw-runtime": None,
    "bridge-node-modules": None,
}
RUNTIME_BUILD_EXECUTOR = r"""
set -eu
umask 022
mkdir -p /work/source /work/package-manager /work/home /work/tmp /output/payload
tar -xf /input/source.tar -C /work/source
tar -xf /input/offline-package-manager.tar -C /work/package-manager
case "$PERSONAL_OPERATOR_BUILD_COMPONENT" in
  openclaw-runtime)
    cd /work/source
    mkdir -p /work/pnpm
    tar -xf /work/package-manager/pnpm-distribution.tgz -C /work/pnpm
    test "$(node /work/pnpm/package/bin/pnpm.cjs --version)" = "11.2.2"
    node /work/pnpm/package/bin/pnpm.cjs install \
      --offline --frozen-lockfile \
      --store-dir=/work/package-manager/store
    node /work/pnpm/package/bin/pnpm.cjs run build
    node /work/pnpm/package/bin/pnpm.cjs prune \
      --prod --offline --store-dir=/work/package-manager/store
    cp -LR openclaw.mjs package.json pnpm-lock.yaml dist node_modules \
      /output/payload/
    ;;
  bridge-node-modules)
    cd /work/source/bridge
    test "$(node /usr/local/lib/node_modules/npm/bin/npm-cli.js --version)" = "11.12.1"
    node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci \
      --offline --omit=dev --ignore-scripts \
      --cache=/work/package-manager/cache
    cp package.json package-lock.json /output/payload/
    cp -LR node_modules /output/payload/
    ;;
  *) exit 64 ;;
esac
find /output/payload -exec touch -h -d @0 {} +
""".strip().encode("utf-8")
CAPABILITY_CATALOG_SOURCE_SHA256 = (
    "b4385b54dfa5aaa7ecf2e916111e44248b647b15208432bb9d31883c26e87a26"
)
CAPABILITY_TOOL_NAMES = (
    "po_file_list",
    "po_file_read",
    "po_file_write",
    "po_file_delete",
    "po_web_read",
    "po_schedule_list",
    "po_schedule_propose",
    "po_schedule_cancel_propose",
    "po_compute_run",
    "po_compute_status",
)
CAPABILITY_SCHEMA_SHA256 = {
    "po-compute-run-input.json": "01cf09ff29529611b51bbee73b86f32b99a6814f7319ba27b18bef5e579b2a1d",
    "po-compute-run-output.json": "ea26fc131f78e0377d5fbda50e8b1b9cf688d1c43f5a3a61dd3bd66953c08eb4",
    "po-compute-status-input.json": "54c5277b5f0b1da875e5c4321161cff110ab3ef9048409c9f1e7d7a70ab938d8",
    "po-compute-status-output.json": "144dd0d56b7897d0dfe225fdb20780e30225cddf7da19b7acbdbbd4ab1618c08",
    "po-file-delete-input.json": "b731089b54f1c87185741f0045d04555a1e77fb05ded2c7a7e3ce4389759b224",
    "po-file-delete-output.json": "657d345a47d261692729a01ca96a6cd0d9b9b65c7bb862989503ee111cfd2d19",
    "po-file-list-input.json": "35e141ebe098d3cbc73ef1655e93ed743520f76cab08a533574c1262330d344a",
    "po-file-list-output.json": "6a6e9fe40d241f055d5509e7c337a1c279ba4e175fbddfed5bd9cc76ad09663f",
    "po-file-read-input.json": "adf79b35ad5da0e5ccfba630254e1b8cb6b0ee41db07cfe34d7d5004851bfb5f",
    "po-file-read-output.json": "f6d5ec6a082465d3b66d4af47bdc41cc0fdcded0adfed706e14d18019188b34a",
    "po-file-write-input.json": "3c83b398c5709887c626fd70d36e251cce8a0c96c7b28080c530ff2c587d652d",
    "po-file-write-output.json": "efacdca9b890b9ba2cb239a25488f487d00c4a6f070f532b2910d6cbd88e0052",
    "po-schedule-cancel-propose-input.json": "fdee4f975779c5e83d216d20b1aabd68623c257903bd1d86ac3b776116383f15",
    "po-schedule-cancel-propose-output.json": "311792ffe90921e24eca03e043c21b5b790e32be1046a0dd533b148d0c98fa78",
    "po-schedule-list-input.json": "1ea525d11a67b6581efb9f064818e5da4782914ed8e7ef93c295ab3c93e3a710",
    "po-schedule-list-output.json": "605a0da1316c22151b3e64d8be0a6fc5168353e7e9a99097ec35758f6c4986fa",
    "po-schedule-propose-input.json": "e3586bade20d0d184b65d5c79b41c22703c4feccd9fdbdb137fafc1006b3f12b",
    "po-schedule-propose-output.json": "8a82e3987b4f193e6c0edf8ca98245c950183883bf688f1d3690f7a05410198d",
    "po-web-read-input.json": "3ce72f18e68a9c53329670e403dbf176c8fb5203d25d23438f29f5285defd80f",
    "po-web-read-output.json": "c241a4bd2a4c986821f0aa71b4c6c883319c43598e3904e18e52ece8ad6e99e5",
}
FORBIDDEN_RUNTIME_COMMANDS = (
    "ar",
    "as",
    "apt",
    "apt-cache",
    "apt-cdrom",
    "apt-config",
    "apt-get",
    "apt-key",
    "apt-mark",
    "c++",
    "cc",
    "cmake",
    "corepack",
    "cpp",
    "dpkg",
    "dpkg-deb",
    "dpkg-divert",
    "dpkg-maintscript-helper",
    "dpkg-query",
    "dpkg-realpath",
    "dpkg-split",
    "dpkg-statoverride",
    "dpkg-trigger",
    "g++",
    "gcc",
    "git",
    "ld",
    "make",
    "ninja",
    "npm",
    "npx",
    "objcopy",
    "pip",
    "pip3",
    "pnpm",
    "ranlib",
    "strip",
    "update-alternatives",
    "yarn",
)

_SHA_40 = re.compile(r"[0-9a-f]{40}")
_SHA_64 = re.compile(r"[0-9a-f]{64}")
_SHA_128 = re.compile(r"[0-9a-f]{128}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_CREATED = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_NETWORK_BUILD_INPUT = re.compile(
    r"(?:\bapt-get\s+(?:update|install)\b|\bgit\s+fetch\b|"
    r"\bcorepack\s+prepare\b|\bpnpm\s+install\b|\bnpm\s+ci\b)",
    re.IGNORECASE,
)
_CREDENTIAL_ENV = re.compile(
    r"(?:ACCESS_KEY|SECRET|SESSION_TOKEN|PASSWORD|CREDENTIAL|API_KEY|PRIVATE_KEY)",
    re.IGNORECASE,
)
_EXCLUDED_COMPONENTS = frozenset(
    {".git", ".pytest_cache", "__pycache__", "node_modules"}
)
_REQUIRED_BUILD_FILES = frozenset(
    {
        "bridge/Dockerfile",
        "bridge/package.json",
        "bridge/package-lock.json",
        "bridge/entrypoint.sh",
        "bridge/agentcore-contract.js",
        "bridge/capabilities/catalog-v1.json",
    }
)
_REQUIRED_RUNTIME_FILES = frozenset(
    {
        "/app/agentcore-contract.js",
        "/app/capabilities/release-v1.json",
        "/app/entrypoint.sh",
        "/etc/ssl/certs/ca-certificates.crt",
        "/opt/openclaw/openclaw.mjs",
    }
)


def _forbidden_browser_path(path: str) -> bool:
    lowered = path.casefold()
    parts = PurePosixPath(lowered).parts
    return (
        any("playwright" in part or "puppeteer" in part for part in parts)
        or any(part in {"chrome", "chromium", "chrome-headless-shell"} for part in parts)
        or any(
            marker in lowered
            for marker in (
                "browser-gateway",
                "browser_gateway",
                "agentcore-browser",
                "/browsers/",
                "/browser-tools/",
            )
        )
    )


class ImagePublicationError(RuntimeError):
    """The immutable image-production or publication contract failed closed."""


class BuildReproducibilityError(ImagePublicationError):
    """Two fresh builds did not produce one byte-identical OCI closure."""


class ArtifactSubstitutionError(ImagePublicationError):
    """Plan-bound manifest or blob bytes were replaced."""


class ImagePublicationAmbiguous(ImagePublicationError):
    """A registry mutation may have happened but exact persistence is unknown."""


class ImagePublicationCollision(ImagePublicationError):
    """An immutable registry subject already occupies the planned identity."""


class RuntimeBuildClosureError(ImagePublicationError):
    """Independent offline runtime-build materials are incomplete or unequal."""


class GitArchiveExporter(Protocol):
    def export_archive(
        self,
        *,
        source_commit: str,
        source_tree: str,
        path: str,
    ) -> bytes: ...


class OciBuilder(Protocol):
    def build(self, archive: bytes, **kwargs: Any) -> Mapping[str, Any]: ...


class ImageProbe(Protocol):
    def run(self, **kwargs: Any) -> Mapping[str, Any]: ...


class EcrMutationClient(Protocol):
    def batch_check_layer_availability(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def initiate_layer_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def upload_layer_part(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def complete_layer_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_image(self, **kwargs: Any) -> Mapping[str, Any]: ...


class RuntimeSourceExporter(Protocol):
    def export_runtime_source(
        self,
        *,
        component: str,
        attempt: int,
        source_commit: str,
        source_tree: str,
    ) -> bytes: ...


class RuntimeDependencyBuilder(Protocol):
    def build_runtime(self, **kwargs: Any) -> Mapping[str, Any]: ...


_ARTIFACT_STREAM_CHUNK = 1024 * 1024


def _stream_reader_sha256_size(
    reader: Any,
    *,
    chunk: int = _ARTIFACT_STREAM_CHUNK,
    extra: Any = None,
) -> tuple[str, int]:
    """Consume a reader in bounded chunks; never issue an unbounded read.

    This is the single bounded-read primitive for every retained-file stream.
    Reads are always sized, so a full artifact can never be materialized by a
    single ``read()`` call.  ``extra`` optionally receives the same chunks (for
    a second digest) and must expose ``update``.
    """

    if not isinstance(chunk, int) or chunk <= 0:
        raise RuntimeBuildClosureError("artifact stream chunk is invalid")
    digest = hashlib.sha256()
    size = 0
    while True:
        block = reader.read(chunk)
        if not block:
            break
        digest.update(block)
        if extra is not None:
            extra.update(block)
        size += len(block)
    return digest.hexdigest(), size


@runtime_checkable
class ArtifactSource(Protocol):
    """A validated, streamable package-manager artifact byte source."""

    @property
    def size(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    def open(self) -> Any: ...

    def stream_into(self, dest_path: Path) -> None: ...


class _InMemoryArtifactSource:
    """Backward-compatible source for callers that still hold artifact bytes.

    Only small, hostile-fixture and development-evidence callers supply bytes;
    the production consumer path never materializes an artifact this way.
    """

    __slots__ = ("_payload", "_sha256")

    def __init__(self, payload: bytes) -> None:
        if not isinstance(payload, (bytes, bytearray)):
            raise RuntimeBuildClosureError(
                "package-manager artifact source bytes are invalid"
            )
        self._payload = bytes(payload)
        self._sha256 = _sha256(self._payload)

    @property
    def size(self) -> int:
        return len(self._payload)

    @property
    def sha256(self) -> str:
        return self._sha256

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        stream = io.BytesIO(self._payload)
        try:
            yield stream
        finally:
            stream.close()

    def stream_into(self, dest_path: Path) -> None:
        with self.open() as reader:
            _stream_copy_exclusive(reader, dest_path, self.size)


@dataclass(frozen=True, slots=True)
class RetainedRegularFile:
    """A large artifact referenced by a retained descriptor, never by bytes.

    Establishment opens the caller path once with ``O_NOFOLLOW``, confirms a
    regular file, streams its sha256 in bounded chunks, and keeps the exact
    descriptor.  Every later read is of the *same* inode: the caller path is
    never reopened.  Any post-validation size/inode/digest drift fails closed.
    """

    path: str
    size: int
    sha256: str
    _dev: int
    _ino: int
    _fd: int

    @classmethod
    def establish(
        cls, path: Path, *, label: str = "package-manager artifact"
    ) -> "RetainedRegularFile":
        try:
            fd = os.open(
                os.fspath(path),
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            raise RuntimeBuildClosureError(
                f"{label} is unavailable"
            ) from error
        try:
            meta = os.fstat(fd)
            if (
                not stat.S_ISREG(meta.st_mode)
                or not 1 <= meta.st_size <= MAX_BLOB_BYTES
            ):
                raise RuntimeBuildClosureError(
                    f"{label} is not a bounded regular file"
                )
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(fd, _ARTIFACT_STREAM_CHUNK)
                if not block:
                    break
                digest.update(block)
                size += len(block)
            if size != meta.st_size:
                raise RuntimeBuildClosureError(
                    f"{label} changed while being read"
                )
        except BaseException:
            os.close(fd)
            raise
        return cls(
            os.path.abspath(os.fspath(path)),
            size,
            digest.hexdigest(),
            meta.st_dev,
            meta.st_ino,
            fd,
        )

    def _revalidate(self) -> None:
        if self._fd < 0:
            raise RuntimeBuildClosureError(
                "retained package-manager artifact is closed"
            )
        try:
            meta = os.fstat(self._fd)
        except OSError as error:
            raise RuntimeBuildClosureError(
                "retained package-manager artifact is unavailable"
            ) from error
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_dev != self._dev
            or meta.st_ino != self._ino
            or meta.st_size != self.size
        ):
            raise RuntimeBuildClosureError(
                "retained package-manager artifact inode differs"
            )

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        self._revalidate()
        duplicate = os.dup(self._fd)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            stream = os.fdopen(duplicate, "rb", closefd=True)
        except OSError as error:
            os.close(duplicate)
            raise RuntimeBuildClosureError(
                "retained package-manager artifact cannot be read"
            ) from error
        try:
            yield stream
        finally:
            stream.close()

    def stream_into(self, dest_path: Path) -> None:
        with self.open() as reader:
            observed = _stream_copy_exclusive(reader, dest_path, self.size)
        if observed != self.size:
            raise RuntimeBuildClosureError(
                "retained package-manager artifact stream is truncated"
            )

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            object.__setattr__(self, "_fd", -1)

    def __enter__(self) -> "RetainedRegularFile":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _stream_copy_exclusive(reader: Any, dest_path: Path, size: int) -> int:
    descriptor = os.open(
        os.fspath(dest_path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    written = 0
    try:
        while True:
            block = reader.read(_ARTIFACT_STREAM_CHUNK)
            if not block:
                break
            view = memoryview(block)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise RuntimeBuildClosureError(
                        "package-manager artifact stream write is short"
                    )
                view = view[count:]
            written += len(block)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if written > size:
        raise RuntimeBuildClosureError(
            "package-manager artifact stream exceeds its bound"
        )
    return written


def _coerce_artifact_source(source: Any) -> ArtifactSource:
    if isinstance(source, RetainedRegularFile):
        return source
    if isinstance(source, (bytes, bytearray)):
        return _InMemoryArtifactSource(bytes(source))
    if (
        hasattr(source, "open")
        and hasattr(source, "stream_into")
        and hasattr(source, "sha256")
        and hasattr(source, "size")
    ):
        return source
    raise RuntimeBuildClosureError(
        "package-manager artifact source is invalid"
    )


@dataclass(frozen=True, slots=True)
class PackageManagerArtifact:
    manager: str
    version: str
    source: ArtifactSource
    reviewed_sha256: str
    distribution_sha512: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source", _coerce_artifact_source(self.source)
        )


def reviewed_package_manager_artifact(
    *,
    component: str,
    payload: Any,
) -> PackageManagerArtifact:
    """Bind an exact artifact source to the committed production review input.

    The checked-in digest map intentionally remains open until real offline
    artifacts are reviewed.  Supplying bytes, a retained file, or a digest on
    the command line cannot close this gate.  The artifact digest is streamed,
    never materialized, against the pinned ``reviewed_sha256``.
    """

    try:
        manager, version = RUNTIME_PACKAGE_MANAGERS[component]
        reviewed_sha256 = (
            REVIEWED_RUNTIME_PACKAGE_MANAGER_ARTIFACT_SHA256[component]
        )
    except KeyError as error:
        raise RuntimeBuildClosureError(
            "package-manager component is not reviewed"
        ) from error
    if (
        not isinstance(reviewed_sha256, str)
        or _SHA_64.fullmatch(reviewed_sha256) is None
    ):
        raise RuntimeBuildClosureError(
            f"reviewed package-manager artifact digest is not pinned: {component}"
        )
    source = _coerce_artifact_source(payload)
    if source.sha256 != reviewed_sha256:
        raise RuntimeBuildClosureError(
            f"reviewed package-manager artifact digest differs: {component}"
        )
    distribution_sha512 = (
        OPENCLAW_PNPM_DISTRIBUTION_SHA512
        if component == "openclaw-runtime"
        else ""
    )
    return PackageManagerArtifact(
        manager,
        version,
        source,
        reviewed_sha256,
        distribution_sha512,
    )


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ImagePublicationError("publication value is not canonical JSON") from error


def _strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload:
        raise ImagePublicationError(f"{label} bytes are empty")

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ImagePublicationError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=exact_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImagePublicationError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ImagePublicationError(f"{label} is not an object")
    return value


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ImagePublicationError(f"{label} is not an object")
    return dict(value)


def _exact_mapping(value: Any, fields: set[str], *, label: str) -> dict[str, Any]:
    result = _mapping(value, label=label)
    if set(result) != fields:
        raise ImagePublicationError(f"{label} fields are not exact")
    return result


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(payload: bytes) -> str:
    return "sha256:" + _sha256(payload)


def _require_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ImagePublicationError(f"{label} digest is malformed")
    return value


def _identity(
    *,
    source_commit: str,
    source_tree: str,
    account: str,
    region: str,
) -> None:
    if not isinstance(source_commit, str) or _SHA_40.fullmatch(source_commit) is None:
        raise ImagePublicationError("source commit is not canonical")
    if not isinstance(source_tree, str) or _SHA_40.fullmatch(source_tree) is None:
        raise ImagePublicationError("source tree is not canonical")
    if (
        not isinstance(account, str)
        or _ACCOUNT.fullmatch(account) is None
        or account == "000000000000"
    ):
        raise ImagePublicationError("release account is not canonical")
    if region != REQUIRED_REGION:
        raise ImagePublicationError(f"release region must be exactly {REQUIRED_REGION}")


@dataclass(frozen=True, slots=True)
class BuilderDependency:
    uri: str
    digest: str

    def to_mapping(self) -> dict[str, str]:
        return {"uri": self.uri, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class OciDescriptor:
    media_type: str
    digest: str
    size: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class RuntimeFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ProbeEvidenceDescriptor:
    build_id: str
    sha256: str
    size: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "buildId": self.build_id,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "ProbeEvidenceDescriptor":
        value = _exact_mapping(
            raw, {"buildId", "sha256", "size"}, label="probe evidence descriptor"
        )
        build_id = value["buildId"]
        sha256 = value["sha256"]
        size = value["size"]
        if build_id not in {"fresh-1", "fresh-2"}:
            raise ImagePublicationError("probe evidence build identity differs")
        if not isinstance(sha256, str) or _SHA_64.fullmatch(sha256) is None:
            raise ImagePublicationError("probe evidence digest is malformed")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= 1024 * 1024
        ):
            raise ImagePublicationError("probe evidence size is invalid")
        return cls(build_id, sha256, size)


@dataclass(frozen=True, slots=True)
class _ClosureFile:
    path: str
    payload: bytes
    mode: str

    def inventory(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": _sha256(self.payload),
            "size": len(self.payload),
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class _BuildMaterial:
    component: str
    source_commit: str
    source_tree: str
    source_archive_sha256: str
    source_archive_size: int
    package_path: str
    package_sha256: str
    lock_path: str
    lock_sha256: str
    build_recipe: tuple[tuple[str, object], ...]
    build_recipe_sha256: str
    toolchain: tuple[tuple[str, object], ...]
    toolchain_sha256: str
    dependency_mode: str
    files: tuple[_ClosureFile, ...]

    def toolchain_mapping(self) -> dict[str, object]:
        return dict(self.toolchain)

    def build_recipe_mapping(self) -> dict[str, object]:
        return dict(self.build_recipe)

    @property
    def output_sha256(self) -> str:
        return _sha256(
            _canonical_json([file.inventory() for file in self.files])
        )


@dataclass(frozen=True, slots=True)
class RuntimeBuildClosure:
    artifacts: Mapping[str, bytes]
    manifest_sha256: str
    reviewed_package_manager_artifacts: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _OciClosure:
    manifest: bytes
    manifest_descriptor: OciDescriptor
    config_descriptor: OciDescriptor
    layer_descriptors: tuple[OciDescriptor, ...]
    blobs: tuple[tuple[str, bytes], ...]
    inventory: tuple[RuntimeFile, ...]

    def blob_mapping(self) -> dict[str, bytes]:
        return dict(self.blobs)


@dataclass(frozen=True, slots=True)
class ImagePublicationPlanV1:
    source_commit: str
    source_tree: str
    account: str
    region: str
    git_archive_sha256: str
    build_archive_sha256: str
    build_archive_size: int
    catalog_source_sha256: str
    capability_catalog_digest: str
    model_callable_tools: tuple[str, ...]
    created: str
    builder_id: str
    builder_dependencies: tuple[BuilderDependency, ...]
    subject: OciDescriptor
    config: OciDescriptor
    layers: tuple[OciDescriptor, ...]
    sbom_payload: OciDescriptor
    sbom_manifest: OciDescriptor
    provenance_payload: OciDescriptor
    provenance_manifest: OciDescriptor
    probe_evidence: tuple[ProbeEvidenceDescriptor, ...]

    SCHEMA = "personal-operator.image-publication-plan.v1"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ImagePublicationPlanV1":
        fields = {
            "schema",
            "sourceCommit",
            "sourceTree",
            "account",
            "region",
            "repositoryName",
            "commitTag",
            "platform",
            "gitArchiveSha256",
            "buildArchiveSha256",
            "buildArchiveSize",
            "catalogSourceSha256",
            "capabilityCatalogDigest",
            "modelCallableTools",
            "created",
            "builderId",
            "builderDependencies",
            "subject",
            "config",
            "layers",
            "sbom",
            "provenance",
            "probeEvidence",
        }
        value = _exact_mapping(raw, fields, label="image publication plan")
        if value["schema"] != cls.SCHEMA:
            raise ImagePublicationError("image publication plan schema is invalid")
        commit = value["sourceCommit"]
        tree = value["sourceTree"]
        account = value["account"]
        region = value["region"]
        _identity(
            source_commit=commit,
            source_tree=tree,
            account=account,
            region=region,
        )
        if value["repositoryName"] != REPOSITORY_NAME:
            raise ImagePublicationError("image publication repository is invalid")
        if value["commitTag"] != f"commit-{commit}":
            raise ImagePublicationError("image publication commit tag differs")
        if value["platform"] != PLATFORM:
            raise ImagePublicationError("image publication platform is not ARM64")
        archive_sha = value["gitArchiveSha256"]
        build_sha = value["buildArchiveSha256"]
        catalog_source_sha = value["catalogSourceSha256"]
        catalog_digest = value["capabilityCatalogDigest"]
        for label, digest in (
            ("Git archive", archive_sha),
            ("build archive", build_sha),
            ("catalog source", catalog_source_sha),
            ("capability catalog", catalog_digest),
        ):
            if not isinstance(digest, str) or _SHA_64.fullmatch(digest) is None:
                raise ImagePublicationError(f"{label} digest is malformed")
        raw_probe_evidence = value["probeEvidence"]
        if not isinstance(raw_probe_evidence, list) or len(raw_probe_evidence) != 2:
            raise ImagePublicationError("probe evidence inventory differs")
        probe_evidence = tuple(
            ProbeEvidenceDescriptor.from_mapping(item)
            for item in raw_probe_evidence
        )
        if [item.build_id for item in probe_evidence] != ["fresh-1", "fresh-2"]:
            raise ImagePublicationError("probe evidence order differs")
        model_callable_tools = value["modelCallableTools"]
        if model_callable_tools != list(CAPABILITY_TOOL_NAMES):
            raise ImagePublicationError("model-callable capability surface differs")
        build_size = value["buildArchiveSize"]
        if (
            not isinstance(build_size, int)
            or isinstance(build_size, bool)
            or not 1 <= build_size <= MAX_BUILD_CONTEXT_BYTES
        ):
            raise ImagePublicationError("build archive size is invalid")
        created = value["created"]
        if not isinstance(created, str) or _CREATED.fullmatch(created) is None:
            raise ImagePublicationError("SPDX creation time is not canonical UTC")
        builder_id = value["builderId"]
        if (
            not isinstance(builder_id, str)
            or not builder_id
            or len(builder_id) > 1024
        ):
            raise ImagePublicationError("builder identity is invalid")
        dependencies = _builder_dependencies(value["builderDependencies"])
        if [item.to_mapping() for item in dependencies] != value["builderDependencies"]:
            raise ImagePublicationError(
                "image publication builder dependencies are not canonical"
            )
        subject = _descriptor_from_mapping(
            value["subject"],
            label="image publication subject",
            allowed_media_types={OCI_MANIFEST_MEDIA_TYPE},
        )
        config = _descriptor_from_mapping(
            value["config"],
            label="image publication config",
            allowed_media_types={OCI_CONFIG_MEDIA_TYPE},
        )
        raw_layers = value["layers"]
        if not isinstance(raw_layers, list) or not raw_layers:
            raise ImagePublicationError("image publication layer inventory is empty")
        layers = tuple(
            _descriptor_from_mapping(
                item,
                label="image publication layer",
                allowed_media_types=OCI_LAYER_MEDIA_TYPES,
            )
            for item in raw_layers
        )
        if len({item.digest for item in layers}) != len(layers):
            raise ImagePublicationError(
                "image publication layer inventory contains duplicates"
            )
        sbom = _exact_mapping(
            value["sbom"], {"payload", "manifest"}, label="SBOM publication"
        )
        provenance = _exact_mapping(
            value["provenance"],
            {"payload", "manifest"},
            label="provenance publication",
        )
        sbom_payload = _descriptor_from_mapping(
            sbom["payload"],
            label="SBOM payload",
            allowed_media_types={SBOM_ARTIFACT_TYPE},
        )
        sbom_manifest = _descriptor_from_mapping(
            sbom["manifest"],
            label="SBOM manifest",
            allowed_media_types={OCI_MANIFEST_MEDIA_TYPE},
        )
        provenance_payload = _descriptor_from_mapping(
            provenance["payload"],
            label="provenance payload",
            allowed_media_types={PROVENANCE_ARTIFACT_TYPE},
        )
        provenance_manifest = _descriptor_from_mapping(
            provenance["manifest"],
            label="provenance manifest",
            allowed_media_types={OCI_MANIFEST_MEDIA_TYPE},
        )
        if len({sbom_manifest.digest, provenance_manifest.digest}) != 2:
            raise ImagePublicationError("referrer manifest identities collide")
        return cls(
            commit,
            tree,
            account,
            region,
            archive_sha,
            build_sha,
            build_size,
            catalog_source_sha,
            catalog_digest,
            tuple(model_callable_tools),
            created,
            builder_id,
            dependencies,
            subject,
            config,
            layers,
            sbom_payload,
            sbom_manifest,
            provenance_payload,
            provenance_manifest,
            probe_evidence,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ImagePublicationPlanV1":
        value = _strict_json(payload, label="image publication plan")
        if _canonical_json(value) != payload:
            raise ImagePublicationError("image publication plan is not canonical")
        return cls.from_mapping(value)

    @property
    def repository_name(self) -> str:
        return REPOSITORY_NAME

    @property
    def commit_tag(self) -> str:
        return f"commit-{self.source_commit}"

    @property
    def subject_manifest_digest(self) -> str:
        return self.subject.digest

    @property
    def sbom_payload_digest(self) -> str:
        return self.sbom_payload.digest

    @property
    def sbom_manifest_digest(self) -> str:
        return self.sbom_manifest.digest

    @property
    def provenance_payload_digest(self) -> str:
        return self.provenance_payload.digest

    @property
    def provenance_manifest_digest(self) -> str:
        return self.provenance_manifest.digest

    @property
    def publication_plan_sha256(self) -> str:
        return _sha256(self.to_bytes())

    @property
    def capability_catalog_sha256(self) -> str:
        """Compatibility spelling for the compiled, commit-bound digest."""

        return self.capability_catalog_digest

    @property
    def probe_evidence_sha256(self) -> str:
        """Compatibility digest; both independent probes must be byte-identical."""

        if len(self.probe_evidence) != 2 or len(
            {item.sha256 for item in self.probe_evidence}
        ) != 1:
            raise ImagePublicationError("independent probe evidence differs")
        return self.probe_evidence[0].sha256

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "repositoryName": REPOSITORY_NAME,
            "commitTag": self.commit_tag,
            "platform": PLATFORM,
            "gitArchiveSha256": self.git_archive_sha256,
            "buildArchiveSha256": self.build_archive_sha256,
            "buildArchiveSize": self.build_archive_size,
            "catalogSourceSha256": self.catalog_source_sha256,
            "capabilityCatalogDigest": self.capability_catalog_digest,
            "modelCallableTools": list(self.model_callable_tools),
            "created": self.created,
            "builderId": self.builder_id,
            "builderDependencies": [
                dependency.to_mapping() for dependency in self.builder_dependencies
            ],
            "subject": self.subject.to_mapping(),
            "config": self.config.to_mapping(),
            "layers": [descriptor.to_mapping() for descriptor in self.layers],
            "sbom": {
                "payload": self.sbom_payload.to_mapping(),
                "manifest": self.sbom_manifest.to_mapping(),
            },
            "provenance": {
                "payload": self.provenance_payload.to_mapping(),
                "manifest": self.provenance_manifest.to_mapping(),
            },
            "probeEvidence": [item.to_mapping() for item in self.probe_evidence],
        }

    def to_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ImagePublicationEffectV1:
    publication_plan_sha256: str
    effect_id: str
    effect_kind: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    digest: str
    media_type: str
    size: int
    tag: str | None
    subject_digest: str | None
    artifact_type: str | None
    payload: bytes

    SCHEMA = "personal-operator.image-publication-effect.v1"

    @property
    def provider_subject(self) -> str:
        base = (
            f"ecr:{self.account}:{self.region}:repository:{REPOSITORY_NAME}:"
        )
        suffix = {
            "ECR_BLOB_PUT": f"blob:{self.digest}",
            "ECR_SUBJECT_MANIFEST_PUT": (
                f"subject-manifest:{self.digest}:tag:{self.tag}"
            ),
            "ECR_SBOM_REFERRER_PUT": (
                f"sbom-referrer-manifest:{self.digest}:"
                f"subject:{self.subject_digest}"
            ),
            "ECR_PROVENANCE_REFERRER_PUT": (
                f"provenance-referrer-manifest:{self.digest}:"
                f"subject:{self.subject_digest}"
            ),
        }.get(self.effect_kind)
        if suffix is None:
            raise ImagePublicationError("image publication effect kind is invalid")
        return base + suffix

    def _expected_effect_id(self) -> str:
        prefix = {
            "ECR_BLOB_PUT": "ecr-blob-",
            "ECR_SUBJECT_MANIFEST_PUT": "ecr-subject-",
            "ECR_SBOM_REFERRER_PUT": "ecr-sbom-",
            "ECR_PROVENANCE_REFERRER_PUT": "ecr-provenance-",
        }.get(self.effect_kind)
        if prefix is None:
            raise ImagePublicationError("image publication effect kind is invalid")
        return prefix + self.digest.removeprefix("sha256:")

    def validate(self) -> None:
        try:
            _identity(
                source_commit=self.source_commit,
                source_tree=self.source_tree,
                account=self.account,
                region=self.region,
            )
        except ImagePublicationError as error:
            raise ArtifactSubstitutionError(
                "image publication effect release identity differs"
            ) from error
        if (
            not isinstance(self.publication_plan_sha256, str)
            or _SHA_64.fullmatch(self.publication_plan_sha256) is None
            or not isinstance(self.effect_kind, str)
            or not isinstance(self.effect_id, str)
            or not isinstance(self.digest, str)
            or _DIGEST.fullmatch(self.digest) is None
            or self.effect_id != self._expected_effect_id()
            or not isinstance(self.media_type, str)
            or not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or not isinstance(self.tag, (str, type(None)))
            or not isinstance(self.subject_digest, (str, type(None)))
            or not isinstance(self.artifact_type, (str, type(None)))
            or not isinstance(self.payload, bytes)
            or not 1 <= len(self.payload) <= MAX_BLOB_BYTES
            or self.size != len(self.payload)
            or _digest(self.payload) != self.digest
        ):
            raise ArtifactSubstitutionError(
                "image publication effect identity differs"
            )
        allowed_blob_media = (
            {OCI_CONFIG_MEDIA_TYPE, OCI_EMPTY_CONFIG_MEDIA_TYPE}
            | set(OCI_LAYER_MEDIA_TYPES)
            | {SBOM_ARTIFACT_TYPE, PROVENANCE_ARTIFACT_TYPE}
        )
        if self.effect_kind == "ECR_BLOB_PUT":
            valid = (
                self.media_type in allowed_blob_media
                and self.tag is None
                and self.subject_digest is None
                and self.artifact_type is None
            )
        elif self.effect_kind == "ECR_SUBJECT_MANIFEST_PUT":
            valid = (
                self.media_type == OCI_MANIFEST_MEDIA_TYPE
                and self.tag == f"commit-{self.source_commit}"
                and self.subject_digest is None
                and self.artifact_type is None
            )
        else:
            expected_artifact_type = {
                "ECR_SBOM_REFERRER_PUT": SBOM_ARTIFACT_TYPE,
                "ECR_PROVENANCE_REFERRER_PUT": PROVENANCE_ARTIFACT_TYPE,
            }.get(self.effect_kind)
            valid = (
                expected_artifact_type is not None
                and self.media_type == OCI_MANIFEST_MEDIA_TYPE
                and self.tag is None
                and isinstance(self.subject_digest, str)
                and _DIGEST.fullmatch(self.subject_digest) is not None
                and self.artifact_type == expected_artifact_type
            )
        if not valid:
            raise ArtifactSubstitutionError(
                "image publication effect target differs"
            )
        if self.effect_kind != "ECR_BLOB_PUT":
            try:
                manifest = _strict_json(
                    self.payload, label="image publication effect manifest"
                )
            except ImagePublicationError as error:
                raise ArtifactSubstitutionError(
                    "image publication effect manifest is invalid"
                ) from error
            if (
                _canonical_json(manifest) != self.payload
                or manifest.get("schemaVersion") != 2
                or manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
            ):
                raise ArtifactSubstitutionError(
                    "image publication effect manifest differs"
                )
            if self.effect_kind != "ECR_SUBJECT_MANIFEST_PUT":
                subject = manifest.get("subject")
                if (
                    not isinstance(subject, Mapping)
                    or subject.get("digest") != self.subject_digest
                    or manifest.get("artifactType") != self.artifact_type
                ):
                    raise ArtifactSubstitutionError(
                        "image publication referrer manifest subject differs"
                    )

    def header_mapping(self) -> dict[str, object]:
        self.validate()
        return {
            "schema": self.SCHEMA,
            "publicationPlanSha256": self.publication_plan_sha256,
            "effectId": self.effect_id,
            "effectKind": self.effect_kind,
            "providerSubject": self.provider_subject,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "repositoryName": REPOSITORY_NAME,
            "target": {
                "digest": self.digest,
                "mediaType": self.media_type,
                "size": self.size,
                "tag": self.tag,
                "subjectDigest": self.subject_digest,
                "artifactType": self.artifact_type,
            },
            "payloadSha256": _sha256(self.payload),
            "payloadSize": len(self.payload),
        }

    def to_private_bytes(self) -> bytes:
        """Return the one-effect raw artifact used by an IMAGE_PUBLISH step."""

        header = _canonical_json(self.header_mapping())
        if len(header) > 64 * 1024:
            raise ImagePublicationError("image effect header is unbounded")
        prefix = IMAGE_EFFECT_MAGIC + struct.pack(">I", len(header)) + header
        total_size = len(prefix) + len(self.payload)
        if total_size > MAX_PRIVATE_EFFECT_BYTES:
            raise ImagePublicationError("image effect artifact is unbounded")
        return prefix + self.payload

    def write_private_file(self, path: str | Path) -> dict[str, object]:
        artifact = self.to_private_bytes()
        destination = Path(path)
        try:
            with destination.open("xb") as target:
                target.write(artifact)
                target.flush()
                os.fsync(target.fileno())
        except OSError as error:
            raise ImagePublicationError("image effect artifact write failed") from error
        return {
            "schema": "personal-operator.image-effect-private-file.v1",
            "path": str(destination),
            "sha256": _sha256(artifact),
            "size": len(artifact),
            "publicationPlanSha256": self.publication_plan_sha256,
            "effectId": self.effect_id,
            "effectKind": self.effect_kind,
            "providerSubject": self.provider_subject,
            "expectedContent": self.digest,
            "payloadSha256": _sha256(self.payload),
            "payloadSize": len(self.payload),
        }

    @classmethod
    def from_private_bytes(
        cls,
        artifact: bytes,
        *,
        expected_private_file_sha256: str,
        expected_effect_id: str,
        expected_publication_plan_sha256: str,
    ) -> "ImagePublicationEffectV1":
        if (
            not isinstance(expected_private_file_sha256, str)
            or _SHA_64.fullmatch(expected_private_file_sha256) is None
            or not isinstance(expected_effect_id, str)
            or not expected_effect_id
            or not isinstance(expected_publication_plan_sha256, str)
            or _SHA_64.fullmatch(expected_publication_plan_sha256) is None
        ):
            raise ArtifactSubstitutionError(
                "image effect expected identity is invalid"
            )
        if (
            not isinstance(artifact, bytes)
            or not 1 <= len(artifact) <= MAX_PRIVATE_EFFECT_BYTES
            or _sha256(artifact) != expected_private_file_sha256
        ):
            raise ArtifactSubstitutionError(
                "image effect artifact bytes differ"
            )
        try:
            source = io.BytesIO(artifact)
            magic = source.read(len(IMAGE_EFFECT_MAGIC))
            if magic != IMAGE_EFFECT_MAGIC:
                raise ArtifactSubstitutionError(
                    "image effect artifact magic differs"
                )
            encoded_length = source.read(4)
            if len(encoded_length) != 4:
                raise ArtifactSubstitutionError(
                    "image effect artifact header is truncated"
                )
            header_size = struct.unpack(">I", encoded_length)[0]
            if not 1 <= header_size <= 64 * 1024:
                raise ArtifactSubstitutionError(
                    "image effect artifact header is unbounded"
                )
            header_payload = source.read(header_size)
            if len(header_payload) != header_size:
                raise ArtifactSubstitutionError(
                    "image effect artifact header is truncated"
                )
            try:
                header = _strict_json(
                    header_payload, label="image effect artifact header"
                )
            except ImagePublicationError as error:
                raise ArtifactSubstitutionError(
                    "image effect artifact header is invalid"
                ) from error
            if _canonical_json(header) != header_payload:
                raise ArtifactSubstitutionError(
                    "image effect artifact header is not canonical"
                )
            try:
                header = _exact_mapping(
                    header,
                    {
                        "schema",
                        "publicationPlanSha256",
                        "effectId",
                        "effectKind",
                        "providerSubject",
                        "sourceCommit",
                        "sourceTree",
                        "account",
                        "region",
                        "repositoryName",
                        "target",
                        "payloadSha256",
                        "payloadSize",
                    },
                    label="image effect artifact header",
                )
                target = _exact_mapping(
                    header["target"],
                    {
                        "digest",
                        "mediaType",
                        "size",
                        "tag",
                        "subjectDigest",
                        "artifactType",
                    },
                    label="image effect target",
                )
            except ImagePublicationError as error:
                raise ArtifactSubstitutionError(
                    "image effect artifact header fields differ"
                ) from error
            payload_size = header["payloadSize"]
            if (
                header["schema"] != cls.SCHEMA
                or header["publicationPlanSha256"]
                != expected_publication_plan_sha256
                or header["effectId"] != expected_effect_id
                or header["repositoryName"] != REPOSITORY_NAME
                or not isinstance(payload_size, int)
                or isinstance(payload_size, bool)
                or not 1 <= payload_size <= MAX_BLOB_BYTES
                or target["size"] != payload_size
            ):
                raise ArtifactSubstitutionError(
                    "image effect artifact header identity differs"
                )
            payload = source.read(payload_size)
            if len(payload) != payload_size or source.read(1):
                raise ArtifactSubstitutionError(
                    "image effect artifact payload framing differs"
                )
            if (
                len(artifact)
                != len(IMAGE_EFFECT_MAGIC) + 4 + header_size + payload_size
                or header["payloadSha256"] != _sha256(payload)
            ):
                raise ArtifactSubstitutionError(
                    "image effect artifact bytes differ"
                )
        except (OverflowError, ValueError) as error:
            raise ArtifactSubstitutionError(
                "image effect artifact framing differs"
            ) from error
        effect = cls(
            header["publicationPlanSha256"],
            header["effectId"],
            header["effectKind"],
            header["sourceCommit"],
            header["sourceTree"],
            header["account"],
            header["region"],
            target["digest"],
            target["mediaType"],
            target["size"],
            target["tag"],
            target["subjectDigest"],
            target["artifactType"],
            payload,
        )
        effect.validate()
        if header["providerSubject"] != effect.provider_subject:
            raise ArtifactSubstitutionError(
                "image effect provider subject differs"
            )
        return effect

    @classmethod
    def from_private_file(
        cls,
        path: str | Path,
        *,
        expected_private_file_sha256: str,
        expected_effect_id: str,
        expected_publication_plan_sha256: str,
    ) -> "ImagePublicationEffectV1":
        source_path = Path(path)
        if source_path.is_symlink() or not source_path.is_file():
            raise ArtifactSubstitutionError(
                "image effect artifact is not a regular file"
            )
        try:
            file_size = source_path.stat().st_size
            if not 1 <= file_size <= MAX_PRIVATE_EFFECT_BYTES:
                raise ArtifactSubstitutionError(
                    "image effect artifact size is invalid"
                )
            artifact = source_path.read_bytes()
        except OSError as error:
            raise ArtifactSubstitutionError(
                "image effect artifact read failed"
            ) from error
        return cls.from_private_bytes(
            artifact,
            expected_private_file_sha256=expected_private_file_sha256,
            expected_effect_id=expected_effect_id,
            expected_publication_plan_sha256=(
                expected_publication_plan_sha256
            ),
        )


@dataclass(frozen=True, slots=True)
class ImagePublicationBundle:
    plan: ImagePublicationPlanV1
    manifests: Mapping[str, bytes]
    blobs: Mapping[str, bytes]
    probe_evidence: Mapping[str, bytes]

    @property
    def plan_sha256(self) -> str:
        return self.plan.publication_plan_sha256

    def blob(self, digest: str) -> bytes:
        try:
            return self.blobs[digest]
        except KeyError as error:
            raise ArtifactSubstitutionError("plan-bound blob is absent") from error

    def replace(self, **changes: Any) -> "ImagePublicationBundle":
        return replace(self, **changes)

    def publication_effects(
        self,
        *,
        expected_plan_sha256: str,
    ) -> tuple[ImagePublicationEffectV1, ...]:
        """Return the deterministic one-provider-effect publication sequence."""

        self.validate(expected_plan_sha256=expected_plan_sha256)
        descriptors: dict[str, OciDescriptor] = {}

        def register(descriptor: OciDescriptor) -> None:
            previous = descriptors.get(descriptor.digest)
            if previous is not None and previous != descriptor:
                raise ArtifactSubstitutionError(
                    "publication blob descriptor identity collides"
                )
            descriptors[descriptor.digest] = descriptor

        for descriptor in (
            self.plan.config,
            *self.plan.layers,
            self.plan.sbom_payload,
            self.plan.provenance_payload,
        ):
            register(descriptor)
        for manifest_digest in (
            self.plan.sbom_manifest.digest,
            self.plan.provenance_manifest.digest,
        ):
            manifest = _strict_json(
                self.manifests[manifest_digest],
                label="publication effect referrer manifest",
            )
            register(
                _descriptor_from_mapping(
                    manifest["config"],
                    label="publication effect empty config",
                    allowed_media_types={OCI_EMPTY_CONFIG_MEDIA_TYPE},
                )
            )

        plan = self.plan

        def effect(
            effect_kind: str,
            descriptor: OciDescriptor,
            payload: bytes,
            *,
            tag: str | None = None,
            subject_digest: str | None = None,
            artifact_type: str | None = None,
        ) -> ImagePublicationEffectV1:
            prefix = {
                "ECR_BLOB_PUT": "ecr-blob-",
                "ECR_SUBJECT_MANIFEST_PUT": "ecr-subject-",
                "ECR_SBOM_REFERRER_PUT": "ecr-sbom-",
                "ECR_PROVENANCE_REFERRER_PUT": "ecr-provenance-",
            }[effect_kind]
            result = ImagePublicationEffectV1(
                plan.publication_plan_sha256,
                prefix + descriptor.digest.removeprefix("sha256:"),
                effect_kind,
                plan.source_commit,
                plan.source_tree,
                plan.account,
                plan.region,
                descriptor.digest,
                descriptor.media_type,
                descriptor.size,
                tag,
                subject_digest,
                artifact_type,
                payload,
            )
            result.validate()
            return result

        effects = [
            effect(
                "ECR_BLOB_PUT",
                descriptors[digest],
                self.blobs[digest],
            )
            for digest in sorted(descriptors)
        ]
        effects.extend(
            (
                effect(
                    "ECR_SUBJECT_MANIFEST_PUT",
                    plan.subject,
                    self.manifests[plan.subject.digest],
                    tag=plan.commit_tag,
                ),
                effect(
                    "ECR_SBOM_REFERRER_PUT",
                    plan.sbom_manifest,
                    self.manifests[plan.sbom_manifest.digest],
                    subject_digest=plan.subject.digest,
                    artifact_type=SBOM_ARTIFACT_TYPE,
                ),
                effect(
                    "ECR_PROVENANCE_REFERRER_PUT",
                    plan.provenance_manifest,
                    self.manifests[plan.provenance_manifest.digest],
                    subject_digest=plan.subject.digest,
                    artifact_type=PROVENANCE_ARTIFACT_TYPE,
                ),
            )
        )
        if len({item.effect_id for item in effects}) != len(effects):
            raise ArtifactSubstitutionError(
                "publication effect inventory contains duplicate identities"
            )
        return tuple(effects)

    def validate(self, *, expected_plan_sha256: str) -> None:
        if (
            not isinstance(expected_plan_sha256, str)
            or _SHA_64.fullmatch(expected_plan_sha256) is None
            or expected_plan_sha256 != self.plan.publication_plan_sha256
        ):
            raise ArtifactSubstitutionError("publication plan digest differs")

        expected_manifests = {
            self.plan.subject.digest: self.plan.subject,
            self.plan.sbom_manifest.digest: self.plan.sbom_manifest,
            self.plan.provenance_manifest.digest: self.plan.provenance_manifest,
        }
        if set(self.manifests) != set(expected_manifests):
            raise ArtifactSubstitutionError("publication manifest set differs")
        for digest, descriptor in expected_manifests.items():
            payload = self.manifests.get(digest)
            if (
                not isinstance(payload, bytes)
                or len(payload) != descriptor.size
                or _digest(payload) != digest
            ):
                raise ArtifactSubstitutionError("publication manifest bytes differ")

        subject_manifest = _strict_json(
            self.manifests[self.plan.subject.digest], label="subject manifest"
        )
        if (
            set(subject_manifest)
            != {"schemaVersion", "mediaType", "config", "layers"}
            or subject_manifest.get("schemaVersion") != 2
            or subject_manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
            or subject_manifest.get("config") != self.plan.config.to_mapping()
            or subject_manifest.get("layers")
            != [descriptor.to_mapping() for descriptor in self.plan.layers]
        ):
            raise ArtifactSubstitutionError(
                "subject manifest differs from the publication plan closure"
            )

        expected_blobs = {
            descriptor.digest: descriptor
            for descriptor in (
                self.plan.config,
                *self.plan.layers,
                self.plan.sbom_payload,
                self.plan.provenance_payload,
            )
        }
        for manifest_digest in (
            self.plan.sbom_manifest.digest,
            self.plan.provenance_manifest.digest,
        ):
            manifest = _strict_json(
                self.manifests[manifest_digest], label="referrer manifest"
            )
            config = _descriptor_from_mapping(
                manifest.get("config"),
                label="referrer config",
                allowed_media_types={OCI_EMPTY_CONFIG_MEDIA_TYPE},
            )
            expected_blobs[config.digest] = config
        if set(self.blobs) != set(expected_blobs):
            raise ArtifactSubstitutionError("publication blob set differs")
        for digest, descriptor in expected_blobs.items():
            payload = self.blobs.get(digest)
            if (
                not isinstance(payload, bytes)
                or len(payload) != descriptor.size
                or _digest(payload) != digest
            ):
                raise ArtifactSubstitutionError("publication blob bytes differ")

        expected_probe = {item.build_id: item for item in self.plan.probe_evidence}
        self.plan.probe_evidence_sha256
        if set(self.probe_evidence) != set(expected_probe):
            raise ArtifactSubstitutionError("probe evidence set differs")
        for build_id, descriptor in expected_probe.items():
            payload = self.probe_evidence.get(build_id)
            if (
                not isinstance(payload, bytes)
                or len(payload) != descriptor.size
                or _sha256(payload) != descriptor.sha256
            ):
                raise ArtifactSubstitutionError("probe evidence bytes differ")
            parsed = _strict_json(payload, label="probe evidence")
            if _canonical_json(parsed) != payload:
                raise ArtifactSubstitutionError("probe evidence is not canonical")
        if self.probe_evidence["fresh-1"] != self.probe_evidence["fresh-2"]:
            raise ArtifactSubstitutionError("independent probe evidence differs")

        for manifest_digest, artifact_type, payload in (
            (
                self.plan.sbom_manifest.digest,
                SBOM_ARTIFACT_TYPE,
                self.plan.sbom_payload,
            ),
            (
                self.plan.provenance_manifest.digest,
                PROVENANCE_ARTIFACT_TYPE,
                self.plan.provenance_payload,
            ),
        ):
            manifest = _strict_json(
                self.manifests[manifest_digest], label="referrer manifest"
            )
            if manifest.get("artifactType") != artifact_type:
                raise ArtifactSubstitutionError("referrer manifest type differs")
            if manifest.get("subject") != self.plan.subject.to_mapping():
                raise ArtifactSubstitutionError("referrer manifest subject differs")
            if manifest.get("layers") != [payload.to_mapping()]:
                raise ArtifactSubstitutionError("referrer manifest payload differs")

def validate_image_publication_effect_for_release_step(
    effect: ImagePublicationEffectV1,
    release_step: Any,
    *,
    expected_publication_plan_sha256: str,
) -> ImagePublicationEffectV1:
    """Bind one verified provider effect to the selected release-plan step."""

    if not isinstance(effect, ImagePublicationEffectV1):
        raise ArtifactSubstitutionError(
            "image publication release step effect type differs"
        )
    effect.validate()
    resolved_request = getattr(release_step, "mutation_request", None)
    if resolved_request is None:
        kind = getattr(release_step, "kind", None)
        subject = getattr(release_step, "subject", None)
        resolved_identity_matches = True
    else:
        kind = getattr(resolved_request, "kind", None)
        subject = getattr(resolved_request, "subject", None)
        resolved_identity_matches = (
            getattr(release_step, "source_commit", None)
            == effect.source_commit
            and getattr(release_step, "source_tree", None)
            == effect.source_tree
            and getattr(release_step, "account", None) == effect.account
            and getattr(release_step, "region", None) == effect.region
        )
    expected_content = getattr(
        release_step, "expected_content_sha256", None
    )
    if (
        not isinstance(expected_publication_plan_sha256, str)
        or _SHA_64.fullmatch(expected_publication_plan_sha256) is None
        or effect.publication_plan_sha256
        != expected_publication_plan_sha256
        or not resolved_identity_matches
        or kind != "IMAGE_PUBLISH"
        or subject != effect.provider_subject
        or not isinstance(expected_content, str)
        or _SHA_64.fullmatch(expected_content) is None
        or expected_content != effect.digest.removeprefix("sha256:")
    ):
        raise ArtifactSubstitutionError(
            "image publication release step binding differs"
        )
    return effect


_IMAGE_PREFLIGHT_TOKEN = object()
_IMAGE_OBSERVE_TOKEN = object()


class VerifiedImagePublicationObserveV1:
    """Private capability for the exact current aggregate IMAGE_OBSERVE."""

    __slots__ = (
        "_publication_plan",
        "_ordered_effects",
        "_release_plan_sha256",
        "_completed_prefix_sha256",
        "_operation_sha256",
        "_step_id",
        "_subject",
    )

    def __init__(
        self,
        *,
        publication_plan: ImagePublicationPlanV1,
        ordered_effects: tuple[ImagePublicationEffectV1, ...],
        release_plan_sha256: str,
        completed_prefix_sha256: str,
        operation_sha256: str,
        step_id: str,
        subject: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _IMAGE_OBSERVE_TOKEN:
            raise ArtifactSubstitutionError(
                "verified aggregate image observation is not constructible"
            )
        self._publication_plan = publication_plan
        self._ordered_effects = ordered_effects
        self._release_plan_sha256 = release_plan_sha256
        self._completed_prefix_sha256 = completed_prefix_sha256
        self._operation_sha256 = operation_sha256
        self._step_id = step_id
        self._subject = subject

    @property
    def publication_plan(self) -> ImagePublicationPlanV1:
        return self._publication_plan

    @property
    def ordered_effects(self) -> tuple[ImagePublicationEffectV1, ...]:
        return self._ordered_effects

    @property
    def release_plan_sha256(self) -> str:
        return self._release_plan_sha256

    @property
    def completed_prefix_sha256(self) -> str:
        return self._completed_prefix_sha256

    @property
    def operation_sha256(self) -> str:
        return self._operation_sha256

    @property
    def step_id(self) -> str:
        return self._step_id

    @property
    def subject(self) -> str:
        return self._subject


class VerifiedImagePublicationPreflightV1:
    """Unforgeable-in-normal-use authority over one closed image effect set."""

    __slots__ = (
        "_release_plan",
        "_publication_plan",
        "_ordered_effects",
        "_observe_step",
        "_release_plan_sha256",
        "_publication_plan_sha256",
        "_effects_by_request_sha256",
    )

    def __init__(
        self,
        *,
        release_plan: ReleasePlanV2 | None = None,
        publication_plan: ImagePublicationPlanV1 | None = None,
        ordered_effects: tuple[ImagePublicationEffectV1, ...] = (),
        observe_step: Any = None,
        release_plan_sha256: str,
        publication_plan_sha256: str,
        effects_by_request_sha256: Mapping[str, ImagePublicationEffectV1],
        _token: object | None = None,
    ) -> None:
        if _token is not _IMAGE_PREFLIGHT_TOKEN:
            raise ArtifactSubstitutionError(
                "verified image publication preflight is not constructible"
            )
        if (
            not isinstance(release_plan, ReleasePlanV2)
            or not isinstance(publication_plan, ImagePublicationPlanV1)
            or not ordered_effects
            or observe_step is None
        ):
            raise ArtifactSubstitutionError(
                "verified image publication preflight inputs are incomplete"
            )
        self._release_plan = release_plan
        self._publication_plan = publication_plan
        self._ordered_effects = ordered_effects
        self._observe_step = observe_step
        self._release_plan_sha256 = release_plan_sha256
        self._publication_plan_sha256 = publication_plan_sha256
        self._effects_by_request_sha256 = dict(effects_by_request_sha256)

    @property
    def publication_plan_sha256(self) -> str:
        return self._publication_plan_sha256

    @property
    def effect_count(self) -> int:
        return len(self._effects_by_request_sha256)

    def bind_current_observe(
        self,
        *,
        release_plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
    ) -> VerifiedImagePublicationObserveV1:
        """Bind this closed aggregate to the exact stable read-only cursor."""

        try:
            canonical_plan = ReleasePlanV2.from_bytes(release_plan.to_bytes())
            if canonical_plan != self._release_plan:
                raise ArtifactSubstitutionError(
                    "aggregate image observation release plan differs"
                )
            canonical_transaction = StagingTransactionV2.from_bytes(
                transaction.to_bytes(), plan=canonical_plan
            )
        except ArtifactSubstitutionError:
            raise
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise ArtifactSubstitutionError(
                "aggregate image observation release plan or transaction is invalid"
            ) from error
        count = canonical_transaction.completed_step_count
        if count >= len(canonical_plan.steps):
            raise ArtifactSubstitutionError(
                "aggregate image observation has no current observe step"
            )
        step = canonical_plan.steps[count]
        if (
            canonical_transaction.state
            in {
                "NEW",
                "UNCERTAIN",
                "VERIFIED",
                "ABORTED_RETAINED",
                "ROLLED_BACK",
            }
            or step != self._observe_step
            or step.phase != "image"
            or step.kind != "IMAGE_OBSERVE"
            or step.mutation
            or step.request_sha256 != self._publication_plan_sha256
        ):
            raise ArtifactSubstitutionError(
                "aggregate image observation is not the exact current observe step"
            )
        prefix = _completed_prefix_sha256(
            [item.to_mapping() for item in canonical_transaction.completed_steps]
        )
        operation = _release_operation_sha256(
            canonical_plan.digest(), step, prefix
        )
        return VerifiedImagePublicationObserveV1(
            publication_plan=self._publication_plan,
            ordered_effects=self._ordered_effects,
            release_plan_sha256=self._release_plan_sha256,
            completed_prefix_sha256=prefix,
            operation_sha256=operation,
            step_id=step.step_id,
            subject=step.subject,
            _token=_IMAGE_OBSERVE_TOKEN,
        )

    def _bind_verified_mutation(
        self,
        verified: VerifiedPrivateMutationV2,
    ) -> ImagePublicationEffectV1:
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise ArtifactSubstitutionError(
                "image publication requires a verified private mutation"
            )
        try:
            resolved = verified.resolved_request
            metadata = verified.metadata
            artifact = verified.read_artifact_bytes(
                limit=MAX_PRIVATE_EFFECT_BYTES
            )
        except ContractError as error:
            raise ArtifactSubstitutionError(
                "image publication verified private mutation is invalid"
            ) from error
        request = resolved.mutation_request
        expected = self._effects_by_request_sha256.get(
            metadata.request_artifact_sha256
        )
        if (
            resolved.step_phase != "image"
            or request.kind != "IMAGE_PUBLISH"
            or request.plan_sha256 != self._release_plan_sha256
            or request.request_sha256 != metadata.request_artifact_sha256
            or expected is None
        ):
            raise ArtifactSubstitutionError(
                "image publication verified current step differs"
            )
        parsed = ImagePublicationEffectV1.from_private_bytes(
            artifact,
            expected_private_file_sha256=metadata.request_artifact_sha256,
            expected_effect_id=expected.effect_id,
            expected_publication_plan_sha256=(
                self._publication_plan_sha256
            ),
        )
        if parsed != expected:
            raise ArtifactSubstitutionError(
                "image publication verified effect differs from preflight"
            )
        return validate_image_publication_effect_for_release_step(
            parsed,
            resolved,
            expected_publication_plan_sha256=(
                self._publication_plan_sha256
            ),
        )


def validate_image_publication_preflight(
    publication_plan_payload: bytes,
    effects: Sequence[ImagePublicationEffectV1],
    *,
    release_plan: ReleasePlanV2,
) -> tuple[ImagePublicationPlanV1, VerifiedImagePublicationPreflightV1]:
    """Close IMAGE_OBSERVE plan bytes over every exact IMAGE_PUBLISH effect.

    ``publication_plan_payload`` is the unique
    ``build/image-publication-plan.json`` IMAGE_OBSERVE request artifact. Each
    canonical effect artifact must already be in the exact ReleasePlanV2.
    Dispatch re-parses its retained VerifiedPrivateMutationV2 bytes and also
    requires the capability returned by this complete preflight.
    """

    if not isinstance(release_plan, ReleasePlanV2):
        raise ArtifactSubstitutionError(
            "image publication preflight release plan is invalid"
        )
    try:
        canonical_release_plan = ReleasePlanV2.from_bytes(
            release_plan.to_bytes()
        )
    except ContractError as error:
        raise ArtifactSubstitutionError(
            "image publication preflight release plan is invalid"
        ) from error
    image_steps = tuple(
        step
        for step in canonical_release_plan.steps
        if step.phase == "image"
    )
    if (
        len(image_steps) < 2
        or image_steps[-1].kind != "IMAGE_OBSERVE"
        or image_steps[-1].request_artifact
        != "build/image-publication-plan.json"
        or any(step.kind != "IMAGE_PUBLISH" for step in image_steps[:-1])
    ):
        raise ArtifactSubstitutionError(
            "image publication preflight release recipe differs"
        )
    verified_steps = image_steps[:-1]
    observe_step = image_steps[-1]
    expected_publication_plan_sha256 = observe_step.request_sha256
    artifacts_by_path = {
        artifact.path: artifact for artifact in canonical_release_plan.artifacts
    }
    observe_artifact = artifacts_by_path.get(observe_step.request_artifact)
    if (
        not isinstance(publication_plan_payload, bytes)
        or not publication_plan_payload
        or observe_artifact is None
        or observe_artifact.size != len(publication_plan_payload)
        or _sha256(publication_plan_payload)
        != expected_publication_plan_sha256
    ):
        raise ArtifactSubstitutionError(
            "image publication observe plan digest differs"
        )
    try:
        plan = ImagePublicationPlanV1.from_bytes(publication_plan_payload)
    except ImagePublicationError as error:
        raise ArtifactSubstitutionError(
            "image publication observe plan bytes differ"
        ) from error
    if plan.publication_plan_sha256 != expected_publication_plan_sha256:
        raise ArtifactSubstitutionError(
            "image publication observe plan digest differs"
        )
    try:
        plan.probe_evidence_sha256
    except ImagePublicationError as error:
        raise ArtifactSubstitutionError(
            "independent probe evidence differs"
        ) from error

    if (
        plan.source_commit,
        plan.source_tree,
        plan.account,
        plan.region,
        plan.subject.digest,
    ) != (
        canonical_release_plan.source_commit,
        canonical_release_plan.source_tree,
        canonical_release_plan.account,
        canonical_release_plan.region,
        canonical_release_plan.runtime_image_digest,
    ):
        raise ArtifactSubstitutionError(
            "image publication plan crosses the release identity"
        )
    try:
        verified_effects = tuple(effects)
    except TypeError as error:
        raise ArtifactSubstitutionError(
            "image publication preflight inventory is invalid"
        ) from error

    empty_config_payload = b"{}"
    empty_config = OciDescriptor(
        OCI_EMPTY_CONFIG_MEDIA_TYPE,
        _digest(empty_config_payload),
        len(empty_config_payload),
    )
    blob_descriptors: dict[str, OciDescriptor] = {}

    def register_blob(descriptor: OciDescriptor) -> None:
        previous = blob_descriptors.get(descriptor.digest)
        if previous is not None and previous != descriptor:
            raise ArtifactSubstitutionError(
                "image publication effect closure descriptor collides"
            )
        blob_descriptors[descriptor.digest] = descriptor

    for descriptor in (
        plan.config,
        *plan.layers,
        empty_config,
        plan.sbom_payload,
        plan.provenance_payload,
    ):
        register_blob(descriptor)

    expected_targets: list[
        tuple[
            str,
            OciDescriptor,
            str | None,
            str | None,
            str | None,
        ]
    ] = [
        ("ECR_BLOB_PUT", blob_descriptors[digest], None, None, None)
        for digest in sorted(blob_descriptors)
    ]
    expected_targets.extend(
        (
            (
                "ECR_SUBJECT_MANIFEST_PUT",
                plan.subject,
                plan.commit_tag,
                None,
                None,
            ),
            (
                "ECR_SBOM_REFERRER_PUT",
                plan.sbom_manifest,
                None,
                plan.subject.digest,
                SBOM_ARTIFACT_TYPE,
            ),
            (
                "ECR_PROVENANCE_REFERRER_PUT",
                plan.provenance_manifest,
                None,
                plan.subject.digest,
                PROVENANCE_ARTIFACT_TYPE,
            ),
        )
    )
    expected_digests = [descriptor.digest for _, descriptor, *_ in expected_targets]
    if len(expected_digests) != len(set(expected_digests)):
        raise ArtifactSubstitutionError(
            "image publication effect closure identities collide"
        )
    if (
        len(verified_effects) != len(expected_targets)
        or len(verified_steps) != len(expected_targets)
    ):
        raise ArtifactSubstitutionError(
            "image publication effect closure inventory differs"
        )

    for effect, expected in zip(
        verified_effects, expected_targets, strict=True
    ):
        if not isinstance(effect, ImagePublicationEffectV1):
            raise ArtifactSubstitutionError(
                "image publication effect closure type differs"
            )
        effect.validate()
        kind, descriptor, tag, subject_digest, artifact_type = expected
        if (
            effect.publication_plan_sha256
            != expected_publication_plan_sha256
            or effect.source_commit != plan.source_commit
            or effect.source_tree != plan.source_tree
            or effect.account != plan.account
            or effect.region != plan.region
            or effect.effect_kind != kind
            or effect.digest != descriptor.digest
            or effect.media_type != descriptor.media_type
            or effect.size != descriptor.size
            or effect.tag != tag
            or effect.subject_digest != subject_digest
            or effect.artifact_type != artifact_type
        ):
            raise ArtifactSubstitutionError(
                "image publication effect closure target differs"
            )

    subject_effect, sbom_effect, provenance_effect = verified_effects[-3:]
    try:
        subject_manifest = _strict_json(
            subject_effect.payload,
            label="preflight subject manifest",
        )
        sbom_manifest = _strict_json(
            sbom_effect.payload,
            label="preflight SBOM referrer manifest",
        )
        provenance_manifest = _strict_json(
            provenance_effect.payload,
            label="preflight provenance referrer manifest",
        )
    except ImagePublicationError as error:
        raise ArtifactSubstitutionError(
            "image publication effect closure manifest differs"
        ) from error
    expected_subject_manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "config": plan.config.to_mapping(),
        "layers": [descriptor.to_mapping() for descriptor in plan.layers],
    }
    expected_sbom_manifest = {
        "artifactType": SBOM_ARTIFACT_TYPE,
        "config": empty_config.to_mapping(),
        "layers": [plan.sbom_payload.to_mapping()],
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "schemaVersion": 2,
        "subject": plan.subject.to_mapping(),
    }
    expected_provenance_manifest = {
        "artifactType": PROVENANCE_ARTIFACT_TYPE,
        "config": empty_config.to_mapping(),
        "layers": [plan.provenance_payload.to_mapping()],
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "schemaVersion": 2,
        "subject": plan.subject.to_mapping(),
    }
    if (
        subject_manifest != expected_subject_manifest
        or _canonical_json(subject_manifest) != subject_effect.payload
        or sbom_manifest != expected_sbom_manifest
        or _canonical_json(sbom_manifest) != sbom_effect.payload
        or provenance_manifest != expected_provenance_manifest
        or _canonical_json(provenance_manifest) != provenance_effect.payload
    ):
        raise ArtifactSubstitutionError(
            "image publication effect closure manifest differs"
        )

    effects_by_request_sha256: dict[str, ImagePublicationEffectV1] = {}
    for effect, step in zip(verified_effects, verified_steps, strict=True):
        validate_image_publication_effect_for_release_step(
            effect,
            step,
            expected_publication_plan_sha256=(
                expected_publication_plan_sha256
            ),
        )
        artifact = effect.to_private_bytes()
        request_sha256 = _sha256(artifact)
        release_artifact = artifacts_by_path.get(step.request_artifact)
        if (
            request_sha256 != step.request_sha256
            or release_artifact is None
            or release_artifact.size != len(artifact)
            or request_sha256 in effects_by_request_sha256
        ):
            raise ArtifactSubstitutionError(
                "image publication effect artifact differs from release plan"
            )
        effects_by_request_sha256[request_sha256] = effect
    return plan, VerifiedImagePublicationPreflightV1(
        release_plan=canonical_release_plan,
        publication_plan=plan,
        ordered_effects=verified_effects,
        observe_step=observe_step,
        release_plan_sha256=canonical_release_plan.digest(),
        publication_plan_sha256=expected_publication_plan_sha256,
        effects_by_request_sha256=effects_by_request_sha256,
        _token=_IMAGE_PREFLIGHT_TOKEN,
    )


def _descriptor_from_mapping(
    raw: Any,
    *,
    label: str,
    allowed_media_types: set[str] | frozenset[str],
) -> OciDescriptor:
    value = _exact_mapping(raw, {"mediaType", "digest", "size"}, label=label)
    media_type = value["mediaType"]
    digest = _require_digest(value["digest"], label=label)
    size = value["size"]
    if media_type not in allowed_media_types:
        raise ImagePublicationError(f"{label} media type is invalid")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= MAX_BLOB_BYTES
    ):
        raise ImagePublicationError(f"{label} size is invalid")
    return OciDescriptor(str(media_type), digest, size)


def _decoded_layer(descriptor: OciDescriptor, payload: bytes) -> bytes:
    if descriptor.media_type == "application/vnd.oci.image.layer.v1.tar":
        return payload
    if descriptor.media_type != "application/vnd.oci.image.layer.v1.tar+gzip":
        raise ImagePublicationError("OCI layer compression is unsupported")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(payload, MAX_BLOB_BYTES + 1)
        decoded += decoder.flush()
    except zlib.error as error:
        raise ImagePublicationError("OCI gzip layer is invalid") from error
    if (
        not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
        or len(decoded) > MAX_BLOB_BYTES
    ):
        raise ImagePublicationError("OCI gzip layer is truncated or unbounded")
    return decoded


def _canonical_layer_path(member: tarfile.TarInfo) -> str:
    raw = member.name.rstrip("/") if member.isdir() else member.name
    path = PurePosixPath(raw)
    if (
        not raw
        or "\x00" in raw
        or len(raw.encode("utf-8")) > 4096
        or path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ImagePublicationError("OCI layer path is unsafe")
    return path.as_posix()


def _safe_link_target(path: str, target: str, *, root_relative: bool) -> str:
    raw = PurePosixPath(target)
    if not target or "\x00" in target or raw.is_absolute():
        raise ImagePublicationError("OCI layer link target is unsafe")
    parts = [] if root_relative else list(PurePosixPath(path).parent.parts)
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ImagePublicationError("OCI layer link target traverses root")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise ImagePublicationError("OCI layer link target is empty")
    return PurePosixPath(*parts).as_posix()


def _remove_overlay_path(state: dict[str, tuple[str, str, int]], target: str) -> None:
    for existing in tuple(state):
        if existing == target or existing.startswith(target + "/"):
            del state[existing]


def _derive_runtime_inventory(
    layers: Sequence[OciDescriptor],
    blobs: Mapping[str, bytes],
    diff_ids: Sequence[str],
) -> tuple[RuntimeFile, ...]:
    if len(diff_ids) != len(layers):
        raise ImagePublicationError("OCI rootfs diff ID count differs from layers")
    state: dict[str, tuple[str, str, int]] = {}
    for layer_index, (descriptor, expected_diff_id) in enumerate(
        zip(layers, diff_ids, strict=True)
    ):
        decoded = _decoded_layer(descriptor, blobs[descriptor.digest])
        if _digest(decoded) != expected_diff_id:
            raise ImagePublicationError("OCI layer diff ID differs")
        try:
            with tarfile.open(fileobj=io.BytesIO(decoded), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) > 250_000:
                    raise ImagePublicationError("OCI layer member count is unbounded")
                parsed: list[tuple[tarfile.TarInfo, str]] = []
                seen: set[str] = set()
                for member in members:
                    path = _canonical_layer_path(member)
                    if path in seen:
                        raise ImagePublicationError("OCI layer path is duplicated")
                    seen.add(path)
                    if getattr(member, "sparse", None):
                        raise ImagePublicationError("OCI sparse layer member is forbidden")
                    parsed.append((member, path))

                whiteouts: list[tuple[tarfile.TarInfo, str]] = []
                ordinary: list[tuple[tarfile.TarInfo, str]] = []
                for item in parsed:
                    basename = PurePosixPath(item[1]).name
                    (whiteouts if basename.startswith(".wh.") else ordinary).append(
                        item
                    )
                if whiteouts and layer_index == 0:
                    raise ImagePublicationError("first OCI layer contains a whiteout")
                for member, path in whiteouts:
                    if not member.isreg() or member.size != 0:
                        raise ImagePublicationError("OCI whiteout is malformed")
                    parent = PurePosixPath(path).parent
                    basename = PurePosixPath(path).name
                    if basename == ".wh..wh..opq":
                        prefix = "" if parent == PurePosixPath(".") else parent.as_posix()
                        for existing in tuple(state):
                            if not prefix or existing.startswith(prefix + "/"):
                                del state[existing]
                    else:
                        target_name = basename.removeprefix(".wh.")
                        if not target_name or target_name.startswith(".wh."):
                            raise ImagePublicationError("OCI whiteout target is malformed")
                        target = (
                            target_name
                            if parent == PurePosixPath(".")
                            else (parent / target_name).as_posix()
                        )
                        _remove_overlay_path(state, target)

                for member, path in ordinary:
                    ancestors = list(PurePosixPath(path).parents)[:-1]
                    if any(
                        state.get(ancestor.as_posix(), ("", "", 0))[0]
                        in {"file", "symlink"}
                        for ancestor in ancestors
                    ):
                        raise ImagePublicationError(
                            "OCI layer member descends through a non-directory"
                        )
                    if member.isdir():
                        if state.get(path, ("directory", "", 0))[0] != "directory":
                            _remove_overlay_path(state, path)
                        state[path] = ("directory", "", 0)
                    elif member.isreg():
                        source = archive.extractfile(member)
                        payload = source.read() if source is not None else b""
                        if len(payload) != member.size:
                            raise ImagePublicationError("OCI layer file is truncated")
                        _remove_overlay_path(state, path)
                        state[path] = ("file", _sha256(payload), len(payload))
                    elif member.issym():
                        target = _safe_link_target(
                            path, member.linkname, root_relative=False
                        )
                        _remove_overlay_path(state, path)
                        state[path] = ("symlink", target, 0)
                    elif member.islnk():
                        target = _safe_link_target(
                            path, member.linkname, root_relative=True
                        )
                        target_node = state.get(target)
                        if target_node is None or target_node[0] != "file":
                            raise ImagePublicationError(
                                "OCI hardlink target is not an existing regular file"
                            )
                        _remove_overlay_path(state, path)
                        state[path] = target_node
                    else:
                        raise ImagePublicationError(
                            "OCI layer contains a special or unknown member"
                        )
        except (tarfile.TarError, OSError, UnicodeError) as error:
            raise ImagePublicationError("OCI layer tar is invalid") from error
        if len(state) > 250_000:
            raise ImagePublicationError("OCI root filesystem inventory is unbounded")

    inventory = tuple(
        RuntimeFile("/" + path, checksum, size)
        for path, (kind, checksum, size) in sorted(state.items())
        if kind == "file"
    )
    paths = {item.path for item in inventory}
    if not _REQUIRED_RUNTIME_FILES.issubset(paths):
        raise ImagePublicationError("runtime filesystem inventory is incomplete")
    if any(_forbidden_browser_path(path) for path in paths):
        raise ImagePublicationError("runtime filesystem contains a browser artifact")
    return inventory


def _validate_oci_build(raw: Any) -> _OciClosure:
    result = _exact_mapping(
        raw,
        {"schema", "platform", "manifest", "blobs"},
        label="OCI build result",
    )
    if result["schema"] != "personal-operator.oci-build-result.v2":
        raise ImagePublicationError("OCI build result schema is invalid")
    if result["platform"] != PLATFORM:
        raise ImagePublicationError("OCI build result is not exact ARM64")
    manifest_payload = result["manifest"]
    if not isinstance(manifest_payload, bytes):
        raise ImagePublicationError("OCI manifest bytes are missing")
    manifest = _strict_json(manifest_payload, label="OCI manifest")
    if set(manifest) != {"schemaVersion", "mediaType", "config", "layers"}:
        raise ImagePublicationError("OCI manifest fields are not exact")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
    ):
        raise ImagePublicationError("OCI manifest schema or media type is invalid")
    config_descriptor = _descriptor_from_mapping(
        manifest.get("config"),
        label="OCI config",
        allowed_media_types={OCI_CONFIG_MEDIA_TYPE},
    )
    raw_layers = manifest.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise ImagePublicationError("OCI layer inventory is empty")
    layers = tuple(
        _descriptor_from_mapping(
            raw_layer,
            label="OCI layer",
            allowed_media_types=OCI_LAYER_MEDIA_TYPES,
        )
        for raw_layer in raw_layers
    )
    if len({layer.digest for layer in layers}) != len(layers):
        raise ImagePublicationError("OCI layer inventory contains duplicates")

    raw_blobs = result["blobs"]
    if not isinstance(raw_blobs, Mapping):
        raise ImagePublicationError("OCI blob inventory is not an object")
    blobs = dict(raw_blobs)
    expected = {config_descriptor.digest, *(layer.digest for layer in layers)}
    if set(blobs) != expected:
        raise ImagePublicationError("OCI blob closure differs from its manifest")
    descriptors = {config_descriptor.digest: config_descriptor}
    descriptors.update({layer.digest: layer for layer in layers})
    for digest, descriptor in descriptors.items():
        payload = blobs[digest]
        if (
            not isinstance(payload, bytes)
            or len(payload) != descriptor.size
            or _digest(payload) != digest
        ):
            raise ImagePublicationError("OCI blob digest or size differs")

    config = _strict_json(blobs[config_descriptor.digest], label="OCI image config")
    if config.get("architecture") != "arm64" or config.get("os") != "linux":
        raise ImagePublicationError("OCI image config is not exact ARM64 Linux")
    runtime = _mapping(config.get("config"), label="OCI runtime config")
    user = runtime.get("User")
    if not isinstance(user, str) or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user) is None:
        raise ImagePublicationError("OCI runtime user is not explicit nonroot")
    if runtime.get("Entrypoint") != ["/app/entrypoint.sh"]:
        raise ImagePublicationError("OCI runtime entrypoint is not exact")
    environment = runtime.get("Env", [])
    if not isinstance(environment, list) or any(
        not isinstance(item, str) or "=" not in item for item in environment
    ):
        raise ImagePublicationError("OCI environment is malformed")
    names = [item.split("=", 1)[0] for item in environment]
    if len(names) != len(set(names)) or any(_CREDENTIAL_ENV.search(name) for name in names):
        raise ImagePublicationError("OCI image config embeds credential material")
    rootfs = _mapping(config.get("rootfs"), label="OCI root filesystem")
    diff_ids = rootfs.get("diff_ids")
    if rootfs.get("type") != "layers" or not isinstance(diff_ids, list):
        raise ImagePublicationError("OCI root filesystem inventory is malformed")
    if len(diff_ids) != len(layers) or any(
        not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
        for digest in diff_ids
    ):
        raise ImagePublicationError("OCI root filesystem does not bind its layers")
    inventory = _derive_runtime_inventory(layers, blobs, diff_ids)

    manifest_descriptor = OciDescriptor(
        OCI_MANIFEST_MEDIA_TYPE,
        _digest(manifest_payload),
        len(manifest_payload),
    )
    return _OciClosure(
        manifest_payload,
        manifest_descriptor,
        config_descriptor,
        layers,
        tuple(sorted(blobs.items())),
        inventory,
    )


def _archive_members(payload: bytes) -> tuple[dict[str, tuple[bytes, int]], bytes]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_GIT_ARCHIVE_BYTES:
        raise ImagePublicationError("exact Git archive size is invalid")
    files: dict[str, tuple[bytes, int]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or not path.parts
                    or path.parts[0] != "bridge"
                    or any(part in {"", ".", ".."} for part in path.parts)
                ):
                    raise ImagePublicationError(
                        "exact Git archive contains a path outside the bridge tree"
                    )
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    raise ImagePublicationError("exact Git archive contains a symlink")
                if not member.isreg():
                    raise ImagePublicationError(
                        "exact Git archive contains a special file"
                    )
                name = path.as_posix()
                if name in files:
                    raise ImagePublicationError(
                        "exact Git archive contains a duplicate path"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise ImagePublicationError("exact Git archive file is unreadable")
                data = source.read()
                if len(data) != member.size:
                    raise ImagePublicationError("exact Git archive file is truncated")
                files[name] = (data, member.mode)
    except (tarfile.TarError, OSError) as error:
        raise ImagePublicationError("exact Git archive is invalid") from error
    if not _REQUIRED_BUILD_FILES.issubset(files):
        raise ImagePublicationError("exact Git archive omits required bridge files")
    if not any(
        name.startswith("bridge/capabilities/schemas/") and name.endswith(".json")
        for name in files
    ):
        raise ImagePublicationError("exact Git archive omits capability schemas")
    return files, payload


def _compile_capability_catalog(
    files: Mapping[str, tuple[bytes, int]],
    *,
    release_commit: str,
) -> tuple[str, str, tuple[str, ...]]:
    """Compile the exact frozen source/schema bytes using the runtime algorithm."""

    if not isinstance(release_commit, str) or _SHA_40.fullmatch(release_commit) is None:
        raise ImagePublicationError("capability release commit is not canonical")
    source_path = "bridge/capabilities/catalog-v1.json"
    try:
        source_payload = files[source_path][0]
    except KeyError as error:
        raise ImagePublicationError("exact Git archive omits catalog source") from error
    source_sha256 = _sha256(source_payload)
    if source_sha256 != CAPABILITY_CATALOG_SOURCE_SHA256:
        raise ImagePublicationError("catalog source differs from frozen v1")
    source = _strict_json(source_payload, label="capability catalog source")
    if _canonical_json(source) + b"\n" != source_payload:
        raise ImagePublicationError("capability catalog source is not canonical")
    if set(source) != {"schema", "packs"} or source["schema"] != (
        "personal-operator.capability-catalog-source.v1"
    ):
        raise ImagePublicationError("capability catalog source schema differs")
    packs = source["packs"]
    if not isinstance(packs, list) or len(packs) != len(CAPABILITY_TOOL_NAMES):
        raise ImagePublicationError("capability catalog does not contain ten packs")

    schema_prefix = "bridge/capabilities/schemas/"
    observed_schema_names = {
        name.removeprefix(schema_prefix)
        for name in files
        if name.startswith(schema_prefix) and name.endswith(".json")
    }
    if observed_schema_names != set(CAPABILITY_SCHEMA_SHA256):
        raise ImagePublicationError("capability schema inventory differs")
    for name, expected_sha256 in CAPABILITY_SCHEMA_SHA256.items():
        payload = files[schema_prefix + name][0]
        if _sha256(payload) != expected_sha256:
            raise ImagePublicationError("capability schema bytes differ")
        parsed = _strict_json(payload, label=f"capability schema {name}")
        if _canonical_json(parsed) + b"\n" != payload:
            raise ImagePublicationError("capability schema is not canonical")

    compiled_packs: list[dict[str, Any]] = []
    tool_names: list[str] = []
    referenced: set[str] = set()
    for raw_pack in packs:
        pack = _mapping(raw_pack, label="capability pack")
        operations = pack.get("operations")
        if not isinstance(operations, list) or len(operations) != 1:
            raise ImagePublicationError("capability pack operation set differs")
        operation = _exact_mapping(
            operations[0],
            {"operationId", "toolName", "inputSchema", "outputSchema"},
            label="capability operation",
        )
        tool_name = operation["toolName"]
        if not isinstance(tool_name, str):
            raise ImagePublicationError("capability tool name is invalid")
        compiled_operation = {
            "operationId": operation["operationId"],
            "toolName": tool_name,
        }
        for source_field, digest_field in (
            ("inputSchema", "inputSchemaDigest"),
            ("outputSchema", "outputSchemaDigest"),
        ):
            schema_name = operation[source_field]
            if schema_name not in CAPABILITY_SCHEMA_SHA256:
                raise ImagePublicationError(
                    "capability operation references an unreviewed schema"
                )
            referenced.add(schema_name)
            compiled_operation[digest_field] = CAPABILITY_SCHEMA_SHA256[schema_name]
        compiled_packs.append({**pack, "operations": [compiled_operation]})
        tool_names.append(tool_name)
    if referenced != set(CAPABILITY_SCHEMA_SHA256) or tuple(tool_names) != (
        CAPABILITY_TOOL_NAMES
    ):
        raise ImagePublicationError("model-callable capability surface differs")
    digest_input = {
        "schema": "personal-operator.capability-catalog.v1",
        "releaseCommit": release_commit,
        "packs": compiled_packs,
    }
    return source_sha256, _sha256(_canonical_json(digest_input)), tuple(tool_names)


def _excluded_build_path(path: PurePosixPath) -> bool:
    name = path.name
    return (
        any(part in _EXCLUDED_COMPONENTS for part in path.parts)
        or name == "CLAUDE.md"
        or name == ".DS_Store"
        or name.startswith(".env")
        or name.endswith(".test.js")
        or name.endswith(".test.mjs")
        or name.endswith(".pyc")
    )


def _normalized_build_archive(
    files: Mapping[str, tuple[bytes, int]],
) -> tuple[bytes, tuple[tuple[str, bytes], ...]]:
    retained = tuple(
        (name, files[name][0])
        for name in sorted(files)
        if not _excluded_build_path(PurePosixPath(name))
    )
    if not _REQUIRED_BUILD_FILES.issubset({name for name, _ in retained}):
        raise ImagePublicationError("build archive omits required production files")
    output = io.BytesIO()
    try:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, payload in retained:
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                original_mode = files[name][1]
                member.mode = 0o755 if original_mode & 0o111 else 0o644
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = 0
                tar.addfile(member, io.BytesIO(payload))
    except (tarfile.TarError, OSError) as error:
        raise ImagePublicationError("build archive normalization failed") from error
    normalized = output.getvalue()
    if not 1 <= len(normalized) <= MAX_BUILD_CONTEXT_BYTES:
        raise ImagePublicationError("normalized build archive size is invalid")
    return normalized, retained


def _builder_dependencies(raw: Sequence[Mapping[str, Any]]) -> tuple[BuilderDependency, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or not raw:
        raise ImagePublicationError("reviewed builder dependency closure is empty")
    dependencies: list[BuilderDependency] = []
    for item in raw:
        value = _exact_mapping(item, {"uri", "digest"}, label="builder dependency")
        uri = value["uri"]
        if not isinstance(uri, str) or not uri or len(uri) > 1024:
            raise ImagePublicationError("builder dependency URI is invalid")
        digest = _require_digest(value["digest"], label="builder dependency")
        dependencies.append(BuilderDependency(uri, digest))
    if len({item.uri for item in dependencies}) != len(dependencies):
        raise ImagePublicationError(
            "reviewed builder dependency closure is not unique"
        )
    return tuple(sorted(dependencies, key=lambda item: (item.uri, item.digest)))


def _runtime_build_recipe(component: str) -> dict[str, object]:
    try:
        manager, version = RUNTIME_PACKAGE_MANAGERS[component]
    except KeyError as error:
        raise RuntimeBuildClosureError(
            "runtime build component is invalid"
        ) from error
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
        "packageManager": manager,
        "packageManagerVersion": version,
        "installMode": "offline-frozen-production",
        "buildMode": (
            "source-production" if component == "openclaw-runtime" else "none"
        ),
        "outputContract": "personal-operator.runtime-file-inventory.v1",
        "executorSha256": _sha256(RUNTIME_BUILD_EXECUTOR),
        "sourceDateEpoch": 0,
    }


def _runtime_source_files(
    archive: bytes,
    *,
    component: str,
) -> dict[str, tuple[bytes, str]]:
    if (
        not isinstance(archive, bytes)
        or not 1 <= len(archive) <= MAX_GIT_ARCHIVE_BYTES
    ):
        raise RuntimeBuildClosureError("runtime source archive size is invalid")
    prefix = "" if component == "openclaw-runtime" else "bridge/"
    files: dict[str, tuple[bytes, str]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            for member in source.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or member.issym()
                    or member.islnk()
                ):
                    raise RuntimeBuildClosureError(
                        "runtime source archive contains an unsafe member"
                    )
                if member.isdir():
                    continue
                if not member.isreg() or not 0 <= member.size <= MAX_BLOB_BYTES:
                    raise RuntimeBuildClosureError(
                        "runtime source archive contains a special member"
                    )
                if prefix and not member.name.startswith(prefix):
                    continue
                relative = member.name.removeprefix(prefix)
                relative_path = PurePosixPath(relative)
                if (
                    not relative
                    or relative_path.is_absolute()
                    or any(
                        part in {"", ".", ".."} for part in relative_path.parts
                    )
                    or relative in files
                ):
                    raise RuntimeBuildClosureError(
                        "runtime source archive inventory is invalid"
                    )
                descriptor = source.extractfile(member)
                payload = descriptor.read() if descriptor is not None else b""
                if len(payload) != member.size:
                    raise RuntimeBuildClosureError(
                        "runtime source archive member is truncated"
                    )
                mode = "0755" if member.mode & 0o111 else "0644"
                files[relative] = (payload, mode)
    except (tarfile.TarError, OSError) as error:
        raise RuntimeBuildClosureError(
            "runtime source archive is invalid"
        ) from error
    recipe = _runtime_build_recipe(component)
    required = {str(recipe["packagePath"]), str(recipe["lockPath"])}
    if not required.issubset(files):
        raise RuntimeBuildClosureError(
            "runtime source archive omits package or lock bytes"
        )
    return files


_LOCK_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]+={0,2}")


def _offline_artifact_contract(
    *,
    component: str,
    artifact: PackageManagerArtifact,
    lock_payload: bytes,
) -> str:
    """Validate the non-executable cache and exact package-manager distribution."""

    if component not in RUNTIME_PACKAGE_MANAGERS:
        raise RuntimeBuildClosureError(
            "offline package-manager artifact component differs"
        )
    lock_integrities = sorted(
        set(_LOCK_INTEGRITY.findall(lock_payload.decode("utf-8", errors="strict")))
    )
    if not lock_integrities:
        raise RuntimeBuildClosureError(
            "offline dependency cache has no lock integrity inventory"
        )
    # Stream the outer tar directly from the retained artifact source; each
    # member's sha256/size is computed in bounded chunks and the payload bytes
    # are never retained.  Only the small integrity manifest is materialized.
    inventory_by_name: dict[str, tuple[str, int]] = {}
    manifest_payload: bytes | None = None
    distribution_member: tarfile.TarInfo | None = None
    try:
        with artifact.source.open() as fileobj:
            with tarfile.open(fileobj=fileobj, mode="r:") as archive:
                for member in archive.getmembers():
                    path = PurePosixPath(member.name)
                    if (
                        path.is_absolute()
                        or any(part in {"", ".", ".."} for part in path.parts)
                        or member.issym()
                        or member.islnk()
                        or member.uid != 0
                        or member.gid != 0
                        or member.mtime != 0
                    ):
                        raise RuntimeBuildClosureError(
                            "offline package-manager artifact is unsafe"
                        )
                    if member.isdir():
                        continue
                    if not member.isreg() or member.name in inventory_by_name:
                        raise RuntimeBuildClosureError(
                            "offline package-manager artifact inventory differs"
                        )
                    reader = archive.extractfile(member)
                    if reader is None:
                        raise RuntimeBuildClosureError(
                            "offline package-manager artifact inventory differs"
                        )
                    if member.name == "integrity-manifest.json":
                        manifest_payload = reader.read(member.size + 1)
                        if len(manifest_payload) != member.size:
                            raise RuntimeBuildClosureError(
                                "offline package-manager artifact is truncated"
                            )
                        member_sha256 = _sha256(manifest_payload)
                        member_size = len(manifest_payload)
                    else:
                        member_sha256, member_size = _stream_reader_sha256_size(
                            reader
                        )
                    if member_size != member.size:
                        raise RuntimeBuildClosureError(
                            "offline package-manager artifact is truncated"
                        )
                    if member.name == "pnpm-distribution.tgz":
                        distribution_member = member
                    if member.name != "integrity-manifest.json":
                        inventory_by_name[member.name] = (
                            member_sha256,
                            member_size,
                        )

                if manifest_payload is None:
                    raise RuntimeBuildClosureError(
                        "offline dependency cache integrity manifest is missing"
                    )
                manifest = _strict_json(
                    manifest_payload,
                    label="offline dependency cache integrity manifest",
                )
                inventory = [
                    {"path": path, "sha256": sha256, "size": size}
                    for path, (sha256, size) in sorted(
                        inventory_by_name.items()
                    )
                ]
                expected_manifest = {
                    "schema": "personal-operator.offline-dependency-cache.v1",
                    "component": component,
                    "lockSha256": _sha256(lock_payload),
                    "lockIntegrities": lock_integrities,
                    "files": inventory,
                }
                if (
                    manifest != expected_manifest
                    or _canonical_json(manifest) != manifest_payload
                ):
                    raise RuntimeBuildClosureError(
                        "offline dependency cache integrity binding differs"
                    )
                if component == "openclaw-runtime":
                    if (
                        distribution_member is None
                        or not isinstance(artifact.distribution_sha512, str)
                        or _SHA_128.fullmatch(artifact.distribution_sha512)
                        is None
                        or not any(
                            name.startswith("store/")
                            for name in inventory_by_name
                        )
                    ):
                        raise RuntimeBuildClosureError(
                            "reviewed pnpm distribution or offline store differs"
                        )
                    distribution_digest = hashlib.sha512()
                    dist_reader = archive.extractfile(distribution_member)
                    if dist_reader is None:
                        raise RuntimeBuildClosureError(
                            "reviewed pnpm distribution or offline store differs"
                        )
                    while True:
                        block = dist_reader.read(_ARTIFACT_STREAM_CHUNK)
                        if not block:
                            break
                        distribution_digest.update(block)
                    if (
                        distribution_digest.hexdigest()
                        != artifact.distribution_sha512
                    ):
                        raise RuntimeBuildClosureError(
                            "reviewed pnpm distribution or offline store differs"
                        )
                    observed_entry = False
                    dist_stream = archive.extractfile(distribution_member)
                    if dist_stream is None:
                        raise RuntimeBuildClosureError(
                            "reviewed pnpm distribution is invalid"
                        )
                    with tarfile.open(
                        fileobj=dist_stream, mode="r:*"
                    ) as pnpm:
                        for member in pnpm.getmembers():
                            path = PurePosixPath(member.name)
                            if (
                                path.is_absolute()
                                or any(
                                    part in {"", ".", ".."}
                                    for part in path.parts
                                )
                                or member.issym()
                                or member.islnk()
                            ):
                                raise RuntimeBuildClosureError(
                                    "reviewed pnpm distribution is unsafe"
                                )
                            if (
                                member.name == "package/bin/pnpm.cjs"
                                and member.isreg()
                            ):
                                observed_entry = True
                    if not observed_entry:
                        raise RuntimeBuildClosureError(
                            "reviewed pnpm distribution entry is missing"
                        )
                else:
                    if (
                        artifact.distribution_sha512 != ""
                        or not inventory_by_name
                        or any(
                            not name.startswith("cache/")
                            for name in inventory_by_name
                        )
                    ):
                        raise RuntimeBuildClosureError(
                            "bridge dependency artifact is not cache-only"
                        )
    except RuntimeBuildClosureError:
        raise
    except UnicodeDecodeError as error:
        raise RuntimeBuildClosureError(
            "runtime lockfile is not UTF-8"
        ) from error
    except (tarfile.TarError, OSError) as error:
        raise RuntimeBuildClosureError(
            "offline package-manager artifact is invalid"
        ) from error
    return _sha256(manifest_payload)


class TrustedRuntimeBuildMaterialProvider:
    """Derive build material from authenticated source and raw build outputs.

    The exporter authenticates exact Git commit/tree objects.  The builder is
    given only the exported archive plus a digest-bound offline package-manager
    artifact and must create a fresh, cache-free, networkless output root.  It
    returns only file bytes; all provenance metadata is derived here.
    """

    __slots__ = ("_exporter", "_builder", "_artifacts", "_preflight_sources")

    def __init__(
        self,
        *,
        exporter: RuntimeSourceExporter,
        builder: RuntimeDependencyBuilder,
        package_manager_artifacts: Mapping[str, PackageManagerArtifact],
    ) -> None:
        export = getattr(exporter, "export_runtime_source", None)
        build = getattr(builder, "build_runtime", None)
        if export is None or not callable(export):
            raise RuntimeBuildClosureError(
                "trusted runtime Git object exporter is missing"
            )
        if build is None or not callable(build):
            raise RuntimeBuildClosureError(
                "trusted isolated runtime builder is missing"
            )
        if set(package_manager_artifacts) != set(RUNTIME_PACKAGE_MANAGERS):
            raise RuntimeBuildClosureError(
                "offline package-manager artifact set differs"
            )
        retained: dict[str, PackageManagerArtifact] = {}
        for component, expected in RUNTIME_PACKAGE_MANAGERS.items():
            artifact = package_manager_artifacts[component]
            if (
                not isinstance(artifact, PackageManagerArtifact)
                or (artifact.manager, artifact.version) != expected
                or not isinstance(artifact.source, ArtifactSource)
                or not 1 <= artifact.source.size <= MAX_BLOB_BYTES
                or not isinstance(artifact.distribution_sha512, str)
            ):
                raise RuntimeBuildClosureError(
                    "offline package-manager artifact identity differs"
                )
            if (
                not isinstance(artifact.reviewed_sha256, str)
                or _SHA_64.fullmatch(artifact.reviewed_sha256) is None
                or artifact.source.sha256 != artifact.reviewed_sha256
            ):
                raise RuntimeBuildClosureError(
                    "reviewed package-manager artifact digest differs"
                )
            retained[component] = artifact
        self._exporter = exporter
        self._builder = builder
        self._artifacts = retained
        self._preflight_sources: dict[
            tuple[str, int], tuple[bytes, dict[str, tuple[bytes, str]], str]
        ] = {}

    @staticmethod
    def _validate_openclaw_source(
        source_files: Mapping[str, tuple[bytes, str]],
        artifact: PackageManagerArtifact,
    ) -> None:
        try:
            package = json.loads(source_files["package.json"][0])
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeBuildClosureError(
                "runtime source OpenClaw package is invalid"
            ) from error
        if (
            not isinstance(package, Mapping)
            or package.get("version") != OPENCLAW_RUNTIME_VERSION
            or package.get("packageManager")
            != ("pnpm@11.2.2+sha512." + artifact.distribution_sha512)
        ):
            raise RuntimeBuildClosureError(
                "runtime source OpenClaw version or package-manager integrity differs"
            )

    def preflight_sources(
        self,
        bindings: Mapping[str, tuple[str, str]],
    ) -> None:
        """Authenticate every source/cache input before any executable builder."""

        if set(bindings) != set(RUNTIME_PACKAGE_MANAGERS):
            raise RuntimeBuildClosureError(
                "runtime build source preflight differs"
            )
        retained: dict[
            tuple[str, int], tuple[bytes, dict[str, tuple[bytes, str]], str]
        ] = {}
        for component in ("openclaw-runtime", "bridge-node-modules"):
            commit, tree = bindings[component]
            artifact = self._artifacts[component]
            for attempt in (1, 2):
                archive = self._exporter.export_runtime_source(
                    component=component,
                    attempt=attempt,
                    source_commit=commit,
                    source_tree=tree,
                )
                source_files = _runtime_source_files(
                    archive, component=component
                )
                if component == "openclaw-runtime":
                    self._validate_openclaw_source(source_files, artifact)
                lock_path = str(_runtime_build_recipe(component)["lockPath"])
                contract_sha256 = _offline_artifact_contract(
                    component=component,
                    artifact=artifact,
                    lock_payload=source_files[lock_path][0],
                )
                retained[(component, attempt)] = (
                    archive,
                    source_files,
                    contract_sha256,
                )
        self._preflight_sources = retained

    def build_material(
        self,
        *,
        component: str,
        attempt: int,
        source_commit: str,
        source_tree: str,
    ) -> Mapping[str, Any]:
        if component not in RUNTIME_PACKAGE_MANAGERS or attempt not in {1, 2}:
            raise RuntimeBuildClosureError(
                "trusted runtime build request is invalid"
            )
        recipe = _runtime_build_recipe(component)
        recipe_payload = _canonical_json(recipe)
        artifact = self._artifacts[component]
        retained = self._preflight_sources.get((component, attempt))
        if retained is None:
            archive = self._exporter.export_runtime_source(
                component=component,
                attempt=attempt,
                source_commit=source_commit,
                source_tree=source_tree,
            )
            source_files = _runtime_source_files(archive, component=component)
            if component == "openclaw-runtime":
                self._validate_openclaw_source(source_files, artifact)
            lock_path = str(recipe["lockPath"])
            artifact_contract_sha256 = _offline_artifact_contract(
                component=component,
                artifact=artifact,
                lock_payload=source_files[lock_path][0],
            )
        else:
            archive, source_files, artifact_contract_sha256 = retained
            lock_path = str(recipe["lockPath"])
        raw_files = self._builder.build_runtime(
            component=component,
            attempt=attempt,
            source_commit=source_commit,
            source_tree=source_tree,
            source_archive=archive,
            source_archive_sha256=_sha256(archive),
            package_manager_artifact=artifact.source,
            package_manager_artifact_sha256=artifact.source.sha256,
            package_manager_artifact_contract_sha256=(
                artifact_contract_sha256
            ),
            package_manager_distribution_sha512=(
                artifact.distribution_sha512
            ),
            build_recipe=recipe_payload,
            build_recipe_sha256=_sha256(recipe_payload),
            build_executor=RUNTIME_BUILD_EXECUTOR,
            build_executor_sha256=_sha256(RUNTIME_BUILD_EXECUTOR),
            builder_image=RUNTIME_BUILD_BUILDER_IMAGE,
            fresh_root_id=f"{component}-fresh-{attempt}",
            fresh_root=True,
            network_mode="none",
            no_cache=True,
            pull=False,
            source_date_epoch=0,
        )
        if not isinstance(raw_files, Mapping) or not raw_files:
            raise RuntimeBuildClosureError(
                "trusted runtime builder output inventory is empty"
            )
        files: dict[str, dict[str, object]] = {}
        for raw_path in sorted(raw_files):
            if not isinstance(raw_path, str):
                raise RuntimeBuildClosureError(
                    "trusted runtime builder output path is invalid"
                )
            path = PurePosixPath(raw_path)
            if (
                path.is_absolute()
                or path.as_posix() != raw_path
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise RuntimeBuildClosureError(
                    "trusted runtime builder output path is unsafe"
                )
            item = _exact_mapping(
                raw_files[raw_path],
                {"payload", "mode"},
                label="trusted runtime builder file",
            )
            payload = item["payload"]
            mode = item["mode"]
            if (
                not isinstance(payload, bytes)
                or not payload
                or len(payload) > MAX_BLOB_BYTES
                or mode not in {"0644", "0755"}
            ):
                raise RuntimeBuildClosureError(
                    "trusted runtime builder output entry is invalid"
                )
            files[raw_path] = {"payload": payload, "mode": mode}
        package_path = str(recipe["packagePath"])
        for source_path in (package_path, lock_path):
            if (
                source_path not in files
                or files[source_path]["payload"] != source_files[source_path][0]
            ):
                raise RuntimeBuildClosureError(
                    "trusted runtime output replaced package or lock bytes"
                )
        manager, version = RUNTIME_PACKAGE_MANAGERS[component]
        toolchain = {
            "platform": PLATFORM,
            "builderImage": RUNTIME_BUILD_BUILDER_IMAGE,
            "nodeBaseImage": NODE_RUNTIME_BASE,
            "pythonBaseImage": PYTHON_RUNTIME_BASE,
            "nodeVersion": "24.15.0",
            "packageManager": manager,
            "packageManagerVersion": version,
            "packageManagerArtifactSha256": artifact.source.sha256,
            "packageManagerArtifactContractSha256": (
                artifact_contract_sha256
            ),
            "packageManagerDistributionSha512": (
                artifact.distribution_sha512
            ),
            "networkMode": "none",
            "noCache": True,
            "pull": False,
            "sourceDateEpoch": 0,
        }
        package_payload = source_files[package_path][0]
        lock_payload = source_files[lock_path][0]
        return {
            "schema": "personal-operator.runtime-build-material.v2",
            "component": component,
            "sourceCommit": source_commit,
            "sourceTree": source_tree,
            "sourceArchiveSha256": _sha256(archive),
            "sourceArchiveSize": len(archive),
            "packagePath": package_path,
            "packageSha256": _sha256(package_payload),
            "lockPath": lock_path,
            "lockSha256": _sha256(lock_payload),
            "buildRecipe": recipe,
            "buildRecipeSha256": _sha256(recipe_payload),
            "toolchain": toolchain,
            "toolchainSha256": _sha256(_canonical_json(toolchain)),
            "dependencyMode": "production",
            "files": files,
        }


_TOOLCHAIN_FIELDS = {
    "platform",
    "builderImage",
    "nodeBaseImage",
    "pythonBaseImage",
    "nodeVersion",
    "packageManager",
    "packageManagerVersion",
    "packageManagerArtifactSha256",
    "packageManagerArtifactContractSha256",
    "packageManagerDistributionSha512",
    "networkMode",
    "noCache",
    "pull",
    "sourceDateEpoch",
}


def _closure_material(
    raw: Any,
    *,
    component: str,
    source_commit: str,
    source_tree: str,
) -> _BuildMaterial:
    fields = {
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
        "files",
    }
    value = _exact_mapping(raw, fields, label="runtime build material")
    if value["schema"] != "personal-operator.runtime-build-material.v2":
        raise RuntimeBuildClosureError("runtime build material schema is invalid")
    if value["component"] != component:
        raise RuntimeBuildClosureError("runtime build material component differs")
    if (
        value["sourceCommit"] != source_commit
        or value["sourceTree"] != source_tree
    ):
        raise RuntimeBuildClosureError("runtime build material source differs")
    package_path = value["packagePath"]
    lock_path = value["lockPath"]
    expected_lock = (
        "pnpm-lock.yaml"
        if component == "openclaw-runtime"
        else "package-lock.json"
    )
    if package_path != "package.json" or lock_path != expected_lock:
        raise RuntimeBuildClosureError(
            "runtime build material package or lock path differs"
        )
    source_archive_sha256 = value["sourceArchiveSha256"]
    source_archive_size = value["sourceArchiveSize"]
    package_sha256 = value["packageSha256"]
    lock_sha256 = value["lockSha256"]
    build_recipe_sha256 = value["buildRecipeSha256"]
    toolchain_sha256 = value["toolchainSha256"]
    if any(
        not isinstance(digest, str) or _SHA_64.fullmatch(digest) is None
        for digest in (
            source_archive_sha256,
            package_sha256,
            lock_sha256,
            build_recipe_sha256,
            toolchain_sha256,
        )
    ):
        raise RuntimeBuildClosureError(
            "runtime build material bound digest is invalid"
        )
    if (
        not isinstance(source_archive_size, int)
        or isinstance(source_archive_size, bool)
        or not 1 <= source_archive_size <= MAX_GIT_ARCHIVE_BYTES
    ):
        raise RuntimeBuildClosureError(
            "runtime build material source archive size is invalid"
        )
    if value["dependencyMode"] != "production":
        raise RuntimeBuildClosureError(
            "runtime build material is not production-only"
        )
    toolchain = _exact_mapping(
        value["toolchain"], _TOOLCHAIN_FIELDS, label="runtime build toolchain"
    )
    expected_manager, expected_manager_version = RUNTIME_PACKAGE_MANAGERS[component]
    if (
        toolchain["platform"] != PLATFORM
        or toolchain["builderImage"] != RUNTIME_BUILD_BUILDER_IMAGE
        or toolchain["nodeBaseImage"] != NODE_RUNTIME_BASE
        or toolchain["pythonBaseImage"] != PYTHON_RUNTIME_BASE
        or toolchain["nodeVersion"] != "24.15.0"
        or toolchain["packageManager"] != expected_manager
        or toolchain["packageManagerVersion"] != expected_manager_version
        or not isinstance(toolchain["packageManagerArtifactSha256"], str)
        or _SHA_64.fullmatch(toolchain["packageManagerArtifactSha256"])
        is None
        or not isinstance(
            toolchain["packageManagerArtifactContractSha256"], str
        )
        or _SHA_64.fullmatch(
            toolchain["packageManagerArtifactContractSha256"]
        )
        is None
        or (
            component == "openclaw-runtime"
            and (
                not isinstance(
                    toolchain["packageManagerDistributionSha512"], str
                )
                or _SHA_128.fullmatch(
                    toolchain["packageManagerDistributionSha512"]
                )
                is None
            )
        )
        or (
            component == "bridge-node-modules"
            and toolchain["packageManagerDistributionSha512"] != ""
        )
        or toolchain["networkMode"] != "none"
        or toolchain["noCache"] is not True
        or toolchain["pull"] is not False
        or toolchain["sourceDateEpoch"] != 0
    ):
        raise RuntimeBuildClosureError(
            "runtime build material toolchain is incomplete"
        )
    canonical_toolchain = _canonical_json(toolchain)
    if _sha256(canonical_toolchain) != toolchain_sha256:
        raise RuntimeBuildClosureError(
            "runtime build material toolchain digest differs"
        )
    build_recipe = _exact_mapping(
        value["buildRecipe"],
        set(_runtime_build_recipe(component)),
        label="runtime build recipe",
    )
    if (
        build_recipe != _runtime_build_recipe(component)
        or _sha256(_canonical_json(build_recipe)) != build_recipe_sha256
    ):
        raise RuntimeBuildClosureError(
            "runtime build material recipe differs"
        )

    raw_files = value["files"]
    if not isinstance(raw_files, Mapping) or not raw_files:
        raise RuntimeBuildClosureError("runtime build material inventory is empty")
    files: list[_ClosureFile] = []
    for raw_path in sorted(raw_files):
        if not isinstance(raw_path, str):
            raise RuntimeBuildClosureError(
                "runtime build material inventory path is invalid"
            )
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or path.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(
                part in {".git", ".pytest_cache", "__pycache__"}
                for part in path.parts
            )
        ):
            raise RuntimeBuildClosureError(
                "runtime build material inventory path is unsafe"
            )
        file = _exact_mapping(
            raw_files[raw_path], {"payload", "mode"}, label="runtime build file"
        )
        payload = file["payload"]
        mode = file["mode"]
        if (
            not isinstance(payload, bytes)
            or not payload
            or len(payload) > MAX_BLOB_BYTES
            or mode not in {"0644", "0755"}
        ):
            raise RuntimeBuildClosureError(
                "runtime build material inventory entry is invalid"
            )
        files.append(_ClosureFile(raw_path, payload, mode))
    by_path = {file.path: file for file in files}
    package_file = by_path.get(package_path)
    lock = by_path.get(lock_path)
    if (
        package_file is None
        or _sha256(package_file.payload) != package_sha256
        or lock is None
        or _sha256(lock.payload) != lock_sha256
    ):
        raise RuntimeBuildClosureError(
            "runtime build material package or lock digest differs"
        )
    paths = set(by_path)
    if component == "openclaw-runtime":
        try:
            package = json.loads(by_path["package.json"].payload)
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeBuildClosureError(
                "runtime build material OpenClaw package is invalid"
            ) from error
        if (
            not isinstance(package, Mapping)
            or package.get("version") != OPENCLAW_RUNTIME_VERSION
        ):
            raise RuntimeBuildClosureError(
                "runtime build material OpenClaw version differs"
            )
        complete = (
            {"openclaw.mjs", "package.json", "pnpm-lock.yaml"}.issubset(paths)
            and any(path.startswith("dist/") for path in paths)
            and any(path.startswith("node_modules/") for path in paths)
        )
    else:
        complete = (
            {"package.json", "package-lock.json"}.issubset(paths)
            and any(path.startswith("node_modules/@aws-sdk/") for path in paths)
            and any(path.startswith("node_modules/ws/") for path in paths)
        )
    if not complete:
        raise RuntimeBuildClosureError(
            "runtime build material inventory is incomplete"
        )
    if any(
        path.endswith((".test.js", ".test.mjs"))
        or "/test/" in f"/{path}/"
        for path in paths
    ):
        raise RuntimeBuildClosureError(
            "runtime build material inventory contains test content"
        )
    if any(_forbidden_browser_path(path) for path in paths):
        raise RuntimeBuildClosureError(
            "runtime build material contains a forbidden browser artifact"
        )
    return _BuildMaterial(
        component,
        source_commit,
        source_tree,
        source_archive_sha256,
        source_archive_size,
        package_path,
        package_sha256,
        lock_path,
        lock_sha256,
        tuple(sorted(build_recipe.items())),
        build_recipe_sha256,
        tuple(sorted(toolchain.items())),
        toolchain_sha256,
        "production",
        tuple(files),
    )


def _runtime_archive(material: _BuildMaterial) -> bytes:
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for file in material.files:
            if material.component == "bridge-node-modules" and not file.path.startswith(
                "node_modules/"
            ):
                continue
            prefix = (
                "opt/openclaw/"
                if material.component == "openclaw-runtime"
                else "app/"
            )
            member = tarfile.TarInfo(prefix + file.path)
            member.size = len(file.payload)
            member.mode = int(file.mode, 8)
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            tar.addfile(member, io.BytesIO(file.payload))
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=compressed,
        mtime=0,
    ) as archive:
        archive.write(tar_bytes.getvalue())
    return compressed.getvalue()


def _component_closure(
    material: _BuildMaterial,
) -> tuple[str, bytes, str, bytes]:
    archive_name = (
        "openclaw-runtime.tar.gz"
        if material.component == "openclaw-runtime"
        else "bridge-node-modules.tar.gz"
    )
    manifest_name = archive_name.removesuffix(".tar.gz") + ".manifest.json"
    archive = _runtime_archive(material)
    manifest = _canonical_json(
        {
            "schema": "personal-operator.runtime-build-component.v2",
            "component": material.component,
            "sourceCommit": material.source_commit,
            "sourceTree": material.source_tree,
            "sourceArchiveSha256": material.source_archive_sha256,
            "sourceArchiveSize": material.source_archive_size,
            "packagePath": material.package_path,
            "packageSha256": material.package_sha256,
            "lockPath": material.lock_path,
            "lockSha256": material.lock_sha256,
            "buildRecipe": material.build_recipe_mapping(),
            "buildRecipeSha256": material.build_recipe_sha256,
            "toolchain": material.toolchain_mapping(),
            "toolchainSha256": material.toolchain_sha256,
            "dependencyMode": material.dependency_mode,
            "outputSha256": material.output_sha256,
            "files": [file.inventory() for file in material.files],
            "archiveName": archive_name,
            "archiveSha256": _sha256(archive),
            "archiveSize": len(archive),
        }
    )
    return manifest_name, manifest, archive_name, archive


def prepare_runtime_build_closure(
    *,
    provider: TrustedRuntimeBuildMaterialProvider,
    release_commit: str,
    release_tree: str,
    openclaw_commit: str,
    openclaw_tree: str,
) -> RuntimeBuildClosure:
    """Verify two independent offline builds and bind their exact archives."""

    for label, value in (
        ("release commit", release_commit),
        ("release tree", release_tree),
        ("OpenClaw commit", openclaw_commit),
        ("OpenClaw tree", openclaw_tree),
    ):
        pattern = _SHA_40
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise RuntimeBuildClosureError(f"{label} is not canonical")
    if openclaw_commit != OPENCLAW_RUNTIME_COMMIT:
        raise RuntimeBuildClosureError("audited OpenClaw commit differs")
    if openclaw_tree != OPENCLAW_RUNTIME_TREE:
        raise RuntimeBuildClosureError("audited OpenClaw tree differs")
    if type(provider) is not TrustedRuntimeBuildMaterialProvider:
        raise RuntimeBuildClosureError(
            "trusted runtime build material provider is required"
        )
    build = provider.build_material
    bindings = {
        "openclaw-runtime": (openclaw_commit, openclaw_tree),
        "bridge-node-modules": (release_commit, release_tree),
    }
    provider.preflight_sources(bindings)
    retained: dict[str, _BuildMaterial] = {}
    for component, (commit, tree) in bindings.items():
        attempts: list[_BuildMaterial] = []
        for attempt in (1, 2):
            try:
                raw = build(
                    component=component,
                    attempt=attempt,
                    source_commit=commit,
                    source_tree=tree,
                )
            except RuntimeBuildClosureError:
                raise
            except Exception as error:
                raise RuntimeBuildClosureError(
                    f"independent {component} build material failed"
                ) from error
            attempts.append(
                _closure_material(
                    raw,
                    component=component,
                    source_commit=commit,
                    source_tree=tree,
                )
            )
        if attempts[0] != attempts[1]:
            raise RuntimeBuildClosureError(
                f"independent {component} build outputs differ"
            )
        retained[component] = attempts[0]

    artifacts: dict[str, bytes] = {}
    component_bindings: list[dict[str, object]] = []
    for component in ("openclaw-runtime", "bridge-node-modules"):
        manifest_name, manifest, archive_name, archive = _component_closure(
            retained[component]
        )
        artifacts[manifest_name] = manifest
        artifacts[archive_name] = archive
        component_bindings.append(
            {
                "component": component,
                "manifestName": manifest_name,
                "manifestSha256": _sha256(manifest),
                "archiveName": archive_name,
                "archiveSha256": _sha256(archive),
                "sourceArchiveSha256": retained[
                    component
                ].source_archive_sha256,
                "buildRecipeSha256": retained[
                    component
                ].build_recipe_sha256,
                "packageManagerArtifactSha256": retained[
                    component
                ].toolchain_mapping()["packageManagerArtifactSha256"],
                "packageManagerArtifactContractSha256": retained[
                    component
                ].toolchain_mapping()[
                    "packageManagerArtifactContractSha256"
                ],
                "packageManagerDistributionSha512": retained[
                    component
                ].toolchain_mapping()["packageManagerDistributionSha512"],
                "outputSha256": retained[component].output_sha256,
            }
        )
    closure_manifest = _canonical_json(
        {
            "schema": "personal-operator.runtime-build-closure.v2",
            "platform": PLATFORM,
            "pythonBaseImage": PYTHON_RUNTIME_BASE,
            "nodeBaseImage": NODE_RUNTIME_BASE,
            "releaseCommit": release_commit,
            "releaseTree": release_tree,
            "components": component_bindings,
        }
    )
    artifacts["runtime-build-closure.json"] = closure_manifest
    return RuntimeBuildClosure(
        dict(sorted(artifacts.items())),
        _sha256(closure_manifest),
        tuple(
            (
                component,
                str(
                    retained[component].toolchain_mapping()[
                        "packageManagerArtifactSha256"
                    ]
                ),
            )
            for component in sorted(retained)
        ),
    )


def _canonical_closure_object(payload: bytes, *, label: str) -> dict[str, Any]:
    value = _strict_json(payload, label=label)
    if _canonical_json(value) != payload:
        raise RuntimeBuildClosureError(f"{label} is not canonical")
    return value


def _validate_runtime_build_closure(
    closure: RuntimeBuildClosure,
    *,
    release_commit: str,
    release_tree: str,
) -> dict[str, str]:
    if not isinstance(closure, RuntimeBuildClosure):
        raise RuntimeBuildClosureError("runtime build closure type is invalid")
    expected_reviewed = dict(closure.reviewed_package_manager_artifacts)
    if (
        closure.reviewed_package_manager_artifacts
        != tuple(sorted(closure.reviewed_package_manager_artifacts))
        or set(expected_reviewed) != set(RUNTIME_PACKAGE_MANAGERS)
        or len(expected_reviewed) != len(
            closure.reviewed_package_manager_artifacts
        )
        or any(
            not isinstance(digest, str) or _SHA_64.fullmatch(digest) is None
            for digest in expected_reviewed.values()
        )
    ):
        raise RuntimeBuildClosureError(
            "reviewed package-manager artifact binding differs"
        )
    expected_names = {
        "runtime-build-closure.json",
        "openclaw-runtime.manifest.json",
        "openclaw-runtime.tar.gz",
        "bridge-node-modules.manifest.json",
        "bridge-node-modules.tar.gz",
    }
    if set(closure.artifacts) != expected_names or any(
        not isinstance(payload, bytes) or not payload
        for payload in closure.artifacts.values()
    ):
        raise RuntimeBuildClosureError("runtime build closure artifact set differs")
    closure_payload = closure.artifacts["runtime-build-closure.json"]
    if (
        not isinstance(closure.manifest_sha256, str)
        or _SHA_64.fullmatch(closure.manifest_sha256) is None
        or _sha256(closure_payload) != closure.manifest_sha256
    ):
        raise RuntimeBuildClosureError("runtime build closure manifest digest differs")
    root = _canonical_closure_object(
        closure_payload, label="runtime build closure manifest"
    )
    if set(root) != {
        "schema",
        "platform",
        "pythonBaseImage",
        "nodeBaseImage",
        "releaseCommit",
        "releaseTree",
        "components",
    }:
        raise RuntimeBuildClosureError("runtime build closure fields differ")
    if (
        root["schema"] != "personal-operator.runtime-build-closure.v2"
        or root["platform"] != PLATFORM
        or root["pythonBaseImage"] != PYTHON_RUNTIME_BASE
        or root["nodeBaseImage"] != NODE_RUNTIME_BASE
        or root["releaseCommit"] != release_commit
        or root["releaseTree"] != release_tree
    ):
        raise RuntimeBuildClosureError("runtime build closure identity differs")
    raw_bindings = root["components"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) != 2:
        raise RuntimeBuildClosureError("runtime build closure components differ")
    bindings: dict[str, dict[str, Any]] = {}
    for raw in raw_bindings:
        binding = _exact_mapping(
            raw,
            {
                "component",
                "manifestName",
                "manifestSha256",
                "archiveName",
                "archiveSha256",
                "sourceArchiveSha256",
                "buildRecipeSha256",
                "packageManagerArtifactSha256",
                "packageManagerArtifactContractSha256",
                "packageManagerDistributionSha512",
                "outputSha256",
            },
            label="runtime build closure component",
        )
        component = binding["component"]
        if component in bindings or component not in {
            "openclaw-runtime",
            "bridge-node-modules",
        }:
            raise RuntimeBuildClosureError(
                "runtime build closure component identity differs"
            )
        bindings[component] = binding
    if set(bindings) != {"openclaw-runtime", "bridge-node-modules"}:
        raise RuntimeBuildClosureError("runtime build closure components differ")

    arguments = {
        "RUNTIME_BUILD_CLOSURE_MANIFEST_SHA256": closure.manifest_sha256,
    }
    for component in ("openclaw-runtime", "bridge-node-modules"):
        binding = bindings[component]
        expected_manifest_name = component + ".manifest.json"
        expected_archive_name = component + ".tar.gz"
        if (
            binding["manifestName"] != expected_manifest_name
            or binding["archiveName"] != expected_archive_name
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure artifact name differs"
            )
        manifest_payload = closure.artifacts[expected_manifest_name]
        archive_payload = closure.artifacts[expected_archive_name]
        if (
            binding["manifestSha256"] != _sha256(manifest_payload)
            or binding["archiveSha256"] != _sha256(archive_payload)
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure archive or manifest digest differs"
            )
        manifest = _canonical_closure_object(
            manifest_payload, label=f"{component} manifest"
        )
        fields = {
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
        if set(manifest) != fields:
            raise RuntimeBuildClosureError(
                "runtime build closure component manifest fields differ"
            )
        if (
            manifest["schema"]
            != "personal-operator.runtime-build-component.v2"
            or manifest["component"] != component
            or manifest["archiveName"] != expected_archive_name
            or manifest["archiveSha256"] != _sha256(archive_payload)
            or manifest["archiveSize"] != len(archive_payload)
            or manifest["dependencyMode"] != "production"
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure component identity differs"
            )
        if component == "bridge-node-modules" and (
            manifest["sourceCommit"] != release_commit
            or manifest["sourceTree"] != release_tree
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure bridge source differs"
            )
        if any(
            not isinstance(manifest[field], str)
            or pattern.fullmatch(manifest[field]) is None
            for field, pattern in (
                ("sourceCommit", _SHA_40),
                ("sourceTree", _SHA_40),
                ("sourceArchiveSha256", _SHA_64),
                ("packageSha256", _SHA_64),
                ("lockSha256", _SHA_64),
                ("buildRecipeSha256", _SHA_64),
                ("toolchainSha256", _SHA_64),
                ("outputSha256", _SHA_64),
            )
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure component digest is malformed"
            )
        if (
            not isinstance(manifest["sourceArchiveSize"], int)
            or isinstance(manifest["sourceArchiveSize"], bool)
            or not 1
            <= manifest["sourceArchiveSize"]
            <= MAX_GIT_ARCHIVE_BYTES
            or manifest["packagePath"] != "package.json"
            or manifest["lockPath"]
            != (
                "pnpm-lock.yaml"
                if component == "openclaw-runtime"
                else "package-lock.json"
            )
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure source binding differs"
            )
        recipe = _exact_mapping(
            manifest["buildRecipe"],
            set(_runtime_build_recipe(component)),
            label="runtime build closure recipe",
        )
        if (
            recipe != _runtime_build_recipe(component)
            or _sha256(_canonical_json(recipe))
            != manifest["buildRecipeSha256"]
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure recipe differs"
            )
        toolchain = _exact_mapping(
            manifest["toolchain"],
            _TOOLCHAIN_FIELDS,
            label="runtime build closure toolchain",
        )
        expected_manager, expected_version = RUNTIME_PACKAGE_MANAGERS[component]
        if (
            toolchain["platform"] != PLATFORM
            or toolchain["builderImage"] != RUNTIME_BUILD_BUILDER_IMAGE
            or toolchain["nodeBaseImage"] != NODE_RUNTIME_BASE
            or toolchain["pythonBaseImage"] != PYTHON_RUNTIME_BASE
            or toolchain["nodeVersion"] != "24.15.0"
            or toolchain["packageManager"] != expected_manager
            or toolchain["packageManagerVersion"] != expected_version
            or not isinstance(toolchain["packageManagerArtifactSha256"], str)
            or _SHA_64.fullmatch(toolchain["packageManagerArtifactSha256"])
            is None
            or not isinstance(
                toolchain["packageManagerArtifactContractSha256"], str
            )
            or _SHA_64.fullmatch(
                toolchain["packageManagerArtifactContractSha256"]
            )
            is None
            or (
                component == "openclaw-runtime"
                and (
                    not isinstance(
                        toolchain["packageManagerDistributionSha512"], str
                    )
                    or _SHA_128.fullmatch(
                        toolchain["packageManagerDistributionSha512"]
                    )
                    is None
                )
            )
            or (
                component == "bridge-node-modules"
                and toolchain["packageManagerDistributionSha512"] != ""
            )
            or toolchain["networkMode"] != "none"
            or toolchain["noCache"] is not True
            or toolchain["pull"] is not False
            or toolchain["sourceDateEpoch"] != 0
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure toolchain differs"
            )
        if _sha256(_canonical_json(manifest["toolchain"])) != manifest[
            "toolchainSha256"
        ]:
            raise RuntimeBuildClosureError(
                "runtime build closure toolchain digest differs"
            )
        if (
            binding["sourceArchiveSha256"]
            != manifest["sourceArchiveSha256"]
            or binding["buildRecipeSha256"]
            != manifest["buildRecipeSha256"]
            or binding["packageManagerArtifactSha256"]
            != toolchain["packageManagerArtifactSha256"]
            or binding["packageManagerArtifactSha256"]
            != expected_reviewed[component]
            or binding["packageManagerArtifactContractSha256"]
            != toolchain["packageManagerArtifactContractSha256"]
            or binding["packageManagerDistributionSha512"]
            != toolchain["packageManagerDistributionSha512"]
            or binding["outputSha256"] != manifest["outputSha256"]
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure root binding differs"
            )
        files = manifest["files"]
        if (
            not isinstance(files, list)
            or not files
            or _sha256(_canonical_json(files)) != manifest["outputSha256"]
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure output inventory differs"
            )
        inventory: dict[str, dict[str, Any]] = {}
        for raw_file in files:
            file = _exact_mapping(
                raw_file,
                {"path", "sha256", "size", "mode"},
                label="runtime build closure file",
            )
            path = file["path"]
            if not isinstance(path, str) or path in inventory:
                raise RuntimeBuildClosureError(
                    "runtime build closure file inventory differs"
                )
            inventory[path] = file
        if (
            inventory.get(manifest["packagePath"], {}).get("sha256")
            != manifest["packageSha256"]
            or inventory.get(manifest["lockPath"], {}).get("sha256")
            != manifest["lockSha256"]
        ):
            raise RuntimeBuildClosureError(
                "runtime build closure package or lock binding differs"
            )
        if any(_forbidden_browser_path(path) for path in inventory):
            raise RuntimeBuildClosureError(
                "runtime build closure contains a forbidden browser artifact"
            )
        observed: set[str] = set()
        openclaw_package_payload: bytes | None = None
        try:
            with tarfile.open(fileobj=io.BytesIO(archive_payload), mode="r:gz") as tar:
                for member in tar.getmembers():
                    path = PurePosixPath(member.name)
                    prefix = (
                        "opt/openclaw/"
                        if component == "openclaw-runtime"
                        else "app/node_modules/"
                    )
                    if (
                        not member.isreg()
                        or path.is_absolute()
                        or any(part in {"", ".", ".."} for part in path.parts)
                        or not member.name.startswith(prefix)
                        or member.uid != 0
                        or member.gid != 0
                        or member.mtime != 0
                    ):
                        raise RuntimeBuildClosureError(
                            "runtime build closure archive member is unsafe"
                        )
                    relative = member.name.removeprefix("opt/openclaw/")
                    if component == "bridge-node-modules":
                        relative = "node_modules/" + member.name.removeprefix(
                            "app/node_modules/"
                        )
                    source = tar.extractfile(member)
                    payload = source.read() if source is not None else b""
                    if inventory.get(relative) != {
                        "path": relative,
                        "sha256": _sha256(payload),
                        "size": len(payload),
                        "mode": format(member.mode & 0o777, "04o"),
                    }:
                        raise RuntimeBuildClosureError(
                            "runtime build closure archive inventory differs"
                        )
                    if (
                        component == "openclaw-runtime"
                        and relative == "package.json"
                    ):
                        openclaw_package_payload = payload
                    observed.add(relative)
        except (tarfile.TarError, OSError) as error:
            raise RuntimeBuildClosureError(
                "runtime build closure archive is invalid"
            ) from error
        expected_observed = set(inventory)
        if component == "bridge-node-modules":
            expected_observed = {
                path for path in inventory if path.startswith("node_modules/")
            }
        if observed != expected_observed:
            raise RuntimeBuildClosureError(
                "runtime build closure archive inventory is incomplete"
            )
        if component == "openclaw-runtime":
            if manifest["sourceCommit"] != OPENCLAW_RUNTIME_COMMIT:
                raise RuntimeBuildClosureError(
                    "runtime build closure audited OpenClaw commit differs"
                )
            if manifest["sourceTree"] != OPENCLAW_RUNTIME_TREE:
                raise RuntimeBuildClosureError(
                    "runtime build closure audited OpenClaw tree differs"
                )
            try:
                package = json.loads(openclaw_package_payload)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeBuildClosureError(
                    "runtime build closure OpenClaw package is invalid"
                ) from error
            if (
                not isinstance(package, Mapping)
                or package.get("version") != OPENCLAW_RUNTIME_VERSION
                or package.get("packageManager")
                != (
                    "pnpm@11.2.2+sha512."
                    + str(toolchain["packageManagerDistributionSha512"])
                )
            ):
                raise RuntimeBuildClosureError(
                    "runtime build closure OpenClaw version or package-manager integrity differs"
                )
        prefix = "OPENCLAW" if component == "openclaw-runtime" else "BRIDGE"
        arguments.update(
            {
                f"{prefix}_RUNTIME_MANIFEST_SHA256"
                if prefix == "OPENCLAW"
                else "BRIDGE_NODE_MODULES_MANIFEST_SHA256": _sha256(
                    manifest_payload
                ),
                f"{prefix}_RUNTIME_ARCHIVE_SHA256"
                if prefix == "OPENCLAW"
                else "BRIDGE_NODE_MODULES_ARCHIVE_SHA256": _sha256(
                    archive_payload
                ),
                f"{prefix}_LOCK_SHA256": manifest["lockSha256"],
                f"{prefix}_TOOLCHAIN_SHA256": manifest["toolchainSha256"],
                f"{prefix}_PACKAGE_MANAGER_ARTIFACT_SHA256": toolchain[
                    "packageManagerArtifactSha256"
                ],
                f"{prefix}_PACKAGE_MANAGER_ARTIFACT_CONTRACT_SHA256": toolchain[
                    "packageManagerArtifactContractSha256"
                ],
                f"{prefix}_OUTPUT_SHA256": manifest["outputSha256"],
            }
        )
        if component == "openclaw-runtime":
            arguments["OPENCLAW_PACKAGE_MANAGER_DISTRIBUTION_SHA512"] = (
                toolchain["packageManagerDistributionSha512"]
            )
            arguments["OPENCLAW_SOURCE_COMMIT"] = manifest["sourceCommit"]
            arguments["OPENCLAW_SOURCE_TREE"] = manifest["sourceTree"]
    return arguments


def _dockerfile_instructions(text: str) -> tuple[tuple[str, str, str | None], ...]:
    lines = text.splitlines()
    instructions: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        match = re.match(r"^([A-Z]+)\s+(.+)$", raw)
        if match is None:
            raise ImagePublicationError("Dockerfile instruction syntax is invalid")
        name = match.group(1)
        logical = raw
        index += 1
        while logical.rstrip().endswith("\\"):
            if index >= len(lines):
                raise ImagePublicationError("Dockerfile continuation is truncated")
            logical = logical.rstrip()[:-1] + " " + lines[index].strip()
            index += 1
        heredoc: str | None = None
        marker = re.search(r"<<'([A-Z][A-Z0-9_]*)'\s*$", logical)
        if marker is not None:
            body: list[str] = []
            terminator = marker.group(1)
            while index < len(lines) and lines[index] != terminator:
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ImagePublicationError("Dockerfile heredoc is truncated")
            index += 1
            heredoc = "\n".join(body) + "\n"
        instructions.append((name, logical, heredoc))
    return tuple(instructions)


_FORBIDDEN_SHELL_BUILD = re.compile(
    r"(?:https?://|/dev/(?:tcp|udp)|"
    r"\b(?:curl|wget|aria2c|ftp|sftp|scp|ssh|ncat|socat)\b|"
    r"\bgit\s+(?:clone|fetch|pull|submodule)\b|"
    r"\b(?:apk|apt|apt-get|aptitude|dnf|yum|microdnf|zypper)\s+"
    r"(?:add|install|update|upgrade)\b|"
    r"\b(?:pip|pip3)\s+install\b|"
    r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+pip\s+install\b|"
    r"\b(?:npm|pnpm|yarn)\s+(?:add|ci|fetch|install|update)\b|"
    r"\bcorepack\s+(?:enable|install|prepare|use)\b|"
    r"\bgo\s+(?:get|install)\b|\bcargo\s+install\b|"
    r"\bgem\s+install\b|\bbundle\s+install\b|\bcomposer\s+install\b)",
    re.IGNORECASE,
)


def _validate_dockerfile_is_offline(files: Mapping[str, tuple[bytes, int]]) -> None:
    dockerfile = files["bridge/Dockerfile"][0]
    try:
        text = dockerfile.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImagePublicationError("Dockerfile is not UTF-8") from error
    if re.search(
        r"^[ \t]*#[ \t]*(?:syntax|escape|check)[ \t]*=",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        raise ImagePublicationError(
            "Dockerfile parser directive can change offline semantics"
        )
    if _FORBIDDEN_SHELL_BUILD.search(text):
        raise ImagePublicationError(
            "Dockerfile contains a network-fetched build input"
        )
    instructions = _dockerfile_instructions(text)
    from_values = [
        instruction
        for name, instruction, _ in instructions
        if name == "FROM"
    ]
    if from_values != ["FROM scratch"]:
        raise ImagePublicationError("Dockerfile base-image closure differs")
    run_count = 0
    for name, instruction, heredoc in instructions:
        if name == "ADD" and instruction != "ADD base/python-rootfs.tar /":
            raise ImagePublicationError("Dockerfile ADD is not the retained base")
        if name == "COPY" and re.search(
            r"(?:^|\s)--from(?:=|\s)", instruction, flags=re.IGNORECASE
        ):
            raise ImagePublicationError(
                "Dockerfile COPY cannot resolve an external build source"
            )
        if name != "RUN":
            continue
        run_count += 1
        if not instruction.startswith("RUN --network=none "):
            raise ImagePublicationError(
                "every Dockerfile RUN must declare network none"
            )
        if "--mount=" in instruction:
            raise ImagePublicationError("Dockerfile build mounts are forbidden")
        command = instruction.removeprefix("RUN --network=none ")
        if _FORBIDDEN_SHELL_BUILD.search(command):
            raise ImagePublicationError(
                "Dockerfile contains a network-fetched build input"
            )
        if heredoc is not None:
            if command != "python3 - <<'PY'":
                raise ImagePublicationError(
                    "Dockerfile heredoc interpreter is not exact"
                )
            try:
                tree = ast.parse(heredoc)
            except SyntaxError as error:
                raise ImagePublicationError("Dockerfile Python heredoc is invalid") from error
            forbidden_modules = {
                "ftplib",
                "http",
                "requests",
                "socket",
                "subprocess",
                "urllib",
            }
            for node in ast.walk(tree):
                module: str | None = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".", 1)[0] in forbidden_modules:
                            raise ImagePublicationError(
                                "Dockerfile heredoc imports a network module"
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module
                if module and module.split(".", 1)[0] in forbidden_modules:
                    raise ImagePublicationError(
                        "Dockerfile heredoc imports a network module"
                    )
    required = {
        "FROM scratch",
        "ADD base/python-rootfs.tar /",
        "COPY --chmod=0755 base/node /usr/local/bin/node",
        "ARG PYTHON_BASE_ROOTFS_SHA256",
        "ARG NODE_BASE_BINARY_SHA256",
        "RUNTIME_BUILD_CLOSURE_MANIFEST_SHA256",
        "openclaw-runtime.tar.gz",
        "bridge-node-modules.tar.gz",
        "ARG SOURCE_DATE_EPOCH=0",
    }
    if run_count == 0 or any(value not in text for value in required):
        raise ImagePublicationError(
            "Dockerfile does not consume the exact offline runtime closure"
        )
    if _sha256(dockerfile) != REVIEWED_RUNTIME_DOCKERFILE_SHA256:
        raise ImagePublicationError(
            "Dockerfile reviewed closure semantics differ"
        )


def _validate_probe(
    raw: Any,
    *,
    source_commit: str,
    catalog_sha256: str,
) -> bytes:
    evidence = _exact_mapping(
        raw,
        {
            "schema",
            "platform",
            "uid",
            "gid",
            "tlsRoots",
            "trustedRootsReadOnly",
            "startupStatus",
            "credentialsAbsent",
            "networkDenied",
            "ensurepipUnavailable",
            "pipModuleUnavailable",
            "browserArtifactsAbsent",
            "modelCallableTools",
            "forbiddenCommandsAbsent",
            "releaseCommit",
            "catalogSha256",
        },
        label="image probe evidence",
    )
    if evidence["schema"] != "personal-operator.image-probe.v1":
        raise ImagePublicationError("image probe schema is invalid")
    if evidence["platform"] != PLATFORM:
        raise ImagePublicationError("image probe did not run on exact ARM64")
    uid = evidence["uid"]
    gid = evidence["gid"]
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid <= 0
        or not isinstance(gid, int)
        or isinstance(gid, bool)
        or gid <= 0
    ):
        raise ImagePublicationError("image probe did not prove nonroot identity")
    if evidence["tlsRoots"] is not True:
        raise ImagePublicationError("image probe did not prove TLS roots")
    if evidence["trustedRootsReadOnly"] is not True:
        raise ImagePublicationError("image probe did not prove immutable roots")
    if evidence["startupStatus"] != "HEALTHY":
        raise ImagePublicationError("image probe did not prove startup health")
    if evidence["credentialsAbsent"] is not True:
        raise ImagePublicationError("image probe found credential material")
    if evidence["networkDenied"] is not True:
        raise ImagePublicationError("image probe did not prove network denial")
    if evidence["ensurepipUnavailable"] is not True:
        raise ImagePublicationError("image probe did not prove ensurepip absence")
    if evidence["pipModuleUnavailable"] is not True:
        raise ImagePublicationError("image probe did not prove pip module absence")
    if evidence["browserArtifactsAbsent"] is not True:
        raise ImagePublicationError("image probe found a browser artifact")
    if evidence["modelCallableTools"] != list(CAPABILITY_TOOL_NAMES):
        raise ImagePublicationError(
            "image probe model-callable capability surface differs"
        )
    if evidence["forbiddenCommandsAbsent"] != list(FORBIDDEN_RUNTIME_COMMANDS):
        raise ImagePublicationError(
            "image probe did not prove package or build tool absence"
        )
    if evidence["releaseCommit"] != source_commit:
        raise ImagePublicationError("image probe release binding differs")
    if evidence["catalogSha256"] != catalog_sha256:
        raise ImagePublicationError("image probe catalog binding differs")
    return _canonical_json(evidence)


def _spdx(
    *,
    subject: OciDescriptor,
    files: Sequence[RuntimeFile],
    created: str,
) -> bytes:
    inventory: list[dict[str, object]] = []
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": "SPDXRef-Package-runtime-image",
        }
    ]
    for file in files:
        path = file.path
        checksum = file.sha256
        file_id = "SPDXRef-File-" + hashlib.sha256(path.encode()).hexdigest()[:24]
        inventory.append(
            {
                "SPDXID": file_id,
                "checksums": [{"algorithm": "SHA256", "checksumValue": checksum}],
                "copyrightText": "NOASSERTION",
                "fileName": path,
                "licenseConcluded": "NOASSERTION",
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-Package-runtime-image",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": file_id,
            }
        )
    if not inventory:
        raise ImagePublicationError("SPDX inventory is empty")
    digest_hex = subject.digest.removeprefix("sha256:")
    return _canonical_json(
        {
            "SPDXID": "SPDXRef-DOCUMENT",
            "creationInfo": {
                "created": created,
                "creators": ["Tool: personal-operator-release-builder-2.0"],
            },
            "dataLicense": "CC0-1.0",
            "documentNamespace": (
                "https://personal-operator.invalid/spdx/" + digest_hex
            ),
            "files": inventory,
            "name": "personal-operator-bridge",
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-runtime-image",
                    "checksums": [
                        {"algorithm": "SHA256", "checksumValue": digest_hex}
                    ],
                    "copyrightText": "NOASSERTION",
                    "downloadLocation": "NOASSERTION",
                    "externalRefs": [
                        {
                            "referenceCategory": "PACKAGE-MANAGER",
                            "referenceLocator": (
                                f"pkg:oci/{REPOSITORY_NAME}@{subject.digest}"
                            ),
                            "referenceType": "purl",
                        }
                    ],
                    # The trusted builder inventories runtime files and their
                    # SHA-256 values, but does not claim SPDX's distinct SHA-1
                    # package-verification algorithm was executed.
                    "filesAnalyzed": False,
                    "licenseConcluded": "NOASSERTION",
                    "licenseDeclared": "NOASSERTION",
                    "name": REPOSITORY_NAME,
                }
            ],
            "relationships": relationships,
            "spdxVersion": "SPDX-2.3",
        }
    )


def _provenance(
    *,
    subject: OciDescriptor,
    source_commit: str,
    source_tree: str,
    git_archive_sha256: str,
    build_archive_sha256: str,
    catalog_source_sha256: str,
    capability_catalog_digest: str,
    runtime_build_closure_sha256: str,
    builder_id: str,
    dependencies: Sequence[BuilderDependency],
) -> bytes:
    return _canonical_json(
        {
            "_type": PROVENANCE_STATEMENT_TYPE,
            "predicateType": PROVENANCE_PREDICATE_TYPE,
            "subject": [
                {
                    "name": REPOSITORY_NAME,
                    "digest": {
                        "sha256": subject.digest.removeprefix("sha256:")
                    },
                }
            ],
            "predicate": {
                "buildDefinition": {
                    "buildType": BRIDGE_BUILD_TYPE,
                    "externalParameters": {
                        "buildContext": "bridge",
                        "sourceCommit": source_commit,
                        "sourceTree": source_tree,
                        "gitArchiveSha256": git_archive_sha256,
                        "buildArchiveSha256": build_archive_sha256,
                        "catalogSourceSha256": catalog_source_sha256,
                        "capabilityCatalogDigest": capability_catalog_digest,
                        "runtimeBuildClosureSha256": (
                            runtime_build_closure_sha256
                        ),
                        "platform": PLATFORM,
                    },
                    "internalParameters": {},
                    "resolvedDependencies": [
                        {
                            "uri": dependency.uri,
                            "digest": {
                                "sha256": dependency.digest.removeprefix("sha256:")
                            },
                        }
                        for dependency in dependencies
                    ],
                },
                "runDetails": {
                    "builder": {"id": builder_id},
                    "metadata": {
                        "invocationId": (
                            f"urn:sha256:{subject.digest.removeprefix('sha256:')}"
                        )
                    },
                },
            },
        }
    )


def _artifact_manifest(
    *,
    artifact_type: str,
    payload: OciDescriptor,
    subject: OciDescriptor,
    empty_config: OciDescriptor,
) -> bytes:
    return _canonical_json(
        {
            "artifactType": artifact_type,
            "config": empty_config.to_mapping(),
            "layers": [payload.to_mapping()],
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
            "subject": subject.to_mapping(),
        }
    )


def prepare_image_publication(
    *,
    git_archive: GitArchiveExporter,
    builder: OciBuilder,
    probe: ImageProbe,
    source_commit: str,
    source_tree: str,
    account: str,
    region: str,
    expected_capability_catalog_digest: str | None = None,
    created: str,
    builder_id: str,
    runtime_build_closure: RuntimeBuildClosure,
    builder_dependencies: Sequence[Mapping[str, Any]],
) -> ImagePublicationBundle:
    """Create one reproducible, probed, plan-bound OCI publication bundle."""

    _identity(
        source_commit=source_commit,
        source_tree=source_tree,
        account=account,
        region=region,
    )
    if expected_capability_catalog_digest is not None and (
        not isinstance(expected_capability_catalog_digest, str)
        or _SHA_64.fullmatch(expected_capability_catalog_digest) is None
    ):
        raise ImagePublicationError("expected capability catalog digest is malformed")
    if not isinstance(created, str) or _CREATED.fullmatch(created) is None:
        raise ImagePublicationError("SPDX creation time is not canonical UTC")
    if not isinstance(builder_id, str) or not builder_id or len(builder_id) > 1024:
        raise ImagePublicationError("builder identity is invalid")
    closure_arguments = _validate_runtime_build_closure(
        runtime_build_closure,
        release_commit=source_commit,
        release_tree=source_tree,
    )
    dependencies = _builder_dependencies(builder_dependencies)
    expected_dependencies = {
        (
            "pkg:docker/node@24.15.0-slim",
            NODE_RUNTIME_BASE.rsplit("@", 1)[1],
        ),
        (
            "pkg:docker/python@3.13-slim",
            PYTHON_RUNTIME_BASE.rsplit("@", 1)[1],
        ),
        (
            "urn:personal-operator:runtime-build-closure",
            "sha256:" + runtime_build_closure.manifest_sha256,
        ),
    }
    if {(item.uri, item.digest) for item in dependencies} != expected_dependencies:
        raise RuntimeBuildClosureError(
            "builder dependency closure differs from offline runtime materials"
        )
    exporter = getattr(git_archive, "export_archive", None)
    if exporter is None or not callable(exporter):
        raise ImagePublicationError("injected Git archive exporter is missing")
    try:
        raw_archive = exporter(
            source_commit=source_commit,
            source_tree=source_tree,
            path="bridge",
        )
    except Exception as error:
        raise ImagePublicationError("exact Git archive export failed") from error
    files, exact_archive = _archive_members(raw_archive)
    _validate_dockerfile_is_offline(files)
    (
        catalog_source_sha256,
        capability_catalog_digest,
        model_callable_tools,
    ) = _compile_capability_catalog(files, release_commit=source_commit)
    if (
        expected_capability_catalog_digest is not None
        and capability_catalog_digest != expected_capability_catalog_digest
    ):
        raise ImagePublicationError("compiled capability catalog digest differs")
    build_files = dict(files)
    for name, payload in runtime_build_closure.artifacts.items():
        build_files[f"bridge/build-closure/{name}"] = (payload, 0o644)
    build_archive, _ = _normalized_build_archive(build_files)
    build_method = getattr(builder, "build", None)
    if build_method is None or not callable(build_method):
        raise ImagePublicationError("injected OCI builder is missing")
    closures: list[_OciClosure] = []
    for build_id in ("fresh-1", "fresh-2"):
        try:
            result = build_method(
                build_archive,
                build_id=build_id,
                platform=PLATFORM,
                source_commit=source_commit,
                source_tree=source_tree,
                catalog_source_sha256=catalog_source_sha256,
                capability_catalog_digest=capability_catalog_digest,
                model_callable_tools=model_callable_tools,
                builder_dependencies=tuple(
                    dependency.to_mapping() for dependency in dependencies
                ),
                build_arguments={
                    **closure_arguments,
                    "PERSONAL_OPERATOR_RELEASE_COMMIT": source_commit,
                    "PERSONAL_OPERATOR_RELEASE_TREE": source_tree,
                    "PERSONAL_OPERATOR_CATALOG_SOURCE_SHA256": (
                        catalog_source_sha256
                    ),
                    "PERSONAL_OPERATOR_CAPABILITY_CATALOG_DIGEST": (
                        capability_catalog_digest
                    ),
                },
                network_mode="none",
                no_cache=True,
                pull=False,
                source_date_epoch=0,
            )
        except Exception as error:
            raise ImagePublicationError("fresh OCI build failed") from error
        closures.append(_validate_oci_build(result))
    if closures[0] != closures[1]:
        raise BuildReproducibilityError(
            "two fresh builds did not produce an identical OCI closure"
        )
    closure = closures[0]

    probe_method = getattr(probe, "run", None)
    if probe_method is None or not callable(probe_method):
        raise ImagePublicationError("injected image probe is missing")
    probe_payloads: list[bytes] = []
    for build_id, current in zip(("fresh-1", "fresh-2"), closures, strict=True):
        try:
            raw_probe = probe_method(
                manifest=current.manifest,
                blobs=current.blob_mapping(),
                build_id=build_id,
                platform=PLATFORM,
                network_mode="none",
                credentials={},
                read_only_root=True,
            )
        except Exception as error:
            raise ImagePublicationError("hermetic image probe failed") from error
        probe_payloads.append(
            _validate_probe(
                raw_probe,
                source_commit=source_commit,
                catalog_sha256=capability_catalog_digest,
            )
        )
    if probe_payloads[0] != probe_payloads[1]:
        raise BuildReproducibilityError("fresh image probe evidence differs")

    sbom_payload = _spdx(
        subject=closure.manifest_descriptor,
        files=closure.inventory,
        created=created,
    )
    provenance_payload = _provenance(
        subject=closure.manifest_descriptor,
        source_commit=source_commit,
        source_tree=source_tree,
        git_archive_sha256=_sha256(exact_archive),
        build_archive_sha256=_sha256(build_archive),
        catalog_source_sha256=catalog_source_sha256,
        capability_catalog_digest=capability_catalog_digest,
        runtime_build_closure_sha256=(
            runtime_build_closure.manifest_sha256
        ),
        builder_id=builder_id,
        dependencies=dependencies,
    )
    sbom_descriptor = OciDescriptor(
        SBOM_ARTIFACT_TYPE, _digest(sbom_payload), len(sbom_payload)
    )
    provenance_descriptor = OciDescriptor(
        PROVENANCE_ARTIFACT_TYPE,
        _digest(provenance_payload),
        len(provenance_payload),
    )
    empty_config_payload = b"{}"
    empty_config = OciDescriptor(
        OCI_EMPTY_CONFIG_MEDIA_TYPE,
        _digest(empty_config_payload),
        len(empty_config_payload),
    )
    sbom_manifest_payload = _artifact_manifest(
        artifact_type=SBOM_ARTIFACT_TYPE,
        payload=sbom_descriptor,
        subject=closure.manifest_descriptor,
        empty_config=empty_config,
    )
    provenance_manifest_payload = _artifact_manifest(
        artifact_type=PROVENANCE_ARTIFACT_TYPE,
        payload=provenance_descriptor,
        subject=closure.manifest_descriptor,
        empty_config=empty_config,
    )
    sbom_manifest = OciDescriptor(
        OCI_MANIFEST_MEDIA_TYPE,
        _digest(sbom_manifest_payload),
        len(sbom_manifest_payload),
    )
    provenance_manifest = OciDescriptor(
        OCI_MANIFEST_MEDIA_TYPE,
        _digest(provenance_manifest_payload),
        len(provenance_manifest_payload),
    )
    plan = ImagePublicationPlanV1(
        source_commit,
        source_tree,
        account,
        region,
        _sha256(exact_archive),
        _sha256(build_archive),
        len(build_archive),
        catalog_source_sha256,
        capability_catalog_digest,
        model_callable_tools,
        created,
        builder_id,
        dependencies,
        closure.manifest_descriptor,
        closure.config_descriptor,
        closure.layer_descriptors,
        sbom_descriptor,
        sbom_manifest,
        provenance_descriptor,
        provenance_manifest,
        tuple(
            ProbeEvidenceDescriptor(build_id, _sha256(payload), len(payload))
            for build_id, payload in zip(
                ("fresh-1", "fresh-2"), probe_payloads, strict=True
            )
        ),
    )
    manifests = {
        closure.manifest_descriptor.digest: closure.manifest,
        sbom_manifest.digest: sbom_manifest_payload,
        provenance_manifest.digest: provenance_manifest_payload,
    }
    blobs = {
        **closure.blob_mapping(),
        empty_config.digest: empty_config_payload,
        sbom_descriptor.digest: sbom_payload,
        provenance_descriptor.digest: provenance_payload,
    }
    bundle = ImagePublicationBundle(
        plan,
        manifests,
        blobs,
        dict(zip(("fresh-1", "fresh-2"), probe_payloads, strict=True)),
    )
    bundle.validate(expected_plan_sha256=plan.publication_plan_sha256)
    return bundle


class EcrImagePublisher:
    """Publish only the current verified image effect through attested ECR."""

    def __init__(self, ecr: AttestedAwsClientV2) -> None:
        if not isinstance(ecr, AttestedAwsClientV2):
            raise ImagePublicationError(
                "image publisher requires authenticated ECR authority"
            )
        try:
            account = ecr.account
            ecr.require_scope(
                service="ecr",
                account=account,
                region=REQUIRED_REGION,
                capability="mutation",
            )
        except AwsAuthorityError as error:
            raise ImagePublicationError(
                "image publisher requires authenticated ECR authority"
            ) from error
        if _ACCOUNT.fullmatch(account) is None or account == "000000000000":
            raise ImagePublicationError(
                "image publisher requires authenticated ECR authority"
            )
        self._ecr = ecr

    def _call(self, method_name: str, **arguments: Any) -> dict[str, Any]:
        try:
            response = self._ecr.invoke(method_name, **arguments)
        except AwsAuthorityError as error:
            raise ImagePublicationError(
                f"authenticated ECR authority lacks {method_name}"
            ) from error
        except (TimeoutError, ConnectionError) as error:
            raise ImagePublicationAmbiguous(
                f"{method_name} ended without authoritative acknowledgement"
            ) from error
        except Exception as error:
            response = getattr(error, "response", None)
            body = response.get("Error") if isinstance(response, dict) else None
            code = body.get("Code") if isinstance(body, dict) else None
            if code in {"ImageAlreadyExistsException", "ImageTagAlreadyExistsException"}:
                raise ImagePublicationCollision(
                    "immutable image identity collision; reconcile independently"
                ) from error
            raise ImagePublicationAmbiguous(
                f"{method_name} failed after a possible registry mutation"
            ) from error
        try:
            return _mapping(response, label=method_name)
        except ImagePublicationError as error:
            if method_name != "batch_check_layer_availability":
                raise ImagePublicationAmbiguous(
                    f"{method_name} returned a malformed post-mutation acknowledgement"
                ) from error
            raise

    def _available_layers(
        self,
        *,
        account: str,
        digests: Sequence[str],
    ) -> set[str]:
        available: set[str] = set()
        for offset in range(0, len(digests), 100):
            chunk = list(digests[offset : offset + 100])
            response = self._call(
                "batch_check_layer_availability",
                registryId=account,
                repositoryName=REPOSITORY_NAME,
                layerDigests=chunk,
            )
            layers = response.get("layers")
            failures = response.get("failures")
            if not isinstance(layers, list) or not isinstance(failures, list):
                raise ImagePublicationError("ECR layer check is incomplete")
            seen: set[str] = set()
            for raw in layers:
                layer = _mapping(raw, label="ECR layer check")
                required = {"layerDigest", "layerAvailability"}
                allowed = required | {"layerSize", "mediaType"}
                if not required.issubset(layer) or set(layer) - allowed:
                    raise ImagePublicationError(
                        "ECR layer check fields are not exact"
                    )
                digest = _require_digest(layer["layerDigest"], label="ECR layer")
                if digest not in chunk or digest in seen:
                    raise ArtifactSubstitutionError("ECR layer check subject differs")
                seen.add(digest)
                status = layer["layerAvailability"]
                layer_size = layer.get("layerSize")
                media_type = layer.get("mediaType")
                if layer_size is not None and (
                    not isinstance(layer_size, int)
                    or isinstance(layer_size, bool)
                    or layer_size <= 0
                ):
                    raise ImagePublicationError("ECR layer size is malformed")
                if media_type is not None and (
                    not isinstance(media_type, str) or not media_type
                ):
                    raise ImagePublicationError("ECR layer media type is malformed")
                if status == "AVAILABLE":
                    available.add(digest)
                elif status in {"UNAVAILABLE", "ARCHIVED"}:
                    raise ImagePublicationError(
                        "ECR layer is not safely publishable"
                    )
                else:
                    raise ImagePublicationError(
                        "ECR layer availability is not authoritative"
                    )
            for raw in failures:
                failure = _mapping(raw, label="ECR layer failure")
                required = {"layerDigest", "failureCode"}
                allowed = required | {"failureReason"}
                if not required.issubset(failure) or set(failure) - allowed:
                    raise ImagePublicationError(
                        "ECR layer failure fields are not exact"
                    )
                digest = _require_digest(
                    failure["layerDigest"], label="ECR failed layer"
                )
                reason = failure.get("failureReason")
                if reason is not None and (
                    not isinstance(reason, str) or not reason
                ):
                    raise ImagePublicationError(
                        "ECR layer failure reason is malformed"
                    )
                if digest not in chunk or digest in seen:
                    raise ArtifactSubstitutionError(
                        "ECR layer failure subject differs"
                    )
                seen.add(digest)
                code = failure["failureCode"]
                if code == "MissingLayerDigest":
                    continue
                if code == "InvalidLayerDigest":
                    raise ImagePublicationError(
                        "ECR rejected the plan-bound layer digest"
                    )
                raise ImagePublicationError(
                    "ECR layer failure code is not authoritative"
                )
            if seen != set(chunk):
                raise ImagePublicationError("ECR layer check omitted a subject")
        return available

    def _upload_blob(self, *, account: str, digest: str, payload: bytes) -> None:
        initiated = self._call(
            "initiate_layer_upload",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
        )
        upload_id = initiated.get("uploadId")
        part_size = initiated.get("partSize")
        if (
            not isinstance(upload_id, str)
            or not upload_id
            or not isinstance(part_size, int)
            or isinstance(part_size, bool)
            or part_size <= 0
        ):
            raise ImagePublicationAmbiguous(
                "ECR layer upload initiation acknowledgement is malformed"
            )
        part_size = min(part_size, 20 * 1024 * 1024)
        for first in range(0, len(payload), part_size):
            part = payload[first : first + part_size]
            last = first + len(part) - 1
            uploaded = self._call(
                "upload_layer_part",
                registryId=account,
                repositoryName=REPOSITORY_NAME,
                uploadId=upload_id,
                partFirstByte=first,
                partLastByte=last,
                layerPartBlob=part,
            )
            if (
                uploaded.get("uploadId") != upload_id
                or uploaded.get("lastByteReceived") != last
                or uploaded.get("registryId") != account
                or uploaded.get("repositoryName") != REPOSITORY_NAME
            ):
                raise ImagePublicationAmbiguous(
                    "ECR layer upload returned a partial acknowledgement"
                )
        completed = self._call(
            "complete_layer_upload",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            uploadId=upload_id,
            layerDigests=[digest],
        )
        if (
            completed.get("uploadId") != upload_id
            or completed.get("layerDigest") != digest
            or completed.get("registryId") != account
            or completed.get("repositoryName") != REPOSITORY_NAME
        ):
            raise ImagePublicationAmbiguous(
                "ECR layer completion acknowledgement differs"
            )

    def _put_manifest(
        self,
        *,
        account: str,
        descriptor: OciDescriptor,
        payload: bytes,
        tag: str | None,
    ) -> None:
        arguments: dict[str, object] = {
            "registryId": account,
            "repositoryName": REPOSITORY_NAME,
            "imageManifest": payload.decode("utf-8"),
            "imageManifestMediaType": OCI_MANIFEST_MEDIA_TYPE,
            "imageDigest": descriptor.digest,
        }
        if tag is not None:
            arguments["imageTag"] = tag
        response = self._call("put_image", **arguments)
        try:
            image = _mapping(response.get("image"), label="put_image image")
            if (
                image.get("registryId") != account
                or image.get("repositoryName") != REPOSITORY_NAME
                or image.get("imageManifestMediaType") != OCI_MANIFEST_MEDIA_TYPE
            ):
                raise ArtifactSubstitutionError("ECR manifest repository differs")
            image_id = _mapping(image.get("imageId"), label="put_image image ID")
            expected_id = {"imageDigest": descriptor.digest}
            if tag is not None:
                expected_id["imageTag"] = tag
            if image_id != expected_id:
                raise ArtifactSubstitutionError("ECR manifest identity differs")
            returned_manifest = image.get("imageManifest")
            if (
                returned_manifest is not None
                and returned_manifest != payload.decode("utf-8")
            ):
                raise ArtifactSubstitutionError("ECR manifest bytes differ")
        except ImagePublicationError as error:
            raise ImagePublicationAmbiguous(
                "ECR manifest post-mutation acknowledgement differs"
            ) from error

    def publish_effect(
        self,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedImagePublicationPreflightV1 | None = None,
        *,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        """Dispatch exactly the current journal-bound, preflight-closed effect."""

        if type(fresh_authority) is not FreshDispatchAuthorityV1:
            raise ArtifactSubstitutionError(
                "image publication requires fresh dispatch authority"
            )
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise ArtifactSubstitutionError(
                "image publication requires a verified private mutation"
            )
        if not isinstance(preflight, VerifiedImagePublicationPreflightV1):
            raise ArtifactSubstitutionError(
                "image publication requires verified preflight authority"
            )
        effect = preflight._bind_verified_mutation(verified)
        try:
            self._ecr.require_scope(
                service="ecr",
                account=effect.account,
                region=effect.region,
                capability="mutation",
            )
        except AwsAuthorityError as error:
            raise ArtifactSubstitutionError(
                "image effect differs from authenticated ECR authority"
            ) from error
        try:
            operation_sha256 = (
                verified.resolved_request.mutation_request.operation_sha256
            )
            resolved_request_sha256 = verified.resolved_request.digest()
        except ContractError as error:
            raise ArtifactSubstitutionError(
                "image publication verified private mutation is invalid"
            ) from error
        if effect.effect_kind == "ECR_BLOB_PUT":
            available = self._available_layers(
                account=effect.account,
                digests=(effect.digest,),
            )
            try:
                attempt = fresh_authority.consume(
                    provider="ECR",
                    operation_sha256=operation_sha256,
                    resolved_request_sha256=resolved_request_sha256,
                )
            except DispatchAttemptError as error:
                raise ArtifactSubstitutionError(
                    "image publication fresh dispatch authority differs"
                ) from error
            if effect.digest not in available:
                self._upload_blob(
                    account=effect.account,
                    digest=effect.digest,
                    payload=effect.payload,
                )
        else:
            try:
                attempt = fresh_authority.consume(
                    provider="ECR",
                    operation_sha256=operation_sha256,
                    resolved_request_sha256=resolved_request_sha256,
                )
            except DispatchAttemptError as error:
                raise ArtifactSubstitutionError(
                    "image publication fresh dispatch authority differs"
                ) from error
            self._put_manifest(
                account=effect.account,
                descriptor=OciDescriptor(
                    effect.media_type,
                    effect.digest,
                    effect.size,
                ),
                payload=effect.payload,
                tag=effect.tag,
            )
        return attempt
