"""Durable, descriptor-pinned request artifacts for release transaction v2.

The assembler retains exact request bytes in memory.  This module moves only
those bytes, plus the canonical release plan, into an owner-only bundle that
can be reopened by a later CLI process.  Logical request paths never become
filesystem paths: they are mapped to fixed, ordinal record names inside the
bundle.  Reopened callers receive a typed plan and a verified chunk reader,
not a source path or a writable buffer.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import threading
from types import MappingProxyType
from typing import Iterator, Mapping
import uuid

from release_tools.contracts import (
    ContractError,
    MAX_CONTRACT_BYTES,
    MAX_PRIVATE_MUTATION_ARTIFACT_BYTES,
    PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES,
    PRIVATE_MUTATION_ENVELOPE_MAGIC,
    PrivateMutationEnvelopeV2,
    ReleaseArtifactV2,
    ReleasePlanV2,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.release_plan_v2 import (
    AssembledReleasePlanV2,
    ReleasePlanAssemblyError,
)


class ReleaseArtifactStoreV2Error(RuntimeError):
    """A durable release bundle is absent, mutable, crossed, or unsafe."""


_DIRECTORY_MODE = 0o700
_WRITE_MODE = 0o600
_RECORD_MODE = 0o400
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 * 1024
_VERIFY_CHUNK_BYTES = 1024 * 1024
_MAX_CALLER_CHUNK_BYTES = 16 * 1024 * 1024
_SCHEMA = "personal-operator.release-artifact-bundle.v2"
_COMMIT_SCHEMA = "personal-operator.release-artifact-commit.v2"
_INTENT_SCHEMA = "personal-operator.release-artifact-intent.v2"
_CONSUMED_INTENT_SCHEMA = (
    "personal-operator.release-artifact-intent-consumed.v2"
)
_COMMIT_NAME = "COMMITTED"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STORAGE_NAME = re.compile(r"payload-[0-9]{8}\.bin")
_PRIVATE_MUTATION_RESERVED_FIELDS = (
    b"operationSha256",
    b"driverRequestSha256",
)
_PRIVATE_MUTATION_RESERVED_TAIL_BYTES = max(
    len(field) for field in _PRIVATE_MUTATION_RESERVED_FIELDS
) - 1
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_DIRECTORY_FLAGS = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _stability_hook(_stage: str, _logical_path: str) -> None:
    """Test-only race injection point; production deliberately does nothing."""


@dataclass(frozen=True, slots=True)
class _IdentityV2:
    device: int
    inode: int
    owner: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, details: os.stat_result) -> "_IdentityV2":
        return cls(
            details.st_dev,
            details.st_ino,
            details.st_uid,
            stat.S_IMODE(details.st_mode),
            details.st_nlink,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshotV2:
    logical_path: str
    storage_name: str
    size: int
    sha256: str
    identity: _IdentityV2
    chunk_sha256: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _IntentSnapshotV2:
    payload: bytes
    identity: _IdentityV2


def _require_directory(details: os.stat_result, *, label: str) -> _IdentityV2:
    if not stat.S_ISDIR(details.st_mode):
        raise ReleaseArtifactStoreV2Error(f"{label} is not a directory")
    identity = _IdentityV2.from_stat(details)
    if identity.owner != os.geteuid():
        raise ReleaseArtifactStoreV2Error(f"{label} owner differs")
    if identity.mode != _DIRECTORY_MODE:
        raise ReleaseArtifactStoreV2Error(f"{label} mode is not owner-only")
    return identity


def _require_record(
    details: os.stat_result,
    *,
    label: str,
    expected_size: int | None = None,
) -> _IdentityV2:
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseArtifactStoreV2Error(f"{label} is not a regular file")
    identity = _IdentityV2.from_stat(details)
    if identity.owner != os.geteuid():
        raise ReleaseArtifactStoreV2Error(f"{label} owner differs")
    if identity.mode != _RECORD_MODE:
        raise ReleaseArtifactStoreV2Error(
            f"{label} mode is not read-only owner-only"
        )
    if identity.links != 1:
        raise ReleaseArtifactStoreV2Error(f"{label} link count differs from one")
    if expected_size is not None and identity.size != expected_size:
        raise ReleaseArtifactStoreV2Error(f"{label} size differs")
    return identity


def _same_directory_identity(left: _IdentityV2, right: _IdentityV2) -> bool:
    """Compare the stable namespace identity, not mutable entry metadata."""

    return (
        left.device,
        left.inode,
        left.owner,
        left.mode,
    ) == (
        right.device,
        right.inode,
        right.owner,
        right.mode,
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(
            descriptor,
            view[offset : offset + _VERIFY_CHUNK_BYTES],
        )
        if written <= 0:
            raise ReleaseArtifactStoreV2Error(
                "release artifact write made no progress"
            )
        offset += written


def _read_exact_small(
    descriptor: int, *, expected_size: int, label: str
) -> bytes:
    if not 1 <= expected_size <= MAX_CONTRACT_BYTES:
        raise ReleaseArtifactStoreV2Error(
            f"{label} exceeds the canonical contract boundary"
        )
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise ReleaseArtifactStoreV2Error(f"{label} is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ReleaseArtifactStoreV2Error(f"{label} exceeds its bound size")
    return b"".join(chunks)


def _read_exact_block(descriptor: int, size: int) -> bytes:
    """Read one deterministic verification block despite short OS reads."""

    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _storage_name(ordinal: int) -> str:
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ReleaseArtifactStoreV2Error("artifact ordinal is invalid")
    if ordinal > 99_999_999:
        raise ReleaseArtifactStoreV2Error("artifact inventory is too large")
    return f"payload-{ordinal:08d}.bin"


def _validate_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseArtifactStoreV2Error(f"{label} is invalid")
    return value


def _scan_private_mutation_artifact(prior_tail: bytes, chunk: bytes) -> bytes:
    window = prior_tail + chunk
    if any(field in window for field in _PRIVATE_MUTATION_RESERVED_FIELDS):
        raise ReleaseArtifactStoreV2Error(
            "private mutation request artifact contains a reserved operation field"
        )
    return window[-_PRIVATE_MUTATION_RESERVED_TAIL_BYTES:]


def _require_private_mutation_record(
    details: os.stat_result,
    *,
    label: str,
    expected_size: int | None = None,
    expected_links: int,
) -> _IdentityV2:
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseArtifactStoreV2Error(f"{label} is not a regular file")
    identity = _IdentityV2.from_stat(details)
    if identity.owner != os.geteuid():
        raise ReleaseArtifactStoreV2Error(f"{label} owner differs")
    if identity.mode != _WRITE_MODE:
        raise ReleaseArtifactStoreV2Error(f"{label} mode is not owner-only")
    if identity.links != expected_links:
        raise ReleaseArtifactStoreV2Error(f"{label} link count differs")
    if expected_size is not None and identity.size != expected_size:
        raise ReleaseArtifactStoreV2Error(f"{label} size differs")
    return identity


def _same_inode(left: _IdentityV2, right: _IdentityV2) -> bool:
    return (left.device, left.inode) == (right.device, right.inode)


class _PinnedNamespaceV2:
    """Shared root/bundle identity checks without exposing either path."""

    __slots__ = (
        "_root_path",
        "_root_fd",
        "_root_identity",
        "_bundle_fd",
        "_bundle_name",
        "_bundle_identity",
    )

    def __init__(
        self,
        *,
        root_path: Path,
        root_fd: int,
        root_identity: _IdentityV2,
        bundle_fd: int,
        bundle_name: str,
        bundle_identity: _IdentityV2,
    ) -> None:
        self._root_path = root_path
        self._root_fd = root_fd
        self._root_identity = root_identity
        self._bundle_fd = bundle_fd
        self._bundle_name = bundle_name
        self._bundle_identity = bundle_identity

    def _assert_root_identity(self) -> None:
        if self._root_fd < 0:
            raise ReleaseArtifactStoreV2Error("release artifact bundle is closed")
        retained = _require_directory(
            os.fstat(self._root_fd), label="release artifact root"
        )
        try:
            current = _require_directory(
                os.stat(self._root_path, follow_symlinks=False),
                label="release artifact root",
            )
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact root was replaced"
            ) from error
        if not _same_directory_identity(retained, self._root_identity) or not (
            _same_directory_identity(current, self._root_identity)
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact root identity changed"
            )

    def _assert_bundle_identity(self) -> None:
        self._assert_root_identity()
        retained = _require_directory(
            os.fstat(self._bundle_fd), label="release artifact bundle"
        )
        try:
            current = _require_directory(
                os.stat(
                    self._bundle_name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                ),
                label="release artifact bundle",
            )
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact bundle was replaced"
            ) from error
        if not _same_directory_identity(retained, self._bundle_identity) or not (
            _same_directory_identity(current, self._bundle_identity)
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact bundle identity changed"
            )

    def _open_record(
        self,
        name: str,
        *,
        label: str,
        expected_size: int | None,
        expected_identity: _IdentityV2 | None = None,
        hook_path: str = "",
    ) -> tuple[int, _IdentityV2]:
        self._assert_bundle_identity()
        try:
            before = _require_record(
                os.stat(
                    name,
                    dir_fd=self._bundle_fd,
                    follow_symlinks=False,
                ),
                label=label,
                expected_size=expected_size,
            )
            _stability_hook("record-before-open", hook_path)
            descriptor = os.open(
                name, _READ_FLAGS, dir_fd=self._bundle_fd
            )
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(f"{label} is unavailable") from error
        try:
            opened = _require_record(
                os.fstat(descriptor),
                label=label,
                expected_size=expected_size,
            )
            after = _require_record(
                os.stat(
                    name,
                    dir_fd=self._bundle_fd,
                    follow_symlinks=False,
                ),
                label=label,
                expected_size=expected_size,
            )
            if before != opened or opened != after:
                raise ReleaseArtifactStoreV2Error(f"{label} was replaced")
            if expected_identity is not None and opened != expected_identity:
                raise ReleaseArtifactStoreV2Error(f"{label} identity changed")
            return descriptor, opened
        except BaseException:
            os.close(descriptor)
            raise

    def _assert_record_unchanged(
        self,
        descriptor: int,
        *,
        name: str,
        label: str,
        identity: _IdentityV2,
    ) -> None:
        retained = _require_record(
            os.fstat(descriptor), label=label, expected_size=identity.size
        )
        try:
            current = _require_record(
                os.stat(
                    name,
                    dir_fd=self._bundle_fd,
                    follow_symlinks=False,
                ),
                label=label,
                expected_size=identity.size,
            )
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(f"{label} was replaced") from error
        if retained != identity or current != identity:
            raise ReleaseArtifactStoreV2Error(f"{label} changed during read")
        self._assert_bundle_identity()


class _VerifiedChunkIteratorV2:
    """Linearizable one-chunk-at-a-time reader owned by one bundle."""

    __slots__ = (
        "_bundle",
        "_snapshot",
        "_chunk_size",
        "_generation",
        "_descriptor",
        "_identity",
        "_block",
        "_block_offset",
        "_total_read",
        "_chunk_index",
        "_overall",
        "_exhausted",
        "_failed",
    )

    def __init__(
        self,
        *,
        bundle: "ReleaseArtifactBundleV2",
        snapshot: _ArtifactSnapshotV2,
        chunk_size: int,
        generation: int,
    ) -> None:
        self._bundle = bundle
        self._snapshot = snapshot
        self._chunk_size = chunk_size
        self._generation = generation
        self._descriptor = -1
        self._identity: _IdentityV2 | None = None
        self._block = b""
        self._block_offset = 0
        self._total_read = 0
        self._chunk_index = 0
        self._overall = hashlib.sha256()
        self._exhausted = False
        self._failed = False
        with bundle._reader_lock:
            descriptor, identity = bundle._open_record(
                snapshot.storage_name,
                label="release request artifact",
                expected_size=snapshot.size,
                expected_identity=snapshot.identity,
                hook_path=snapshot.logical_path,
            )
            self._descriptor = descriptor
            self._identity = identity
            bundle._register_reader(descriptor, generation)

    def __iter__(self) -> "_VerifiedChunkIteratorV2":
        return self

    def _release(self) -> None:
        descriptor = self._descriptor
        self._descriptor = -1
        if descriptor >= 0:
            self._bundle._release_reader(descriptor)

    def _assert_unchanged(self) -> None:
        if self._identity is None or self._descriptor < 0:
            raise ReleaseArtifactStoreV2Error(
                "verified release artifact reader was revoked"
            )
        self._bundle._assert_record_unchanged(
            self._descriptor,
            name=self._snapshot.storage_name,
            label="release request artifact",
            identity=self._identity,
        )

    def _finish(self) -> None:
        self._bundle._assert_reader_active(
            self._descriptor, self._generation
        )
        if os.read(self._descriptor, 1):
            raise ReleaseArtifactStoreV2Error(
                "release request artifact exceeds its bound size"
            )
        if (
            self._total_read != self._snapshot.size
            or self._overall.hexdigest() != self._snapshot.sha256
            or self._chunk_index != len(self._snapshot.chunk_sha256)
        ):
            raise ReleaseArtifactStoreV2Error(
                "release request artifact digest changed"
            )
        self._assert_unchanged()
        self._release()
        self._exhausted = True

    def __next__(self) -> bytes:
        with self._bundle._reader_lock:
            if self._exhausted:
                raise StopIteration
            if self._failed:
                raise ReleaseArtifactStoreV2Error(
                    "verified release artifact reader was revoked"
                )
            try:
                self._bundle._assert_reader_active(
                    self._descriptor, self._generation
                )
                if self._block_offset >= len(self._block):
                    if self._total_read == self._snapshot.size:
                        self._finish()
                        raise StopIteration
                    expected_block_size = min(
                        _VERIFY_CHUNK_BYTES,
                        self._snapshot.size - self._total_read,
                    )
                    block = _read_exact_block(
                        self._descriptor, expected_block_size
                    )
                    if len(block) != expected_block_size:
                        raise ReleaseArtifactStoreV2Error(
                            "release request artifact is truncated"
                        )
                    if self._chunk_index >= len(
                        self._snapshot.chunk_sha256
                    ) or hashlib.sha256(block).hexdigest() != (
                        self._snapshot.chunk_sha256[self._chunk_index]
                    ):
                        raise ReleaseArtifactStoreV2Error(
                            "release request artifact content changed"
                        )
                    self._chunk_index += 1
                    self._total_read += len(block)
                    self._overall.update(block)
                    self._block = block
                    self._block_offset = 0
                    _stability_hook(
                        "chunk-before-yield", self._snapshot.logical_path
                    )
                    self._bundle._assert_reader_active(
                        self._descriptor, self._generation
                    )
                    self._assert_unchanged()
                end = min(
                    len(self._block), self._block_offset + self._chunk_size
                )
                value = bytes(self._block[self._block_offset : end])
                _stability_hook(
                    "chunk-before-visible-yield", self._snapshot.logical_path
                )
                self._bundle._assert_reader_active(
                    self._descriptor, self._generation
                )
                self._assert_unchanged()
                self._block_offset = end
                return value
            except StopIteration:
                raise
            except ReleaseArtifactStoreV2Error:
                self._failed = True
                self._release()
                raise
            except OSError as error:
                revoked = (
                    self._generation != self._bundle._reader_generation
                    or self._descriptor
                    not in self._bundle._active_readers
                )
                self._failed = True
                self._release()
                if revoked:
                    raise ReleaseArtifactStoreV2Error(
                        "verified release artifact reader was revoked"
                    ) from error
                raise ReleaseArtifactStoreV2Error(
                    "release request artifact read failed"
                ) from error
            except BaseException:
                self._failed = True
                self._release()
                raise

    def close(self) -> None:
        with self._bundle._reader_lock:
            if not self._exhausted:
                self._failed = True
            self._release()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass


class ReleaseArtifactBundleV2(_PinnedNamespaceV2):
    """One reopened exact release plan and verified request-byte capability."""

    __slots__ = (
        "_plan",
        "_plan_sha256",
        "_plan_size",
        "_artifacts",
        "_snapshots",
        "_reader_lock",
        "_reader_generation",
        "_active_readers",
        "_close_requested",
    )

    def __init__(
        self,
        *,
        root_path: Path,
        root_fd: int,
        root_identity: _IdentityV2,
        bundle_fd: int,
        bundle_name: str,
        bundle_identity: _IdentityV2,
        plan: ReleasePlanV2,
        plan_sha256: str,
        plan_size: int,
        snapshots: Mapping[str, _ArtifactSnapshotV2],
    ) -> None:
        super().__init__(
            root_path=root_path,
            root_fd=root_fd,
            root_identity=root_identity,
            bundle_fd=bundle_fd,
            bundle_name=bundle_name,
            bundle_identity=bundle_identity,
        )
        self._plan = plan
        self._plan_sha256 = plan_sha256
        self._plan_size = plan_size
        self._artifacts = tuple(plan.artifacts)
        self._snapshots = MappingProxyType(dict(snapshots))
        self._reader_lock = threading.RLock()
        self._reader_generation = 0
        self._active_readers: set[int] = set()
        self._close_requested = threading.Event()

    @property
    def plan(self) -> ReleasePlanV2:
        return self._plan

    @property
    def plan_sha256(self) -> str:
        return self._plan_sha256

    @property
    def plan_size(self) -> int:
        return self._plan_size

    @property
    def artifacts(self) -> tuple[ReleaseArtifactV2, ...]:
        return self._artifacts

    def iter_verified_chunks(
        self, logical_path: str, *, chunk_size: int = 65536
    ) -> Iterator[bytes]:
        if (
            not isinstance(chunk_size, int)
            or isinstance(chunk_size, bool)
            or not 1 <= chunk_size <= _MAX_CALLER_CHUNK_BYTES
        ):
            raise ReleaseArtifactStoreV2Error("caller chunk size is invalid")
        try:
            snapshot = self._snapshots[logical_path]
        except (KeyError, TypeError) as error:
            raise ReleaseArtifactStoreV2Error(
                "request artifact is not in the exact release plan"
            ) from error
        with self._reader_lock:
            if (
                self._close_requested.is_set()
                or self._root_fd < 0
                or self._bundle_fd < 0
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact bundle is closed"
                )
            reader_generation = self._reader_generation
            return _VerifiedChunkIteratorV2(
                bundle=self,
                snapshot=snapshot,
                chunk_size=chunk_size,
                generation=reader_generation,
            )

    def write_private_mutation_envelope(
        self,
        target: str | Path,
        *,
        resolved_request: ResolvedMutationRequestV2,
        transaction: StagingTransactionV2,
    ) -> PrivateMutationEnvelopeV2:
        """Stream the exact current pinned request into a no-clobber envelope."""

        try:
            if type(resolved_request) is not ResolvedMutationRequestV2:
                raise ContractError(
                    "resolved mutation request has the wrong concrete type"
                )
            if type(transaction) is not StagingTransactionV2:
                raise ContractError(
                    "staging transaction has the wrong concrete type"
                )
            resolved = ResolvedMutationRequestV2.from_bytes(
                resolved_request.to_bytes()
            )
            canonical_transaction = StagingTransactionV2.from_bytes(
                transaction.to_bytes(), plan=self._plan
            )
            resolved.validate_transaction(self._plan, canonical_transaction)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "private mutation authority binding is not canonical"
            ) from error
        if canonical_transaction.state != "UNCERTAIN" or (
            resolved.mutation_request.step_id
            != canonical_transaction.uncertain_step_id
            or resolved.mutation_request.operation_sha256
            != canonical_transaction.uncertain_operation_sha256
        ):
            raise ReleaseArtifactStoreV2Error(
                "private mutation authority is not the exact current intent"
            )
        header = resolved.to_bytes()
        if not 1 <= len(header) <= MAX_CONTRACT_BYTES:
            raise ReleaseArtifactStoreV2Error(
                "private mutation envelope header size is invalid"
            )
        logical_path = resolved.mutation_request.request_artifact
        try:
            snapshot = self._snapshots[logical_path]
        except (KeyError, TypeError) as error:
            raise ReleaseArtifactStoreV2Error(
                "private mutation artifact is absent from the pinned bundle"
            ) from error
        if (
            snapshot.size != resolved.request_artifact_size
            or snapshot.sha256 != resolved.mutation_request.request_sha256
            or not 1 <= snapshot.size <= MAX_PRIVATE_MUTATION_ARTIFACT_BYTES
        ):
            raise ReleaseArtifactStoreV2Error(
                "private mutation artifact differs from its canonical binding"
            )
        self._assert_envelope_bundle_active()

        try:
            target_path = Path(target)
        except (TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "private mutation envelope target is invalid"
            ) from error
        target_name = target_path.name
        if target_name in {"", ".", ".."}:
            raise ReleaseArtifactStoreV2Error(
                "private mutation envelope target is invalid"
            )
        directory_path = target_path.parent
        try:
            directory_path.mkdir(parents=True, mode=_DIRECTORY_MODE, exist_ok=True)
            before_directory = _require_directory(
                os.stat(directory_path, follow_symlinks=False),
                label="private mutation target directory",
            )
            directory_fd = os.open(directory_path, _DIRECTORY_FLAGS)
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "private mutation target directory is unavailable"
            ) from error

        writer_fd = -1
        temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
        temporary_linked = False
        success = False
        writer_identity: _IdentityV2 | None = None
        published_identity: _IdentityV2 | None = None
        pending_error: BaseException | None = None
        metadata: PrivateMutationEnvelopeV2 | None = None

        def assert_directory_pinned() -> None:
            try:
                retained = _require_directory(
                    os.fstat(directory_fd),
                    label="private mutation target directory",
                )
                current = _require_directory(
                    os.stat(directory_path, follow_symlinks=False),
                    label="private mutation target directory",
                )
            except ReleaseArtifactStoreV2Error:
                raise
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation target directory was replaced"
                ) from error
            if not (
                _same_directory_identity(before_directory, retained)
                and _same_directory_identity(retained, current)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation target directory was replaced"
                )

        def entry_identity(
            name: str, *, links: int, expected_size: int
        ) -> _IdentityV2:
            try:
                return _require_private_mutation_record(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False),
                    label="private mutation envelope",
                    expected_size=expected_size,
                    expected_links=links,
                )
            except ReleaseArtifactStoreV2Error:
                raise
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope was replaced"
                ) from error

        def unlink_if_ours(name: str) -> bool:
            if writer_identity is None:
                return False
            try:
                current = _IdentityV2.from_stat(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                )
            except FileNotFoundError:
                return False
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope cleanup is uncertain"
                ) from error
            allowed = _same_inode(current, writer_identity)
            if name == target_name and published_identity is not None:
                allowed = allowed or _same_inode(current, published_identity)
            if not allowed:
                return False
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope cleanup is uncertain"
                ) from error
            return True

        try:
            opened_directory = _require_directory(
                os.fstat(directory_fd), label="private mutation target directory"
            )
            after_directory = _require_directory(
                os.stat(directory_path, follow_symlinks=False),
                label="private mutation target directory",
            )
            if not (
                _same_directory_identity(before_directory, opened_directory)
                and _same_directory_identity(opened_directory, after_directory)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation target directory was replaced"
                )
            stale_pattern = re.compile(
                rf"\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.tmp"
            )
            if any(stale_pattern.fullmatch(name) for name in os.listdir(directory_fd)):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope has an unresolved staging artifact"
                )
            try:
                writer_fd = os.open(
                    temporary_name,
                    _CREATE_FLAGS,
                    _WRITE_MODE,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope staging could not be created"
                ) from error
            temporary_linked = True
            os.fchmod(writer_fd, _WRITE_MODE)
            writer_identity = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope staging",
                expected_size=0,
                expected_links=1,
            )
            staged_entry = entry_identity(
                temporary_name, links=1, expected_size=0
            )
            if not _same_inode(writer_identity, staged_entry):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope staging was replaced"
                )

            _write_all(writer_fd, PRIVATE_MUTATION_ENVELOPE_MAGIC)
            encoded_header_size = struct.pack(">I", len(header))
            if len(encoded_header_size) != PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope header framing is invalid"
                )
            _write_all(writer_fd, encoded_header_size)
            _write_all(writer_fd, header)
            artifact_digest = hashlib.sha256()
            reserved_tail = b""
            copied = 0
            reader = self.iter_verified_chunks(logical_path, chunk_size=65536)
            try:
                for chunk in reader:
                    copied += len(chunk)
                    if copied > snapshot.size:
                        raise ReleaseArtifactStoreV2Error(
                            "private mutation artifact exceeds its bound size"
                        )
                    reserved_tail = _scan_private_mutation_artifact(
                        reserved_tail, chunk
                    )
                    artifact_digest.update(chunk)
                    _write_all(writer_fd, chunk)
            finally:
                close_reader = getattr(reader, "close", None)
                if close_reader is not None:
                    close_reader()
            if copied != snapshot.size:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation artifact is truncated"
                )
            artifact_sha256 = artifact_digest.hexdigest()
            if artifact_sha256 != snapshot.sha256:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation artifact digest differs"
                )
            self._assert_envelope_bundle_active()
            envelope_size = (
                len(PRIVATE_MUTATION_ENVELOPE_MAGIC)
                + PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES
                + len(header)
                + copied
            )
            _stability_hook("envelope-before-file-fsync", target_name)
            os.fsync(writer_fd)
            self._assert_envelope_bundle_active()
            staged_after_write = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope staging",
                expected_size=envelope_size,
                expected_links=1,
            )
            staged_entry = entry_identity(
                temporary_name, links=1, expected_size=envelope_size
            )
            if not (
                _same_inode(staged_after_write, writer_identity)
                and _same_inode(staged_entry, writer_identity)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope staging was replaced"
                )
            assert_directory_pinned()
            _stability_hook("envelope-before-link", target_name)
            self._assert_envelope_bundle_active()
            assert_directory_pinned()
            prelink_writer = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope staging",
                expected_size=envelope_size,
                expected_links=1,
            )
            prelink_entry = entry_identity(
                temporary_name, links=1, expected_size=envelope_size
            )
            if not (
                _same_inode(prelink_writer, writer_identity)
                and _same_inode(prelink_entry, writer_identity)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope staging was replaced"
                )
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope target already exists"
                ) from error
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope no-clobber link failed"
                ) from error
            created_writer = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope staging",
                expected_size=envelope_size,
                expected_links=2,
            )
            created_source = entry_identity(
                temporary_name, links=2, expected_size=envelope_size
            )
            created_target = entry_identity(
                target_name, links=2, expected_size=envelope_size
            )
            if not (
                _same_inode(created_writer, writer_identity)
                and _same_inode(created_source, writer_identity)
                and _same_inode(created_target, writer_identity)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope no-clobber link was replaced"
                )
            published_identity = created_target
            _stability_hook("envelope-after-link", target_name)
            self._assert_envelope_bundle_active()
            assert_directory_pinned()
            linked_writer = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope staging",
                expected_size=envelope_size,
                expected_links=2,
            )
            linked_target = entry_identity(
                target_name, links=2, expected_size=envelope_size
            )
            if not (
                _same_inode(linked_writer, writer_identity)
                and _same_inode(linked_target, writer_identity)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope target was replaced"
                )
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_linked = False
            final_writer = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope",
                expected_size=envelope_size,
                expected_links=1,
            )
            final_target = entry_identity(
                target_name, links=1, expected_size=envelope_size
            )
            if not (
                _same_inode(final_writer, writer_identity)
                and _same_inode(final_target, writer_identity)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope target was replaced"
                )
            _stability_hook("envelope-before-directory-fsync", target_name)
            os.fsync(directory_fd)
            _stability_hook("envelope-after-directory-fsync", target_name)
            self._assert_envelope_bundle_active()
            assert_directory_pinned()
            final_writer = _require_private_mutation_record(
                os.fstat(writer_fd),
                label="private mutation envelope",
                expected_size=envelope_size,
                expected_links=1,
            )
            final_target = entry_identity(
                target_name, links=1, expected_size=envelope_size
            )
            if not (
                _same_inode(final_writer, writer_identity)
                and _same_inode(final_target, writer_identity)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "private mutation envelope target was replaced"
                )
            metadata = PrivateMutationEnvelopeV2(
                resolved,
                hashlib.sha256(header).hexdigest(),
                len(PRIVATE_MUTATION_ENVELOPE_MAGIC)
                + PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES
                + len(header),
                copied,
                artifact_sha256,
                envelope_size,
            )
            success = True
        except ReleaseArtifactStoreV2Error as error:
            pending_error = error
        except ContractError as error:
            pending_error = ReleaseArtifactStoreV2Error(
                "private mutation envelope binding failed"
            )
            pending_error.__cause__ = error
        except OSError as error:
            pending_error = ReleaseArtifactStoreV2Error(
                "private mutation envelope could not be durably persisted"
            )
            pending_error.__cause__ = error
        finally:
            cleanup_error: BaseException | None = None
            if not success:
                try:
                    removed = False
                    if writer_identity is not None:
                        removed = unlink_if_ours(target_name) or removed
                    if temporary_linked:
                        removed = unlink_if_ours(temporary_name) or removed
                    if removed:
                        os.fsync(directory_fd)
                except BaseException as error:
                    cleanup_error = error
            if writer_fd >= 0:
                try:
                    os.close(writer_fd)
                except OSError as error:
                    if cleanup_error is None:
                        cleanup_error = error
            try:
                os.close(directory_fd)
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = error
            if cleanup_error is not None:
                uncertain = ReleaseArtifactStoreV2Error(
                    "private mutation envelope cleanup durability is uncertain"
                )
                if pending_error is not None:
                    uncertain.__context__ = pending_error
                pending_error = uncertain

        if pending_error is not None:
            raise pending_error
        if not success or metadata is None:
            raise ReleaseArtifactStoreV2Error(
                "private mutation envelope persistence did not complete"
            )
        return metadata

    def _assert_envelope_bundle_active(self) -> None:
        with self._reader_lock:
            if (
                self._close_requested.is_set()
                or self._root_fd < 0
                or self._bundle_fd < 0
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact bundle is closed"
                )
            self._assert_bundle_identity()

    def _register_reader(self, descriptor: int, generation: int) -> None:
        with self._reader_lock:
            if (
                descriptor < 0
                or self._close_requested.is_set()
                or generation != self._reader_generation
                or self._root_fd < 0
                or self._bundle_fd < 0
            ):
                if descriptor >= 0:
                    os.close(descriptor)
                raise ReleaseArtifactStoreV2Error(
                    "verified release artifact reader was revoked"
                )
            self._active_readers.add(descriptor)

    def _assert_reader_active(self, descriptor: int, generation: int) -> None:
        with self._reader_lock:
            if (
                generation != self._reader_generation
                or self._close_requested.is_set()
                or descriptor not in self._active_readers
                or self._root_fd < 0
                or self._bundle_fd < 0
            ):
                raise ReleaseArtifactStoreV2Error(
                    "verified release artifact reader was revoked"
                )

    def _release_reader(self, descriptor: int) -> None:
        if descriptor < 0:
            return
        with self._reader_lock:
            if descriptor not in self._active_readers:
                return
            self._active_readers.remove(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass

    def close(self) -> None:
        self._close_requested.set()
        with self._reader_lock:
            self._reader_generation += 1
            readers = tuple(self._active_readers)
            self._active_readers.clear()
            for descriptor in readers:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if self._bundle_fd >= 0:
                os.close(self._bundle_fd)
                self._bundle_fd = -1
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1

    def __enter__(self) -> "ReleaseArtifactBundleV2":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass


class ReleaseArtifactStoreV2:
    """Owner-only root that atomically commits exact plan-named bundles."""

    __slots__ = (
        "_root_path",
        "_root_fd",
        "_root_identity",
        "_local_lock",
    )

    def __init__(self, root: Path, *, create: bool) -> None:
        self._root_path = Path(root)
        self._root_fd = -1
        self._local_lock = threading.RLock()
        if not self._root_path.name or self._root_path.name in {".", ".."}:
            raise ReleaseArtifactStoreV2Error(
                "release artifact root path is invalid"
            )
        try:
            if create:
                os.mkdir(self._root_path, mode=_DIRECTORY_MODE)
            self._root_fd = os.open(self._root_path, _DIRECTORY_FLAGS)
            if create:
                os.fchmod(self._root_fd, _DIRECTORY_MODE)
                os.fsync(self._root_fd)
                parent_fd = os.open(self._root_path.parent, _DIRECTORY_FLAGS)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            self._root_identity = _require_directory(
                os.fstat(self._root_fd), label="release artifact root"
            )
            current = _require_directory(
                os.stat(self._root_path, follow_symlinks=False),
                label="release artifact root",
            )
            if (current.device, current.inode) != (
                self._root_identity.device,
                self._root_identity.inode,
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact root was replaced while opening"
                )
        except ReleaseArtifactStoreV2Error:
            self.close()
            raise
        except (OSError, TypeError, ValueError) as error:
            self.close()
            action = "created" if create else "opened"
            raise ReleaseArtifactStoreV2Error(
                f"release artifact root could not be {action}"
            ) from error

    @classmethod
    def create(cls, root: str | Path) -> "ReleaseArtifactStoreV2":
        return cls(Path(root), create=True)

    @classmethod
    def open(cls, root: str | Path) -> "ReleaseArtifactStoreV2":
        return cls(Path(root), create=False)

    def _assert_root_identity(self) -> None:
        if self._root_fd < 0:
            raise ReleaseArtifactStoreV2Error("release artifact store is closed")
        retained = _require_directory(
            os.fstat(self._root_fd), label="release artifact root"
        )
        try:
            current = _require_directory(
                os.stat(self._root_path, follow_symlinks=False),
                label="release artifact root",
            )
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact root was replaced"
            ) from error
        if not _same_directory_identity(retained, self._root_identity) or not (
            _same_directory_identity(current, self._root_identity)
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact root identity changed"
            )

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        """Serialize CLI processes on the pinned root directory descriptor."""

        with self._local_lock:
            self._assert_root_identity()
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(self._root_fd, operation)
                self._assert_root_identity()
                yield
            except ReleaseArtifactStoreV2Error:
                raise
            except OSError as error:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact store lock failed"
                ) from error
            finally:
                if self._root_fd >= 0:
                    try:
                        fcntl.flock(self._root_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass

    def _create_record(
        self,
        directory_fd: int,
        *,
        name: str,
        payload: bytes,
        label: str,
    ) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                _CREATE_FLAGS,
                _WRITE_MODE,
                dir_fd=directory_fd,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fchmod(descriptor, _RECORD_MODE)
            os.fsync(descriptor)
            details = _require_record(
                os.fstat(descriptor), label=label, expected_size=len(payload)
            )
            if details.links != 1:
                raise ReleaseArtifactStoreV2Error(
                    f"{label} link count differs from one"
                )
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                f"{label} could not be persisted"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_root_record_snapshot(
        self, name: str, *, label: str
    ) -> _IntentSnapshotV2:
        descriptor = -1
        try:
            before = _require_record(
                os.stat(
                    name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                ),
                label=label,
            )
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self._root_fd)
            opened = _require_record(os.fstat(descriptor), label=label)
            after = _require_record(
                os.stat(
                    name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                ),
                label=label,
            )
            if before != opened or opened != after:
                raise ReleaseArtifactStoreV2Error(f"{label} was replaced")
            if not 1 <= opened.size <= MAX_CONTRACT_BYTES:
                raise ReleaseArtifactStoreV2Error(
                    f"{label} exceeds the canonical boundary"
                )
            payload = _read_exact_small(
                descriptor, expected_size=opened.size, label=label
            )
            retained = _require_record(os.fstat(descriptor), label=label)
            current = _require_record(
                os.stat(
                    name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                ),
                label=label,
            )
            if retained != opened or current != opened:
                raise ReleaseArtifactStoreV2Error(f"{label} changed during read")
            return _IntentSnapshotV2(payload, opened)
        except FileNotFoundError:
            raise
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(f"{label} is unsafe") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_root_record(self, name: str, *, label: str) -> bytes:
        return self._read_root_record_snapshot(name, label=label).payload

    @staticmethod
    def _intent_name(plan_sha256: str) -> str:
        return f"INPROGRESS-{plan_sha256}.json"

    @staticmethod
    def _consumed_intent_name(plan_sha256: str) -> str:
        return f"CONSUMED-{plan_sha256}.json"

    @staticmethod
    def _intent_temp_name(intent_name: str) -> str:
        return f".{intent_name}.tmp"

    @staticmethod
    def _intent_bytes(
        *,
        plan_sha256: str,
        plan_size: int,
        inventory_bytes: bytes,
        artifact_count: int,
    ) -> bytes:
        return canonical_json_bytes(
            {
                "schema": _INTENT_SCHEMA,
                "planSha256": plan_sha256,
                "planSize": plan_size,
                "inventorySha256": hashlib.sha256(
                    inventory_bytes
                ).hexdigest(),
                "inventorySize": len(inventory_bytes),
                "artifactCount": artifact_count,
            }
        )

    @staticmethod
    def _consumed_intent_bytes(
        *,
        plan_sha256: str,
        intent: _IntentSnapshotV2,
    ) -> bytes:
        identity = intent.identity
        return canonical_json_bytes(
            {
                "schema": _CONSUMED_INTENT_SCHEMA,
                "planSha256": plan_sha256,
                "intentSha256": hashlib.sha256(intent.payload).hexdigest(),
                "intentDevice": identity.device,
                "intentInode": identity.inode,
                "intentOwner": identity.owner,
                "intentMode": identity.mode,
                "intentLinks": identity.links,
                "intentSize": identity.size,
                "intentModifiedNs": identity.modified_ns,
                "intentChangedNs": identity.changed_ns,
            }
        )

    def _intent_temp_names(self, intent_name: str) -> tuple[str, ...]:
        prefix = self._intent_temp_name(intent_name)
        matches: list[str] = []
        try:
            with os.scandir(self._root_fd) as entries:
                for entry in entries:
                    if not entry.name.startswith(prefix):
                        continue
                    matches.append(entry.name)
                    if len(matches) > 1 or entry.name != prefix:
                        raise ReleaseArtifactStoreV2Error(
                            "release artifact intent temp fanout is unsafe"
                        )
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent temp namespace is unavailable"
            ) from error
        return tuple(matches)

    def _read_intent_temp(
        self, name: str, *, maximum_size: int
    ) -> tuple[bytes, _IdentityV2]:
        descriptor = -1
        try:
            before_details = os.stat(
                name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(before_details.st_mode):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact intent temp is not regular"
                )
            before = _IdentityV2.from_stat(before_details)
            if (
                before.owner != os.geteuid()
                or before.mode not in {_WRITE_MODE, _RECORD_MODE}
                or before.links != 1
                or not 0 <= before.size <= maximum_size
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact intent temp shape is unsafe"
                )
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self._root_fd)
            opened = _IdentityV2.from_stat(os.fstat(descriptor))
            after = _IdentityV2.from_stat(
                os.stat(
                    name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
            )
            if before != opened or opened != after:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact intent temp was replaced"
                )
            chunks: list[bytes] = []
            remaining = opened.size
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent temp is truncated"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact intent temp exceeds its size"
                )
            retained = _IdentityV2.from_stat(os.fstat(descriptor))
            current = _IdentityV2.from_stat(
                os.stat(
                    name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
            )
            if retained != opened or current != opened:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact intent temp changed during read"
                )
            return b"".join(chunks), opened
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent temp is unsafe"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_linked_intent(
        self, name: str, *, expected_size: int
    ) -> tuple[bytes, _IdentityV2]:
        descriptor = -1
        try:
            details = os.stat(
                name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(details.st_mode):
                raise ReleaseArtifactStoreV2Error(
                    "linked release artifact intent is not regular"
                )
            identity = _IdentityV2.from_stat(details)
            if (
                identity.owner != os.geteuid()
                or identity.mode != _RECORD_MODE
                or identity.links != 2
                or identity.size != expected_size
            ):
                raise ReleaseArtifactStoreV2Error(
                    "linked release artifact intent shape is unsafe"
                )
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self._root_fd)
            if _IdentityV2.from_stat(os.fstat(descriptor)) != identity:
                raise ReleaseArtifactStoreV2Error(
                    "linked release artifact intent was replaced"
                )
            payload = _read_exact_small(
                descriptor,
                expected_size=expected_size,
                label="linked release artifact intent",
            )
            if _IdentityV2.from_stat(os.fstat(descriptor)) != identity:
                raise ReleaseArtifactStoreV2Error(
                    "linked release artifact intent changed during read"
                )
            return payload, identity
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "linked release artifact intent is unsafe"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _recover_intent_publication(
        self, intent_name: str, expected: bytes
    ) -> _IntentSnapshotV2 | None:
        temporary_name = self._intent_temp_name(intent_name)
        temp_names = self._intent_temp_names(intent_name)
        temp_exists = bool(temp_names)
        try:
            final_details = os.stat(
                intent_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            final_details = None
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent namespace is unsafe"
            ) from error

        if final_details is None:
            if temp_exists:
                payload, identity = self._read_intent_temp(
                    temporary_name, maximum_size=len(expected)
                )
                if not expected.startswith(payload) or (
                    identity.mode == _RECORD_MODE and payload != expected
                ):
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent temp is substituted"
                    )
                os.unlink(temporary_name, dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            return None

        if not stat.S_ISREG(final_details.st_mode):
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent final is not regular"
            )
        final_identity = _IdentityV2.from_stat(final_details)
        if final_identity.links == 1:
            retained = self._read_root_record_snapshot(
                intent_name, label="release artifact intent"
            )
            if temp_exists:
                payload, _identity = self._read_intent_temp(
                    temporary_name, maximum_size=len(expected)
                )
                if not expected.startswith(payload):
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent temp is substituted"
                    )
                os.unlink(temporary_name, dir_fd=self._root_fd)
                os.fsync(self._root_fd)
                retained = self._read_root_record_snapshot(
                    intent_name, label="release artifact intent"
                )
            return retained
        if final_identity.links != 2 or not temp_exists:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent final link shape is unsafe"
            )
        final_payload, linked_identity = self._read_linked_intent(
            intent_name, expected_size=len(expected)
        )
        temp_payload, temp_identity = self._read_linked_intent(
            temporary_name, expected_size=len(expected)
        )
        if (
            linked_identity != temp_identity
            or final_payload != expected
            or temp_payload != expected
        ):
            raise ReleaseArtifactStoreV2Error(
                "linked release artifact intent is substituted"
            )
        os.unlink(temporary_name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        retained = self._read_root_record_snapshot(
            intent_name, label="release artifact intent"
        )
        return retained

    def _publish_intent(self, intent_name: str, payload: bytes) -> None:
        temporary_name = self._intent_temp_name(intent_name)
        if self._intent_temp_names(intent_name):
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent temp already exists"
            )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary_name,
                _CREATE_FLAGS,
                _WRITE_MODE,
                dir_fd=self._root_fd,
            )
            split = max(1, len(payload) // 2)
            _write_all(descriptor, payload[:split])
            _stability_hook("persist-intent-partial-write", intent_name)
            _write_all(descriptor, payload[split:])
            os.fsync(descriptor)
            os.fchmod(descriptor, _RECORD_MODE)
            os.fsync(descriptor)
            _require_record(
                os.fstat(descriptor),
                label="release artifact intent temp",
                expected_size=len(payload),
            )
            os.close(descriptor)
            descriptor = -1
            os.link(
                temporary_name,
                intent_name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            os.fsync(self._root_fd)
            os.unlink(temporary_name, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
            if self._read_root_record(
                intent_name, label="release artifact intent"
            ) != payload:
                raise ReleaseArtifactStoreV2Error(
                    "published release artifact intent is not exact"
                )
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent could not be atomically published"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _parse_consumed_intent(
        payload: bytes,
        *,
        expected_plan_sha256: str,
    ) -> tuple[str, _IdentityV2]:
        try:
            value = parse_canonical_object(payload)
        except (ContractError, RecursionError, TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent is not canonical"
            ) from error
        fields = {
            "schema",
            "planSha256",
            "intentSha256",
            "intentDevice",
            "intentInode",
            "intentOwner",
            "intentMode",
            "intentLinks",
            "intentSize",
            "intentModifiedNs",
            "intentChangedNs",
        }
        if set(value) != fields or value.get("schema") != _CONSUMED_INTENT_SCHEMA:
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent fields are not exact"
            )
        if value.get("planSha256") != expected_plan_sha256:
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent crosses its plan"
            )
        digest = _validate_digest(
            value.get("intentSha256"),
            label="consumed intent digest",
        )
        raw_identity = tuple(
            value.get(field)
            for field in (
                "intentDevice",
                "intentInode",
                "intentOwner",
                "intentMode",
                "intentLinks",
                "intentSize",
                "intentModifiedNs",
                "intentChangedNs",
            )
        )
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in raw_identity
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent identity is invalid"
            )
        identity = _IdentityV2(*raw_identity)
        if (
            identity.owner != os.geteuid()
            or identity.mode != _RECORD_MODE
            or identity.links != 1
            or not 1 <= identity.size <= MAX_CONTRACT_BYTES
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent identity is unsafe"
            )
        return digest, identity

    def _intent_consumption_state(
        self,
        plan_sha256: str,
        *,
        expected_payload: bytes | None = None,
        expected_identity: _IdentityV2 | None = None,
    ) -> tuple[_IntentSnapshotV2 | None, bool]:
        intent_name = self._intent_name(plan_sha256)
        consumed_name = self._consumed_intent_name(plan_sha256)
        if self._intent_temp_names(intent_name):
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent publication is incomplete"
            )
        if self._intent_temp_names(consumed_name):
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent consumption publication is incomplete"
            )
        try:
            intent = self._read_root_record_snapshot(
                intent_name,
                label="release artifact intent",
            )
        except FileNotFoundError:
            intent = None
        try:
            consumed = self._read_root_record_snapshot(
                consumed_name,
                label="release artifact consumed intent",
            )
        except FileNotFoundError:
            consumed = None
        if expected_payload is not None and (
            intent is None or intent.payload != expected_payload
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent differs from its exact attempt"
            )
        if expected_identity is not None and (
            intent is None or intent.identity != expected_identity
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent identity differs from its accepted entry"
            )
        if consumed is None:
            return intent, False
        if intent is None:
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent has no retained intent"
            )
        consumed_digest, consumed_identity = self._parse_consumed_intent(
            consumed.payload,
            expected_plan_sha256=plan_sha256,
        )
        if (
            consumed_digest != hashlib.sha256(intent.payload).hexdigest()
            or consumed_identity != intent.identity
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact consumed intent pair is crossed"
            )
        return intent, True

    def _intent_exists(self, plan_sha256: str) -> bool:
        intent, consumed = self._intent_consumption_state(plan_sha256)
        return intent is not None and not consumed

    def _consume_intent_durably(
        self,
        *,
        plan_sha256: str,
        expected: _IntentSnapshotV2,
    ) -> None:
        current, consumed = self._intent_consumption_state(
            plan_sha256,
            expected_payload=expected.payload,
            expected_identity=expected.identity,
        )
        if current is None:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent consumption has no exact intent"
            )
        if consumed:
            return
        consumed_name = self._consumed_intent_name(plan_sha256)
        consumed_bytes = self._consumed_intent_bytes(
            plan_sha256=plan_sha256,
            intent=expected,
        )
        _stability_hook(
            "persist-before-intent-consumption-record",
            self._intent_name(plan_sha256),
        )
        self._create_record(
            self._root_fd,
            name=consumed_name,
            payload=consumed_bytes,
            label="release artifact consumed intent",
        )
        _stability_hook(
            "persist-before-intent-removal-root-fsync",
            self._intent_name(plan_sha256),
        )
        try:
            os.fsync(self._root_fd)
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent consumption was not durable"
            ) from error
        retained, consumed = self._intent_consumption_state(
            plan_sha256,
            expected_payload=expected.payload,
            expected_identity=expected.identity,
        )
        if retained is None or not consumed:
            raise ReleaseArtifactStoreV2Error(
                "release artifact intent consumption is incomplete"
            )

    @staticmethod
    def _canonical_input(
        assembled: AssembledReleasePlanV2,
    ) -> tuple[ReleasePlanV2, bytes, tuple[tuple[str, bytes], ...]]:
        if not isinstance(assembled, AssembledReleasePlanV2):
            raise ReleaseArtifactStoreV2Error(
                "release artifact store input is not an assembled plan"
            )
        try:
            plan_bytes = assembled.plan.to_bytes()
            plan = ReleasePlanV2.from_bytes(plan_bytes)
        except (ContractError, ReleasePlanAssemblyError, TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "assembled release plan is not canonical"
            ) from error
        if plan != assembled.plan:
            raise ReleaseArtifactStoreV2Error(
                "assembled release plan changes under canonical parsing"
            )
        if not isinstance(assembled.payloads, tuple):
            raise ReleaseArtifactStoreV2Error(
                "assembled request payload inventory is not immutable"
            )
        payloads: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for item in assembled.payloads:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], bytes)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "assembled request payload inventory is not typed"
                )
            path, payload = item
            if path in seen:
                raise ReleaseArtifactStoreV2Error(
                    "assembled request payload path is duplicated"
                )
            seen.add(path)
            payloads.append((path, payload))
        if [path for path, _ in payloads] != sorted(seen):
            raise ReleaseArtifactStoreV2Error(
                "assembled request payload inventory is not sorted"
            )
        expected = {artifact.path: artifact for artifact in plan.artifacts}
        if seen != set(expected):
            raise ReleaseArtifactStoreV2Error(
                "assembled request payload inventory is not exact"
            )
        for path, payload in payloads:
            artifact = expected[path]
            if not 1 <= len(payload) <= _MAX_PAYLOAD_BYTES:
                raise ReleaseArtifactStoreV2Error(
                    "assembled request payload size exceeds its boundary"
                )
            if len(payload) != artifact.size or (
                hashlib.sha256(payload).hexdigest() != artifact.sha256
            ):
                raise ReleaseArtifactStoreV2Error(
                    "assembled request payload differs from its plan binding"
                )
        return plan, plan_bytes, tuple(payloads)

    @staticmethod
    def _inventory_bytes(
        *,
        plan: ReleasePlanV2,
        plan_sha256: str,
        plan_size: int,
        payloads: tuple[tuple[str, bytes], ...],
    ) -> bytes:
        inventory_artifacts: list[dict[str, object]] = []
        for ordinal, ((path, _payload), artifact) in enumerate(
            zip(payloads, plan.artifacts, strict=True)
        ):
            if path != artifact.path:
                raise ReleaseArtifactStoreV2Error(
                    "assembled request payload order crosses its plan"
                )
            inventory_artifacts.append(
                {
                    "path": artifact.path,
                    "size": artifact.size,
                    "sha256": artifact.sha256,
                    "storage": _storage_name(ordinal),
                }
            )
        payload = canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "planSha256": plan_sha256,
                "planSize": plan_size,
                "artifacts": inventory_artifacts,
            }
        )
        if len(payload) > MAX_CONTRACT_BYTES:
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory exceeds the canonical boundary"
            )
        return payload

    def _remove_incomplete_directory(
        self, directory_name: str, *, maximum_records: int
    ) -> None:
        """Remove one bounded, uncommitted, flat attempt under LOCK_EX."""

        staging_fd = -1
        try:
            staging_fd = os.open(
                directory_name, _DIRECTORY_FLAGS, dir_fd=self._root_fd
            )
        except FileNotFoundError:
            return
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "incomplete release staging directory is unsafe"
            ) from error
        try:
            _require_directory(
                os.fstat(staging_fd), label="release artifact staging directory"
            )
            names = self._bounded_directory_names(
                staging_fd, maximum=maximum_records
            )
            for name in names:
                try:
                    details = os.stat(
                        name,
                        dir_fd=staging_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(details.st_mode):
                        raise ReleaseArtifactStoreV2Error(
                            "incomplete release staging attempt is not flat"
                        )
                    os.unlink(name, dir_fd=staging_fd)
                except ReleaseArtifactStoreV2Error:
                    raise
                except OSError as error:
                    raise ReleaseArtifactStoreV2Error(
                        "incomplete release staging record cannot be removed"
                    ) from error
            os.fsync(staging_fd)
        finally:
            if staging_fd >= 0:
                os.close(staging_fd)
        try:
            os.rmdir(directory_name, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "incomplete release staging directory cannot be removed"
            ) from error

    def _publish_commit_marker(
        self,
        directory_fd: int,
        *,
        plan_sha256: str,
        plan_size: int,
        inventory_bytes: bytes,
    ) -> None:
        marker = canonical_json_bytes(
            {
                "schema": _COMMIT_SCHEMA,
                "planSha256": plan_sha256,
                "planSize": plan_size,
                "inventorySha256": hashlib.sha256(
                    inventory_bytes
                ).hexdigest(),
                "inventorySize": len(inventory_bytes),
            }
        )
        try:
            self._create_record(
                directory_fd,
                name=_COMMIT_NAME,
                payload=marker,
                label="release artifact commit marker",
            )
            _stability_hook(
                "persist-before-commit-directory-fsync", plan_sha256
            )
            os.fsync(directory_fd)
        except BaseException:
            try:
                os.unlink(_COMMIT_NAME, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
            raise

    def persist(self, assembled: AssembledReleasePlanV2) -> ReleaseArtifactBundleV2:
        plan, plan_bytes, payloads = self._canonical_input(assembled)
        plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
        inventory_bytes = self._inventory_bytes(
            plan=plan,
            plan_sha256=plan_sha256,
            plan_size=len(plan_bytes),
            payloads=payloads,
        )
        intent_name = self._intent_name(plan_sha256)
        intent_bytes = self._intent_bytes(
            plan_sha256=plan_sha256,
            plan_size=len(plan_bytes),
            inventory_bytes=inventory_bytes,
            artifact_count=len(payloads),
        )
        staging_name = f".staging-{plan_sha256}"
        with self._locked(exclusive=True):
            retained_intent = self._recover_intent_publication(
                intent_name, intent_bytes
            )
            if retained_intent is not None:
                if retained_intent.payload != intent_bytes:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent crosses the exact plan attempt"
                    )
                os.fsync(self._root_fd)
                durable_intent = self._read_root_record_snapshot(
                    intent_name, label="release artifact intent"
                )
                if (
                    durable_intent.payload != intent_bytes
                    or durable_intent.identity != retained_intent.identity
                ):
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent is not durably exact"
                    )
                accepted_intent = durable_intent
                current_intent, consumed = self._intent_consumption_state(
                    plan_sha256,
                    expected_payload=intent_bytes,
                    expected_identity=accepted_intent.identity,
                )
                if current_intent is None:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent disappeared during recovery"
                    )
                final_fd = -1
                try:
                    final_fd = os.open(
                        plan_sha256,
                        _DIRECTORY_FLAGS,
                        dir_fd=self._root_fd,
                    )
                except FileNotFoundError:
                    final_exists = False
                    final_has_commit = False
                except OSError as error:
                    raise ReleaseArtifactStoreV2Error(
                        "committed release artifact bundle is unsafe"
                    ) from error
                else:
                    final_exists = True
                    final_identity = _require_directory(
                        os.fstat(final_fd),
                        label="release artifact bundle",
                    )
                    current_final = _require_directory(
                        os.stat(
                            plan_sha256,
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        ),
                        label="release artifact bundle",
                    )
                    if not _same_directory_identity(
                        final_identity, current_final
                    ):
                        raise ReleaseArtifactStoreV2Error(
                            "release artifact bundle was replaced during recovery"
                        )
                    try:
                        os.stat(
                            _COMMIT_NAME,
                            dir_fd=final_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        final_has_commit = False
                    except OSError as error:
                        raise ReleaseArtifactStoreV2Error(
                            "release artifact commit namespace is unsafe"
                        ) from error
                    else:
                        final_has_commit = True
                finally:
                    if final_fd >= 0:
                        os.close(final_fd)
                if consumed:
                    if not final_exists:
                        raise ReleaseArtifactStoreV2Error(
                            "consumed release intent has no committed bundle"
                        )
                    return self._reopen_locked(plan_sha256)
                if final_exists and final_has_commit:
                    with self._reopen_locked(
                        plan_sha256,
                        allow_in_progress_intent=True,
                    ):
                        pass
                    current_intent, consumed = self._intent_consumption_state(
                        plan_sha256,
                        expected_payload=intent_bytes,
                        expected_identity=accepted_intent.identity,
                    )
                    if current_intent is None or consumed:
                        raise ReleaseArtifactStoreV2Error(
                            "release artifact intent changed during recovery"
                        )
                    self._consume_intent_durably(
                        plan_sha256=plan_sha256,
                        expected=accepted_intent,
                    )
                    return self._reopen_locked(plan_sha256)
                self._remove_incomplete_directory(
                    plan_sha256, maximum_records=len(payloads) + 4
                )
                self._remove_incomplete_directory(
                    staging_name, maximum_records=len(payloads) + 2
                )
            else:
                orphan_intent, orphan_consumed = self._intent_consumption_state(
                    plan_sha256
                )
                if orphan_intent is not None or orphan_consumed:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent state is crossed"
                    )
                try:
                    os.stat(
                        plan_sha256,
                        dir_fd=self._root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    final_exists = False
                except OSError as error:
                    raise ReleaseArtifactStoreV2Error(
                        "committed release artifact bundle is unsafe"
                    ) from error
                else:
                    final_exists = True
                if final_exists:
                    return self._reopen_locked(plan_sha256)
                try:
                    os.stat(
                        staging_name,
                        dir_fd=self._root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact staging namespace is unsafe"
                    ) from error
                else:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact staging exists without a durable intent"
                    )
                self._publish_intent(intent_name, intent_bytes)
                _stability_hook(
                    "persist-before-intent-root-fsync", plan_sha256
                )
                os.fsync(self._root_fd)
                accepted_intent = self._read_root_record_snapshot(
                    intent_name, label="release artifact intent"
                )
                if accepted_intent.payload != intent_bytes:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact intent is not durably exact"
                    )

            staging_fd = -1
            try:
                os.mkdir(
                    staging_name,
                    mode=_DIRECTORY_MODE,
                    dir_fd=self._root_fd,
                )
                os.fsync(self._root_fd)
                staging_fd = os.open(
                    staging_name, _DIRECTORY_FLAGS, dir_fd=self._root_fd
                )
                os.fchmod(staging_fd, _DIRECTORY_MODE)
                staging_identity = _require_directory(
                    os.fstat(staging_fd),
                    label="release artifact staging directory",
                )
                self._create_record(
                    staging_fd,
                    name="plan.json",
                    payload=plan_bytes,
                    label="release plan record",
                )
                _stability_hook("persist-after-plan", plan_sha256)
                for ordinal, (_path, payload) in enumerate(payloads):
                    self._create_record(
                        staging_fd,
                        name=_storage_name(ordinal),
                        payload=payload,
                        label="release request artifact",
                    )
                self._create_record(
                    staging_fd,
                    name="inventory.json",
                    payload=inventory_bytes,
                    label="release artifact inventory",
                )
                os.fsync(staging_fd)
                current_staging = _require_directory(
                    os.stat(
                        staging_name,
                        dir_fd=self._root_fd,
                        follow_symlinks=False,
                    ),
                    label="release artifact staging directory",
                )
                if not _same_directory_identity(
                    current_staging, staging_identity
                ):
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact staging directory was replaced"
                    )
                os.rename(
                    staging_name,
                    plan_sha256,
                    src_dir_fd=self._root_fd,
                    dst_dir_fd=self._root_fd,
                )
                _stability_hook("persist-before-final-fsync", plan_sha256)
                os.fsync(self._root_fd)
                os.fsync(staging_fd)
                self._publish_commit_marker(
                    staging_fd,
                    plan_sha256=plan_sha256,
                    plan_size=len(plan_bytes),
                    inventory_bytes=inventory_bytes,
                )
                committed_identity = _require_directory(
                    os.stat(
                        plan_sha256,
                        dir_fd=self._root_fd,
                        follow_symlinks=False,
                    ),
                    label="release artifact bundle",
                )
                if not _same_directory_identity(
                    committed_identity, staging_identity
                ):
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact bundle changed during publication"
                    )
                self._consume_intent_durably(
                    plan_sha256=plan_sha256,
                    expected=accepted_intent,
                )
            except ReleaseArtifactStoreV2Error:
                raise
            except (ContractError, OSError, TypeError, ValueError) as error:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact bundle could not be persisted"
                ) from error
            finally:
                if staging_fd >= 0:
                    os.close(staging_fd)
            return self._reopen_locked(plan_sha256)

    @staticmethod
    def _parse_commit_marker(
        payload: bytes, *, expected_plan_sha256: str
    ) -> tuple[int, int, str]:
        try:
            value = parse_canonical_object(payload)
        except (ContractError, RecursionError, TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact commit marker is not canonical"
            ) from error
        if set(value) != {
            "schema",
            "planSha256",
            "planSize",
            "inventorySha256",
            "inventorySize",
        } or value.get("schema") != _COMMIT_SCHEMA:
            raise ReleaseArtifactStoreV2Error(
                "release artifact commit marker fields are not exact"
            )
        if value.get("planSha256") != expected_plan_sha256:
            raise ReleaseArtifactStoreV2Error(
                "release artifact commit marker crosses its plan digest"
            )
        plan_size = value.get("planSize")
        inventory_size = value.get("inventorySize")
        if (
            not isinstance(plan_size, int)
            or isinstance(plan_size, bool)
            or not 1 <= plan_size <= MAX_CONTRACT_BYTES
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact commit plan size is invalid"
            )
        if (
            not isinstance(inventory_size, int)
            or isinstance(inventory_size, bool)
            or not 1 <= inventory_size <= MAX_CONTRACT_BYTES
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact commit inventory size is invalid"
            )
        inventory_sha256 = _validate_digest(
            value.get("inventorySha256"), label="commit inventory digest"
        )
        return plan_size, inventory_size, inventory_sha256

    @staticmethod
    def _parse_inventory(
        payload: bytes, *, expected_plan_sha256: str
    ) -> tuple[int, tuple[tuple[str, int, str, str], ...]]:
        try:
            value = parse_canonical_object(payload)
        except (ContractError, RecursionError, TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory is not canonical"
            ) from error
        if set(value) != {"schema", "planSha256", "planSize", "artifacts"}:
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory fields are not exact"
            )
        if value.get("schema") != _SCHEMA:
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory schema is invalid"
            )
        if value.get("planSha256") != expected_plan_sha256:
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory crosses its plan digest"
            )
        plan_size = value.get("planSize")
        if (
            not isinstance(plan_size, int)
            or isinstance(plan_size, bool)
            or not 1 <= plan_size <= MAX_CONTRACT_BYTES
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory plan size is invalid"
            )
        raw_artifacts = value.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory is empty"
            )
        artifacts: list[tuple[str, int, str, str]] = []
        seen_paths: set[str] = set()
        seen_storage: set[str] = set()
        for ordinal, raw in enumerate(raw_artifacts):
            if not isinstance(raw, dict) or set(raw) != {
                "path",
                "size",
                "sha256",
                "storage",
            }:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact inventory entry is invalid"
                )
            path = raw.get("path")
            size = raw.get("size")
            digest = raw.get("sha256")
            storage = raw.get("storage")
            if not isinstance(path, str) or not path or "\x00" in path:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact inventory path is invalid"
                )
            if path in seen_paths:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact inventory path is duplicated"
                )
            seen_paths.add(path)
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or not 1 <= size <= _MAX_PAYLOAD_BYTES
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact inventory payload size is invalid"
                )
            digest = _validate_digest(digest, label="artifact digest")
            expected_storage = _storage_name(ordinal)
            if (
                not isinstance(storage, str)
                or _STORAGE_NAME.fullmatch(storage) is None
                or storage != expected_storage
                or storage in seen_storage
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact storage inventory is not exact"
                )
            seen_storage.add(storage)
            artifacts.append((path, size, digest, storage))
        if [item[0] for item in artifacts] != sorted(seen_paths):
            raise ReleaseArtifactStoreV2Error(
                "release artifact inventory is not sorted"
            )
        return plan_size, tuple(artifacts)

    @staticmethod
    def _bounded_directory_names(
        descriptor: int, *, maximum: int
    ) -> set[str]:
        names: set[str] = set()
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if entry.name in names:
                        raise ReleaseArtifactStoreV2Error(
                            "release artifact bundle has duplicate names"
                        )
                    names.add(entry.name)
                    if len(names) > maximum:
                        raise ReleaseArtifactStoreV2Error(
                            "release artifact bundle has extra records"
                        )
        except ReleaseArtifactStoreV2Error:
            raise
        except OSError as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact bundle inventory is unavailable"
            ) from error
        return names

    @staticmethod
    def _scan_payload(
        namespace: _PinnedNamespaceV2,
        *,
        logical_path: str,
        storage_name: str,
        size: int,
        digest: str,
    ) -> _ArtifactSnapshotV2:
        descriptor, identity = namespace._open_record(
            storage_name,
            label="release request artifact",
            expected_size=size,
            hook_path=logical_path,
        )
        chunks: list[str] = []
        overall = hashlib.sha256()
        total = 0
        try:
            while total < size:
                expected_block_size = min(
                    _VERIFY_CHUNK_BYTES, size - total
                )
                chunk = _read_exact_block(descriptor, expected_block_size)
                if len(chunk) != expected_block_size:
                    raise ReleaseArtifactStoreV2Error(
                        "release request artifact is truncated"
                    )
                total += len(chunk)
                overall.update(chunk)
                chunks.append(hashlib.sha256(chunk).hexdigest())
            if os.read(descriptor, 1):
                raise ReleaseArtifactStoreV2Error(
                    "release request artifact exceeds its bound size"
                )
            _stability_hook("record-after-read", logical_path)
            namespace._assert_record_unchanged(
                descriptor,
                name=storage_name,
                label="release request artifact",
                identity=identity,
            )
        finally:
            os.close(descriptor)
        if total != size or overall.hexdigest() != digest:
            raise ReleaseArtifactStoreV2Error(
                "release request artifact differs from its plan binding"
            )
        return _ArtifactSnapshotV2(
            logical_path,
            storage_name,
            size,
            digest,
            identity,
            tuple(chunks),
        )

    @staticmethod
    def _reconcile_validated_bundle(
        namespace: _PinnedNamespaceV2,
        records: tuple[tuple[str, int, _IdentityV2], ...],
    ) -> None:
        """Make every validated record and containing namespace durable."""

        for name, size, expected_identity in records:
            descriptor, opened = namespace._open_record(
                name,
                label="validated release artifact record",
                expected_size=size,
                expected_identity=expected_identity,
            )
            try:
                _stability_hook("reconcile-before-record-fsync", name)
                os.fsync(descriptor)
                namespace._assert_record_unchanged(
                    descriptor,
                    name=name,
                    label="validated release artifact record",
                    identity=opened,
                )
            finally:
                os.close(descriptor)
        _stability_hook("reconcile-before-bundle-fsync", namespace._bundle_name)
        os.fsync(namespace._bundle_fd)
        namespace._assert_bundle_identity()
        _stability_hook("reconcile-before-root-fsync", namespace._bundle_name)
        os.fsync(namespace._root_fd)
        namespace._assert_bundle_identity()

    def _reopen_locked(
        self,
        expected_plan_sha256: str,
        *,
        allow_in_progress_intent: bool = False,
    ) -> ReleaseArtifactBundleV2:
        expected_plan_sha256 = _validate_digest(
            expected_plan_sha256, label="expected release plan digest"
        )
        self._assert_root_identity()
        if (
            not allow_in_progress_intent
            and self._intent_exists(expected_plan_sha256)
        ):
            raise ReleaseArtifactStoreV2Error(
                "release artifact plan has a durable in-progress intent"
            )
        bundle_fd = -1
        root_copy = -1
        try:
            before = _require_directory(
                os.stat(
                    expected_plan_sha256,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                ),
                label="release artifact bundle",
            )
            _stability_hook("bundle-before-open", expected_plan_sha256)
            bundle_fd = os.open(
                expected_plan_sha256,
                _DIRECTORY_FLAGS,
                dir_fd=self._root_fd,
            )
            opened = _require_directory(
                os.fstat(bundle_fd), label="release artifact bundle"
            )
            after = _require_directory(
                os.stat(
                    expected_plan_sha256,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                ),
                label="release artifact bundle",
            )
            if not _same_directory_identity(before, opened) or not (
                _same_directory_identity(opened, after)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact bundle was replaced while reopening"
                )
            root_copy = os.dup(self._root_fd)
            namespace = _PinnedNamespaceV2(
                root_path=self._root_path,
                root_fd=root_copy,
                root_identity=self._root_identity,
                bundle_fd=bundle_fd,
                bundle_name=expected_plan_sha256,
                bundle_identity=opened,
            )
            try:
                commit_descriptor, commit_identity = namespace._open_record(
                    _COMMIT_NAME,
                    label="release artifact commit marker",
                    expected_size=None,
                )
            except ReleaseArtifactStoreV2Error as error:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact bundle is incomplete without its commit marker"
                ) from error
            try:
                if not 1 <= commit_identity.size <= MAX_CONTRACT_BYTES:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact commit marker exceeds its boundary"
                    )
                commit_bytes = _read_exact_small(
                    commit_descriptor,
                    expected_size=commit_identity.size,
                    label="release artifact commit marker",
                )
                namespace._assert_record_unchanged(
                    commit_descriptor,
                    name=_COMMIT_NAME,
                    label="release artifact commit marker",
                    identity=commit_identity,
                )
            finally:
                os.close(commit_descriptor)
            (
                committed_plan_size,
                committed_inventory_size,
                committed_inventory_sha256,
            ) = self._parse_commit_marker(
                commit_bytes, expected_plan_sha256=expected_plan_sha256
            )
            inventory_descriptor, inventory_identity = namespace._open_record(
                "inventory.json",
                label="release artifact inventory",
                expected_size=committed_inventory_size,
            )
            try:
                if not 1 <= inventory_identity.size <= MAX_CONTRACT_BYTES:
                    raise ReleaseArtifactStoreV2Error(
                        "release artifact inventory exceeds the canonical boundary"
                    )
                inventory_bytes = _read_exact_small(
                    inventory_descriptor,
                    expected_size=inventory_identity.size,
                    label="release artifact inventory",
                )
                namespace._assert_record_unchanged(
                    inventory_descriptor,
                    name="inventory.json",
                    label="release artifact inventory",
                    identity=inventory_identity,
                )
            finally:
                os.close(inventory_descriptor)
            if (
                hashlib.sha256(inventory_bytes).hexdigest()
                != committed_inventory_sha256
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact inventory differs from its commit marker"
                )
            plan_size, raw_artifacts = self._parse_inventory(
                inventory_bytes,
                expected_plan_sha256=expected_plan_sha256,
            )
            if plan_size != committed_plan_size:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact plan size differs from its commit marker"
                )
            plan_descriptor, plan_identity = namespace._open_record(
                "plan.json",
                label="release plan record",
                expected_size=plan_size,
            )
            try:
                plan_bytes = _read_exact_small(
                    plan_descriptor,
                    expected_size=plan_size,
                    label="release plan record",
                )
                namespace._assert_record_unchanged(
                    plan_descriptor,
                    name="plan.json",
                    label="release plan record",
                    identity=plan_identity,
                )
            finally:
                os.close(plan_descriptor)
            if hashlib.sha256(plan_bytes).hexdigest() != expected_plan_sha256:
                raise ReleaseArtifactStoreV2Error(
                    "release plan record crosses its bundle digest"
                )
            try:
                plan = ReleasePlanV2.from_bytes(plan_bytes)
            except (ContractError, TypeError, ValueError) as error:
                raise ReleaseArtifactStoreV2Error(
                    "release plan record is not canonical"
                ) from error
            expected_artifacts = tuple(
                (item.path, item.size, item.sha256)
                for item in plan.artifacts
            )
            if expected_artifacts != tuple(
                (path, size, digest)
                for path, size, digest, _storage in raw_artifacts
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact inventory differs from the release plan"
                )
            expected_names = {
                _COMMIT_NAME,
                "inventory.json",
                "plan.json",
                *(storage for *_other, storage in raw_artifacts),
            }
            actual_names = self._bounded_directory_names(
                bundle_fd, maximum=len(expected_names)
            )
            if actual_names != expected_names:
                raise ReleaseArtifactStoreV2Error(
                    "release artifact bundle has missing or extra records"
                )
            snapshots: dict[str, _ArtifactSnapshotV2] = {}
            for path, size, digest, storage in raw_artifacts:
                snapshots[path] = self._scan_payload(
                    namespace,
                    logical_path=path,
                    storage_name=storage,
                    size=size,
                    digest=digest,
                )
            namespace._assert_bundle_identity()
            reconciliation_records = (
                (_COMMIT_NAME, commit_identity.size, commit_identity),
                (
                    "inventory.json",
                    inventory_identity.size,
                    inventory_identity,
                ),
                ("plan.json", plan_identity.size, plan_identity),
                *tuple(
                    (
                        snapshot.storage_name,
                        snapshot.size,
                        snapshot.identity,
                    )
                    for snapshot in snapshots.values()
                ),
            )
            self._reconcile_validated_bundle(
                namespace, reconciliation_records
            )
            if (
                not allow_in_progress_intent
                and self._intent_exists(expected_plan_sha256)
            ):
                raise ReleaseArtifactStoreV2Error(
                    "release artifact plan gained an in-progress intent"
                )
            result = ReleaseArtifactBundleV2(
                root_path=self._root_path,
                root_fd=root_copy,
                root_identity=self._root_identity,
                bundle_fd=bundle_fd,
                bundle_name=expected_plan_sha256,
                bundle_identity=opened,
                plan=plan,
                plan_sha256=expected_plan_sha256,
                plan_size=plan_size,
                snapshots=snapshots,
            )
            root_copy = -1
            bundle_fd = -1
            return result
        except ReleaseArtifactStoreV2Error:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise ReleaseArtifactStoreV2Error(
                "release artifact bundle could not be reopened"
            ) from error
        finally:
            if bundle_fd >= 0:
                os.close(bundle_fd)
            if root_copy >= 0:
                os.close(root_copy)

    def reopen(self, expected_plan_sha256: str) -> ReleaseArtifactBundleV2:
        expected_plan_sha256 = _validate_digest(
            expected_plan_sha256, label="expected release plan digest"
        )
        with self._locked(exclusive=False):
            return self._reopen_locked(expected_plan_sha256)

    def close(self) -> None:
        with self._local_lock:
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1

    def __enter__(self) -> "ReleaseArtifactStoreV2":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass
