"""Concrete, local-only production path for the reviewed runtime image.

The generic protocols in :mod:`release_tools.image_publication` remain useful
for hostile unit fixtures, but they are not accepted here.  This module owns
the exact Git, BuildKit, OCI-layout and runtime-probe implementations used by
the production command.  It performs no AWS or provider call.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
import platform
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import stat
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Mapping, Sequence

from release_tools.image_publication import (
    CAPABILITY_TOOL_NAMES,
    FORBIDDEN_RUNTIME_COMMANDS,
    MAX_BLOB_BYTES,
    NODE_RUNTIME_BASE,
    OCI_CONFIG_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPES,
    OCI_MANIFEST_MEDIA_TYPE,
    OPENCLAW_RUNTIME_COMMIT,
    OPENCLAW_RUNTIME_TREE,
    PLATFORM,
    PYTHON_RUNTIME_BASE,
    RUNTIME_BUILD_BUILDER_IMAGE,
    RUNTIME_BUILD_EXECUTOR,
    RUNTIME_PACKAGE_MANAGERS,
    RuntimeBuildClosure,
    RuntimeBuildClosureError,
    ImagePublicationBundle,
    ImagePublicationError,
    PackageManagerArtifact,
    RetainedRegularFile,
    TrustedRuntimeBuildMaterialProvider,
    _coerce_artifact_source,
    _canonical_json,
    _digest,
    _require_digest,
    _sha256,
    _strict_json,
    _offline_artifact_contract,
    _runtime_source_files,
    _validate_oci_build,
    prepare_image_publication,
    prepare_runtime_build_closure,
    reviewed_package_manager_artifact,
)


PRODUCTION_BUILDER_ID = (
    "https://personal-operator.invalid/builders/offline-buildkit-v1"
)
# Build /work tmpfs must hold the extracted ~2.1 GiB offline artifact plus its
# offline install expansion; 4 GiB is under that floor.
BUILD_TMPFS_BYTES = 8 * 1024 * 1024 * 1024
_SHA_40 = re.compile(r"[0-9a-f]{40}")
_SHA_64 = re.compile(r"[0-9a-f]{64}")
_CONTAINER_ID_OUTPUT = re.compile(rb"[0-9a-f]{64}\n?")
_CREDENTIAL_NAME = re.compile(
    r"(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|"
    r"GOOGLE_CLIENT_SECRET|TELEGRAM_BOT_TOKEN|PASSWORD|PRIVATE_KEY)"
)
@dataclass(frozen=True, slots=True)
class _ReviewedPlatformToolsV1:
    git_path: str
    git_sha256: str
    docker_path: str
    docker_sha256: str
    buildx_path: str
    buildx_sha256: str
    docker_socket: str
    git_in_place: bool = False


_REVIEWED_PLATFORM_TOOLS = {
    ("Darwin", "arm64"): _ReviewedPlatformToolsV1(
        git_path="/Library/Developer/CommandLineTools/usr/bin/git",
        git_sha256=(
            "3121e7e4d16059539731c58d94888709c12904abe922acde8e37caef4607c1d1"
        ),
        docker_path="/opt/homebrew/Cellar/docker/29.6.2/bin/docker",
        docker_sha256=(
            "eade1c3a5dda47534dc776f2f534c99cc94cfcf9ce07c4bf09e98258d13e7d7a"
        ),
        buildx_path=(
            "/opt/homebrew/Cellar/docker-buildx/0.35.0/bin/docker-buildx"
        ),
        buildx_sha256=(
            "8d50dd2ab46d37b57f6cb41f31ee64ebdfd20ea402e3ffbe26b7c1ff42d3ca7e"
        ),
        docker_socket="{account_home}/.colima/default/docker.sock",
        git_in_place=True,
    ),
}
_LOCAL_EXECUTION_TOKEN = object()


class _ReviewedExecutableV1:
    __slots__ = ("path", "sha256", "_descriptor", "_identity")

    def __init__(self, path: str, sha256: str) -> None:
        if _SHA_64.fullmatch(sha256) is None:
            raise ImagePublicationError("reviewed executable digest is invalid")
        candidate = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(candidate, flags)
            metadata = os.fstat(descriptor)
            path_metadata = candidate.lstat()
        except OSError as error:
            raise ImagePublicationError(
                "reviewed executable is unavailable"
            ) from error
        try:
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(path_metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
                or metadata.st_uid not in {0, os.getuid()}
                or metadata.st_mode & 0o022
            ):
                raise ImagePublicationError(
                    "reviewed executable ownership or mode differs"
                )
            if self._descriptor_sha256(descriptor) != sha256:
                raise ImagePublicationError(
                    "reviewed executable bytes differ"
                )
        except Exception:
            os.close(descriptor)
            raise
        self.path = str(candidate)
        self.sha256 = sha256
        self._descriptor = descriptor
        self._identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_uid,
            stat.S_IMODE(metadata.st_mode),
        )

    @staticmethod
    def _descriptor_sha256(descriptor: int) -> str:
        digest = hashlib.sha256()
        offset = 0
        while True:
            block = os.pread(descriptor, 1024 * 1024, offset)
            if not block:
                break
            digest.update(block)
            offset += len(block)
        return digest.hexdigest()

    def validate_path(self) -> None:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
            retained = os.fstat(self._descriptor)
        except OSError as error:
            raise ImagePublicationError(
                "reviewed executable identity is unavailable"
            ) from error
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_uid,
            stat.S_IMODE(metadata.st_mode),
        )
        retained_identity = (
            retained.st_dev,
            retained.st_ino,
            retained.st_size,
            retained.st_mtime_ns,
            retained.st_ctime_ns,
            retained.st_uid,
            stat.S_IMODE(retained.st_mode),
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(retained.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or retained.st_uid not in {0, os.getuid()}
            or metadata.st_mode & 0o022
            or retained.st_mode & 0o022
        ):
            raise ImagePublicationError(
                "reviewed executable ownership or mode differs"
            )
        if self._descriptor_sha256(self._descriptor) != self.sha256:
            raise ImagePublicationError("reviewed executable bytes differ")
        if identity != self._identity or retained_identity != self._identity:
            raise ImagePublicationError(
                "reviewed executable identity changed during execution"
            )

    def copy_to(self, destination: Path) -> None:
        self.validate_path()
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o500,
        )
        try:
            offset = 0
            while True:
                block = os.pread(self._descriptor, 1024 * 1024, offset)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short reviewed executable copy")
                    view = view[written:]
                offset += len(block)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o500)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            destination.unlink(missing_ok=True)
            raise
        os.close(descriptor)
        self.validate_path()

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1


class ReviewedLocalExecutionV1:
    """Retained reviewed tools plus one exact owned local Docker socket."""

    __slots__ = (
        "_git",
        "_docker",
        "_buildx",
        "_socket_path",
        "_socket_identity",
        "_environment_root",
        "_closed",
    )

    def __init__(
        self,
        *,
        config: _ReviewedPlatformToolsV1,
        account_home: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _LOCAL_EXECUTION_TOKEN:
            raise ImagePublicationError(
                "reviewed local execution capability is not constructible"
            )
        self._environment_root = tempfile.TemporaryDirectory(
            prefix="personal-operator-reviewed-execution-"
        )
        environment_root = Path(self._environment_root.name)
        environment_root.chmod(0o700)
        tools_root = environment_root / "tools"
        tools_root.mkdir(mode=0o700)
        sources: list[_ReviewedExecutableV1] = []
        try:
            for name, path, digest in (
                ("git", config.git_path, config.git_sha256),
                ("docker", config.docker_path, config.docker_sha256),
                ("buildx", config.buildx_path, config.buildx_sha256),
            ):
                source = _ReviewedExecutableV1(path, digest)
                if name == "git" and config.git_in_place:
                    expected = Path(
                        "/Library/Developer/CommandLineTools/usr/bin/git"
                    )
                    candidate = Path(path)
                    if candidate != expected:
                        source.close()
                        raise ImagePublicationError(
                            "in-place reviewed Git path differs"
                        )
                    for protected in (
                        Path("/Library"),
                        Path("/Library/Developer"),
                        Path("/Library/Developer/CommandLineTools"),
                        Path("/Library/Developer/CommandLineTools/usr"),
                        Path("/Library/Developer/CommandLineTools/usr/bin"),
                        expected,
                    ):
                        metadata = protected.lstat()
                        if (
                            metadata.st_uid != 0
                            or metadata.st_mode & 0o022
                            or protected.is_symlink()
                        ):
                            source.close()
                            raise ImagePublicationError(
                                "in-place reviewed Git ownership differs"
                            )
                    self._git = source
                    continue
                sources.append(source)
                source.copy_to(tools_root / name)
            for source in sources:
                source.close()
            sources.clear()
            if not hasattr(self, "_git"):
                self._git = _ReviewedExecutableV1(
                    str(tools_root / "git"), config.git_sha256
                )
            self._docker = _ReviewedExecutableV1(
                str(tools_root / "docker"), config.docker_sha256
            )
            self._buildx = _ReviewedExecutableV1(
                str(tools_root / "buildx"), config.buildx_sha256
            )
            self._socket_path = config.docker_socket.format(
                account_home=account_home
            )
            self._socket_identity = self._validate_socket()
        except Exception:
            for source in sources:
                source.close()
            for name in ("_git", "_docker", "_buildx"):
                if hasattr(self, name):
                    getattr(self, name).close()
            self._environment_root.cleanup()
            raise
        self._closed = False

    def _validate_socket(self) -> tuple[int, int, int, int]:
        try:
            metadata = os.stat(self._socket_path, follow_symlinks=False)
        except OSError as error:
            raise ImagePublicationError(
                "reviewed local Docker socket is unavailable"
            ) from error
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            stat.S_IMODE(metadata.st_mode),
        )
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (
                hasattr(self, "_socket_identity")
                and identity != self._socket_identity
            )
        ):
            raise ImagePublicationError(
                "reviewed local Docker socket identity differs"
            )
        return identity

    def _environment(self) -> dict[str, str]:
        root = self._environment_root.name
        return {
            "BUILDKIT_PROGRESS": "plain",
            "DOCKER_CONFIG": root,
            "DOCKER_HOST": "unix://" + self._socket_path,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": root,
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "SOURCE_DATE_EPOCH": "0",
        }

    def _run(
        self,
        executable: _ReviewedExecutableV1,
        arguments: Sequence[str],
        *,
        input: bytes | None = None,
        stdin: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if self._closed or any(
            not isinstance(argument, str) or "\x00" in argument
            for argument in arguments
        ):
            raise ImagePublicationError("reviewed command request is invalid")
        executable.validate_path()
        if executable in {self._docker, self._buildx}:
            self._validate_socket()
        completed = subprocess.run(
            [executable.path, *arguments],
            input=input,
            stdin=(subprocess.DEVNULL if input is None and stdin is None else stdin),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(),
            check=False,
        )
        executable.validate_path()
        if executable in {self._docker, self._buildx}:
            self._validate_socket()
        return completed

    def run_git(
        self,
        arguments: Sequence[str],
        *,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(self._git, arguments, input=input)

    def run_docker(
        self, arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(self._docker, arguments)

    def run_buildx(
        self, arguments: Sequence[str], *, input: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(self._buildx, arguments, input=input)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._git.close()
        self._docker.close()
        self._buildx.close()
        self._environment_root.cleanup()

    def __enter__(self) -> "ReviewedLocalExecutionV1":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_reviewed_local_execution() -> ReviewedLocalExecutionV1:
    """Open the sole platform-reviewed local command and daemon capability."""

    key = (platform.system(), platform.machine())
    config = _REVIEWED_PLATFORM_TOOLS.get(key)
    if config is None:
        raise ImagePublicationError(
            "local production toolchain platform is not reviewed"
        )
    try:
        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError) as error:
        raise ImagePublicationError("local account home is unavailable") from error
    return ReviewedLocalExecutionV1(
        config=config,
        account_home=account_home,
        _token=_LOCAL_EXECUTION_TOKEN,
    )


def _regular_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ImagePublicationError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or not 1 <= metadata.st_size <= maximum
    ):
        raise ImagePublicationError(f"{label} is not a bounded regular file")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ImagePublicationError(f"{label} cannot be read") from error
    if len(payload) != metadata.st_size:
        raise ImagePublicationError(f"{label} changed while being read")
    return payload


def _exact_git_object_archive(
    *,
    execution: ReviewedLocalExecutionV1,
    repository: Path,
    source_commit: str,
    source_tree: str,
    path_prefix: str | None,
) -> bytes:
    """Build a tar after independently hashing raw commit/tree/blob objects."""

    if (
        _SHA_40.fullmatch(source_commit) is None
        or _SHA_40.fullmatch(source_tree) is None
        or path_prefix not in {None, "bridge"}
    ):
        raise ImagePublicationError("exact Git object request is invalid")

    def run(
        arguments: Sequence[str], *, input: bytes | None = None
    ) -> bytes:
        completed = execution.run_git(
            ["-C", str(repository), *arguments], input=input
        )
        if completed.returncode != 0 or not isinstance(
            completed.stdout, bytes
        ):
            raise ImagePublicationError("exact Git object read failed")
        return completed.stdout

    def read_objects(
        requests: Sequence[tuple[str, bytes]],
    ) -> dict[str, bytes]:
        if not requests or len({object_id for object_id, _ in requests}) != len(
            requests
        ):
            raise ImagePublicationError("exact Git object request is invalid")
        raw_objects = run(
            ["cat-file", "--batch"],
            input=b"".join(
                object_id.encode("ascii") + b"\n"
                for object_id, _ in requests
            ),
        )
        objects: dict[str, bytes] = {}
        offset = 0
        try:
            for expected_id, expected_type in requests:
                header_end = raw_objects.index(b"\n", offset)
                raw_id, object_type, raw_size = raw_objects[
                    offset:header_end
                ].split(b" ")
                object_id = raw_id.decode("ascii")
                size = int(raw_size.decode("ascii"))
                start = header_end + 1
                end = start + size
                if (
                    object_id != expected_id
                    or object_type != expected_type
                    or raw_size != str(size).encode("ascii")
                    or not 0 <= size <= MAX_BLOB_BYTES
                    or end >= len(raw_objects)
                    or raw_objects[end : end + 1] != b"\n"
                ):
                    raise ImagePublicationError(
                        "exact Git object response differs"
                    )
                payload = raw_objects[start:end]
                canonical = (
                    object_type
                    + b" "
                    + str(size).encode("ascii")
                    + b"\x00"
                    + payload
                )
                if hashlib.sha1(canonical).hexdigest() != object_id:
                    raise ImagePublicationError(
                        "exact Git object identity differs"
                    )
                objects[object_id] = payload
                offset = end + 1
        except (UnicodeDecodeError, ValueError) as error:
            raise ImagePublicationError(
                "exact Git object batch response is invalid"
            ) from error
        if offset != len(raw_objects) or len(objects) != len(requests):
            raise ImagePublicationError("exact Git object batch is incomplete")
        return objects

    commit = read_objects([(source_commit, b"commit")])[source_commit]
    if not commit.startswith(
        b"tree " + source_tree.encode("ascii") + b"\n"
    ):
        raise ImagePublicationError("exact Git commit tree binding differs")

    def parse_tree(payload: bytes) -> list[tuple[bytes, str, str]]:
        parsed: list[tuple[bytes, str, str]] = []
        offset = 0
        seen_names: set[str] = set()
        try:
            while offset < len(payload):
                mode_end = payload.index(b" ", offset)
                name_end = payload.index(b"\x00", mode_end + 1)
                object_end = name_end + 21
                if object_end > len(payload):
                    raise ValueError("truncated tree object")
                mode = payload[offset:mode_end]
                raw_name = payload[mode_end + 1 : name_end]
                name = raw_name.decode("utf-8")
                object_id = payload[name_end + 1 : object_end].hex()
                if (
                    mode not in {b"40000", b"100644", b"100755"}
                    or not name
                    or "/" in name
                    or name in {".", ".."}
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in name
                    )
                    or name in seen_names
                    or _SHA_40.fullmatch(object_id) is None
                ):
                    raise ImagePublicationError(
                        "exact Git tree entry is unsafe"
                    )
                seen_names.add(name)
                parsed.append((mode, name, object_id))
                offset = object_end
        except (UnicodeDecodeError, ValueError) as error:
            raise ImagePublicationError(
                "exact Git tree object is invalid"
            ) from error
        if not parsed:
            raise ImagePublicationError("exact Git tree object is empty")
        return parsed

    tree_objects = read_objects([(source_tree, b"tree")])
    root_entries = parse_tree(tree_objects[source_tree])
    if path_prefix is None:
        pending: list[tuple[str, str]] = [(source_tree, "")]
    else:
        selected = [
            object_id
            for mode, name, object_id in root_entries
            if mode == b"40000" and name == path_prefix
        ]
        if len(selected) != 1:
            raise ImagePublicationError("exact Git path prefix differs")
        pending = [(selected[0], path_prefix)]

    entries: list[tuple[str, str, int]] = []
    seen_paths: set[str] = set()
    seen_pending: set[tuple[str, str]] = set()
    object_ids: list[str] = []
    seen_objects: set[str] = set()
    while pending:
        tree_id, prefix = pending.pop()
        if (tree_id, prefix) in seen_pending:
            raise ImagePublicationError("exact Git tree traversal repeats")
        seen_pending.add((tree_id, prefix))
        if len(seen_pending) > 250_000:
            raise ImagePublicationError("exact Git tree traversal is unbounded")
        if tree_id not in tree_objects:
            tree_objects.update(read_objects([(tree_id, b"tree")]))
        for mode, name, object_id in parse_tree(tree_objects[tree_id]):
            full_name = f"{prefix}/{name}" if prefix else name
            path = PurePosixPath(full_name)
            if (
                path.is_absolute()
                or path.as_posix() != full_name
                or any(part in {"", ".", ".."} for part in path.parts)
                or full_name in seen_paths
            ):
                raise ImagePublicationError("exact Git tree path is unsafe")
            seen_paths.add(full_name)
            if mode == b"40000":
                pending.append((object_id, full_name))
                continue
            entries.append(
                (
                    full_name,
                    object_id,
                    0o755 if mode == b"100755" else 0o644,
                )
            )
            if object_id not in seen_objects:
                seen_objects.add(object_id)
                object_ids.append(object_id)
    if not entries or len(entries) > 250_000:
        raise ImagePublicationError(
            "exact Git tree inventory is empty or unbounded"
        )
    objects = read_objects(
        [(object_id, b"blob") for object_id in object_ids]
    )

    output = io.BytesIO()
    try:
        with tarfile.open(
            fileobj=output, mode="w", format=tarfile.PAX_FORMAT
        ) as archive:
            for name, object_id, mode in sorted(entries):
                payload = objects[object_id]
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                member.mode = mode
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                archive.addfile(member, io.BytesIO(payload))
    except (OSError, tarfile.TarError) as error:
        raise ImagePublicationError("exact Git archive failed") from error
    payload = output.getvalue()
    if not 1 <= len(payload) <= MAX_BLOB_BYTES:
        raise ImagePublicationError("exact Git archive is unbounded")
    return payload


class ProductionRuntimeGitObjectExporterV2:
    """Export only the exact release and audited OpenClaw Git objects."""

    __slots__ = ("_execution", "_repositories")

    def __init__(
        self,
        *,
        execution: ReviewedLocalExecutionV1,
        release_repository: Path,
        openclaw_repository: Path,
    ) -> None:
        if type(execution) is not ReviewedLocalExecutionV1:
            raise RuntimeBuildClosureError(
                "runtime source export requires reviewed local execution"
            )
        repositories = {
            "bridge-node-modules": Path(release_repository),
            "openclaw-runtime": Path(openclaw_repository),
        }
        for repository in repositories.values():
            try:
                metadata = repository.lstat()
            except OSError as error:
                raise RuntimeBuildClosureError(
                    "runtime source repository is unavailable"
                ) from error
            if not stat.S_ISDIR(metadata.st_mode) or repository.is_symlink():
                raise RuntimeBuildClosureError(
                    "runtime source repository is not a trusted directory"
                )
        self._execution = execution
        self._repositories = repositories

    def export_runtime_source(
        self,
        *,
        component: str,
        attempt: int,
        source_commit: str,
        source_tree: str,
    ) -> bytes:
        if component not in self._repositories or attempt not in {1, 2}:
            raise RuntimeBuildClosureError(
                "runtime Git export request is invalid"
            )
        repository = self._repositories[component]
        try:
            return _exact_git_object_archive(
                execution=self._execution,
                repository=repository,
                source_commit=source_commit,
                source_tree=source_tree,
                path_prefix=(
                    "bridge" if component == "bridge-node-modules" else None
                ),
            )
        except ImagePublicationError as error:
            raise RuntimeBuildClosureError(
                "exact runtime Git object export failed"
            ) from error


class ProductionHermeticRuntimeBuilderV2:
    """Run each dependency build in the reviewed local Docker capability."""

    __slots__ = ("_execution",)

    def __init__(self, *, execution: ReviewedLocalExecutionV1) -> None:
        if type(execution) is not ReviewedLocalExecutionV1:
            raise RuntimeBuildClosureError(
                "runtime builder requires reviewed local execution"
            )
        self._execution = execution

    @staticmethod
    def _read_output(root: Path) -> dict[str, dict[str, object]]:
        if not root.is_dir() or root.is_symlink():
            raise RuntimeBuildClosureError(
                "runtime build output root is missing"
            )
        files: dict[str, dict[str, object]] = {}
        for path in sorted(
            root.rglob("*"),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise RuntimeBuildClosureError(
                    "runtime build output contains a special file"
                )
            relative = path.relative_to(root).as_posix()
            payload = path.read_bytes()
            if not payload or len(payload) > MAX_BLOB_BYTES:
                raise RuntimeBuildClosureError(
                    "runtime build output file size is invalid"
                )
            files[relative] = {
                "payload": payload,
                "mode": "0755" if metadata.st_mode & 0o111 else "0644",
            }
        if not files:
            raise RuntimeBuildClosureError("runtime build output is empty")
        return files

    def build_runtime(self, **kwargs: Any) -> Mapping[str, Any]:
        expected = {
            "component",
            "attempt",
            "source_commit",
            "source_tree",
            "source_archive",
            "source_archive_sha256",
            "package_manager_artifact",
            "package_manager_artifact_sha256",
            "package_manager_artifact_contract_sha256",
            "package_manager_distribution_sha512",
            "build_recipe",
            "build_recipe_sha256",
            "build_executor",
            "build_executor_sha256",
            "builder_image",
            "fresh_root_id",
            "fresh_root",
            "network_mode",
            "no_cache",
            "pull",
            "source_date_epoch",
        }
        if set(kwargs) != expected:
            raise RuntimeBuildClosureError(
                "runtime build isolation fields differ"
            )
        component = kwargs["component"]
        attempt = kwargs["attempt"]
        source_archive = kwargs["source_archive"]
        # The trusted provider hands the retained artifact source, never bytes;
        # hostile fixtures may still pass bytes and are coerced in memory.
        artifact_source = _coerce_artifact_source(
            kwargs["package_manager_artifact"]
        )
        recipe_payload = kwargs["build_recipe"]
        executor = kwargs["build_executor"]
        try:
            recipe = json.loads(recipe_payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeBuildClosureError(
                "runtime build recipe is invalid"
            ) from error
        if (
            component not in RUNTIME_PACKAGE_MANAGERS
            or attempt not in {1, 2}
            or not isinstance(recipe, dict)
            or _canonical_json(recipe) != recipe_payload
            or recipe.get("component") != component
            or _sha256(recipe_payload) != kwargs["build_recipe_sha256"]
            or executor != RUNTIME_BUILD_EXECUTOR
            or _sha256(executor) != kwargs["build_executor_sha256"]
            or _sha256(source_archive) != kwargs["source_archive_sha256"]
            or artifact_source.sha256
            != kwargs["package_manager_artifact_sha256"]
            or kwargs["builder_image"] != RUNTIME_BUILD_BUILDER_IMAGE
            or kwargs["fresh_root"] is not True
            or kwargs["fresh_root_id"]
            != f"{component}-fresh-{attempt}"
            or kwargs["network_mode"] != "none"
            or kwargs["no_cache"] is not True
            or kwargs["pull"] is not False
            or kwargs["source_date_epoch"] != 0
        ):
            raise RuntimeBuildClosureError(
                "runtime build isolation contract differs"
            )
        manager, version = RUNTIME_PACKAGE_MANAGERS[component]
        artifact = PackageManagerArtifact(
            manager,
            version,
            artifact_source,
            kwargs["package_manager_artifact_sha256"],
            kwargs["package_manager_distribution_sha512"],
        )
        source_files = _runtime_source_files(
            source_archive, component=component
        )
        lock_path = (
            "pnpm-lock.yaml"
            if component == "openclaw-runtime"
            else "package-lock.json"
        )
        if _offline_artifact_contract(
            component=component,
            artifact=artifact,
            lock_payload=source_files[lock_path][0],
        ) != kwargs["package_manager_artifact_contract_sha256"]:
            raise RuntimeBuildClosureError(
                "runtime build dependency cache contract differs"
            )
        with tempfile.TemporaryDirectory(
            prefix=f"personal-operator-{kwargs['fresh_root_id']}-"
        ) as temporary:
            root = Path(temporary)
            if "," in root.as_posix():
                raise RuntimeBuildClosureError(
                    "fresh build root path is unsafe"
                )
            inputs = root / "input"
            output = root / "output"
            inputs.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            (inputs / "source.tar").write_bytes(source_archive)
            # Stream the retained artifact into the fresh input root in bounded
            # chunks; the ~2.1 GiB payload is never held in memory here.
            artifact.source.stream_into(
                inputs / "offline-package-manager.tar"
            )
            (inputs / "build-recipe.json").write_bytes(recipe_payload)
            command = [
                "run",
                "--rm",
                "--network=none",
                "--pull=never",
                "--platform=linux/arm64",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--user={os.getuid()}:{os.getgid()}",
                "--tmpfs",
                f"/work:rw,nosuid,nodev,size={BUILD_TMPFS_BYTES}",
                "--mount",
                f"type=bind,src={inputs},dst=/input,readonly",
                "--mount",
                f"type=bind,src={output},dst=/output",
                "--env",
                "HOME=/work/home",
                "--env",
                "TMPDIR=/work/tmp",
                "--env",
                "SOURCE_DATE_EPOCH=0",
                "--env",
                f"PERSONAL_OPERATOR_BUILD_COMPONENT={component}",
                "--env",
                f"PERSONAL_OPERATOR_FRESH_ROOT={kwargs['fresh_root_id']}",
                RUNTIME_BUILD_BUILDER_IMAGE,
                "sh",
                "-euc",
                executor.decode("utf-8"),
            ]
            completed = self._execution.run_docker(command)
            if completed.returncode != 0:
                raise RuntimeBuildClosureError(
                    "fresh networkless runtime build failed"
                )
            return self._read_output(output / "payload")


_TRUSTED_RUNTIME_CLOSURE_TOKEN = object()


class TrustedRuntimeBuildClosureV2:
    """Non-serializable production authority over one concretely built closure."""

    __slots__ = ("_closure",)

    def __init__(
        self,
        *,
        closure: RuntimeBuildClosure,
        _token: object | None = None,
    ) -> None:
        if _token is not _TRUSTED_RUNTIME_CLOSURE_TOKEN:
            raise RuntimeBuildClosureError(
                "trusted runtime build closure is not constructible"
            )
        if not isinstance(closure, RuntimeBuildClosure):
            raise RuntimeBuildClosureError(
                "trusted runtime build closure payload is invalid"
        )
        self._closure = closure

    @property
    def manifest_sha256(self) -> str:
        return self._closure.manifest_sha256

    def development_artifacts(self) -> dict[str, bytes]:
        """Copy serialization bytes; production has no inverse loader."""

        return dict(self._closure.artifacts)


class TrustedRuntimeBuildClosureFactoryV2:
    """Sole production closure mint from concrete local sources and builders."""

    __slots__ = ("_execution", "_exporter", "_builder")

    def __init__(
        self,
        *,
        execution: ReviewedLocalExecutionV1,
        release_repository: Path,
        openclaw_repository: Path,
    ) -> None:
        if type(execution) is not ReviewedLocalExecutionV1:
            raise RuntimeBuildClosureError(
                "trusted closure factory requires reviewed local execution"
            )
        self._execution = execution
        self._exporter = ProductionRuntimeGitObjectExporterV2(
            execution=execution,
            release_repository=release_repository,
            openclaw_repository=openclaw_repository,
        )
        self._builder = ProductionHermeticRuntimeBuilderV2(
            execution=execution
        )

    def build(
        self,
        *,
        release_commit: str,
        release_tree: str,
        openclaw_commit: str,
        openclaw_tree: str,
        openclaw_package_manager_artifact: RetainedRegularFile | bytes,
        bridge_package_manager_artifact: RetainedRegularFile | bytes,
    ) -> TrustedRuntimeBuildClosureV2:
        artifacts = {
            "openclaw-runtime": reviewed_package_manager_artifact(
                component="openclaw-runtime",
                payload=openclaw_package_manager_artifact,
            ),
            "bridge-node-modules": reviewed_package_manager_artifact(
                component="bridge-node-modules",
                payload=bridge_package_manager_artifact,
            ),
        }
        provider = TrustedRuntimeBuildMaterialProvider(
            exporter=self._exporter,
            builder=self._builder,
            package_manager_artifacts=artifacts,
        )
        closure = prepare_runtime_build_closure(
            provider=provider,
            release_commit=release_commit,
            release_tree=release_tree,
            openclaw_commit=openclaw_commit,
            openclaw_tree=openclaw_tree,
        )
        return TrustedRuntimeBuildClosureV2(
            closure=closure,
            _token=_TRUSTED_RUNTIME_CLOSURE_TOKEN,
        )


class LocalGitObjectArchiveExporter:
    """Authenticate and export one exact local Git commit/tree object."""

    __slots__ = ("_repository", "_execution")

    def __init__(
        self,
        repository: Path,
        *,
        execution: ReviewedLocalExecutionV1,
    ) -> None:
        if type(execution) is not ReviewedLocalExecutionV1:
            raise ImagePublicationError(
                "release Git exporter requires reviewed local execution"
            )
        path = Path(repository)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ImagePublicationError("release Git repository is unavailable") from error
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise ImagePublicationError("release Git repository is not trusted")
        self._repository = path
        self._execution = execution

    def export_archive(
        self,
        *,
        source_commit: str,
        source_tree: str,
        path: str,
    ) -> bytes:
        if (
            _SHA_40.fullmatch(source_commit) is None
            or _SHA_40.fullmatch(source_tree) is None
            or path != "bridge"
        ):
            raise ImagePublicationError("exact Git archive request is invalid")
        return _exact_git_object_archive(
            execution=self._execution,
            repository=self._repository,
            source_commit=source_commit,
            source_tree=source_tree,
            path_prefix=path,
        )


def _resolved_rootfs_link_target(
    *, member_name: str, linkname: str, hardlink: bool
) -> str:
    if not linkname or "\x00" in linkname:
        raise ImagePublicationError(
            "local base root filesystem link is unsafe"
        )
    if hardlink and linkname.startswith("/"):
        raise ImagePublicationError(
            "local base root filesystem link is unsafe"
        )
    resolved = (
        []
        if hardlink or linkname.startswith("/")
        else list(PurePosixPath(member_name).parent.parts)
    )
    for part in linkname.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ImagePublicationError(
                    "local base root filesystem link is unsafe"
                )
            resolved.pop()
            continue
        resolved.append(part)
    return "/".join(resolved)


def _normalized_rootfs_export(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_BLOB_BYTES:
        raise ImagePublicationError("local base root filesystem is unbounded")
    retained: list[tuple[tarfile.TarInfo, bytes | None]] = []
    seen: set[str] = set()
    hardlinks: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as source:
            members = source.getmembers()
            if not members or len(members) > 250_000:
                raise ImagePublicationError(
                    "local base root filesystem inventory is invalid"
                )
            for member in members:
                name = member.name.removeprefix("./").rstrip(
                    "/" if member.isdir() else ""
                )
                path = PurePosixPath(name)
                if (
                    not name
                    or path.is_absolute()
                    or path.as_posix() != name
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or name in seen
                    or getattr(member, "sparse", None)
                ):
                    raise ImagePublicationError(
                        "local base root filesystem path is unsafe"
                    )
                seen.add(name)
                if not (
                    member.isdir()
                    or member.isreg()
                    or member.issym()
                    or member.islnk()
                ):
                    raise ImagePublicationError(
                        "local base root filesystem contains a special file"
                    )
                if member.issym() or member.islnk():
                    resolved_target = _resolved_rootfs_link_target(
                        member_name=name,
                        linkname=member.linkname,
                        hardlink=member.islnk(),
                    )
                    if member.islnk():
                        hardlinks.append(resolved_target)
                data: bytes | None = None
                if member.isreg():
                    reader = source.extractfile(member)
                    data = reader.read() if reader is not None else b""
                    if len(data) != member.size:
                        raise ImagePublicationError(
                            "local base root filesystem is truncated"
                        )
                normalized = tarfile.TarInfo(name)
                normalized.type = member.type
                normalized.linkname = member.linkname
                normalized.size = len(data) if data is not None else 0
                normalized.mode = member.mode & 0o7777
                normalized.uid = member.uid
                normalized.gid = member.gid
                normalized.uname = normalized.gname = ""
                normalized.mtime = 0
                retained.append((normalized, data))
            if any(target not in seen for target in hardlinks):
                raise ImagePublicationError(
                    "local base root filesystem hard link target is missing"
                )
    except (tarfile.TarError, OSError, UnicodeError) as error:
        raise ImagePublicationError(
            "local base root filesystem export is invalid"
        ) from error
    output = io.BytesIO()
    try:
        with tarfile.open(
            fileobj=output, mode="w", format=tarfile.PAX_FORMAT
        ) as archive:
            for member, data in sorted(retained, key=lambda item: item[0].name):
                archive.addfile(
                    member, io.BytesIO(data) if data is not None else None
                )
    except (tarfile.TarError, OSError) as error:
        raise ImagePublicationError(
            "local base root filesystem normalization failed"
        ) from error
    normalized = output.getvalue()
    if len(normalized) > MAX_BLOB_BYTES:
        raise ImagePublicationError(
            "normalized local base root filesystem is unbounded"
        )
    return normalized


def _rootfs_regular_file(payload: bytes, path: str) -> tuple[bytes, int]:
    observed: tuple[bytes, int] | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                if member.name != path:
                    continue
                if observed is not None or not member.isreg():
                    raise ImagePublicationError(
                        "retained local base file identity is ambiguous"
                    )
                reader = archive.extractfile(member)
                data = reader.read() if reader is not None else b""
                if len(data) != member.size:
                    raise ImagePublicationError(
                        "retained local base file is truncated"
                    )
                observed = (data, member.mode & 0o777)
    except (tarfile.TarError, OSError) as error:
        raise ImagePublicationError("retained local base is invalid") from error
    if observed is None or observed[1] & 0o111 == 0:
        raise ImagePublicationError("retained local base executable is missing")
    return observed


@dataclass(frozen=True, slots=True)
class _RetainedLocalBasesV1:
    python_rootfs: bytes
    python_rootfs_sha256: str
    node_binary: bytes
    node_binary_sha256: str


def _local_repo_digest(image: str) -> str:
    repository, digest = image.split("@", 1)
    repository = repository.rsplit(":", 1)[0]
    return repository + "@" + digest


def _created_container_id(payload: bytes, *, label: str) -> str:
    if (
        not isinstance(payload, bytes)
        or _CONTAINER_ID_OUTPUT.fullmatch(payload) is None
    ):
        raise ImagePublicationError(f"{label} container identity is invalid")
    return payload.rstrip(b"\n").decode("ascii")


def _single_docker_inspection(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        values = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImagePublicationError(f"{label} inspection is invalid") from error
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], dict)
    ):
        raise ImagePublicationError(f"{label} inspection is invalid")
    return values[0]


def _retain_local_base(
    execution: ReviewedLocalExecutionV1,
    *,
    component: str,
    image: str,
) -> bytes:
    inspect = execution.run_docker(["image", "inspect", image])
    inspected_image = _single_docker_inspection(
        inspect.stdout, label="retained local base image"
    )
    manifest_digest = image.rsplit("@", 1)[1]
    image_config_id = inspected_image.get("Id")
    if (
        inspect.returncode != 0
        or not isinstance(image_config_id, str)
        or not image_config_id.startswith("sha256:")
        or _SHA_64.fullmatch(image_config_id.removeprefix("sha256:")) is None
        or inspected_image.get("Os") != "linux"
        or inspected_image.get("Architecture") != "arm64"
        or inspected_image.get("RepoDigests") != [_local_repo_digest(image)]
    ):
        raise ImagePublicationError(
            "retained local base image identity differs"
        )
    container = (
        f"personal-operator-base-{component}-{manifest_digest[-12:]}-"
        + secrets.token_hex(8)
    )
    created = execution.run_docker(
        [
            "create",
            "--pull=never",
            "--platform=linux/arm64",
            "--network=none",
            "--read-only",
            "--name",
            container,
            image,
            "/bin/true",
        ]
    )
    if created.returncode != 0:
        raise ImagePublicationError(
            "exact local base image cannot be retained without pulling"
        )
    container_id = _created_container_id(
        created.stdout, label="retained local base"
    )
    container_inspection = execution.run_docker(
        ["container", "inspect", container_id]
    )
    if container_inspection.returncode != 0:
        raise ImagePublicationError(
            "retained local base container identity differs"
        )
    inspected_container = _single_docker_inspection(
        container_inspection.stdout,
        label="retained local base container",
    )
    config = inspected_container.get("Config")
    state = inspected_container.get("State")
    if (
        inspected_container.get("Id") != container_id
        or inspected_container.get("Image") != image_config_id
        or not isinstance(config, dict)
        or config.get("Image") != image
        or inspected_container.get("Name") != "/" + container
        or inspected_container.get("Platform") != "linux"
        or not isinstance(state, dict)
        or state.get("Status") != "created"
    ):
        raise ImagePublicationError(
            "retained local base container identity differs"
        )
    try:
        exported = execution.run_docker(["export", container_id])
        if exported.returncode != 0 or not isinstance(exported.stdout, bytes):
            raise ImagePublicationError(
                "exact local base root filesystem export failed"
            )
        return _normalized_rootfs_export(exported.stdout)
    finally:
        execution.run_docker(["rm", "--force", container_id])


_EXPECTED_DOCKERIGNORE_LINES = (
    "**",
    "!Dockerfile",
    "!package.json",
    "!package-lock.json",
    "!agentcore-contract.js",
    "!agentcore-proxy.js",
    "!lightweight-agent.js",
    "!runtime-policy.js",
    "!capability-catalog.js",
    "!capability-relay.js",
    "!capabilities/",
    "!capabilities/catalog-v1.json",
    "!capabilities/schemas/",
    "!capabilities/schemas/*.json",
    "!gateway-invocation.js",
    "!session-binding.js",
    "!invocation-handler.js",
    "!workspace-path-policy.js",
    "!workspace-manifest.js",
    "!sqlite-snapshot.js",
    "!workspace-sync.js",
    "!workspace-lifecycle.js",
    "!workspace-s3-client.js",
    "!cloudwatch-logger.js",
    "!scoped-credentials.js",
    "!force-ipv4.js",
    "!plugins/",
    "!plugins/personal-operator/",
    "!plugins/personal-operator/index.js",
    "!plugins/personal-operator/openclaw.plugin.json",
    "!plugins/personal-operator/package.json",
    "!entrypoint.sh",
    "!base/",
    "base/**",
    "!base/python-rootfs.tar",
    "!base/node",
    "!build-closure/",
    "build-closure/**",
    "!build-closure/runtime-build-closure.json",
    "!build-closure/openclaw-runtime.manifest.json",
    "!build-closure/openclaw-runtime.tar.gz",
    "!build-closure/bridge-node-modules.manifest.json",
    "!build-closure/bridge-node-modules.tar.gz",
)
_GENERATED_BASE_CONTEXT_PATHS = frozenset(
    {"base/python-rootfs.tar", "base/node"}
)


def _docker_context(
    build_archive: bytes,
    *,
    bases: _RetainedLocalBasesV1,
) -> bytes:
    """Project the authenticated ``bridge/`` archive to BuildKit context root."""

    files: dict[str, tuple[bytes, int]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(build_archive), mode="r:") as source:
            for member in source.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or not member.name.startswith("bridge/")
                    or member.issym()
                    or member.islnk()
                ):
                    raise ImagePublicationError("build context member is unsafe")
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ImagePublicationError("build context member is not regular")
                payload_reader = source.extractfile(member)
                payload = payload_reader.read() if payload_reader is not None else b""
                if len(payload) != member.size:
                    raise ImagePublicationError("build context member is truncated")
                projected = member.name.removeprefix("bridge/")
                if projected in _GENERATED_BASE_CONTEXT_PATHS:
                    raise ImagePublicationError(
                        "generated base context path came from Git"
                    )
                if projected in files:
                    raise ImagePublicationError(
                        "build context projection contains a duplicate path"
                    )
                files[projected] = (payload, member.mode & 0o777)
    except (tarfile.TarError, OSError) as error:
        raise ImagePublicationError("build context archive is invalid") from error
    dockerignore = files.get(".dockerignore")
    if dockerignore is None:
        raise ImagePublicationError("production Dockerignore is missing")
    try:
        dockerignore_text = dockerignore[0].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImagePublicationError("production Dockerignore is invalid") from error
    dockerignore_lines = tuple(
        line
        for raw in dockerignore_text.splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )
    if dockerignore_lines != _EXPECTED_DOCKERIGNORE_LINES:
        raise ImagePublicationError("production Dockerignore policy differs")
    files.update(
        {
            "base/python-rootfs.tar": (bases.python_rootfs, 0o644),
            "base/node": (bases.node_binary, 0o755),
        }
    )
    if not files or any(not name for name in files):
        raise ImagePublicationError("build context projection is empty")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as target:
        for name, (payload, mode) in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = mode
            member.uid = member.gid = member.mtime = 0
            target.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _oci_result_from_archive(payload: bytes) -> dict[str, object]:
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or member.issym()
                    or member.islnk()
                ):
                    raise ImagePublicationError("OCI archive member is unsafe")
                if member.isdir():
                    continue
                if not member.isreg() or member.name in files:
                    raise ImagePublicationError("OCI archive inventory is invalid")
                reader = archive.extractfile(member)
                data = reader.read() if reader is not None else b""
                if len(data) != member.size:
                    raise ImagePublicationError("OCI archive member is truncated")
                files[member.name] = data
    except (tarfile.TarError, OSError) as error:
        raise ImagePublicationError("OCI archive is invalid") from error
    layout = _strict_json(files.get("oci-layout", b""), label="OCI layout")
    index = _strict_json(files.get("index.json", b""), label="OCI index")
    if layout != {"imageLayoutVersion": "1.0.0"}:
        raise ImagePublicationError("OCI layout version differs")
    manifests = index.get("manifests")
    if index.get("schemaVersion") != 2 or not isinstance(manifests, list):
        raise ImagePublicationError("OCI index is invalid")
    candidates = [
        item
        for item in manifests
        if isinstance(item, dict)
        and item.get("mediaType") == OCI_MANIFEST_MEDIA_TYPE
        and item.get("platform", {}).get("os") == "linux"
        and item.get("platform", {}).get("architecture") == "arm64"
    ]
    if len(manifests) != 1 or len(candidates) != 1:
        raise ImagePublicationError("OCI index ARM64 subject is ambiguous")
    descriptor = candidates[0]
    digest = _require_digest(descriptor.get("digest"), label="OCI index subject")
    manifest_path = "blobs/sha256/" + digest.removeprefix("sha256:")
    manifest = files.get(manifest_path)
    if (
        not isinstance(manifest, bytes)
        or descriptor.get("size") != len(manifest)
        or _digest(manifest) != digest
    ):
        raise ImagePublicationError("OCI index subject bytes differ")
    manifest_value = _strict_json(manifest, label="OCI manifest")
    config = manifest_value.get("config")
    layers = manifest_value.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list):
        raise ImagePublicationError("OCI manifest descriptor inventory is invalid")
    raw_descriptors = [config, *layers]
    blobs: dict[str, bytes] = {}
    referenced_paths = {manifest_path}
    for raw in raw_descriptors:
        if not isinstance(raw, dict):
            raise ImagePublicationError("OCI manifest descriptor is invalid")
        blob_digest = _require_digest(raw.get("digest"), label="OCI blob")
        blob_path = "blobs/sha256/" + blob_digest.removeprefix("sha256:")
        blob = files.get(blob_path)
        if (
            not isinstance(blob, bytes)
            or raw.get("size") != len(blob)
            or _digest(blob) != blob_digest
        ):
            raise ImagePublicationError("OCI blob bytes differ")
        blobs[blob_digest] = blob
        referenced_paths.add(blob_path)
    expected_files = {"index.json", "oci-layout", *referenced_paths}
    if set(files) != expected_files:
        raise ImagePublicationError("OCI archive contains unreferenced regular files")
    return {
        "schema": "personal-operator.oci-build-result.v2",
        "platform": PLATFORM,
        "manifest": manifest,
        "blobs": blobs,
    }


def _canonical_oci_archive(
    *, manifest: bytes, blobs: Mapping[str, bytes], reference: str
) -> bytes:
    manifest_digest = _digest(manifest)
    index = _canonical_json(
        {
            "manifests": [
                {
                    "annotations": {"org.opencontainers.image.ref.name": reference},
                    "digest": manifest_digest,
                    "mediaType": OCI_MANIFEST_MEDIA_TYPE,
                    "platform": {"architecture": "arm64", "os": "linux"},
                    "size": len(manifest),
                }
            ],
            "schemaVersion": 2,
        }
    )
    files = {
        "index.json": index,
        "oci-layout": _canonical_json({"imageLayoutVersion": "1.0.0"}),
        "blobs/sha256/" + manifest_digest.removeprefix("sha256:"): manifest,
        **{
            "blobs/sha256/" + digest.removeprefix("sha256:"): payload
            for digest, payload in blobs.items()
        },
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            member.uid = member.gid = member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


@dataclass(frozen=True, slots=True)
class _BuiltImage:
    result: Mapping[str, object]
    oci_archive: bytes
    reference: str
    source_commit: str
    catalog_sha256: str


class OfflineBuildkitOciBuilder:
    """Build twice with exact offline BuildKit flags and parse retained OCI bytes."""

    __slots__ = ("_execution", "_built", "_bases")

    def __init__(self, *, execution: ReviewedLocalExecutionV1) -> None:
        if type(execution) is not ReviewedLocalExecutionV1:
            raise ImagePublicationError(
                "production OCI builder requires reviewed local execution"
            )
        self._execution = execution
        self._built: dict[str, _BuiltImage] = {}
        self._bases: _RetainedLocalBasesV1 | None = None

    def _retained_bases(self) -> _RetainedLocalBasesV1:
        if self._bases is not None:
            return self._bases
        python_rootfs = _retain_local_base(
            self._execution,
            component="python",
            image=PYTHON_RUNTIME_BASE,
        )
        node_rootfs = _retain_local_base(
            self._execution,
            component="node",
            image=NODE_RUNTIME_BASE,
        )
        node_binary, _ = _rootfs_regular_file(
            node_rootfs, "usr/local/bin/node"
        )
        self._bases = _RetainedLocalBasesV1(
            python_rootfs=python_rootfs,
            python_rootfs_sha256=_sha256(python_rootfs),
            node_binary=node_binary,
            node_binary_sha256=_sha256(node_binary),
        )
        return self._bases

    def build(self, archive: bytes, **kwargs: Any) -> Mapping[str, Any]:
        required = {
            "build_id",
            "platform",
            "source_commit",
            "source_tree",
            "catalog_source_sha256",
            "capability_catalog_digest",
            "model_callable_tools",
            "builder_dependencies",
            "build_arguments",
            "network_mode",
            "no_cache",
            "pull",
            "source_date_epoch",
        }
        if set(kwargs) != required:
            raise ImagePublicationError("production OCI build request fields differ")
        build_id = kwargs["build_id"]
        if (
            build_id not in {"fresh-1", "fresh-2"}
            or kwargs["platform"] != PLATFORM
            or kwargs["network_mode"] != "none"
            or kwargs["no_cache"] is not True
            or kwargs["pull"] is not False
            or kwargs["source_date_epoch"] != 0
            or kwargs["model_callable_tools"] != CAPABILITY_TOOL_NAMES
            or _SHA_40.fullmatch(str(kwargs["source_commit"])) is None
            or _SHA_40.fullmatch(str(kwargs["source_tree"])) is None
            or _SHA_64.fullmatch(str(kwargs["capability_catalog_digest"])) is None
        ):
            raise ImagePublicationError("production OCI build isolation differs")
        build_arguments = kwargs["build_arguments"]
        if not isinstance(build_arguments, Mapping) or not build_arguments:
            raise ImagePublicationError("production OCI build arguments are empty")
        bases = self._retained_bases()
        context = _docker_context(archive, bases=bases)
        reference = (
            "personal-operator-local:"
            + str(kwargs["source_commit"])[:12]
            + "-"
            + str(build_id)
        )
        with tempfile.TemporaryDirectory(prefix="personal-operator-oci-") as temporary:
            destination = Path(temporary) / "image.oci.tar"
            output = f"type=oci,dest={destination},rewrite-timestamp=true"
            command = [
                "build",
                "--builder",
                "default",
                "--platform=linux/arm64",
                "--network=none",
                "--pull=false",
                "--no-cache",
                "--provenance=false",
                "--sbom=false",
                "--tag",
                reference,
                "--output",
                output,
            ]
            exact_build_arguments = {
                **dict(build_arguments),
                "PYTHON_BASE_ROOTFS_SHA256": bases.python_rootfs_sha256,
                "NODE_BASE_BINARY_SHA256": bases.node_binary_sha256,
            }
            for name, value in sorted(exact_build_arguments.items()):
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(value, str)
                    or not value
                ):
                    raise ImagePublicationError(
                        "production OCI build argument is invalid"
                    )
                command.extend(["--build-arg", f"{name}={value}"])
            command.append("-")
            completed = self._execution.run_buildx(
                command,
                input=context,
            )
            if completed.returncode != 0:
                raise ImagePublicationError("offline OCI build failed")
            raw_oci = _regular_bytes(
                destination,
                maximum=MAX_BLOB_BYTES,
                label="offline OCI build output",
            )
        result = _oci_result_from_archive(raw_oci)
        closure = _validate_oci_build(result)
        config = _strict_json(
            closure.blob_mapping()[closure.config_descriptor.digest],
            label="production OCI config",
        )
        runtime = config.get("config")
        labels = runtime.get("Labels") if isinstance(runtime, dict) else None
        expected_labels = {
            "personal.operator.python-base-rootfs-sha256": (
                bases.python_rootfs_sha256
            ),
            "personal.operator.node-base-binary-sha256": (
                bases.node_binary_sha256
            ),
        }
        if labels != expected_labels:
            raise ImagePublicationError(
                "production OCI base evidence labels differ"
            )
        normalized = _canonical_oci_archive(
            manifest=closure.manifest,
            blobs=closure.blob_mapping(),
            reference=reference,
        )
        self._built[str(build_id)] = _BuiltImage(
            result=result,
            oci_archive=normalized,
            reference=reference,
            source_commit=str(kwargs["source_commit"]),
            catalog_sha256=str(kwargs["capability_catalog_digest"]),
        )
        return result

    def _exact_build(
        self,
        *,
        build_id: str,
        manifest: bytes,
        blobs: Mapping[str, bytes],
    ) -> _BuiltImage:
        built = self._built.get(build_id)
        if built is None:
            raise ImagePublicationError("probe has no exact retained OCI build")
        result = _validate_oci_build(built.result)
        if result.manifest != manifest or result.blob_mapping() != dict(blobs):
            raise ImagePublicationError("probe OCI bytes cross the retained build")
        return built


class OfflineContainerImageProbe:
    """Load and probe the exact retained OCI image with no network or credentials."""

    __slots__ = ("_execution", "_builder")

    def __init__(
        self,
        *,
        execution: ReviewedLocalExecutionV1,
        builder: OfflineBuildkitOciBuilder,
    ) -> None:
        if type(execution) is not ReviewedLocalExecutionV1:
            raise ImagePublicationError(
                "production probe requires reviewed local execution"
            )
        if type(builder) is not OfflineBuildkitOciBuilder:
            raise ImagePublicationError("production probe requires the concrete builder")
        if builder._execution is not execution:
            raise ImagePublicationError("production probe execution differs")
        self._execution = execution
        self._builder = builder

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        return self._execution.run_docker(arguments)

    def _require(self, arguments: Sequence[str], *, label: str) -> bytes:
        completed = self._run(arguments)
        if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
            raise ImagePublicationError(f"production image probe failed: {label}")
        return completed.stdout

    def run(self, *, manifest: bytes, blobs: dict[str, bytes], **kwargs: Any):
        if set(kwargs) != {
            "build_id",
            "platform",
            "network_mode",
            "credentials",
            "read_only_root",
        }:
            raise ImagePublicationError("production image probe request fields differ")
        build_id = kwargs["build_id"]
        if (
            build_id not in {"fresh-1", "fresh-2"}
            or kwargs["platform"] != PLATFORM
            or kwargs["network_mode"] != "none"
            or kwargs["credentials"] != {}
            or kwargs["read_only_root"] is not True
        ):
            raise ImagePublicationError("production image probe isolation differs")
        built = self._builder._exact_build(
            build_id=str(build_id), manifest=manifest, blobs=blobs
        )
        image_id = _validate_oci_build(
            built.result
        ).config_descriptor.digest
        container_name = (
            "personal-operator-image-probe-"
            + _sha256(manifest)[:12]
            + "-"
            + str(build_id)
            + "-"
            + secrets.token_hex(8)
        )
        with tempfile.TemporaryDirectory(prefix="personal-operator-probe-") as temporary:
            oci_path = Path(temporary) / "image.oci.tar"
            oci_path.write_bytes(built.oci_archive)
            self._require(["image", "load", "--input", str(oci_path)], label="load")
        inspected_image = _single_docker_inspection(
            self._require(
                ["image", "inspect", built.reference],
                label="image identity",
            ),
            label="production probe image",
        )
        repository_tags = inspected_image.get("RepoTags")
        if (
            inspected_image.get("Id") != image_id
            or inspected_image.get("Os") != "linux"
            or inspected_image.get("Architecture") != "arm64"
            or not isinstance(repository_tags, list)
            or built.reference not in repository_tags
        ):
            raise ImagePublicationError(
                "production probe image identity differs"
            )
        container_id: str | None = None
        try:
            started = self._require(
                [
                    "run",
                    "--detach",
                    "--pull=never",
                    "--name",
                    container_name,
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--env",
                    "AWS_REGION=eu-west-1",
                    "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
                    "--tmpfs=/run/personal-operator/home:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000,mode=0700",
                    "--tmpfs=/mnt/workspace:rw,nosuid,nodev,noexec,size=64m,uid=1000,gid=1000,mode=0700",
                    image_id,
                ],
                label="start",
            )
            candidate_container_id = _created_container_id(
                started, label="production probe"
            )
            inspected_container = _single_docker_inspection(
                self._require(
                    ["container", "inspect", candidate_container_id],
                    label="container identity",
                ),
                label="production probe container",
            )
            config = inspected_container.get("Config")
            state = inspected_container.get("State")
            if (
                inspected_container.get("Id") != candidate_container_id
                or inspected_container.get("Image") != image_id
                or not isinstance(config, dict)
                or config.get("Image") != image_id
                or inspected_container.get("Name") != "/" + container_name
                or inspected_container.get("Platform") != "linux"
                or not isinstance(state, dict)
                or state.get("Status") != "running"
            ):
                raise ImagePublicationError(
                    "production probe container identity differs"
                )
            container_id = candidate_container_id
            ping: dict[str, Any] | None = None
            for _ in range(20):
                completed = self._run(
                    [
                        "exec",
                        container_id,
                        "python3",
                        "-c",
                        (
                            "import urllib.request;print(urllib.request.urlopen("
                            "'http://127.0.0.1:8080/ping',timeout=1).read().decode())"
                        ),
                    ]
                )
                if completed.returncode == 0:
                    try:
                        value = json.loads(completed.stdout)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        value = None
                    if isinstance(value, dict):
                        ping = value
                        break
                time.sleep(0.25)
            if ping is None or ping.get("status") != "Healthy":
                raise ImagePublicationError("production image startup probe failed")
            command_gate = " ".join(FORBIDDEN_RUNTIME_COMMANDS)
            self._require(
                [
                    "exec",
                    container_id,
                    "/bin/bash",
                    "-ceu",
                    (
                        'test "$(id -u):$(id -g)" = 1000:1000; '
                        "test -s /etc/ssl/certs/ca-certificates.crt; "
                        "for path in /app /opt/openclaw /opt/personal-operator/seed /home/node; "
                        'do test ! -w "$path"; done; '
                        "! python3 -m ensurepip --version >/dev/null 2>&1; "
                        "! python3 -m pip --version >/dev/null 2>&1; "
                        f"for command in {command_gate}; do "
                        'if command -v "$command" >/dev/null 2>&1; then exit 1; fi; done'
                    ),
                ],
                label="filesystem",
            )
            release_payload = self._require(
                [
                    "exec",
                    container_id,
                    "python3",
                    "-c",
                    "print(open('/app/capabilities/release-v1.json').read())",
                ],
                label="capability release",
            )
            try:
                release = json.loads(release_payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ImagePublicationError(
                    "production capability release probe is invalid"
                ) from error
            if release != {
                "schema": "personal-operator.capability-release.v1",
                "releaseCommit": built.source_commit,
                "catalogDigest": built.catalog_sha256,
            }:
                raise ImagePublicationError(
                    "production capability release probe differs"
                )
            tools_payload = self._require(
                [
                    "exec",
                    container_id,
                    "node",
                    "-e",
                    (
                        "const loaded=require('/app/capability-catalog')"
                        ".loadRuntimeCapabilityRelease();"
                        "process.stdout.write(JSON.stringify(loaded.toolNames))"
                    ),
                ],
                label="capability catalog",
            )
            try:
                tools = json.loads(tools_payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ImagePublicationError(
                    "production capability catalog probe is invalid"
                ) from error
            if tools != list(CAPABILITY_TOOL_NAMES):
                raise ImagePublicationError(
                    "production capability catalog probe differs"
                )
            self._require(
                [
                    "exec",
                    container_id,
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path;"
                        "roots=(Path('/app'),Path('/opt/openclaw'));"
                        "needles=('playwright','puppeteer','chromium','browser-gateway','agentcore-browser');"
                        "hits=[str(p) for r in roots for p in r.rglob('*') "
                        "if any(n in str(p).casefold() for n in needles)];"
                        "assert not hits,hits"
                    ),
                ],
                label="browser absence",
            )
            environment = self._require(
                ["exec", container_id, "env"], label="credential environment"
            )
            if _CREDENTIAL_NAME.search(environment.decode("utf-8", errors="replace")):
                raise ImagePublicationError(
                    "production image probe found credential material"
                )
            self._require(
                [
                    "exec",
                    container_id,
                    "node",
                    "-e",
                    (
                        "const net=require('net');"
                        "const socket=net.connect({host:'1.1.1.1',port:443});"
                        "const timer=setTimeout(()=>process.exit(2),2000);"
                        "socket.on('connect',()=>process.exit(3));"
                        "socket.on('error',(error)=>{clearTimeout(timer);"
                        "process.exit(['ENETUNREACH','EHOSTUNREACH'].includes(error.code)?0:4)});"
                    ),
                ],
                label="network denial",
            )
        finally:
            if container_id is not None:
                self._run(["rm", "--force", container_id])
            self._run(["image", "rm", "--force", built.reference])
        return {
            "schema": "personal-operator.image-probe.v1",
            "platform": PLATFORM,
            "uid": 1000,
            "gid": 1000,
            "tlsRoots": True,
            "trustedRootsReadOnly": True,
            "startupStatus": "HEALTHY",
            "credentialsAbsent": True,
            "networkDenied": True,
            "ensurepipUnavailable": True,
            "pipModuleUnavailable": True,
            "browserArtifactsAbsent": True,
            "modelCallableTools": list(CAPABILITY_TOOL_NAMES),
            "forbiddenCommandsAbsent": list(FORBIDDEN_RUNTIME_COMMANDS),
            "releaseCommit": built.source_commit,
            "catalogSha256": built.catalog_sha256,
        }


class TrustedImageProducerV2:
    """Capability object that admits only this module's concrete production path."""

    __slots__ = ("_git", "_builder", "_probe")

    def __init__(
        self,
        *,
        git_archive: LocalGitObjectArchiveExporter,
        builder: OfflineBuildkitOciBuilder,
        probe: OfflineContainerImageProbe,
    ) -> None:
        if (
            type(git_archive) is not LocalGitObjectArchiveExporter
            or type(builder) is not OfflineBuildkitOciBuilder
            or type(probe) is not OfflineContainerImageProbe
            or probe._builder is not builder
            or probe._execution is not builder._execution
        ):
            raise ImagePublicationError(
                "production image evidence requires concrete trusted adapters"
            )
        self._git = git_archive
        self._builder = builder
        self._probe = probe

    def prepare(
        self,
        *,
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
        created: str,
        trusted_runtime_build_closure: TrustedRuntimeBuildClosureV2,
        expected_capability_catalog_digest: str | None = None,
    ) -> ImagePublicationBundle:
        if type(trusted_runtime_build_closure) is not TrustedRuntimeBuildClosureV2:
            raise RuntimeBuildClosureError(
                "production image requires an in-process trusted runtime closure"
            )
        runtime_build_closure = trusted_runtime_build_closure._closure
        bundle = prepare_image_publication(
            git_archive=self._git,
            builder=self._builder,
            probe=self._probe,
            source_commit=source_commit,
            source_tree=source_tree,
            account=account,
            region=region,
            expected_capability_catalog_digest=(
                expected_capability_catalog_digest
            ),
            created=created,
            builder_id=PRODUCTION_BUILDER_ID,
            runtime_build_closure=runtime_build_closure,
            builder_dependencies=(
                {
                    "uri": "pkg:docker/node@24.15.0-slim",
                    "digest": NODE_RUNTIME_BASE.rsplit("@", 1)[1],
                },
                {
                    "uri": "pkg:docker/python@3.13-slim",
                    "digest": PYTHON_RUNTIME_BASE.rsplit("@", 1)[1],
                },
                {
                    "uri": "urn:personal-operator:runtime-build-closure",
                    "digest": "sha256:" + runtime_build_closure.manifest_sha256,
                },
            ),
        )
        first = self._builder._built.get("fresh-1")
        second = self._builder._built.get("fresh-2")
        if first is None or second is None:
            raise ImagePublicationError("production OCI build evidence is incomplete")
        if _sha256(first.oci_archive) != _sha256(second.oci_archive):
            # References differ by build id; rebuild the canonical closure with
            # one subject-derived name before comparing exact exported bytes.
            first_closure = _validate_oci_build(first.result)
            second_closure = _validate_oci_build(second.result)
            stable_reference = (
                "personal-operator-release@"
                + first_closure.manifest_descriptor.digest
            )
            if _canonical_oci_archive(
                manifest=first_closure.manifest,
                blobs=first_closure.blob_mapping(),
                reference=stable_reference,
            ) != _canonical_oci_archive(
                manifest=second_closure.manifest,
                blobs=second_closure.blob_mapping(),
                reference=stable_reference,
            ):
                raise ImagePublicationError(
                    "production OCI exported bytes are not deterministic"
                )
        bundle.validate(expected_plan_sha256=bundle.plan_sha256)
        return bundle


__all__ = [
    "LocalGitObjectArchiveExporter",
    "OfflineBuildkitOciBuilder",
    "OfflineContainerImageProbe",
    "PRODUCTION_BUILDER_ID",
    "ReviewedLocalExecutionV1",
    "TrustedImageProducerV2",
    "TrustedRuntimeBuildClosureFactoryV2",
    "TrustedRuntimeBuildClosureV2",
    "open_reviewed_local_execution",
]
