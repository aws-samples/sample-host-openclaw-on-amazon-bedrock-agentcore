"""Durable, exact-plan execution session for accepted release v2.

The session root is useful only after its final commit record is durable.  A
later process reopens the exact plan-named artifact bundle and journal through
fixed owner-only paths, authenticates the plan-bound AWS authority, and asks
the accepted controller to advance no more than one step.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import ClassVar, Iterator, Mapping

from release_tools.aws_authority_v2 import AuthenticatedAwsAuthorityV2
from release_tools.contracts import (
    ContractError,
    MAX_CONTRACT_BYTES,
    RELEASE_V2_TRANSACTION_STATES,
    ReleasePlanV2,
    StagingTransactionV2,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.evidence_store_v2 import ReleaseEvidenceStoreV2
from release_tools.release_artifact_store_v2 import (
    ReleaseArtifactBundleV2,
    ReleaseArtifactStoreV2,
)
from release_tools.release_controller_v2 import AcceptedReleaseControllerV2
from release_tools.release_plan_v2 import AssembledReleasePlanV2
from release_tools.release_runner_v2 import ReleaseRunnerStepResultV2
from release_tools.transaction import TransactionJournalV2


class ReleaseSessionV2Error(RuntimeError):
    """A release session is incomplete, crossed, mutable, or unavailable."""


class _SessionBoundaryError(RuntimeError):
    """Internal fail-closed boundary with no externally returned details."""


class _StepFailed(RuntimeError):
    """A provider/controller step failed after all capabilities closed."""


_DIRECTORY_MODE = 0o700
_RECORD_MODE = 0o400
_MUTABLE_RECORD_MODE = 0o600
_BINDING_SCHEMA = "personal-operator.release-session-binding.v2"
_COMMIT_SCHEMA = "personal-operator.release-session-commit.v2"
_RESULT_SCHEMA = "personal-operator.release-session-result.v2"
_ARTIFACTS = "artifacts"
_EVIDENCE = "evidence"
_JOURNAL = "journal.json"
_JOURNAL_LOCK = ".journal.json.lock"
_ENVELOPES = "envelopes"
_SCRATCH = "scratch"
_RUNTIME_CONTEXT = "runtime-context"
_BINDING = "PLAN-BINDING.json"
_COMMIT = "COMMITTED"
_FIXED_DIRECTORIES = (
    _ARTIFACTS,
    _ENVELOPES,
    _EVIDENCE,
    _RUNTIME_CONTEXT,
    _SCRATCH,
)
_FIXED_NAMES = frozenset(
    (*_FIXED_DIRECTORIES, _JOURNAL, _JOURNAL_LOCK, _BINDING, _COMMIT)
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_STEP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_PHASE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_KIND = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_RESULT_ACTIONS = frozenset(
    {"DISPATCHED_UNCERTAIN", "OBSERVED_READ_ONLY", "OBSERVED_UNCERTAIN"}
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_DIRECTORY_FLAGS = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _stability_hook(_stage: str) -> None:
    """Test-only race point; production deliberately performs no action."""


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    owner: int
    mode: int

    @classmethod
    def from_stat(cls, details: os.stat_result) -> "_DirectoryIdentity":
        if not stat.S_ISDIR(details.st_mode):
            raise _SessionBoundaryError("session entry is not a directory")
        if details.st_uid != os.geteuid():
            raise _SessionBoundaryError("session directory owner differs")
        if stat.S_IMODE(details.st_mode) != _DIRECTORY_MODE:
            raise _SessionBoundaryError("session directory is not owner-only")
        return cls(
            details.st_dev,
            details.st_ino,
            details.st_uid,
            stat.S_IMODE(details.st_mode),
        )


def _exact_text(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str] | None = None,
    maximum: int = 256,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise ContractError(f"release session {label} is invalid")
    return value


def _exact_count(value: object, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > 10000
    ):
        raise ContractError(f"release session {label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ReleaseSessionResultV2:
    """Canonical redacted progress returned by the session boundary."""

    SCHEMA: ClassVar[str] = _RESULT_SCHEMA

    plan_sha256: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    state: str
    revision: int
    completed_step_count: int
    total_step_count: int
    step_result: Mapping[str, str] | None

    def __post_init__(self) -> None:
        _exact_text(
            self.plan_sha256, label="plan digest", pattern=_SHA256
        )
        _exact_text(
            self.source_commit, label="source commit", pattern=_SHA1
        )
        _exact_text(self.source_tree, label="source tree", pattern=_SHA1)
        _exact_text(self.account, label="account", pattern=_ACCOUNT)
        _exact_text(self.region, label="region", maximum=32)
        if self.state not in RELEASE_V2_TRANSACTION_STATES:
            raise ContractError("release session state is invalid")
        revision = _exact_count(self.revision, label="revision")
        completed = _exact_count(
            self.completed_step_count, label="completed step count"
        )
        total = _exact_count(self.total_step_count, label="total step count")
        if total == 0 or completed > total or revision < completed:
            raise ContractError("release session progress is invalid")
        if self.step_result is None:
            return
        if not isinstance(self.step_result, Mapping):
            raise ContractError("release session step result is invalid")
        step = dict(self.step_result)
        if set(step) != {"stepId", "phase", "kind", "action"}:
            raise ContractError("release session step result fields are invalid")
        _exact_text(step["stepId"], label="step ID", pattern=_STEP_ID)
        _exact_text(step["phase"], label="step phase", pattern=_PHASE)
        _exact_text(step["kind"], label="step kind", pattern=_KIND)
        if step["action"] not in _RESULT_ACTIONS:
            raise ContractError("release session step action is invalid")
        object.__setattr__(self, "step_result", MappingProxyType(step))

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object]
    ) -> "ReleaseSessionResultV2":
        if not isinstance(raw, Mapping):
            raise ContractError("release session result is not an object")
        value = dict(raw)
        expected = {
            "schema",
            "planSha256",
            "sourceCommit",
            "sourceTree",
            "account",
            "region",
            "state",
            "revision",
            "completedStepCount",
            "totalStepCount",
            "stepResult",
        }
        if set(value) != expected or value["schema"] != cls.SCHEMA:
            raise ContractError("release session result fields are invalid")
        raw_step = value["stepResult"]
        if raw_step is not None and not isinstance(raw_step, dict):
            raise ContractError("release session step result is invalid")
        return cls(
            plan_sha256=value["planSha256"],  # type: ignore[arg-type]
            source_commit=value["sourceCommit"],  # type: ignore[arg-type]
            source_tree=value["sourceTree"],  # type: ignore[arg-type]
            account=value["account"],  # type: ignore[arg-type]
            region=value["region"],  # type: ignore[arg-type]
            state=value["state"],  # type: ignore[arg-type]
            revision=value["revision"],  # type: ignore[arg-type]
            completed_step_count=value["completedStepCount"],  # type: ignore[arg-type]
            total_step_count=value["totalStepCount"],  # type: ignore[arg-type]
            step_result=raw_step,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ReleaseSessionResultV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "state": self.state,
            "revision": self.revision,
            "completedStepCount": self.completed_step_count,
            "totalStepCount": self.total_step_count,
            "stepResult": (
                None if self.step_result is None else dict(self.step_result)
            ),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


def _validated_root_path(root: object) -> Path:
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or root.name in {"", ".", ".."}
    ):
        raise _SessionBoundaryError("release session root is invalid")
    return root


def _validated_digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _SessionBoundaryError("release session plan digest is invalid")
    return value


def _assert_root_identity(
    root: Path, root_fd: int, expected: _DirectoryIdentity
) -> None:
    retained = _DirectoryIdentity.from_stat(os.fstat(root_fd))
    current = _DirectoryIdentity.from_stat(
        os.stat(root, follow_symlinks=False)
    )
    if retained != expected or current != expected:
        raise _SessionBoundaryError("release session root identity changed")


def _lock_root(root_fd: int) -> None:
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise _SessionBoundaryError("release session is already active") from error
        raise _SessionBoundaryError("release session lock failed") from error


def _unlock_root(root_fd: int) -> None:
    try:
        fcntl.flock(root_fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset : offset + 65536])
        if written <= 0:
            raise _SessionBoundaryError("release session write made no progress")
        offset += written


def _create_record(
    directory_fd: int,
    *,
    name: str,
    payload: bytes,
    final_mode: int,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            _CREATE_FLAGS,
            _MUTABLE_RECORD_MODE,
            dir_fd=directory_fd,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fchmod(descriptor, final_mode)
        os.fsync(descriptor)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != final_mode
            or details.st_nlink != 1
            or details.st_size != len(payload)
        ):
            raise _SessionBoundaryError("release session record is unsafe")
    except _SessionBoundaryError:
        raise
    except OSError as error:
        raise _SessionBoundaryError("release session record could not be created") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.fsync(directory_fd)


def _read_record(
    directory_fd: int,
    *,
    name: str,
    expected_mode: int,
    maximum: int = MAX_CONTRACT_BYTES,
) -> bytes:
    descriptor = -1
    try:
        path_details = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_details.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or (path_details.st_dev, path_details.st_ino)
            != (before.st_dev, before.st_ino)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum
        ):
            raise _SessionBoundaryError("release session record is unsafe")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > maximum
            or before.st_size != len(payload)
            or (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_mode,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise _SessionBoundaryError("release session record changed during read")
        return payload
    except _SessionBoundaryError:
        raise
    except OSError as error:
        raise _SessionBoundaryError("release session record is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_fixed_directory(
    root_fd: int, name: str
) -> tuple[int, _DirectoryIdentity]:
    descriptor = -1
    try:
        path_details = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        opened = _DirectoryIdentity.from_stat(os.fstat(descriptor))
        path_identity = _DirectoryIdentity.from_stat(path_details)
        if opened != path_identity:
            raise _SessionBoundaryError("release session directory was replaced")
        retained = descriptor
        descriptor = -1
        return retained, opened
    except _SessionBoundaryError:
        raise
    except OSError as error:
        raise _SessionBoundaryError("release session directory is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_fixed_directories(
    root_fd: int,
) -> tuple[tuple[tuple[str, int], ...], Mapping[str, _DirectoryIdentity]]:
    retained: list[tuple[str, int]] = []
    identities: dict[str, _DirectoryIdentity] = {}
    try:
        for name in _FIXED_DIRECTORIES:
            descriptor, identity = _open_fixed_directory(root_fd, name)
            retained.append((name, descriptor))
            identities[name] = identity
        return tuple(retained), MappingProxyType(identities)
    except Exception:
        for _name, descriptor in retained:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _close_fixed_directories(
    retained: tuple[tuple[str, int], ...]
) -> None:
    for _name, descriptor in retained:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _namespace_identity_sha256(
    root_identity: _DirectoryIdentity,
    directory_identities: Mapping[str, _DirectoryIdentity],
) -> str:
    if set(directory_identities) != set(_FIXED_DIRECTORIES):
        raise _SessionBoundaryError("release session directory inventory differs")

    def value(identity: _DirectoryIdentity) -> dict[str, int]:
        return {
            "device": identity.device,
            "inode": identity.inode,
            "owner": identity.owner,
            "mode": identity.mode,
        }

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-session-namespace.v2",
                "root": value(root_identity),
                "directories": [
                    {"name": name, **value(directory_identities[name])}
                    for name in sorted(_FIXED_DIRECTORIES)
                ],
            }
        )
    ).hexdigest()


def _require_top_names(root_fd: int) -> None:
    try:
        names = os.listdir(root_fd)
    except OSError as error:
        raise _SessionBoundaryError("release session namespace is unavailable") from error
    if len(names) != len(_FIXED_NAMES) or frozenset(names) != _FIXED_NAMES:
        raise _SessionBoundaryError("release session namespace differs")


def _require_journal_records(root_fd: int) -> None:
    _read_record(
        root_fd, name=_JOURNAL, expected_mode=_MUTABLE_RECORD_MODE
    )
    lock_payload = _read_record(
        root_fd,
        name=_JOURNAL_LOCK,
        expected_mode=_MUTABLE_RECORD_MODE,
    )
    if lock_payload:
        raise _SessionBoundaryError("release session journal lock is not empty")


def _fixed_directory_identities(
    root_fd: int,
) -> Mapping[str, _DirectoryIdentity]:
    retained, identities = _open_fixed_directories(root_fd)
    _close_fixed_directories(retained)
    return identities


def _require_fixed_namespace(
    root_fd: int,
    *,
    expected: Mapping[str, _DirectoryIdentity] | None = None,
) -> Mapping[str, _DirectoryIdentity]:
    _require_top_names(root_fd)
    identities = _fixed_directory_identities(root_fd)
    if expected is not None and dict(identities) != dict(expected):
        raise _SessionBoundaryError("release session directory identity changed")
    _require_journal_records(root_fd)
    return identities


def _assert_retained_directories(
    root_fd: int,
    retained: tuple[tuple[str, int], ...],
    expected: Mapping[str, _DirectoryIdentity],
) -> None:
    _require_top_names(root_fd)
    if {name for name, _descriptor in retained} != set(_FIXED_DIRECTORIES):
        raise _SessionBoundaryError("release session retained inventory differs")
    for name, descriptor in retained:
        retained_identity = _DirectoryIdentity.from_stat(os.fstat(descriptor))
        path_identity = _DirectoryIdentity.from_stat(
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        )
        if retained_identity != expected[name] or path_identity != expected[name]:
            raise _SessionBoundaryError("release session directory identity changed")
    _require_journal_records(root_fd)


def _binding_for(
    plan: ReleasePlanV2, *, namespace_identity_sha256: str
) -> dict[str, object]:
    namespace_identity_sha256 = _validated_digest(namespace_identity_sha256)
    return {
        "schema": _BINDING_SCHEMA,
        "planSha256": plan.digest(),
        "namespaceIdentitySha256": namespace_identity_sha256,
        "planSize": len(plan.to_bytes()),
        "transactionId": plan.transaction_id,
        "sourceCommit": plan.source_commit,
        "sourceTree": plan.source_tree,
        "account": plan.account,
        "region": plan.region,
        "artifactCount": len(plan.artifacts),
        "stepCount": len(plan.steps),
    }


def _validated_binding(
    root_fd: int,
    *,
    expected_plan_sha256: str,
    expected_namespace_identity_sha256: str,
) -> tuple[dict[str, object], bytes]:
    try:
        binding_payload = _read_record(
            root_fd, name=_BINDING, expected_mode=_RECORD_MODE
        )
        marker_payload = _read_record(
            root_fd, name=_COMMIT, expected_mode=_RECORD_MODE
        )
        binding = parse_canonical_object(binding_payload)
        marker = parse_canonical_object(marker_payload)
    except (ContractError, TypeError, ValueError) as error:
        raise _SessionBoundaryError("release session commit is invalid") from error
    binding_fields = {
        "schema",
        "planSha256",
        "namespaceIdentitySha256",
        "planSize",
        "transactionId",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "artifactCount",
        "stepCount",
    }
    if set(binding) != binding_fields or binding.get("schema") != _BINDING_SCHEMA:
        raise _SessionBoundaryError("release session binding fields differ")
    if (
        binding.get("planSha256") != expected_plan_sha256
        or binding.get("namespaceIdentitySha256")
        != expected_namespace_identity_sha256
        or not isinstance(binding.get("planSize"), int)
        or isinstance(binding.get("planSize"), bool)
        or not 1 <= binding["planSize"] <= MAX_CONTRACT_BYTES
        or not isinstance(binding.get("artifactCount"), int)
        or isinstance(binding.get("artifactCount"), bool)
        or not 1 <= binding["artifactCount"] <= 10000
        or not isinstance(binding.get("stepCount"), int)
        or isinstance(binding.get("stepCount"), bool)
        or not 1 <= binding["stepCount"] <= 10000
    ):
        raise _SessionBoundaryError("release session binding differs")
    _exact_text(binding.get("transactionId"), label="transaction ID")
    _exact_text(
        binding.get("sourceCommit"), label="source commit", pattern=_SHA1
    )
    _exact_text(binding.get("sourceTree"), label="source tree", pattern=_SHA1)
    _exact_text(binding.get("account"), label="account", pattern=_ACCOUNT)
    _exact_text(binding.get("region"), label="region", maximum=32)
    expected_marker = {
        "schema": _COMMIT_SCHEMA,
        "planSha256": expected_plan_sha256,
        "bindingSha256": hashlib.sha256(binding_payload).hexdigest(),
    }
    if marker != expected_marker:
        raise _SessionBoundaryError("release session commit differs")
    return binding, binding_payload


def _validate_plan_binding(
    plan: ReleasePlanV2,
    binding: Mapping[str, object],
    expected_plan_sha256: str,
    expected_namespace_identity_sha256: str,
) -> None:
    try:
        canonical = ReleasePlanV2.from_bytes(plan.to_bytes())
    except (AttributeError, ContractError, TypeError, ValueError) as error:
        raise _SessionBoundaryError("release session plan is invalid") from error
    if canonical != plan or canonical.digest() != expected_plan_sha256:
        raise _SessionBoundaryError("release session plan differs")
    if _binding_for(
        canonical,
        namespace_identity_sha256=expected_namespace_identity_sha256,
    ) != dict(binding):
        raise _SessionBoundaryError("release session plan binding differs")


def _status_result(
    *,
    plan: ReleasePlanV2,
    current: StagingTransactionV2,
    step_result: ReleaseRunnerStepResultV2 | None,
) -> ReleaseSessionResultV2:
    try:
        canonical_plan = ReleasePlanV2.from_bytes(plan.to_bytes())
        canonical_current = StagingTransactionV2.from_bytes(
            current.to_bytes(), plan=canonical_plan
        )
    except (AttributeError, ContractError, TypeError, ValueError) as error:
        raise _SessionBoundaryError("release session status is invalid") from error
    if step_result is not None:
        if type(step_result) is not ReleaseRunnerStepResultV2:
            raise _SessionBoundaryError("release session step result is invalid")
        if (
            step_result.revision != canonical_current.revision
            or step_result.state != canonical_current.state
            or not any(
                (
                    step.step_id,
                    step.phase,
                    step.kind,
                )
                == (
                    step_result.step_id,
                    step_result.phase,
                    step_result.kind,
                )
                for step in canonical_plan.steps
            )
        ):
            raise _SessionBoundaryError("release session step result differs")
        redacted_step: Mapping[str, str] | None = {
            "stepId": step_result.step_id,
            "phase": step_result.phase,
            "kind": step_result.kind,
            "action": step_result.action,
        }
    else:
        redacted_step = None
    result = ReleaseSessionResultV2(
        plan_sha256=canonical_plan.digest(),
        source_commit=canonical_plan.source_commit,
        source_tree=canonical_plan.source_tree,
        account=canonical_plan.account,
        region=canonical_plan.region,
        state=canonical_current.state,
        revision=canonical_current.revision,
        completed_step_count=canonical_current.completed_step_count,
        total_step_count=len(canonical_plan.steps),
        step_result=redacted_step,
    )
    if ReleaseSessionResultV2.from_bytes(result.to_bytes()) != result:
        raise _SessionBoundaryError("release session status is not canonical")
    return result


@dataclass(slots=True)
class _OpenedSession:
    root: Path
    root_fd: int
    root_identity: _DirectoryIdentity
    store: ReleaseArtifactStoreV2
    bundle: ReleaseArtifactBundleV2
    evidence_store: ReleaseEvidenceStoreV2
    journal: TransactionJournalV2
    directory_fds: tuple[tuple[str, int], ...]
    directory_identities: Mapping[str, _DirectoryIdentity]

    def assert_current(self) -> None:
        _assert_root_identity(self.root, self.root_fd, self.root_identity)
        _assert_retained_directories(
            self.root_fd,
            self.directory_fds,
            self.directory_identities,
        )

    def close(self) -> None:
        try:
            self.evidence_store.close()
        finally:
            try:
                self.bundle.close()
            finally:
                try:
                    self.store.close()
                finally:
                    _close_fixed_directories(self.directory_fds)


def _reopen_session(
    root: Path, *, expected_plan_sha256: str
) -> _OpenedSession:
    root_fd = -1
    store: ReleaseArtifactStoreV2 | None = None
    bundle: ReleaseArtifactBundleV2 | None = None
    evidence_store: ReleaseEvidenceStoreV2 | None = None
    directory_fds: tuple[tuple[str, int], ...] = ()
    locked = False
    completed = False
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        identity = _DirectoryIdentity.from_stat(os.fstat(root_fd))
        _assert_root_identity(root, root_fd, identity)
        _lock_root(root_fd)
        locked = True
        _stability_hook("open-after-lock")
        _assert_root_identity(root, root_fd, identity)
        _require_top_names(root_fd)
        directory_fds, directory_identities = _open_fixed_directories(root_fd)
        _require_journal_records(root_fd)
        namespace_identity_sha256 = _namespace_identity_sha256(
            identity, directory_identities
        )
        binding, _binding_payload = _validated_binding(
            root_fd,
            expected_plan_sha256=expected_plan_sha256,
            expected_namespace_identity_sha256=namespace_identity_sha256,
        )
        store = ReleaseArtifactStoreV2.open(root / _ARTIFACTS)
        bundle = store.reopen(expected_plan_sha256)
        _validate_plan_binding(
            bundle.plan,
            binding,
            expected_plan_sha256,
            namespace_identity_sha256,
        )
        evidence_store = ReleaseEvidenceStoreV2(root / _EVIDENCE)
        journal = TransactionJournalV2.load(
            root / _JOURNAL,
            plan=bundle.plan,
            evidence_store=evidence_store,
        )
        _assert_root_identity(root, root_fd, identity)
        _assert_retained_directories(
            root_fd, directory_fds, directory_identities
        )
        opened = _OpenedSession(
            root,
            root_fd,
            identity,
            store,
            bundle,
            evidence_store,
            journal,
            directory_fds,
            directory_identities,
        )
        completed = True
        return opened
    except _SessionBoundaryError:
        raise
    except Exception as error:
        raise _SessionBoundaryError("release session could not be opened") from error
    finally:
        if not completed:
            if evidence_store is not None:
                evidence_store.close()
            if bundle is not None:
                bundle.close()
            if store is not None:
                store.close()
            _close_fixed_directories(directory_fds)
            if locked and root_fd >= 0:
                _unlock_root(root_fd)
            if root_fd >= 0:
                os.close(root_fd)


@contextmanager
def _opened_session(
    root: Path, *, expected_plan_sha256: str
) -> Iterator[_OpenedSession]:
    opened = _reopen_session(
        root, expected_plan_sha256=expected_plan_sha256
    )
    try:
        yield opened
    finally:
        try:
            opened.close()
        finally:
            _unlock_root(opened.root_fd)
            os.close(opened.root_fd)


class AcceptedReleaseSessionV2:
    """Initialize, inspect, or advance one exact accepted release session."""

    @classmethod
    def initialize(
        cls,
        root: Path,
        assembled: AssembledReleasePlanV2,
    ) -> ReleaseSessionResultV2:
        root_fd = -1
        locked = False
        store: ReleaseArtifactStoreV2 | None = None
        bundle: ReleaseArtifactBundleV2 | None = None
        evidence_store: ReleaseEvidenceStoreV2 | None = None
        result: ReleaseSessionResultV2 | None = None
        failed = False
        try:
            root = _validated_root_path(root)
            if type(assembled) is not AssembledReleasePlanV2:
                raise _SessionBoundaryError("release assembly is invalid")
            plan = ReleasePlanV2.from_bytes(assembled.plan.to_bytes())
            if plan != assembled.plan:
                raise _SessionBoundaryError("release assembly plan differs")
            os.mkdir(root, mode=_DIRECTORY_MODE)
            root_fd = os.open(root, _DIRECTORY_FLAGS)
            os.fchmod(root_fd, _DIRECTORY_MODE)
            identity = _DirectoryIdentity.from_stat(os.fstat(root_fd))
            _assert_root_identity(root, root_fd, identity)
            parent_fd = os.open(root.parent, _DIRECTORY_FLAGS)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            _lock_root(root_fd)
            locked = True
            _stability_hook("initialize-after-root")
            _assert_root_identity(root, root_fd, identity)

            for name in (_ENVELOPES, _SCRATCH, _RUNTIME_CONTEXT):
                os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=root_fd)
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
                try:
                    os.fchmod(descriptor, _DIRECTORY_MODE)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            os.fsync(root_fd)

            store = ReleaseArtifactStoreV2.create(root / _ARTIFACTS)
            bundle = store.persist(assembled)
            if bundle.plan != plan or bundle.plan_sha256 != plan.digest():
                raise _SessionBoundaryError("persisted release plan differs")
            bundle.close()
            bundle = None
            store.close()
            store = None

            evidence_store = ReleaseEvidenceStoreV2(root / _EVIDENCE)
            journal = TransactionJournalV2.create(
                root / _JOURNAL,
                plan=plan,
                evidence_store=evidence_store,
            )
            _create_record(
                root_fd,
                name=_JOURNAL_LOCK,
                payload=b"",
                final_mode=_MUTABLE_RECORD_MODE,
            )
            directory_identities = _fixed_directory_identities(root_fd)
            namespace_identity_sha256 = _namespace_identity_sha256(
                identity, directory_identities
            )
            binding_payload = canonical_json_bytes(
                _binding_for(
                    plan,
                    namespace_identity_sha256=namespace_identity_sha256,
                )
            )
            _create_record(
                root_fd,
                name=_BINDING,
                payload=binding_payload,
                final_mode=_RECORD_MODE,
            )
            before_commit = _FIXED_NAMES - {_COMMIT}
            if frozenset(os.listdir(root_fd)) != before_commit:
                raise _SessionBoundaryError("release session staging differs")
            marker_payload = canonical_json_bytes(
                {
                    "schema": _COMMIT_SCHEMA,
                    "planSha256": plan.digest(),
                    "bindingSha256": hashlib.sha256(
                        binding_payload
                    ).hexdigest(),
                }
            )
            _stability_hook("initialize-before-commit")
            _assert_root_identity(root, root_fd, identity)
            if frozenset(os.listdir(root_fd)) != before_commit:
                raise _SessionBoundaryError("release session staging changed")
            current_directory_identities = _fixed_directory_identities(root_fd)
            if dict(current_directory_identities) != dict(directory_identities):
                raise _SessionBoundaryError(
                    "release session staging directory identity changed"
                )
            _require_journal_records(root_fd)
            if _read_record(
                root_fd, name=_BINDING, expected_mode=_RECORD_MODE
            ) != binding_payload:
                raise _SessionBoundaryError(
                    "release session staging binding changed"
                )
            validation_store = ReleaseArtifactStoreV2.open(root / _ARTIFACTS)
            try:
                validation_bundle = validation_store.reopen(plan.digest())
                try:
                    if validation_bundle.plan != plan:
                        raise _SessionBoundaryError(
                            "release session staging plan changed"
                        )
                finally:
                    validation_bundle.close()
            finally:
                validation_store.close()
            durable_journal = TransactionJournalV2.load(
                root / _JOURNAL,
                plan=plan,
                evidence_store=evidence_store,
            )
            if (
                durable_journal.current.to_bytes() != journal.current.to_bytes()
                or durable_journal.journal_execution_id
                != journal.journal_execution_id
            ):
                raise _SessionBoundaryError(
                    "release session staging journal changed"
                )
            _create_record(
                root_fd,
                name=_COMMIT,
                payload=marker_payload,
                final_mode=_RECORD_MODE,
            )
            _assert_root_identity(root, root_fd, identity)
            _require_fixed_namespace(
                root_fd, expected=directory_identities
            )
            _validated_binding(
                root_fd,
                expected_plan_sha256=plan.digest(),
                expected_namespace_identity_sha256=(
                    namespace_identity_sha256
                ),
            )
            durable_journal = TransactionJournalV2.load(
                root / _JOURNAL,
                plan=plan,
                evidence_store=evidence_store,
            )
            if (
                durable_journal.current.to_bytes() != journal.current.to_bytes()
                or durable_journal.journal_execution_id
                != journal.journal_execution_id
            ):
                raise _SessionBoundaryError(
                    "release session committed journal changed"
                )
            result = _status_result(
                plan=plan,
                current=durable_journal.current,
                step_result=None,
            )
        except Exception:
            failed = True
        finally:
            if evidence_store is not None:
                try:
                    evidence_store.close()
                except Exception:
                    failed = True
            if bundle is not None:
                try:
                    bundle.close()
                except Exception:
                    failed = True
            if store is not None:
                try:
                    store.close()
                except Exception:
                    failed = True
            if locked and root_fd >= 0:
                _unlock_root(root_fd)
            if root_fd >= 0:
                try:
                    os.close(root_fd)
                except OSError:
                    failed = True
        if failed or result is None:
            raise ReleaseSessionV2Error(
                "release session could not be initialized"
            ) from None
        return result

    @classmethod
    def status(
        cls,
        root: Path,
        *,
        expected_plan_sha256: str,
    ) -> ReleaseSessionResultV2:
        result: ReleaseSessionResultV2 | None = None
        failed = False
        try:
            root = _validated_root_path(root)
            expected_plan_sha256 = _validated_digest(expected_plan_sha256)
            with _opened_session(
                root, expected_plan_sha256=expected_plan_sha256
            ) as opened:
                opened.assert_current()
                result = _status_result(
                    plan=opened.bundle.plan,
                    current=opened.journal.current,
                    step_result=None,
                )
        except Exception:
            failed = True
        if failed or result is None:
            raise ReleaseSessionV2Error(
                "release session could not be reopened"
            ) from None
        return result

    @classmethod
    def run_one(
        cls,
        root: Path,
        *,
        expected_plan_sha256: str,
        site_packages: Path,
        aws_directory: Path,
    ) -> ReleaseSessionResultV2:
        result: ReleaseSessionResultV2 | None = None
        failure = ""
        try:
            root = _validated_root_path(root)
            expected_plan_sha256 = _validated_digest(expected_plan_sha256)
            if (
                not isinstance(site_packages, Path)
                or not site_packages.is_absolute()
                or not isinstance(aws_directory, Path)
                or not aws_directory.is_absolute()
            ):
                raise _SessionBoundaryError(
                    "release session authentication paths are invalid"
                )
            with _opened_session(
                root, expected_plan_sha256=expected_plan_sha256
            ) as opened:
                opened.assert_current()
                step_failed = False
                try:
                    with AuthenticatedAwsAuthorityV2.open_bootstrap(
                        opened.bundle.plan,
                        site_packages=site_packages,
                        aws_directory=aws_directory,
                    ) as authority:
                        opened.assert_current()
                        controller = AcceptedReleaseControllerV2(
                            plan=opened.bundle.plan,
                            authority=authority,
                            journal=opened.journal,
                            evidence_store=opened.evidence_store,
                            artifact_bundle=opened.bundle,
                            envelope_directory=root / _ENVELOPES,
                            scratch_directory=root / _SCRATCH,
                            runtime_context_root=root / _RUNTIME_CONTEXT,
                        )
                        opened.assert_current()
                        outcome = controller.run_one()
                except Exception:
                    step_failed = True
                if step_failed:
                    raise _StepFailed
                opened.assert_current()
                result = _status_result(
                    plan=opened.bundle.plan,
                    current=opened.journal.current,
                    step_result=outcome,
                )
        except _StepFailed:
            failure = "release session step failed closed"
        except Exception:
            failure = "release session could not be reopened"
        if failure or result is None:
            raise ReleaseSessionV2Error(
                failure or "release session could not be reopened"
            ) from None
        return result

    @classmethod
    def from_bytes(cls, payload: bytes) -> ReleaseSessionResultV2:
        return ReleaseSessionResultV2.from_bytes(payload)


__all__ = [
    "AcceptedReleaseSessionV2",
    "ReleaseSessionResultV2",
    "ReleaseSessionV2Error",
]
