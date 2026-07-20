"""Append-only, execution-bound evidence for release transaction v2.

The evidence store pins its directory namespace, retains exact minimized
provider bytes before a journal CAS, and mints single-use outcome capabilities.
Only :class:`ReleaseOutcomeComposerV2` is a production construction path.  It
derives typed values from the provider projection and refuses step kinds whose
derivation is not yet closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping
import uuid

from release_tools.contracts import (
    ContractError,
    FoundationRuntimeInputsV1,
    MAX_CONTRACT_BYTES,
    RELEASE_V2_PHASE_STATES,
    ReleaseJournalTransitionCommitV2,
    ReleaseJournalTransitionV2,
    ReleasePlanV2,
    ResolvedMutationRequestV2,
    ReleaseStepFailureObservationV2,
    ReleaseStepObservationV2,
    RetainedStepEvidenceV2,
    StagingTransactionV2,
    _FAILED_RETAINED_KIND_REASON_STATUSES,
    _FAILED_RETAINED_REASON_PROVIDERS,
    _canonical_release_plan_v2,
    _completed_prefix_sha256,
    _release_operation_sha256,
    _release_outcome_operation_sha256,
    _write_all,
    canonical_json_bytes,
    parse_canonical_object,
    read_regular_bytes,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    DispatchAttemptStateV1,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)


class EvidenceStoreV2Error(RuntimeError):
    """Evidence is missing, mutable, crossed, or not composer-derived."""


_MAX_EVIDENCE_BYTES = MAX_CONTRACT_BYTES * 4
_DIRECTORY_MODE = 0o700
_RECORD_MODE = 0o400
_WRITE_MODE = 0o600
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_DIRECTORY_FLAGS = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)
_SYNTACTIC_DERIVED_PROJECTION_FIELDS = frozenset(
    {
        "foundationRuntimeInputs",
        "foundationInputsSha256",
        "routerCronChangesetsSha256",
        "routerCronApplicationSha256",
        "schedulerChangesetSha256",
        "schedulerApplicationSha256",
        "webChangesetsSha256",
        "webChangesetSha256",
        "webApplicationSha256",
        "verificationSha256",
        "foundation_runtime_inputs",
        "foundation_inputs_sha256",
        "router_cron_changesets_sha256",
        "router_cron_application_sha256",
        "scheduler_changeset_sha256",
        "scheduler_application_sha256",
        "web_changesets_sha256",
        "web_changeset_sha256",
        "web_application_sha256",
        "verification_sha256",
    }
)
_PHASE_EVIDENCE_FIELDS = {
    "router-cron-cs": "routerCronChangesetsSha256",
    "router-cron": "routerCronApplicationSha256",
    "scheduler-cs": "schedulerChangesetSha256",
    "scheduler": "schedulerApplicationSha256",
    "web-cs": "webChangesetSha256",
    "web": "webApplicationSha256",
    "verify": "verificationSha256",
}
_MUTATION_PROVIDERS = {
    "AGENTCORE_HARDEN": "AGENTCORE",
    "ASSET_PUBLISH": "S3",
    "BOOTSTRAP_STACK": "CLOUDFORMATION",
    "CHANGESET_CREATE": "CLOUDFORMATION",
    "CHANGESET_EXECUTE": "CLOUDFORMATION",
    "IMAGE_PUBLISH": "ECR",
    "RUNTIME_CONTEXT_WRITE": "LOCAL_FILESYSTEM",
    "STACK_CREATE": "CLOUDFORMATION",
    "STACK_DRIFT_CHECK": "CLOUDFORMATION",
    "STACK_UPDATE": "CLOUDFORMATION",
}
_RELEASE_VERIFICATION_PROJECTION_FIELDS = frozenset(
    {
        "planSha256",
        "transactionSha256",
        "completedPrefixSha256",
        "retainedPrefixSha256",
        "evidenceStoreSha256",
        "journalPathSha256",
        "journalExecutionId",
        "journalRevision",
        "completedRecordCount",
        "foundationInputsSha256",
        "runtimeImageDigest",
        "imageObservationSha256",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeEndpointId",
        "runtimeEndpointName",
        "runtimeEndpointArn",
        "runtimeWorkloadIdentityArn",
        "runtimeConfigurationSha256",
        "runtimeIamRequestSha256",
        "runtimeIamObservationSha256",
        "runtimeContextSha256",
        "runtimeContextObservationSha256",
        "guardrailId",
        "guardrailVersion",
    }
)


def _journal_path_sha256(path: Path) -> str:
    absolute = os.path.abspath(os.fspath(path))
    if not absolute or "\x00" in absolute:
        raise EvidenceStoreV2Error("journal path identity is invalid")
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-journal-path.v2",
                "absolutePath": absolute,
            }
        )
    ).hexdigest()


def _is_agentcore_endpoint_api_arn(
    value: object, *, account: str, region: str
) -> bool:
    prefix = (
        f"arn:aws:bedrock-agentcore:{region}:{account}:agentEndpoint/"
    )
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value.removeprefix(prefix)
    try:
        parsed = uuid.UUID(suffix)
    except (AttributeError, ValueError):
        return False
    return str(parsed) == suffix


def _is_agentcore_workload_identity_arn(
    value: object, *, account: str, region: str
) -> bool:
    prefix = (
        f"arn:aws:bedrock-agentcore:{region}:{account}:"
        "workload-identity-directory/default/workload-identity/"
    )
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    name = value.removeprefix(prefix)
    return (
        3 <= len(name) <= 255
        and all(
            character.isascii()
            and (character.isalnum() or character in "_.-")
            for character in name
        )
    )


def _read_all(descriptor: int, *, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, _MAX_EVIDENCE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_EVIDENCE_BYTES:
            raise EvidenceStoreV2Error(f"{label} exceeds its size boundary")
    return b"".join(chunks)


def _require_directory(details: os.stat_result, *, label: str) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise EvidenceStoreV2Error(f"{label} is not a directory")
    if details.st_uid != os.geteuid():
        raise EvidenceStoreV2Error(f"{label} owner differs")
    if stat.S_IMODE(details.st_mode) != _DIRECTORY_MODE:
        raise EvidenceStoreV2Error(f"{label} mode is not owner-only")


def _require_record(details: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(details.st_mode):
        raise EvidenceStoreV2Error(f"{label} is not a regular record")
    if details.st_uid != os.geteuid():
        raise EvidenceStoreV2Error(f"{label} owner differs")
    if stat.S_IMODE(details.st_mode) != _RECORD_MODE:
        raise EvidenceStoreV2Error(f"{label} mode is not read-only owner-only")
    if details.st_nlink != 1:
        raise EvidenceStoreV2Error(f"{label} link count differs from one")


_VERIFIED_STEP_OUTCOME_TOKEN = object()
_JOURNAL_TRANSITION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _ReceiptAuthorityV2:
    """Exact journal operation owning one durable provider receipt slot."""

    kind: str
    release_plan_sha256: str
    evidence_store_sha256: str
    journal_path_sha256: str
    journal_execution_id: str
    journal_revision: int
    completed_prefix_sha256: str
    step_id: str
    subject: str
    operation_sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": "personal-operator.provider-receipt-attempt.v2",
            "kind": self.kind,
            "releasePlanSha256": self.release_plan_sha256,
            "evidenceStoreSha256": self.evidence_store_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
            "journalRevision": self.journal_revision,
            "completedPrefixSha256": self.completed_prefix_sha256,
            "stepId": self.step_id,
            "subject": self.subject,
            "operationSha256": self.operation_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @classmethod
    def from_bytes(cls, payload: bytes) -> "_ReceiptAuthorityV2":
        try:
            value = parse_canonical_object(payload)
        except (ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "provider receipt attempt is not canonical"
            ) from error
        expected = {
            "schema",
            "kind",
            "releasePlanSha256",
            "evidenceStoreSha256",
            "journalPathSha256",
            "journalExecutionId",
            "journalRevision",
            "completedPrefixSha256",
            "stepId",
            "subject",
            "operationSha256",
        }
        if set(value) != expected or value.get("schema") != (
            "personal-operator.provider-receipt-attempt.v2"
        ):
            raise EvidenceStoreV2Error(
                "provider receipt attempt fields are invalid"
            )
        text_fields = expected - {"schema", "journalRevision"}
        if any(
            not isinstance(value.get(field), str)
            or not value[field]
            or "\x00" in value[field]
            for field in text_fields
        ):
            raise EvidenceStoreV2Error(
                "provider receipt attempt identity is invalid"
            )
        revision = value.get("journalRevision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise EvidenceStoreV2Error(
                "provider receipt attempt revision is invalid"
            )
        return cls(
            kind=value["kind"],
            release_plan_sha256=value["releasePlanSha256"],
            evidence_store_sha256=value["evidenceStoreSha256"],
            journal_path_sha256=value["journalPathSha256"],
            journal_execution_id=value["journalExecutionId"],
            journal_revision=revision,
            completed_prefix_sha256=value["completedPrefixSha256"],
            step_id=value["stepId"],
            subject=value["subject"],
            operation_sha256=value["operationSha256"],
        )


def _agentcore_precondition_authority(
    payload: bytes,
) -> _ReceiptAuthorityV2:
    """Extract only the store-owned binding from a canonical precondition."""

    try:
        value = parse_canonical_object(payload)
    except (ContractError, TypeError, ValueError) as error:
        raise EvidenceStoreV2Error(
            "AgentCore hardening precondition is not canonical"
        ) from error
    expected = {
        "schema",
        "receiptAuthority",
        "resolvedRequestSha256",
        "authoritySha256",
        "account",
        "region",
        "runtimeObservationSha256",
        "runtimeObservation",
        "mode",
    }
    if (
        set(value) != expected
        or value.get("schema")
        != "personal-operator.agentcore-hardening-precondition.v1"
        or canonical_json_bytes(value) != payload
    ):
        raise EvidenceStoreV2Error(
            "AgentCore hardening precondition fields are invalid"
        )
    for name in (
        "resolvedRequestSha256",
        "authoritySha256",
        "runtimeObservationSha256",
    ):
        item = value.get(name)
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            raise EvidenceStoreV2Error(
                "AgentCore hardening precondition digest is invalid"
            )
    if (
        not isinstance(value.get("account"), str)
        or re.fullmatch(r"[0-9]{12}", value["account"]) is None
        or value["account"] == "000000000000"
        or value.get("region") != "eu-west-1"
        or value.get("mode") not in {"NOOP", "UPDATE"}
        or not isinstance(value.get("runtimeObservation"), Mapping)
    ):
        raise EvidenceStoreV2Error(
            "AgentCore hardening precondition identity is invalid"
        )
    try:
        authority = _ReceiptAuthorityV2.from_bytes(
            canonical_json_bytes(value.get("receiptAuthority"))
        )
    except (EvidenceStoreV2Error, TypeError, ValueError) as error:
        raise EvidenceStoreV2Error(
            "AgentCore hardening precondition receipt authority is invalid"
        ) from error
    if authority.kind != "agentcore-hardening":
        raise EvidenceStoreV2Error(
            "AgentCore hardening precondition crosses its receipt kind"
        )
    return authority


class _AppendOnlyReceiptBackendV2:
    """Pinned O_EXCL attempt/receipt pair for one exact journal operation."""

    __slots__ = (
        "_store",
        "_authority",
        "_attempt_name",
        "_precondition_name",
        "_receipt_name",
    )

    def __init__(
        self,
        *,
        store: "ReleaseEvidenceStoreV2",
        authority: _ReceiptAuthorityV2,
    ) -> None:
        self._store = store
        self._authority = authority
        operation = authority.operation_sha256.removeprefix("sha256:")
        stem = (
            f"receipt-{authority.kind}-{authority.journal_execution_id}-"
            f"{authority.journal_revision:08d}-{authority.step_id}-{operation}"
        )
        self._attempt_name = f"{stem}-attempt.json"
        self._precondition_name = f"{stem}-precondition.bin"
        self._receipt_name = f"{stem}-receipt.bin"

    @property
    def authority(self) -> _ReceiptAuthorityV2:
        return self._authority

    def binding(self) -> Mapping[str, str]:
        return {
            "evidenceStoreSha256": self._authority.evidence_store_sha256,
            "journalPathSha256": self._authority.journal_path_sha256,
            "journalExecutionId": self._authority.journal_execution_id,
        }

    def _directory(self) -> int:
        return self._store._plan_directory(
            self._authority.release_plan_sha256
        )

    def _read_optional(self, name: str, *, label: str) -> bytes | None:
        try:
            return self._store._read_secure(
                directory_fd=self._directory(), name=name, label=label
            )
        except FileNotFoundError:
            return None

    def load(self) -> tuple[bool, bytes | None]:
        expected = self._authority.to_bytes()
        attempted = self._read_optional(
            self._attempt_name, label="provider receipt attempt"
        )
        if attempted is not None and attempted != expected:
            raise EvidenceStoreV2Error(
                "provider receipt attempt crosses its exact journal operation"
            )
        receipt = self._read_optional(
            self._receipt_name, label="provider dispatch receipt"
        )
        if receipt is not None and attempted is None:
            raise EvidenceStoreV2Error(
                "provider dispatch receipt has no durable prior attempt"
            )
        return attempted is not None, receipt

    def load_precondition(self) -> bytes | None:
        payload = self._read_optional(
            self._precondition_name,
            label="AgentCore hardening precondition",
        )
        if payload is not None:
            authority = _agentcore_precondition_authority(payload)
            if authority != self._authority:
                raise EvidenceStoreV2Error(
                    "AgentCore hardening precondition crosses its journal operation"
                )
        return payload

    def _create_exact(self, name: str, payload: bytes, *, label: str) -> bool:
        if not isinstance(payload, bytes) or not payload:
            raise EvidenceStoreV2Error(f"{label} payload is invalid")
        if len(payload) > _MAX_EVIDENCE_BYTES:
            raise EvidenceStoreV2Error(f"{label} exceeds its size boundary")
        directory_fd = self._directory()
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                _WRITE_MODE,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            existing = self._store._read_secure(
                directory_fd=directory_fd, name=name, label=label
            )
            if existing != payload:
                raise EvidenceStoreV2Error(
                    f"alternate {label} exists for the journal operation"
                )
            return False
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fchmod(descriptor, _RECORD_MODE)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.fsync(directory_fd)
            retained = self._store._read_secure(
                directory_fd=directory_fd, name=name, label=label
            )
            if retained != payload:
                raise EvidenceStoreV2Error(
                    f"{label} retention is not byte-exact"
                )
            return True
        except EvidenceStoreV2Error:
            raise
        except OSError as error:
            raise EvidenceStoreV2Error(
                f"{label} could not be persisted"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def begin_attempt(self) -> bool:
        attempted, retained = self.load()
        if retained is not None:
            return False
        if attempted:
            return False
        if self._authority.kind == "agentcore-hardening":
            if self.load_precondition() is None:
                raise EvidenceStoreV2Error(
                    "AgentCore hardening attempt cannot precede its precondition"
                )
        elif self.load_precondition() is not None:
            raise EvidenceStoreV2Error(
                "non-AgentCore receipt owns an unexpected precondition"
            )
        return self._create_exact(
            self._attempt_name,
            self._authority.to_bytes(),
            label="provider receipt attempt",
        )

    def retain(self, payload: bytes) -> None:
        attempted, retained = self.load()
        if not attempted:
            raise EvidenceStoreV2Error(
                "provider receipt cannot precede durable attempt"
            )
        if retained is not None:
            if retained != payload:
                raise EvidenceStoreV2Error(
                    "alternate provider dispatch receipt exists"
                )
            return
        self._create_exact(
            self._receipt_name,
            payload,
            label="provider dispatch receipt",
        )

    def retain_precondition(self, payload: bytes) -> None:
        if self._authority.kind != "agentcore-hardening":
            raise EvidenceStoreV2Error(
                "provider receipt kind cannot own an AgentCore precondition"
            )
        authority = _agentcore_precondition_authority(payload)
        if authority != self._authority:
            raise EvidenceStoreV2Error(
                "AgentCore hardening precondition crosses its journal operation"
            )
        attempted, retained = self.load()
        existing = self.load_precondition()
        if existing is not None:
            if existing != payload:
                raise EvidenceStoreV2Error(
                    "alternate AgentCore hardening precondition exists"
                )
            return
        if attempted or retained is not None:
            raise EvidenceStoreV2Error(
                "AgentCore hardening precondition cannot follow an attempt"
            )
        self._create_exact(
            self._precondition_name,
            payload,
            label="AgentCore hardening precondition",
        )


@dataclass(slots=True, init=False)
class VerifiedStepOutcomeV2:
    """Single-use authority for one already retained provider outcome."""

    _store: "ReleaseEvidenceStoreV2"
    _record: RetainedStepEvidenceV2
    _record_name: str
    _payload: bytes
    _claimed: bool

    def __init__(
        self,
        *,
        store: "ReleaseEvidenceStoreV2",
        record: RetainedStepEvidenceV2,
        record_name: str,
        payload: bytes,
        _token: object | None = None,
    ) -> None:
        if _token is not _VERIFIED_STEP_OUTCOME_TOKEN:
            raise EvidenceStoreV2Error(
                "verified step outcome is not constructible"
            )
        self._store = store
        self._record = record
        self._record_name = record_name
        self._payload = bytes(payload)
        self._claimed = False

    @property
    def retained_evidence(self) -> RetainedStepEvidenceV2:
        return self._record

    @property
    def disposition(self) -> str:
        return self._record.disposition

    @property
    def step_observation(self) -> ReleaseStepObservationV2 | None:
        return self._record.step_observation

    @property
    def failure_observation(self) -> ReleaseStepFailureObservationV2 | None:
        return self._record.failure_observation

    @property
    def path(self) -> Path:
        return (
            self._store.root
            / self._record.plan_sha256
            / self._record_name
        )

    def _claim(
        self,
        *,
        store: "ReleaseEvidenceStoreV2",
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> RetainedStepEvidenceV2:
        if self._claimed:
            raise EvidenceStoreV2Error("verified step outcome is already consumed")
        if store is not self._store:
            raise EvidenceStoreV2Error("verified step outcome crosses its store")
        store._require_binding(
            journal_path=journal_path,
            plan=plan,
            journal_execution_id=journal_execution_id,
        )
        payload = store._read_plan_record(
            plan_sha256=plan.digest(), name=self._record_name
        )
        if payload != self._payload:
            raise EvidenceStoreV2Error(
                "retained outcome record changed before journal advancement"
            )
        try:
            record = RetainedStepEvidenceV2.from_bytes(payload)
            if record != self._record:
                raise EvidenceStoreV2Error(
                    "retained outcome differs from its verified capability"
                )
            record.validate_transaction(
                plan,
                transaction,
                evidence_store_sha256=store.identity_sha256,
                journal_path_sha256=_journal_path_sha256(journal_path),
                journal_execution_id=journal_execution_id,
            )
        except EvidenceStoreV2Error:
            raise
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(str(error)) from error
        self._claimed = True
        return record


class ReleaseOutcomeComposerV2:
    """Production-only derivation of retained outcomes from canonical reads."""

    def __init__(
        self,
        *,
        store: "ReleaseEvidenceStoreV2",
        plan: ReleasePlanV2,
        journal_path: Path,
        journal_execution_id: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _VERIFIED_STEP_OUTCOME_TOKEN:
            raise EvidenceStoreV2Error("release outcome composer is private")
        self._store = store
        self._plan = _canonical_release_plan_v2(plan)
        self._journal_path = Path(journal_path)
        self._journal_execution_id = journal_execution_id
        store._require_binding(
            journal_path=self._journal_path,
            plan=self._plan,
            journal_execution_id=journal_execution_id,
        )

    def compose(
        self,
        *,
        transaction: StagingTransactionV2,
        provider_observation: object,
    ) -> VerifiedStepOutcomeV2:
        return self._store._compose_outcome(
            plan=self._plan,
            transaction=transaction,
            journal_path=self._journal_path,
            journal_execution_id=self._journal_execution_id,
            provider_observation=provider_observation,
        )


class ReleaseEvidenceStoreV2:
    """Pinned append-only namespace for journal bindings and outcome records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._root_fd = -1
        self._plan_directories: dict[str, tuple[int, int, int]] = {}
        try:
            self.root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            self._root_fd = os.open(self.root, _DIRECTORY_FLAGS)
            details = os.fstat(self._root_fd)
            _require_directory(details, label="evidence root")
            self._root_device = details.st_dev
            self._root_inode = details.st_ino
            self.identity_sha256 = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema": "personal-operator.release-evidence-store.v2",
                        "pathSha256": _journal_path_sha256(self.root),
                        "device": details.st_dev,
                        "inode": details.st_ino,
                        "owner": details.st_uid,
                    }
                )
            ).hexdigest()
        except (ContractError, OSError, TypeError, ValueError) as error:
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1
            raise EvidenceStoreV2Error("evidence root is invalid") from error

    def close(self) -> None:
        for descriptor, _, _ in self._plan_directories.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._plan_directories.clear()
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> "ReleaseEvidenceStoreV2":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass

    def _assert_root_identity(self) -> None:
        if self._root_fd < 0:
            raise EvidenceStoreV2Error("evidence store is closed")
        retained = os.fstat(self._root_fd)
        _require_directory(retained, label="evidence root")
        try:
            current = os.stat(self.root, follow_symlinks=False)
        except OSError as error:
            raise EvidenceStoreV2Error("evidence root was replaced") from error
        _require_directory(current, label="evidence root")
        if (retained.st_dev, retained.st_ino) != (
            current.st_dev,
            current.st_ino,
        ) or (retained.st_dev, retained.st_ino) != (
            self._root_device,
            self._root_inode,
        ):
            raise EvidenceStoreV2Error("evidence root directory identity changed")

    def _plan_directory(self, plan_sha256: str) -> int:
        self._assert_root_identity()
        cached = self._plan_directories.get(plan_sha256)
        if cached is None:
            created = False
            try:
                os.mkdir(
                    plan_sha256,
                    mode=_DIRECTORY_MODE,
                    dir_fd=self._root_fd,
                )
                created = True
            except FileExistsError:
                pass
            descriptor = os.open(
                plan_sha256, _DIRECTORY_FLAGS, dir_fd=self._root_fd
            )
            details = os.fstat(descriptor)
            try:
                _require_directory(details, label="plan evidence directory")
            except EvidenceStoreV2Error:
                os.close(descriptor)
                raise
            cached = (descriptor, details.st_dev, details.st_ino)
            self._plan_directories[plan_sha256] = cached
            if created:
                os.fsync(self._root_fd)
        descriptor, device, inode = cached
        retained = os.fstat(descriptor)
        _require_directory(retained, label="plan evidence directory")
        current = os.stat(
            plan_sha256, dir_fd=self._root_fd, follow_symlinks=False
        )
        _require_directory(current, label="plan evidence directory")
        if (retained.st_dev, retained.st_ino) != (device, inode) or (
            current.st_dev,
            current.st_ino,
        ) != (device, inode):
            raise EvidenceStoreV2Error(
                "plan evidence directory identity changed"
            )
        return descriptor

    @staticmethod
    def _read_secure(
        *, directory_fd: int, name: str, label: str
    ) -> bytes:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        try:
            _require_record(os.fstat(descriptor), label=label)
            return _read_all(descriptor, label=label)
        finally:
            os.close(descriptor)

    @staticmethod
    def _append_secure(
        *,
        directory_fd: int,
        name: str,
        payload: bytes,
        label: str,
        reuse_identical: bool,
    ) -> None:
        try:
            existing = ReleaseEvidenceStoreV2._read_secure(
                directory_fd=directory_fd,
                name=name,
                label=label,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not reuse_identical or existing != payload:
                raise EvidenceStoreV2Error(
                    f"alternate {label} exists for the canonical operation"
                )
            return

        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        linked = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                _WRITE_MODE,
                dir_fd=directory_fd,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fchmod(descriptor, _RECORD_MODE)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                linked = True
            except FileExistsError:
                existing = ReleaseEvidenceStoreV2._read_secure(
                    directory_fd=directory_fd,
                    name=name,
                    label=label,
                )
                if not reuse_identical or existing != payload:
                    raise EvidenceStoreV2Error(
                        f"concurrent alternate {label} exists"
                    )
            os.fsync(directory_fd)
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
            if linked:
                ReleaseEvidenceStoreV2._read_secure(
                    directory_fd=directory_fd,
                    name=name,
                    label=label,
                )
        except EvidenceStoreV2Error:
            raise
        except OSError as error:
            raise EvidenceStoreV2Error(f"{label} could not be persisted") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def _binding_name(self, journal_path: Path) -> str:
        return f"journal-{_journal_path_sha256(journal_path)}.json"

    def bind_new_journal(
        self,
        *,
        journal_path: Path,
        plan: ReleasePlanV2,
        initial_transaction: StagingTransactionV2,
    ) -> str:
        self._assert_root_identity()
        canonical_plan = _canonical_release_plan_v2(plan)
        initial = StagingTransactionV2.from_bytes(
            initial_transaction.to_bytes(), plan=canonical_plan
        )
        if initial.state != "NEW" or initial.revision != 0:
            raise EvidenceStoreV2Error(
                "journal binding requires the exact NEW revision"
            )
        execution_id = secrets.token_hex(32)
        payload = canonical_json_bytes(
            {
                "schema": "personal-operator.release-journal-binding.v2",
                "evidenceStoreSha256": self.identity_sha256,
                "journalPathSha256": _journal_path_sha256(journal_path),
                "journalExecutionId": execution_id,
                "planSha256": canonical_plan.digest(),
                "initialJournalSha256": hashlib.sha256(
                    initial.to_bytes()
                ).hexdigest(),
            }
        )
        self._append_secure(
            directory_fd=self._root_fd,
            name=self._binding_name(journal_path),
            payload=payload,
            label="journal binding",
            reuse_identical=False,
        )
        return execution_id

    def _binding(self, *, journal_path: Path) -> dict[str, Any]:
        self._assert_root_identity()
        try:
            payload = self._read_secure(
                directory_fd=self._root_fd,
                name=self._binding_name(journal_path),
                label="journal binding",
            )
            value = parse_canonical_object(payload)
        except (ContractError, OSError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "journal has no valid evidence-store binding"
            ) from error
        expected = {
            "schema",
            "evidenceStoreSha256",
            "journalPathSha256",
            "journalExecutionId",
            "planSha256",
            "initialJournalSha256",
        }
        if set(value) != expected:
            raise EvidenceStoreV2Error("journal binding fields are invalid")
        return value

    def bind_existing_journal(
        self, *, journal_path: Path, plan: ReleasePlanV2
    ) -> str:
        value = self._binding(journal_path=journal_path)
        canonical_plan = _canonical_release_plan_v2(plan)
        if (
            value["schema"]
            != "personal-operator.release-journal-binding.v2"
            or value["evidenceStoreSha256"] != self.identity_sha256
            or value["journalPathSha256"]
            != _journal_path_sha256(journal_path)
            or value["planSha256"] != canonical_plan.digest()
            or not isinstance(value["initialJournalSha256"], str)
            or len(value["initialJournalSha256"]) != 64
            or not isinstance(value["journalExecutionId"], str)
            or len(value["journalExecutionId"]) != 64
        ):
            raise EvidenceStoreV2Error("journal evidence binding differs")
        return value["journalExecutionId"]

    def _require_binding(
        self,
        *,
        journal_path: Path,
        plan: ReleasePlanV2,
        journal_execution_id: str,
    ) -> None:
        observed = self.bind_existing_journal(
            journal_path=journal_path, plan=plan
        )
        if observed != journal_execution_id:
            raise EvidenceStoreV2Error("journal execution identity differs")

    def composer(
        self,
        *,
        plan: ReleasePlanV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> ReleaseOutcomeComposerV2:
        return ReleaseOutcomeComposerV2(
            store=self,
            plan=plan,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
            _token=_VERIFIED_STEP_OUTCOME_TOKEN,
        )

    @staticmethod
    def _dispatch_attempt_name(
        *, journal_execution_id: str, journal_revision: int, operation: str
    ) -> str:
        return (
            f"dispatch-{journal_execution_id}-{journal_revision:08d}-"
            f"{operation}.json"
        )

    def _dispatch_attempt_identity(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> tuple[
        ReleasePlanV2,
        StagingTransactionV2,
        Any,
        str,
        bytes,
    ]:
        try:
            canonical_plan = _canonical_release_plan_v2(plan)
            current = StagingTransactionV2.from_bytes(
                transaction.to_bytes(), plan=canonical_plan
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "dispatch attempt journal identity is invalid"
            ) from error
        expected_journal = current.to_bytes()
        try:
            if read_regular_bytes(Path(journal_path)) != expected_journal:
                raise EvidenceStoreV2Error(
                    "dispatch attempt transaction is not the current journal"
                )
            self._require_binding(
                journal_path=Path(journal_path),
                plan=canonical_plan,
                journal_execution_id=journal_execution_id,
            )
            self.audit_prefix(
                plan=canonical_plan,
                transaction=current,
                journal_path=Path(journal_path),
                journal_execution_id=journal_execution_id,
            )
        except EvidenceStoreV2Error:
            raise
        except (ContractError, OSError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "dispatch attempt journal identity is invalid"
            ) from error
        count = current.completed_step_count
        if count >= len(canonical_plan.steps):
            raise EvidenceStoreV2Error(
                "dispatch attempt has no exact next plan step"
            )
        step = canonical_plan.steps[count]
        operation = _release_operation_sha256(
            canonical_plan.digest(),
            step,
            _completed_prefix_sha256(
                [item.to_mapping() for item in current.completed_steps]
            ),
        )
        if (
            not step.mutation
            or step.kind not in _MUTATION_PROVIDERS
            or current.state != "UNCERTAIN"
            or current.uncertain_step_id != step.step_id
            or current.uncertain_operation_sha256 != operation
        ):
            raise EvidenceStoreV2Error(
                "dispatch attempt lacks exact write-ahead journal intent"
            )
        return canonical_plan, current, step, operation, expected_journal

    def dispatch_attempt_state(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> DispatchAttemptStateV1:
        """Read the exact current marker without ever minting authority."""

        (
            canonical_plan,
            current,
            step,
            operation,
            expected_journal,
        ) = self._dispatch_attempt_identity(
            plan=plan,
            transaction=transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        name = self._dispatch_attempt_name(
            journal_execution_id=journal_execution_id,
            journal_revision=current.revision,
            operation=operation,
        )
        attempt = self._all_dispatch_attempts(
            plan_sha256=canonical_plan.digest()
        ).get(name)
        if attempt is None:
            state = DispatchAttemptStateV1.absent()
        else:
            prefix = _completed_prefix_sha256(
                [item.to_mapping() for item in current.completed_steps]
            )
            if (
                attempt.release_plan_sha256 != canonical_plan.digest()
                or attempt.evidence_store_sha256 != self.identity_sha256
                or attempt.journal_path_sha256
                != _journal_path_sha256(Path(journal_path))
                or attempt.journal_execution_id != journal_execution_id
                or attempt.journal_revision != current.revision
                or attempt.completed_prefix_sha256 != prefix
                or attempt.step_id != step.step_id
                or attempt.subject != step.subject
                or attempt.operation_sha256 != operation
                or attempt.provider != _MUTATION_PROVIDERS[step.kind]
            ):
                raise EvidenceStoreV2Error(
                    "release dispatch attempt crosses its journal operation"
                )
            state = DispatchAttemptStateV1.retained(attempt)
        if read_regular_bytes(Path(journal_path)) != expected_journal:
            raise EvidenceStoreV2Error(
                "dispatch attempt transaction changed while reading"
            )
        return state

    def arm_current_dispatch(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
        resolved_request: ResolvedMutationRequestV2,
        provider: str,
    ) -> FreshDispatchAuthorityV1:
        """Durably append one marker and mint authority only to its creator."""

        (
            canonical_plan,
            current,
            step,
            operation,
            expected_journal,
        ) = self._dispatch_attempt_identity(
            plan=plan,
            transaction=transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        expected_provider = _MUTATION_PROVIDERS[step.kind]
        if provider != expected_provider:
            raise EvidenceStoreV2Error(
                "dispatch provider differs from the closed step route"
            )
        try:
            if type(resolved_request) is not ResolvedMutationRequestV2:
                raise EvidenceStoreV2Error(
                    "resolved dispatch request has the wrong concrete type"
                )
            resolved = ResolvedMutationRequestV2.from_bytes(
                resolved_request.to_bytes()
            )
            resolved.validate_transaction(canonical_plan, current)
        except EvidenceStoreV2Error:
            raise
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "resolved dispatch request is invalid"
            ) from error
        if (
            resolved.mutation_request.step_id != step.step_id
            or resolved.mutation_request.subject != step.subject
            or resolved.mutation_request.operation_sha256 != operation
        ):
            raise EvidenceStoreV2Error(
                "resolved dispatch request crosses the journal operation"
            )
        existing = self.dispatch_attempt_state(
            plan=canonical_plan,
            transaction=current,
            journal_path=Path(journal_path),
            journal_execution_id=journal_execution_id,
        )
        if existing.attempted:
            raise EvidenceStoreV2Error(
                "release dispatch attempt is already armed"
            )
        attempt = ReleaseDispatchAttemptV1(
            release_plan_sha256=canonical_plan.digest(),
            evidence_store_sha256=self.identity_sha256,
            journal_path_sha256=_journal_path_sha256(Path(journal_path)),
            journal_execution_id=journal_execution_id,
            journal_revision=current.revision,
            completed_prefix_sha256=_completed_prefix_sha256(
                [item.to_mapping() for item in current.completed_steps]
            ),
            step_id=step.step_id,
            subject=step.subject,
            operation_sha256=operation,
            resolved_request_sha256=resolved.digest(),
            provider=provider,
        )
        name = self._dispatch_attempt_name(
            journal_execution_id=journal_execution_id,
            journal_revision=current.revision,
            operation=operation,
        )
        try:
            self._append_secure(
                directory_fd=self._plan_directory(canonical_plan.digest()),
                name=name,
                payload=attempt.to_bytes(),
                label="release dispatch attempt",
                reuse_identical=False,
            )
        except EvidenceStoreV2Error as error:
            try:
                raced = self.dispatch_attempt_state(
                    plan=canonical_plan,
                    transaction=current,
                    journal_path=Path(journal_path),
                    journal_execution_id=journal_execution_id,
                )
            except EvidenceStoreV2Error:
                raise error
            if raced.attempted:
                raise EvidenceStoreV2Error(
                    "release dispatch attempt is already armed"
                ) from error
            raise
        retained = self.dispatch_attempt_state(
            plan=canonical_plan,
            transaction=current,
            journal_path=Path(journal_path),
            journal_execution_id=journal_execution_id,
        )
        if retained.attempt != attempt:
            raise EvidenceStoreV2Error(
                "retained release dispatch attempt differs"
            )
        if read_regular_bytes(Path(journal_path)) != expected_journal:
            raise EvidenceStoreV2Error(
                "dispatch attempt journal changed after retention"
            )
        return _mint_fresh_dispatch_authority(attempt)

    def _receipt_authority(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
        expected_kind: str,
        receipt_kind: str,
    ) -> tuple[ReleasePlanV2, StagingTransactionV2, _ReceiptAuthorityV2]:
        try:
            canonical_plan = _canonical_release_plan_v2(plan)
            current = StagingTransactionV2.from_bytes(
                transaction.to_bytes(), plan=canonical_plan
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "provider receipt authority is not canonical"
            ) from error
        self._require_binding(
            journal_path=journal_path,
            plan=canonical_plan,
            journal_execution_id=journal_execution_id,
        )
        self.audit_prefix(
            plan=canonical_plan,
            transaction=current,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        count = current.completed_step_count
        if count >= len(canonical_plan.steps):
            raise EvidenceStoreV2Error(
                "provider receipt has no exact next plan step"
            )
        step = canonical_plan.steps[count]
        prefix = _completed_prefix_sha256(
            [item.to_mapping() for item in current.completed_steps]
        )
        release_operation = _release_operation_sha256(
            canonical_plan.digest(), step, prefix
        )
        if (
            step.kind != expected_kind
            or current.state != "UNCERTAIN"
            or current.uncertain_step_id != step.step_id
            or current.uncertain_operation_sha256 != release_operation
        ):
            raise EvidenceStoreV2Error(
                "provider receipt lacks its exact write-ahead journal intent"
            )
        authority = _ReceiptAuthorityV2(
            kind=receipt_kind,
            release_plan_sha256=canonical_plan.digest(),
            evidence_store_sha256=self.identity_sha256,
            journal_path_sha256=_journal_path_sha256(journal_path),
            journal_execution_id=journal_execution_id,
            journal_revision=current.revision,
            completed_prefix_sha256=prefix,
            step_id=step.step_id,
            subject=step.subject,
            operation_sha256=release_operation,
        )
        # Canonically round-trip the exact bytes before they become a durable
        # mutation-attempt authority.
        if _ReceiptAuthorityV2.from_bytes(authority.to_bytes()) != authority:
            raise EvidenceStoreV2Error(
                "provider receipt authority changed during canonicalization"
            )
        return canonical_plan, current, authority

    def stack_drift_receipt_sink(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> object:
        """Mint the stack-drift package's sink from the pinned store only."""

        from release_tools.stack_drift_v2 import (  # noqa: PLC0415
            _new_stack_drift_receipt_sink,
        )

        canonical_plan, current, authority = self._receipt_authority(
            plan=plan,
            transaction=transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
            expected_kind="STACK_DRIFT_CHECK",
            receipt_kind="stack-drift",
        )
        if not current.completed_steps:
            raise EvidenceStoreV2Error(
                "stack drift receipt has no retained predecessor"
            )
        tail_digest = current.completed_steps[-1].evidence_sha256
        candidates = [
            record
            for record in self._journal_records(
                plan_sha256=canonical_plan.digest(),
                journal_path_sha256=authority.journal_path_sha256,
                journal_execution_id=journal_execution_id,
            )
            if record.digest() == tail_digest
        ]
        if len(candidates) != 1:
            raise EvidenceStoreV2Error(
                "stack drift receipt predecessor is missing or ambiguous"
            )
        backend = _AppendOnlyReceiptBackendV2(
            store=self, authority=authority
        )
        try:
            return _new_stack_drift_receipt_sink(
                backend,
                transaction=current,
                predecessor_evidence=candidates[0],
            )
        except Exception as error:
            raise EvidenceStoreV2Error(
                "stack drift receipt sink authority is invalid"
            ) from error

    def agentcore_hardening_receipt_sink(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> object:
        """Mint the AgentCore hardening sink from exact journal authority."""

        from release_tools.agentcore_hardening_v2 import (  # noqa: PLC0415
            _new_agentcore_hardening_receipt_sink,
        )

        canonical_plan, current, authority = self._receipt_authority(
            plan=plan,
            transaction=transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
            expected_kind="AGENTCORE_HARDEN",
            receipt_kind="agentcore-hardening",
        )
        backend = _AppendOnlyReceiptBackendV2(
            store=self, authority=authority
        )
        try:
            return _new_agentcore_hardening_receipt_sink(
                backend,
                release_plan=canonical_plan,
                transaction=current,
                evidence_store_sha256=self.identity_sha256,
                journal_path_sha256=authority.journal_path_sha256,
                journal_execution_id=journal_execution_id,
            )
        except Exception as error:
            raise EvidenceStoreV2Error(
                "AgentCore hardening receipt sink authority is invalid"
            ) from error

    @staticmethod
    def _record_name(record: RetainedStepEvidenceV2) -> str:
        operation = record.operation_sha256.removeprefix("sha256:")
        return f"{record.journal_revision:08d}-{record.step_id}-{operation}.json"

    def _read_plan_record(self, *, plan_sha256: str, name: str) -> bytes:
        directory_fd = self._plan_directory(plan_sha256)
        try:
            return self._read_secure(
                directory_fd=directory_fd,
                name=name,
                label="retained outcome record",
            )
        except (OSError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "retained outcome record is missing or invalid"
            ) from error

    def _retain_record(
        self,
        record: RetainedStepEvidenceV2,
        *,
        _token: object | None = None,
    ) -> VerifiedStepOutcomeV2:
        if _token is not _VERIFIED_STEP_OUTCOME_TOKEN:
            raise EvidenceStoreV2Error(
                "retained outcomes are composer-owned"
            )
        canonical = RetainedStepEvidenceV2.from_bytes(record.to_bytes())
        payload = canonical.to_bytes()
        name = self._record_name(canonical)
        directory_fd = self._plan_directory(canonical.plan_sha256)
        self._append_secure(
            directory_fd=directory_fd,
            name=name,
            payload=payload,
            label="retained outcome record",
            reuse_identical=True,
        )
        return VerifiedStepOutcomeV2(
            store=self,
            record=canonical,
            record_name=name,
            payload=payload,
            _token=_VERIFIED_STEP_OUTCOME_TOKEN,
        )

    @staticmethod
    def _journal_sha256(transaction: StagingTransactionV2) -> str:
        return hashlib.sha256(transaction.to_bytes()).hexdigest()

    @staticmethod
    def _transition_name(*, execution_id: str, from_revision: int) -> str:
        return f"transition-{execution_id}-{from_revision:08d}.json"

    @staticmethod
    def _commit_name(*, execution_id: str, from_revision: int) -> str:
        return f"commit-{execution_id}-{from_revision:08d}.json"

    def _journal_records(
        self,
        *,
        plan_sha256: str,
        journal_path_sha256: str,
        journal_execution_id: str,
    ) -> list[RetainedStepEvidenceV2]:
        return [
            record
            for record in self._all_records(plan_sha256=plan_sha256)
            if record.evidence_store_sha256 == self.identity_sha256
            and record.journal_path_sha256 == journal_path_sha256
            and record.journal_execution_id == journal_execution_id
        ]

    def _outcome_for_transition(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path_sha256: str,
        journal_execution_id: str,
        records: list[RetainedStepEvidenceV2] | None = None,
    ) -> RetainedStepEvidenceV2:
        candidates = []
        records = records or self._journal_records(
            plan_sha256=plan.digest(),
            journal_path_sha256=journal_path_sha256,
            journal_execution_id=journal_execution_id,
        )
        for record in records:
            if record.journal_revision != transaction.revision:
                continue
            if transaction.completed_step_count >= len(plan.steps):
                continue
            step = plan.steps[transaction.completed_step_count]
            prefix = _completed_prefix_sha256(
                [item.to_mapping() for item in transaction.completed_steps]
            )
            release_operation = (
                transaction.uncertain_operation_sha256
                if step.mutation
                else _release_operation_sha256(plan.digest(), step, prefix)
            )
            expected_operation = _release_outcome_operation_sha256(
                release_operation_sha256=release_operation,
                journal_path_sha256=journal_path_sha256,
                journal_execution_id=journal_execution_id,
                journal_revision=transaction.revision,
            )
            if (
                record.plan_sha256 != plan.digest()
                or record.completed_prefix_sha256 != prefix
                or record.evidence_store_sha256 != self.identity_sha256
                or record.journal_path_sha256 != journal_path_sha256
                or record.journal_execution_id != journal_execution_id
                or record.step_id != step.step_id
                or record.subject != step.subject
                or record.release_operation_sha256 != release_operation
                or record.operation_sha256 != expected_operation
            ):
                continue
            candidates.append(record)
        if len(candidates) != 1:
            raise EvidenceStoreV2Error(
                "journal outcome transition lacks one exact retained record"
            )
        return candidates[0]

    @staticmethod
    def _expected_present_transaction(
        *,
        plan: ReleasePlanV2,
        prior: StagingTransactionV2,
        record: RetainedStepEvidenceV2,
    ) -> dict[str, Any]:
        observation = record.step_observation
        if observation is None:
            raise EvidenceStoreV2Error(
                "PRESENT journal transition lacks a typed observation"
            )
        count = prior.completed_step_count
        step = plan.steps[count]
        next_index = count + 1
        phase_complete = (
            next_index == len(plan.steps)
            or plan.steps[next_index].phase != step.phase
        )
        mapping = prior.to_mapping()
        completed = list(mapping["completedSteps"])
        completed.append(
            {"stepId": step.step_id, "evidenceSha256": record.digest()}
        )
        mapping.update(
            {
                "completedStepCount": next_index,
                "completedSteps": completed,
                "state": (
                    RELEASE_V2_PHASE_STATES[step.phase]
                    if phase_complete
                    else prior.last_stable_state
                ),
                "lastStableState": (
                    RELEASE_V2_PHASE_STATES[step.phase]
                    if phase_complete
                    else prior.last_stable_state
                ),
                "uncertainStepId": "",
                "uncertainOperationSha256": "",
                "revision": prior.revision + 1,
            }
        )
        if step.kind == "BASELINE_OBSERVE":
            mapping["rollbackBaselineSha256"] = record.digest()
        if observation.foundation_runtime_inputs is not None:
            mapping["foundationInputsSha256"] = (
                observation.foundation_runtime_inputs.digest()
            )
            mapping["agentCoreStackId"] = (
                observation.foundation_runtime_inputs.agent_core_stack_id
            )
        observed_values = {
            "agentCoreStackId": observation.agent_core_stack_id,
            "runtimeImageDigest": observation.runtime_image_digest,
            "runtimeId": observation.runtime_id,
            "runtimeVersion": observation.runtime_version,
            "runtimeArn": observation.runtime_arn,
            "runtimeEndpointId": observation.runtime_endpoint_id,
            "runtimeContextSha256": observation.runtime_context_sha256,
            "routerTargetStackId": observation.router_target_stack_id,
            "routerChangeSetId": observation.router_change_set_id,
            "cronTargetStackId": observation.cron_target_stack_id,
            "cronChangeSetId": observation.cron_change_set_id,
            "routerCronChangesetsSha256": (
                observation.router_cron_changesets_sha256
            ),
            "routerCronApplicationSha256": (
                observation.router_cron_application_sha256
            ),
            "schedulerChangesetSha256": (
                observation.scheduler_changeset_sha256
            ),
            "schedulerTargetStackId": observation.scheduler_target_stack_id,
            "schedulerChangeSetId": observation.scheduler_change_set_id,
            "schedulerApplicationSha256": (
                observation.scheduler_application_sha256
            ),
            "webTargetStackId": observation.web_target_stack_id,
            "webChangeSetId": observation.web_change_set_id,
            "webChangesetSha256": observation.web_changeset_sha256,
            "webApplicationSha256": observation.web_application_sha256,
            "verificationSha256": observation.verification_sha256,
        }
        mapping.update(
            {name: value for name, value in observed_values.items() if value}
        )
        return mapping

    @staticmethod
    def _expected_failed_transaction(
        *,
        plan: ReleasePlanV2,
        prior: StagingTransactionV2,
        record: RetainedStepEvidenceV2,
    ) -> dict[str, Any]:
        failure = record.failure_observation
        if failure is None:
            raise EvidenceStoreV2Error(
                "failure journal transition lacks a typed observation"
            )
        mapping = prior.to_mapping()
        mapping.update(
            {
                "state": "ABORTED_RETAINED",
                "abortEvidenceSha256": "",
                "failedRetainedEvidenceSha256": record.digest(),
                "failureObservationSha256": failure.digest(),
                "failedStepId": failure.step_id,
                "failedSubject": failure.subject,
                "failedOperationSha256": failure.operation_sha256,
                "failureReason": failure.failure_reason,
                "uncertainStepId": "",
                "uncertainOperationSha256": "",
                "revision": prior.revision + 1,
            }
        )
        return mapping

    def _classify_transition(
        self,
        *,
        plan: ReleasePlanV2,
        prior: StagingTransactionV2,
        next_transaction: StagingTransactionV2,
        journal_path_sha256: str,
        journal_execution_id: str,
        outcome_records: list[RetainedStepEvidenceV2] | None = None,
    ) -> tuple[str, str]:
        if next_transaction.revision != prior.revision + 1:
            raise EvidenceStoreV2Error(
                "journal transition must advance exactly one revision"
            )
        prior_mapping = prior.to_mapping()
        if prior.state == "NEW":
            expected = dict(prior_mapping)
            expected.update(
                {
                    "state": "PREFLIGHTED",
                    "lastStableState": "PREFLIGHTED",
                    "revision": prior.revision + 1,
                }
            )
            if next_transaction.to_mapping() != expected:
                raise EvidenceStoreV2Error(
                    "preflight journal transition is not exact"
                )
            return "PREFLIGHT", ""

        count = prior.completed_step_count
        if count >= len(plan.steps):
            raise EvidenceStoreV2Error(
                "terminal journal cannot authorize another transition"
            )
        step = plan.steps[count]
        if prior.state != "UNCERTAIN" and next_transaction.state == "UNCERTAIN":
            if not step.mutation:
                raise EvidenceStoreV2Error(
                    "read-only step cannot create mutation intent"
                )
            expected = dict(prior_mapping)
            expected.update(
                {
                    "state": "UNCERTAIN",
                    "uncertainStepId": step.step_id,
                    "uncertainOperationSha256": _release_operation_sha256(
                        plan.digest(),
                        step,
                        _completed_prefix_sha256(
                            [
                                item.to_mapping()
                                for item in prior.completed_steps
                            ]
                        ),
                    ),
                    "revision": prior.revision + 1,
                }
            )
            if next_transaction.to_mapping() != expected:
                raise EvidenceStoreV2Error(
                    "mutation-intent journal transition is not exact"
                )
            return "MUTATION_INTENT", ""

        if (
            next_transaction.state == "ABORTED_RETAINED"
            and next_transaction.abort_evidence_sha256
        ):
            expected = dict(prior_mapping)
            expected.update(
                {
                    "state": "ABORTED_RETAINED",
                    "abortEvidenceSha256": (
                        next_transaction.abort_evidence_sha256
                    ),
                    "revision": prior.revision + 1,
                }
            )
            if next_transaction.to_mapping() != expected:
                raise EvidenceStoreV2Error(
                    "retained-abort journal transition is not exact"
                )
            return "ABORT_RETAINED", next_transaction.abort_evidence_sha256

        record = self._outcome_for_transition(
            plan=plan,
            transaction=prior,
            journal_path_sha256=journal_path_sha256,
            journal_execution_id=journal_execution_id,
            records=outcome_records,
        )
        kind = f"OUTCOME_{record.disposition}"
        if record.disposition == "PRESENT":
            expected_mapping = self._expected_present_transaction(
                plan=plan, prior=prior, record=record
            )
        elif record.disposition == "FAILED_RETAINED":
            expected_mapping = self._expected_failed_transaction(
                plan=plan, prior=prior, record=record
            )
        elif record.disposition in {"ABSENT", "PENDING"}:
            expected = dict(prior_mapping)
            if record.disposition == "ABSENT" and step.mutation:
                expected.update(
                    {
                        "state": prior.last_stable_state,
                        "uncertainStepId": "",
                        "uncertainOperationSha256": "",
                    }
                )
            expected["revision"] = prior.revision + 1
            expected_mapping = expected
        else:
            raise EvidenceStoreV2Error(
                "journal transition has an unsupported disposition"
            )
        if next_transaction.to_mapping() != expected_mapping:
            raise EvidenceStoreV2Error(
                "retained-outcome journal transition is not exact"
            )
        return kind, record.digest()

    @staticmethod
    def _release_operation(
        *, plan: ReleasePlanV2, transaction: StagingTransactionV2
    ) -> str:
        count = transaction.completed_step_count
        if count >= len(plan.steps):
            raise EvidenceStoreV2Error("outcome has no exact next plan step")
        step = plan.steps[count]
        prefix = _completed_prefix_sha256(
            [item.to_mapping() for item in transaction.completed_steps]
        )
        expected = _release_operation_sha256(plan.digest(), step, prefix)
        if step.mutation:
            if (
                transaction.state != "UNCERTAIN"
                or transaction.uncertain_step_id != step.step_id
                or transaction.uncertain_operation_sha256 != expected
            ):
                raise EvidenceStoreV2Error(
                    "mutation outcome lacks exact write-ahead intent"
                )
            return transaction.uncertain_operation_sha256
        if transaction.state in {
            "NEW",
            "UNCERTAIN",
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            raise EvidenceStoreV2Error(
                "read-only outcome has no stable next plan step"
            )
        return expected

    @staticmethod
    def _allowed_provider_pairs(
        *, phase: str, kind: str, subject: str, disposition: str
    ) -> frozenset[tuple[str, str]]:
        if kind == "BASELINE_OBSERVE":
            return frozenset({("cloudformation", "describe_stacks")})
        if kind == "ASSET_PUBLISH":
            return frozenset({("s3", "head_object")})
        if kind in {"BOOTSTRAP_STACK", "STACK_CREATE"}:
            return frozenset({("cloudformation", "describe_stacks")})
        if kind == "STACK_DRIFT_CHECK":
            return frozenset(
                {
                    (
                        "cloudformation",
                        "describe_stack_drift_detection_status",
                    )
                }
            )
        if kind == "IMAGE_PUBLISH":
            operation = (
                "batch_check_layer_availability"
                if ":blob:" in subject
                else "batch_get_image"
            )
            return frozenset({("ecr", operation)})
        if kind == "IMAGE_OBSERVE":
            return frozenset({("ecr", "describe_image_scan_findings")})
        if kind == "CHANGESET_CREATE":
            return frozenset({("cloudformation", "describe_change_set")})
        if kind == "CHANGESET_EXECUTE":
            pairs = {("cloudformation", "describe_change_set")}
            if disposition in {"PENDING", "PRESENT", "FAILED_RETAINED"}:
                pairs.add(("cloudformation", "describe_stacks"))
            return frozenset(pairs)
        if phase == "runtime" and kind == "STACK_UPDATE":
            return frozenset(
                {
                    ("cloudformation", "describe_stacks"),
                    ("bedrock-agentcore-control", "get_agent_runtime"),
                }
            )
        if phase == "endpoint" and kind == "STACK_UPDATE":
            if disposition == "PRESENT":
                return frozenset(
                    {
                        (
                            "bedrock-agentcore-control",
                            "get_agent_runtime_endpoint",
                        )
                    }
                )
            return frozenset(
                {
                    ("cloudformation", "describe_stacks"),
                    (
                        "bedrock-agentcore-control",
                        "get_agent_runtime_endpoint",
                    ),
                }
            )
        if kind == "STACK_UPDATE":
            return frozenset({("cloudformation", "describe_stacks")})
        if kind == "AGENTCORE_HARDEN":
            return frozenset(
                {("bedrock-agentcore-control", "get_agent_runtime")}
            )
        if kind == "RUNTIME_CONTEXT_WRITE":
            return frozenset(
                {("local-filesystem", "read_runtime_context")}
            )
        if phase == "verify" and kind == "VERIFY":
            return frozenset(
                {("local-release-verifier", "verify_release")}
            )
        return frozenset()

    @staticmethod
    def _empty_step_observation(
        *,
        plan: ReleasePlanV2,
        step: object,
        observer_evidence_sha256: str,
    ) -> dict[str, Any]:
        return {
            "schema": ReleaseStepObservationV2.SCHEMA,
            "planSha256": plan.digest(),
            "stepId": step.step_id,
            "subject": step.subject,
            "observerEvidenceSha256": observer_evidence_sha256,
            "foundationRuntimeInputs": {},
            "agentCoreStackId": "",
            "runtimeImageDigest": "",
            "runtimeId": "",
            "runtimeVersion": "",
            "runtimeArn": "",
            "runtimeEndpointId": "",
            "runtimeContextSha256": "",
            "routerTargetStackId": "",
            "routerChangeSetId": "",
            "cronTargetStackId": "",
            "cronChangeSetId": "",
            "routerCronChangesetsSha256": "",
            "routerCronApplicationSha256": "",
            "schedulerTargetStackId": "",
            "schedulerChangeSetId": "",
            "schedulerChangesetSha256": "",
            "schedulerApplicationSha256": "",
            "webTargetStackId": "",
            "webChangeSetId": "",
            "webChangesetSha256": "",
            "webApplicationSha256": "",
            "verificationSha256": "",
        }

    @staticmethod
    def _validate_present_projection(
        *, step: object, projection: Mapping[str, Any]
    ) -> None:
        if step.kind == "ASSET_PUBLISH":
            if (
                projection.get("assetId") != step.subject.removeprefix("cdk:asset:")
                or projection.get("contentSha256") != step.expected_content_sha256
            ):
                raise EvidenceStoreV2Error(
                    "asset provider projection differs from the plan"
                )
        elif step.kind == "IMAGE_PUBLISH":
            expected = step.expected_content_sha256
            observed = str(projection.get("digest", "")).removeprefix("sha256:")
            if observed != expected.removeprefix("sha256:"):
                raise EvidenceStoreV2Error(
                    "image provider projection differs from the plan"
                )
        elif step.kind in {"BOOTSTRAP_STACK", "STACK_CREATE"}:
            if (
                projection.get("templateParameterSha256")
                != step.expected_template_parameter_sha256
                or projection.get("observedRequestSha256")
                != step.expected_observed_request_sha256
            ):
                raise EvidenceStoreV2Error(
                    "CloudFormation provider projection differs from the plan"
                )
        elif step.kind == "IMAGE_OBSERVE":
            observed = projection.get("runtimeImageDigest")
            if (
                not isinstance(observed, str)
                or observed.removeprefix("sha256:")
                != step.expected_content_sha256.removeprefix("sha256:")
            ):
                raise EvidenceStoreV2Error(
                    "aggregate image observation differs from the plan"
                )
        elif step.kind == "RUNTIME_CONTEXT_WRITE":
            expected = projection.get("expectedRuntimeContextSha256")
            observed = projection.get("observedRuntimeContextSha256")
            if (
                not isinstance(expected, str)
                or len(expected) != 64
                or observed != expected
            ):
                raise EvidenceStoreV2Error(
                    "runtime context observation does not prove exact bytes"
                )

    def _validate_runtime_context_projection(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path_sha256: str,
        journal_execution_id: str,
        disposition: str,
        provider_status: object,
        projection: Mapping[str, Any],
    ) -> None:
        """Bind a local context read to this exact retained endpoint prefix."""

        expected_fields = {
            "planSha256",
            "completedPrefixSha256",
            "sourceCommit",
            "sourceTree",
            "contextRelativePath",
            "runtimeImageDigest",
            "runtimeEndpointName",
            "runtimeEndpointArn",
            "workloadIdentityArn",
            "endpointEvidenceSha256",
            "expectedRuntimeContextSha256",
            "observedRuntimeContextSha256",
            "size",
        }
        if set(projection) != expected_fields:
            raise EvidenceStoreV2Error(
                "runtime context projection fields are invalid"
            )
        completed_prefix_sha256 = _completed_prefix_sha256(
            [item.to_mapping() for item in transaction.completed_steps]
        )
        if (
            projection["planSha256"] != plan.digest()
            or projection["completedPrefixSha256"]
            != completed_prefix_sha256
            or projection["sourceCommit"] != plan.source_commit
            or projection["sourceTree"] != plan.source_tree
            or projection["contextRelativePath"]
            != plan.context_relative_path
            or projection["runtimeImageDigest"]
            != transaction.runtime_image_digest
            or projection["runtimeEndpointName"]
            != plan.runtime_endpoint_name
            or not _is_agentcore_endpoint_api_arn(
                projection["runtimeEndpointArn"],
                account=plan.account,
                region=plan.region,
            )
            or not _is_agentcore_workload_identity_arn(
                projection["workloadIdentityArn"],
                account=plan.account,
                region=plan.region,
            )
        ):
            raise EvidenceStoreV2Error(
                "runtime context projection crosses its release execution"
            )
        def is_sha256(value: object) -> bool:
            return (
                isinstance(value, str)
                and len(value) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in value
                )
            )

        expected_sha256 = projection["expectedRuntimeContextSha256"]
        observed_sha256 = projection["observedRuntimeContextSha256"]
        if not is_sha256(expected_sha256):
            raise EvidenceStoreV2Error(
                "runtime context projection expected hash is invalid"
            )
        size = projection["size"]
        size_is_int = isinstance(size, int) and not isinstance(size, bool)
        if disposition == "PRESENT":
            valid_result = (
                provider_status in {"PRESENT", "CREATED"}
                and observed_sha256 == expected_sha256
                and size_is_int
                and 0 < size <= MAX_CONTRACT_BYTES
            )
        elif disposition == "FAILED_RETAINED":
            valid_result = (
                provider_status == "EXISTING_CONTENT_CONFLICT"
                and is_sha256(observed_sha256)
                and observed_sha256 != expected_sha256
                and size_is_int
                and 0 < size <= MAX_CONTRACT_BYTES
            )
        elif disposition == "ABSENT":
            valid_result = (
                provider_status == "NOT_FOUND"
                and observed_sha256 == ""
                and size_is_int
                and size == 0
            )
        else:
            valid_result = False
        if not valid_result:
            raise EvidenceStoreV2Error(
                "runtime context projection result is invalid"
            )

        endpoint_steps = [
            step
            for step in plan.steps
            if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
        ]
        if len(endpoint_steps) != 1:
            raise EvidenceStoreV2Error(
                "runtime context endpoint owner is ambiguous"
            )
        endpoint_step = endpoint_steps[0]
        ordinal = endpoint_step.ordinal
        if ordinal >= len(transaction.completed_steps):
            raise EvidenceStoreV2Error(
                "runtime context endpoint evidence is incomplete"
        )
        completed = transaction.completed_steps[ordinal]
        endpoint_digest = projection["endpointEvidenceSha256"]
        if (
            not is_sha256(endpoint_digest)
            or completed.step_id != endpoint_step.step_id
            or endpoint_digest != completed.evidence_sha256
        ):
            raise EvidenceStoreV2Error(
                "runtime context endpoint evidence crosses its prefix"
            )
        candidates = [
            record
            for record in self._all_records(plan_sha256=plan.digest())
            if record.digest() == endpoint_digest
        ]
        if len(candidates) != 1:
            raise EvidenceStoreV2Error(
                "runtime context endpoint evidence is missing or ambiguous"
            )
        record = candidates[0]
        endpoint_prefix = _completed_prefix_sha256(
            [
                item.to_mapping()
                for item in transaction.completed_steps[:ordinal]
            ]
        )
        release_operation = _release_operation_sha256(
            plan.digest(), endpoint_step, endpoint_prefix
        )
        outcome_operation = _release_outcome_operation_sha256(
            release_operation_sha256=release_operation,
            journal_path_sha256=journal_path_sha256,
            journal_execution_id=journal_execution_id,
            journal_revision=record.journal_revision,
        )
        if (
            record.plan_sha256 != plan.digest()
            or record.evidence_store_sha256 != self.identity_sha256
            or record.journal_path_sha256 != journal_path_sha256
            or record.journal_execution_id != journal_execution_id
            or record.completed_prefix_sha256 != endpoint_prefix
            or record.step_id != endpoint_step.step_id
            or record.subject != endpoint_step.subject
            or record.release_operation_sha256 != release_operation
            or record.operation_sha256 != outcome_operation
            or record.disposition != "PRESENT"
            or record.step_observation is None
        ):
            raise EvidenceStoreV2Error(
                "runtime context endpoint evidence crosses its execution"
            )
        raw = record.observer_evidence_mapping()
        raw_projection = raw.get("projection")
        is_fixture = isinstance(raw_projection, Mapping) and (
            "fixtureMarker" in raw_projection
        )
        if not is_fixture and (
            raw.get("service") != "bedrock-agentcore-control"
            or raw.get("operation") != "get_agent_runtime_endpoint"
            or raw.get("subject") != endpoint_step.subject
            or raw.get("disposition") != "PRESENT"
            or raw.get("providerStatus") != "READY"
            or not isinstance(raw_projection, Mapping)
            or raw_projection.get("agentCoreStackId")
            != transaction.agent_core_stack_id
            or raw_projection.get("runtimeId") != transaction.runtime_id
            or raw_projection.get("runtimeVersion")
            != transaction.runtime_version
            or raw_projection.get("runtimeArn") != transaction.runtime_arn
            or raw_projection.get("endpointId")
            != transaction.runtime_endpoint_id
            or raw_projection.get("endpointName")
            != plan.runtime_endpoint_name
            or not _is_agentcore_endpoint_api_arn(
                raw_projection.get("endpointArn"),
                account=plan.account,
                region=plan.region,
            )
            or raw_projection.get("endpointArn")
            != projection["runtimeEndpointArn"]
            or not _is_agentcore_workload_identity_arn(
                raw_projection.get("workloadIdentityArn"),
                account=plan.account,
                region=plan.region,
            )
            or raw_projection.get("workloadIdentityArn")
            != projection["workloadIdentityArn"]
        ):
            raise EvidenceStoreV2Error(
                "runtime context endpoint provider evidence is invalid"
            )

    def _validate_release_verification_projection(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path_sha256: str,
        journal_execution_id: str,
        projection: Mapping[str, Any],
    ) -> None:
        """Bind the private terminal verifier read to the completed prefix."""

        if set(projection) != _RELEASE_VERIFICATION_PROJECTION_FIELDS:
            raise EvidenceStoreV2Error(
                "release verification projection fields are invalid"
            )

        def is_sha256(value: object) -> bool:
            return (
                isinstance(value, str)
                and len(value) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in value
                )
            )

        inventory = self._all_records(plan_sha256=plan.digest())
        by_digest: dict[str, list[RetainedStepEvidenceV2]] = {}
        for record in inventory:
            by_digest.setdefault(record.digest(), []).append(record)
        records: list[RetainedStepEvidenceV2] = []
        for ordinal, completed in enumerate(transaction.completed_steps):
            candidates = by_digest.get(completed.evidence_sha256, [])
            if len(candidates) != 1:
                raise EvidenceStoreV2Error(
                    "release verification retained prefix is incomplete"
                )
            record = candidates[0]
            step = plan.steps[ordinal]
            if (
                record.step_id != completed.step_id
                or record.step_id != step.step_id
                or record.subject != step.subject
                or record.plan_sha256 != plan.digest()
                or record.evidence_store_sha256 != self.identity_sha256
                or record.journal_path_sha256 != journal_path_sha256
                or record.journal_execution_id != journal_execution_id
            ):
                raise EvidenceStoreV2Error(
                    "release verification retained prefix crosses its execution"
                )
            records.append(record)

        def single_record(
            *, phase: str, kind: str
        ) -> RetainedStepEvidenceV2:
            candidates = [
                record
                for ordinal, record in enumerate(records)
                if plan.steps[ordinal].phase == phase
                and plan.steps[ordinal].kind == kind
            ]
            if len(candidates) != 1:
                raise EvidenceStoreV2Error(
                    "release verification retained owner is ambiguous"
                )
            return candidates[0]

        foundation_candidates = [
            record.step_observation.foundation_runtime_inputs
            for record in records
            if record.step_observation is not None
            and record.step_observation.foundation_runtime_inputs is not None
        ]
        if len(foundation_candidates) != 1:
            raise EvidenceStoreV2Error(
                "release verification foundation owner is ambiguous"
            )
        foundation = foundation_candidates[0]
        image_record = single_record(phase="image", kind="IMAGE_OBSERVE")
        endpoint_record = single_record(
            phase="endpoint", kind="STACK_UPDATE"
        )
        context_record = single_record(
            phase="context", kind="RUNTIME_CONTEXT_WRITE"
        )
        retained_prefix_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.retained-prefix-audit.v2",
                    "records": [record.digest() for record in records],
                }
            )
        ).hexdigest()
        expected = {
            "planSha256": plan.digest(),
            "transactionSha256": hashlib.sha256(
                transaction.to_bytes()
            ).hexdigest(),
            "completedPrefixSha256": _completed_prefix_sha256(
                [item.to_mapping() for item in transaction.completed_steps]
            ),
            "retainedPrefixSha256": retained_prefix_sha256,
            "evidenceStoreSha256": self.identity_sha256,
            "journalPathSha256": journal_path_sha256,
            "journalExecutionId": journal_execution_id,
            "journalRevision": transaction.revision,
            "completedRecordCount": len(records),
            "foundationInputsSha256": transaction.foundation_inputs_sha256,
            "runtimeImageDigest": transaction.runtime_image_digest,
            "imageObservationSha256": image_record.digest(),
            "runtimeId": transaction.runtime_id,
            "runtimeVersion": transaction.runtime_version,
            "runtimeArn": transaction.runtime_arn,
            "runtimeEndpointId": transaction.runtime_endpoint_id,
            "runtimeEndpointName": plan.runtime_endpoint_name,
            "runtimeContextSha256": transaction.runtime_context_sha256,
            "guardrailId": foundation.guardrail_id,
            "guardrailVersion": foundation.guardrail_version,
        }
        if any(projection[field] != value for field, value in expected.items()):
            raise EvidenceStoreV2Error(
                "release verification projection crosses its execution"
            )
        for field in (
            "runtimeConfigurationSha256",
            "runtimeIamRequestSha256",
            "runtimeIamObservationSha256",
            "runtimeContextObservationSha256",
        ):
            if not is_sha256(projection[field]):
                raise EvidenceStoreV2Error(
                    "release verification evidence digest is invalid"
                )

        endpoint_raw = endpoint_record.observer_evidence_mapping()
        endpoint_projection = endpoint_raw.get("projection")
        endpoint_is_fixture = isinstance(endpoint_projection, Mapping) and (
            "fixtureMarker" in endpoint_projection
        )
        endpoint_api_arn = projection["runtimeEndpointArn"]
        if not _is_agentcore_endpoint_api_arn(
            endpoint_api_arn,
            account=plan.account,
            region=plan.region,
        ):
            raise EvidenceStoreV2Error(
                "release verification endpoint API ARN is invalid"
            )
        workload_identity_arn = projection["runtimeWorkloadIdentityArn"]
        if not _is_agentcore_workload_identity_arn(
            workload_identity_arn,
            account=plan.account,
            region=plan.region,
        ):
            raise EvidenceStoreV2Error(
                "release verification workload identity ARN is invalid"
            )
        if not endpoint_is_fixture and (
            not isinstance(endpoint_projection, Mapping)
            or endpoint_projection.get("endpointArn") != endpoint_api_arn
            or endpoint_projection.get("workloadIdentityArn")
            != workload_identity_arn
            or endpoint_projection.get("runtimeConfigurationSha256")
            != projection["runtimeConfigurationSha256"]
            or endpoint_projection.get("guardrailId")
            != foundation.guardrail_id
            or endpoint_projection.get("guardrailVersion")
            != foundation.guardrail_version
        ):
            raise EvidenceStoreV2Error(
                "release verification runtime configuration differs"
            )

        context_raw = context_record.observer_evidence_mapping()
        context_projection = context_raw.get("projection")
        context_is_fixture = isinstance(context_projection, Mapping) and (
            "fixtureMarker" in context_projection
        )
        if not context_is_fixture:
            expected_context_observation = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema": (
                            "personal-operator.canonical-read-observation.v2"
                        ),
                        "service": "local-filesystem",
                        "operation": "read_runtime_context",
                        "subject": context_record.subject,
                        "disposition": "PRESENT",
                        "providerStatus": "PRESENT",
                        "projection": context_projection,
                    }
                )
            ).hexdigest()
            if (
                projection["runtimeContextObservationSha256"]
                != expected_context_observation
            ):
                raise EvidenceStoreV2Error(
                    "release verification runtime context read differs"
                )

    def _derive_present_observation(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        provider_mapping: Mapping[str, Any],
        provider_digest: str,
        phase_evidence_sha256: str,
        foundation_runtime_inputs: FoundationRuntimeInputsV1 | None,
        trusted_verification_projection: bool = False,
    ) -> ReleaseStepObservationV2:
        step = plan.steps[transaction.completed_step_count]
        projection = provider_mapping["projection"]
        if not isinstance(projection, Mapping):
            raise EvidenceStoreV2Error("provider projection is malformed")
        if (
            _SYNTACTIC_DERIVED_PROJECTION_FIELDS.intersection(projection)
            and not trusted_verification_projection
        ):
            raise EvidenceStoreV2Error(
                "provider projection contains caller-supplied derived evidence"
            )
        ReleaseEvidenceStoreV2._validate_present_projection(
            step=step, projection=projection
        )
        value = ReleaseEvidenceStoreV2._empty_step_observation(
            plan=plan,
            step=step,
            observer_evidence_sha256=provider_digest,
        )
        if foundation_runtime_inputs is not None:
            value["foundationRuntimeInputs"] = (
                foundation_runtime_inputs.to_mapping()
            )
        phase_field = _PHASE_EVIDENCE_FIELDS.get(step.phase)
        if phase_evidence_sha256:
            if phase_field is None:
                raise EvidenceStoreV2Error(
                    "phase evidence was derived for an unsupported phase"
                )
            value[phase_field] = phase_evidence_sha256
        if step.kind == "CHANGESET_CREATE":
            stack_id = projection.get("stackId")
            change_set_id = projection.get("changeSetId")
            stack_name = step.subject.split(":stack:", 1)[1].split(
                ":release:", 1
            )[0]
            fields = {
                "OpenClawRouter": (
                    "routerTargetStackId",
                    "routerChangeSetId",
                ),
                "OpenClawCron": ("cronTargetStackId", "cronChangeSetId"),
                "PersonalOperatorScheduler": (
                    "schedulerTargetStackId",
                    "schedulerChangeSetId",
                ),
                "PersonalOperatorWeb": (
                    "webTargetStackId",
                    "webChangeSetId",
                ),
            }
            if stack_name not in fields:
                raise EvidenceStoreV2Error(
                    "change-set provider subject is unsupported"
                )
            target_field, change_field = fields[stack_name]
            value[target_field] = stack_id
            value[change_field] = change_set_id
        elif step.phase == "image" and step.kind == "IMAGE_OBSERVE":
            value["runtimeImageDigest"] = projection.get(
                "runtimeImageDigest", ""
            )
        elif step.phase == "runtime":
            value.update(
                {
                    "agentCoreStackId": projection.get(
                        "agentCoreStackId", transaction.agent_core_stack_id
                    ),
                    "runtimeId": projection.get(
                        "runtimeId", transaction.runtime_id
                    ),
                    "runtimeVersion": projection.get(
                        "runtimeVersion", transaction.runtime_version
                    ),
                    "runtimeArn": projection.get(
                        "runtimeArn", transaction.runtime_arn
                    ),
                }
            )
        elif step.phase == "endpoint":
            endpoint_id = ""
            next_phase = (
                plan.steps[transaction.completed_step_count + 1].phase
                if transaction.completed_step_count + 1 < len(plan.steps)
                else None
            )
            if next_phase != step.phase:
                if step.kind == "STACK_DRIFT_CHECK":
                    predecessor_digest = transaction.completed_steps[-1].evidence_sha256
                    candidates = [
                        record
                        for record in self._all_records(plan_sha256=plan.digest())
                        if record.digest() == predecessor_digest
                    ]
                    if len(candidates) != 1:
                        raise EvidenceStoreV2Error(
                            "endpoint drift predecessor is missing or ambiguous"
                        )
                    predecessor_projection = candidates[
                        0
                    ].observer_evidence_mapping().get("projection")
                    if not isinstance(predecessor_projection, Mapping):
                        raise EvidenceStoreV2Error(
                            "endpoint drift predecessor projection is malformed"
                        )
                    endpoint_id = predecessor_projection.get("endpointId", "")
                else:
                    endpoint_id = projection.get("endpointId", "")
            value.update(
                {
                    "agentCoreStackId": projection.get(
                        "agentCoreStackId", transaction.agent_core_stack_id
                    ),
                    "runtimeEndpointId": endpoint_id,
                }
            )
        elif step.phase == "context" and step.kind == "RUNTIME_CONTEXT_WRITE":
            value["runtimeContextSha256"] = projection.get(
                "observedRuntimeContextSha256", ""
            )
        try:
            observation = ReleaseStepObservationV2.from_mapping(value)
            observation.validate_plan_step(
                plan,
                completed_step_count=transaction.completed_step_count,
                prior_agent_core_stack_id=transaction.agent_core_stack_id,
                prior_runtime_id=transaction.runtime_id,
                prior_runtime_version=transaction.runtime_version,
                prior_runtime_arn=transaction.runtime_arn,
            )
        except ContractError as error:
            raise EvidenceStoreV2Error(
                f"production outcome derivation is unsupported for {step.phase}/"
                f"{step.kind}: {error}"
            ) from error
        return observation

    def _derive_foundation_runtime_inputs(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path_sha256: str,
        journal_execution_id: str,
        release_operation_sha256: str,
        provider_digest: str,
    ) -> FoundationRuntimeInputsV1 | None:
        """Derive the sole foundation contract at final Observability drift."""

        count = transaction.completed_step_count
        step = plan.steps[count]
        next_phase = (
            plan.steps[count + 1].phase
            if count + 1 < len(plan.steps)
            else None
        )
        if step.phase != "foundation" or next_phase == "foundation":
            return None
        expected_subject_suffix = (
            ":stack:OpenClawObservability:release:"
            f"{plan.source_commit}:drift"
        )
        if (
            step.kind != "STACK_DRIFT_CHECK"
            or not step.subject.endswith(expected_subject_suffix)
            or count < 1
            or any(item.phase != "foundation" for item in plan.steps[: count + 1])
        ):
            raise EvidenceStoreV2Error(
                "foundation runtime inputs are not owned by final Observability drift"
            )
        inventory = self._all_records(plan_sha256=plan.digest())
        by_digest: dict[str, list[RetainedStepEvidenceV2]] = {}
        for record in inventory:
            by_digest.setdefault(record.digest(), []).append(record)
        phase_records: list[dict[str, str]] = []
        stack_outputs: dict[str, Mapping[str, Any]] = {}
        stack_ids: dict[str, str] = {}
        for ordinal in range(count):
            completed = transaction.completed_steps[ordinal]
            candidates = by_digest.get(completed.evidence_sha256, [])
            if len(candidates) != 1:
                raise EvidenceStoreV2Error(
                    "foundation retained record is missing or ambiguous"
                )
            record = candidates[0]
            prior_step = plan.steps[ordinal]
            if (
                record.disposition != "PRESENT"
                or record.step_id != prior_step.step_id
                or record.subject != prior_step.subject
                or record.evidence_store_sha256 != self.identity_sha256
                or record.journal_path_sha256 != journal_path_sha256
                or record.journal_execution_id != journal_execution_id
            ):
                raise EvidenceStoreV2Error(
                    "foundation retained record crosses its journal"
                )
            phase_records.append(
                {
                    "stepId": record.step_id,
                    "subject": record.subject,
                    "operationSha256": record.release_operation_sha256,
                    "observerEvidenceSha256": (
                        record.observer_evidence_sha256
                    ),
                }
            )
            if prior_step.kind not in {"BOOTSTRAP_STACK", "STACK_CREATE"}:
                continue
            projection = record.observer_evidence_mapping().get("projection")
            if not isinstance(projection, Mapping):
                raise EvidenceStoreV2Error(
                    "foundation stack projection is malformed"
                )
            stack_name = prior_step.subject.split(":stack:", 1)[1].split(
                ":release:", 1
            )[0]
            outputs = projection.get("outputs")
            stack_id = projection.get("stackId")
            if not isinstance(outputs, Mapping) or not isinstance(stack_id, str):
                raise EvidenceStoreV2Error(
                    "foundation stack projection lacks exact outputs or StackId"
                )
            stack_outputs[stack_name] = outputs
            stack_ids[stack_name] = stack_id
        phase_records.append(
            {
                "stepId": step.step_id,
                "subject": step.subject,
                "operationSha256": release_operation_sha256,
                "observerEvidenceSha256": provider_digest,
            }
        )
        snapshot_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.phase-evidence.v1",
                    "planSha256": plan.digest(),
                    "phase": "foundation",
                    "records": phase_records,
                }
            )
        ).hexdigest()

        def output(stack_name: str, key: str) -> str:
            outputs = stack_outputs.get(stack_name)
            value = outputs.get(key) if isinstance(outputs, Mapping) else None
            if not isinstance(value, str) or not value or "\x00" in value:
                raise EvidenceStoreV2Error(
                    f"foundation {stack_name} output {key} is missing or invalid"
                )
            return value

        subnet_output = output("OpenClawAgentCore", "PrivateSubnetIds")
        private_subnets = subnet_output.split(",")
        if (
            not private_subnets
            or any(not value or value.strip() != value for value in private_subnets)
            or private_subnets != sorted(private_subnets)
            or len(set(private_subnets)) != len(private_subnets)
        ):
            raise EvidenceStoreV2Error(
                "foundation private subnet output is not exact and canonical"
            )
        agent_core_stack_id = stack_ids.get("OpenClawAgentCore")
        if not isinstance(agent_core_stack_id, str):
            raise EvidenceStoreV2Error(
                "foundation AgentCore StackId is missing"
            )
        try:
            result = FoundationRuntimeInputsV1.from_mapping(
                {
                    "schema": FoundationRuntimeInputsV1.SCHEMA,
                    "sourceCommit": plan.source_commit,
                    "sourceTree": plan.source_tree,
                    "account": plan.account,
                    "region": plan.region,
                    "releasePlanSha256": plan.digest(),
                    "derivationVersion": plan.derivation_version,
                    "privateSubnetIds": private_subnets,
                    "runtimeSecurityGroupIds": [
                        output("OpenClawAgentCore", "SecurityGroupId")
                    ],
                    "userFilesBucketName": output(
                        "OpenClawAgentCore", "UserFilesBucketName"
                    ),
                    "capabilityGatewayFunctionArn": output(
                        "PersonalOperatorCapabilities",
                        "CapabilityGatewayFunctionArn",
                    ),
                    "workspaceBrokerFunctionName": output(
                        "OpenClawAgentCore",
                        "WorkspaceCredentialBrokerFunctionName",
                    ),
                    "agentCoreStackId": agent_core_stack_id,
                    "guardrailId": output(
                        "OpenClawGuardrails", "GuardrailId"
                    ),
                    "guardrailVersion": output(
                        "OpenClawGuardrails", "GuardrailVersion"
                    ),
                    "guardrailArn": output(
                        "OpenClawGuardrails", "GuardrailArn"
                    ),
                    "foundationSnapshotSha256": snapshot_sha256,
                }
            )
            result.validate_plan_identity(plan)
        except ContractError as error:
            raise EvidenceStoreV2Error(
                "derived foundation runtime inputs are invalid"
            ) from error
        return result

    def _derive_phase_evidence_sha256(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path_sha256: str,
        journal_execution_id: str,
        release_operation_sha256: str,
        provider_digest: str,
    ) -> str:
        """Derive a terminal phase digest from ordered raw provider records."""

        count = transaction.completed_step_count
        step = plan.steps[count]
        field = _PHASE_EVIDENCE_FIELDS.get(step.phase)
        next_phase = (
            plan.steps[count + 1].phase
            if count + 1 < len(plan.steps)
            else None
        )
        if field is None or next_phase == step.phase:
            return ""
        inventory = self._all_records(plan_sha256=plan.digest())
        by_digest: dict[str, list[RetainedStepEvidenceV2]] = {}
        for retained in inventory:
            by_digest.setdefault(retained.digest(), []).append(retained)
        phase_start = count
        while phase_start > 0 and plan.steps[phase_start - 1].phase == step.phase:
            phase_start -= 1
        records: list[dict[str, str]] = []
        for ordinal in range(phase_start, count):
            completed = transaction.completed_steps[ordinal]
            candidates = by_digest.get(completed.evidence_sha256, [])
            if len(candidates) != 1:
                raise EvidenceStoreV2Error(
                    "phase evidence predecessor is missing or ambiguous"
                )
            retained = candidates[0]
            prior_step = plan.steps[ordinal]
            if (
                retained.disposition != "PRESENT"
                or retained.step_id != prior_step.step_id
                or retained.subject != prior_step.subject
                or retained.evidence_store_sha256 != self.identity_sha256
                or retained.journal_path_sha256 != journal_path_sha256
                or retained.journal_execution_id != journal_execution_id
            ):
                raise EvidenceStoreV2Error(
                    "phase evidence predecessor crosses its journal"
                )
            records.append(
                {
                    "stepId": retained.step_id,
                    "subject": retained.subject,
                    "operationSha256": retained.release_operation_sha256,
                    "observerEvidenceSha256": (
                        retained.observer_evidence_sha256
                    ),
                }
            )
        records.append(
            {
                "stepId": step.step_id,
                "subject": step.subject,
                "operationSha256": release_operation_sha256,
                "observerEvidenceSha256": provider_digest,
            }
        )
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.phase-evidence.v1",
                    "planSha256": plan.digest(),
                    "phase": step.phase,
                    "records": records,
                }
            )
        ).hexdigest()

    @staticmethod
    def _derive_failure_observation(
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        release_operation_sha256: str,
        provider_mapping: Mapping[str, Any],
        provider_digest: str,
    ) -> ReleaseStepFailureObservationV2:
        step = plan.steps[transaction.completed_step_count]
        status = provider_mapping["providerStatus"]
        service = provider_mapping["service"]
        provider = {
            "cloudformation": "CLOUDFORMATION",
            "s3": "S3",
            "ecr": "ECR",
            "bedrock-agentcore-control": "AGENTCORE",
            "local-filesystem": "LOCAL_FILESYSTEM",
        }.get(service, "")
        reason = ""
        for candidate, statuses in _FAILED_RETAINED_KIND_REASON_STATUSES.get(
            step.kind, {}
        ).items():
            if (
                status in statuses
                and _FAILED_RETAINED_REASON_PROVIDERS[candidate] == provider
            ):
                reason = candidate
                break
        if not reason:
            raise EvidenceStoreV2Error(
                "provider failure status is unsupported for the step kind"
            )
        return ReleaseStepFailureObservationV2.from_mapping(
            {
                "schema": ReleaseStepFailureObservationV2.SCHEMA,
                "planSha256": plan.digest(),
                "stepId": step.step_id,
                "subject": step.subject,
                "operationSha256": release_operation_sha256,
                "provider": provider,
                "terminalStatus": status,
                "failureReason": reason,
                "observerEvidenceSha256": provider_digest,
            }
        )

    def _validate_current_effect_receipt(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        record: RetainedStepEvidenceV2,
        journal_path_sha256: str,
        journal_execution_id: str,
    ) -> None:
        """Validate a receipt-bearing read before its append-only outcome write."""

        step = plan.steps[transaction.completed_step_count]
        if step.kind not in {"STACK_DRIFT_CHECK", "AGENTCORE_HARDEN"}:
            return
        receipt_kind = (
            "stack-drift"
            if step.kind == "STACK_DRIFT_CHECK"
            else "agentcore-hardening"
        )
        relevant = [
            backend
            for backend in self._all_receipt_backends(
                plan_sha256=plan.digest()
            )
            if backend.authority.journal_path_sha256 == journal_path_sha256
            and backend.authority.journal_execution_id == journal_execution_id
            and backend.authority.kind == receipt_kind
            and backend.authority.journal_revision == transaction.revision
        ]
        if len(relevant) != 1:
            raise EvidenceStoreV2Error(
                "provider observation lacks one exact dispatch receipt"
            )
        backend = relevant[0]
        expected = self._expected_receipt_authority(
            plan=plan, record=record, receipt_kind=receipt_kind
        )
        attempted, payload = backend.load()
        if backend.authority != expected or not attempted or payload is None:
            raise EvidenceStoreV2Error(
                "provider observation receipt authority is incomplete"
            )
        if step.kind == "STACK_DRIFT_CHECK":
            if not transaction.completed_steps:
                raise EvidenceStoreV2Error(
                    "stack drift observation lacks its predecessor"
                )
            predecessor_digest = transaction.completed_steps[-1].evidence_sha256
            predecessors = [
                candidate
                for candidate in self._journal_records(
                    plan_sha256=plan.digest(),
                    journal_path_sha256=journal_path_sha256,
                    journal_execution_id=journal_execution_id,
                )
                if candidate.digest() == predecessor_digest
            ]
            if len(predecessors) != 1:
                raise EvidenceStoreV2Error(
                    "stack drift observation predecessor is missing or ambiguous"
                )
            self._validate_stack_drift_receipt(
                plan=plan,
                record=record,
                predecessor=predecessors[0],
                payload=payload,
            )
        else:
            precondition_payload = backend.load_precondition()
            if precondition_payload is None:
                raise EvidenceStoreV2Error(
                    "AgentCore observation lacks its exact precondition"
                )
            self._validate_agentcore_hardening_receipt(
                plan=plan,
                record=record,
                payload=payload,
                precondition_payload=precondition_payload,
            )

    def _compose_outcome(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
        provider_observation: object,
    ) -> VerifiedStepOutcomeV2:
        from release_tools.production_observer_v2 import (  # noqa: PLC0415
            CanonicalReadObservationV2,
        )
        from release_tools.runtime_context_v2 import (  # noqa: PLC0415
            RuntimeContextLocalObservationV2,
        )
        from release_tools.release_verifier_v2 import (  # noqa: PLC0415
            ReleaseVerificationObservationV2,
        )

        if not isinstance(
            provider_observation,
            (
                CanonicalReadObservationV2,
                RuntimeContextLocalObservationV2,
                ReleaseVerificationObservationV2,
            ),
        ):
            raise EvidenceStoreV2Error(
                "outcome composition requires canonical provider evidence"
            )
        canonical_plan = _canonical_release_plan_v2(plan)
        canonical_transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=canonical_plan
        )
        self._require_binding(
            journal_path=journal_path,
            plan=canonical_plan,
            journal_execution_id=journal_execution_id,
        )
        self.audit_prefix(
            plan=canonical_plan,
            transaction=canonical_transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        provider_bytes = provider_observation.to_bytes()
        provider_mapping = parse_canonical_object(provider_bytes)
        count = canonical_transaction.completed_step_count
        if count >= len(canonical_plan.steps):
            raise EvidenceStoreV2Error("outcome has no next plan step")
        step = canonical_plan.steps[count]
        disposition = provider_mapping.get("disposition")
        if not isinstance(disposition, str):
            raise EvidenceStoreV2Error("provider disposition is invalid")
        pairs = self._allowed_provider_pairs(
            phase=step.phase,
            kind=step.kind,
            subject=step.subject,
            disposition=disposition,
        )
        pair = (
            provider_mapping.get("service"),
            provider_mapping.get("operation"),
        )
        if not pairs or pair not in pairs:
            raise EvidenceStoreV2Error(
                "provider service/operation is unsupported for the step kind"
            )
        if provider_mapping.get("subject") != step.subject:
            raise EvidenceStoreV2Error("provider subject differs from the plan step")
        projection = provider_mapping.get("projection")
        if step.kind == "VERIFY" and not isinstance(
            provider_observation, ReleaseVerificationObservationV2
        ):
            raise EvidenceStoreV2Error(
                "VERIFY requires the private release verifier observation"
            )
        if isinstance(provider_observation, RuntimeContextLocalObservationV2):
            if not isinstance(projection, Mapping):
                raise EvidenceStoreV2Error(
                    "runtime context projection is malformed"
                )
            self._validate_runtime_context_projection(
                plan=canonical_plan,
                transaction=canonical_transaction,
                journal_path_sha256=_journal_path_sha256(journal_path),
                journal_execution_id=journal_execution_id,
                disposition=disposition,
                provider_status=provider_mapping.get("providerStatus"),
                projection=projection,
            )
        if isinstance(
            provider_observation, ReleaseVerificationObservationV2
        ):
            if not isinstance(projection, Mapping):
                raise EvidenceStoreV2Error(
                    "release verification projection is malformed"
                )
            self._validate_release_verification_projection(
                plan=canonical_plan,
                transaction=canonical_transaction,
                journal_path_sha256=_journal_path_sha256(journal_path),
                journal_execution_id=journal_execution_id,
                projection=projection,
            )
        release_operation = self._release_operation(
            plan=canonical_plan, transaction=canonical_transaction
        )
        provider_digest = provider_observation.digest()
        step_observation = None
        failure_observation = None
        if disposition == "PRESENT":
            phase_evidence_sha256 = self._derive_phase_evidence_sha256(
                plan=canonical_plan,
                transaction=canonical_transaction,
                journal_path_sha256=_journal_path_sha256(journal_path),
                journal_execution_id=journal_execution_id,
                release_operation_sha256=release_operation,
                provider_digest=provider_digest,
            )
            foundation_runtime_inputs = self._derive_foundation_runtime_inputs(
                plan=canonical_plan,
                transaction=canonical_transaction,
                journal_path_sha256=_journal_path_sha256(journal_path),
                journal_execution_id=journal_execution_id,
                release_operation_sha256=release_operation,
                provider_digest=provider_digest,
            )
            step_observation = self._derive_present_observation(
                plan=canonical_plan,
                transaction=canonical_transaction,
                provider_mapping=provider_mapping,
                provider_digest=provider_digest,
                phase_evidence_sha256=phase_evidence_sha256,
                foundation_runtime_inputs=foundation_runtime_inputs,
                trusted_verification_projection=isinstance(
                    provider_observation,
                    ReleaseVerificationObservationV2,
                ),
            )
        elif disposition == "FAILED_RETAINED":
            failure_observation = self._derive_failure_observation(
                plan=canonical_plan,
                transaction=canonical_transaction,
                release_operation_sha256=release_operation,
                provider_mapping=provider_mapping,
                provider_digest=provider_digest,
            )
        elif disposition not in {"ABSENT", "PENDING"}:
            raise EvidenceStoreV2Error("provider disposition is unsupported")
        path_sha256 = _journal_path_sha256(journal_path)
        outcome_operation = _release_outcome_operation_sha256(
            release_operation_sha256=release_operation,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
            journal_revision=canonical_transaction.revision,
        )
        record = RetainedStepEvidenceV2.from_mapping(
            {
                "schema": RetainedStepEvidenceV2.SCHEMA,
                "planSha256": canonical_plan.digest(),
                "completedPrefixSha256": _completed_prefix_sha256(
                    [
                        item.to_mapping()
                        for item in canonical_transaction.completed_steps
                    ]
                ),
                "evidenceStoreSha256": self.identity_sha256,
                "journalPathSha256": path_sha256,
                "journalExecutionId": journal_execution_id,
                "journalRevision": canonical_transaction.revision,
                "stepId": step.step_id,
                "subject": step.subject,
                "operationSha256": outcome_operation,
                "releaseOperationSha256": release_operation,
                "disposition": disposition,
                "observerEvidenceSha256": provider_digest,
                "observerEvidence": provider_mapping,
                "stepObservationSha256": (
                    step_observation.digest() if step_observation else ""
                ),
                "stepObservation": (
                    step_observation.to_mapping() if step_observation else {}
                ),
                "failureObservationSha256": (
                    failure_observation.digest() if failure_observation else ""
                ),
                "failureObservation": (
                    failure_observation.to_mapping()
                    if failure_observation
                    else {}
                ),
            }
        )
        record.validate_transaction(
            canonical_plan,
            canonical_transaction,
            evidence_store_sha256=self.identity_sha256,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        self._validate_current_effect_receipt(
            plan=canonical_plan,
            transaction=canonical_transaction,
            record=record,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        return self._retain_record(
            record, _token=_VERIFIED_STEP_OUTCOME_TOKEN
        )

    def _all_records(self, *, plan_sha256: str) -> list[RetainedStepEvidenceV2]:
        directory_fd = self._plan_directory(plan_sha256)
        records: list[RetainedStepEvidenceV2] = []
        for name in sorted(os.listdir(directory_fd)):
            if name.startswith(
                (".", "transition-", "commit-", "dispatch-", "receipt-")
            ):
                continue
            try:
                payload = self._read_secure(
                    directory_fd=directory_fd,
                    name=name,
                    label="retained outcome record",
                )
                record = RetainedStepEvidenceV2.from_bytes(payload)
            except (ContractError, OSError, TypeError, ValueError) as error:
                raise EvidenceStoreV2Error(
                    "retained outcome inventory is invalid"
                ) from error
            if self._record_name(record) != name:
                raise EvidenceStoreV2Error(
                    "retained outcome filename differs from its operation"
                )
            records.append(record)
        return records

    def _all_dispatch_attempts(
        self, *, plan_sha256: str
    ) -> dict[str, ReleaseDispatchAttemptV1]:
        """Parse every universal attempt marker; unknown files fail closed."""

        directory_fd = self._plan_directory(plan_sha256)
        attempts: dict[str, ReleaseDispatchAttemptV1] = {}
        for name in sorted(os.listdir(directory_fd)):
            if not name.startswith("dispatch-"):
                continue
            try:
                payload = self._read_secure(
                    directory_fd=directory_fd,
                    name=name,
                    label="release dispatch attempt",
                )
                attempt = ReleaseDispatchAttemptV1.from_bytes(payload)
            except (DispatchAttemptError, OSError, TypeError, ValueError) as error:
                raise EvidenceStoreV2Error(
                    "release dispatch attempt inventory is invalid"
                ) from error
            expected_name = self._dispatch_attempt_name(
                journal_execution_id=attempt.journal_execution_id,
                journal_revision=attempt.journal_revision,
                operation=attempt.operation_sha256,
            )
            if (
                name != expected_name
                or attempt.release_plan_sha256 != plan_sha256
                or attempt.evidence_store_sha256 != self.identity_sha256
            ):
                raise EvidenceStoreV2Error(
                    "release dispatch attempt inventory crosses its namespace"
                )
            attempts[name] = attempt
        return attempts

    def _all_receipt_backends(
        self, *, plan_sha256: str
    ) -> list[_AppendOnlyReceiptBackendV2]:
        """Load every exact receipt, precondition, and attempt authority."""

        directory_fd = self._plan_directory(plan_sha256)
        names = sorted(os.listdir(directory_fd))
        receipt_names = {name for name in names if name.startswith("receipt-")}
        candidates: dict[str, _AppendOnlyReceiptBackendV2] = {}
        claimed: set[str] = set()
        for name in names:
            if not name.startswith("receipt-"):
                continue
            try:
                if name.endswith("-attempt.json"):
                    authority = _ReceiptAuthorityV2.from_bytes(
                        self._read_secure(
                            directory_fd=directory_fd,
                            name=name,
                            label="provider receipt attempt",
                        )
                    )
                elif name.endswith("-precondition.bin"):
                    authority = _agentcore_precondition_authority(
                        self._read_secure(
                            directory_fd=directory_fd,
                            name=name,
                            label="AgentCore hardening precondition",
                        )
                    )
                else:
                    continue
            except (OSError, ValueError) as error:
                raise EvidenceStoreV2Error(
                    "provider receipt inventory is invalid"
                ) from error
            if (
                authority.release_plan_sha256 != plan_sha256
                or authority.evidence_store_sha256 != self.identity_sha256
            ):
                raise EvidenceStoreV2Error(
                    "provider receipt attempt crosses its plan or store"
                )
            backend = _AppendOnlyReceiptBackendV2(
                store=self, authority=authority
            )
            expected_name = (
                backend._attempt_name
                if name.endswith("-attempt.json")
                else backend._precondition_name
            )
            if expected_name != name:
                raise EvidenceStoreV2Error(
                    "provider receipt filename differs from authority"
                )
            existing = candidates.get(backend._attempt_name)
            if existing is not None and existing.authority != authority:
                raise EvidenceStoreV2Error(
                    "provider receipt records cross one journal operation"
                )
            candidates[backend._attempt_name] = backend

        backends: list[_AppendOnlyReceiptBackendV2] = []
        for backend in candidates.values():
            authority = backend.authority
            binding_name = f"journal-{authority.journal_path_sha256}.json"
            try:
                binding = parse_canonical_object(
                    self._read_secure(
                        directory_fd=self._root_fd,
                        name=binding_name,
                        label="journal binding",
                    )
                )
            except (ContractError, OSError, TypeError, ValueError) as error:
                raise EvidenceStoreV2Error(
                    "provider receipt attempt has no exact journal binding"
                ) from error
            if (
                binding.get("evidenceStoreSha256") != self.identity_sha256
                or binding.get("journalPathSha256")
                != authority.journal_path_sha256
                or binding.get("journalExecutionId")
                != authority.journal_execution_id
                or binding.get("planSha256") != plan_sha256
            ):
                raise EvidenceStoreV2Error(
                    "provider receipt attempt crosses its journal binding"
                )
            attempted, receipt = backend.load()
            precondition = backend.load_precondition()
            if authority.kind == "agentcore-hardening":
                if (attempted or receipt is not None) and precondition is None:
                    raise EvidenceStoreV2Error(
                        "AgentCore hardening attempt lacks its precondition"
                    )
                if precondition is not None:
                    from release_tools.agentcore_hardening_v2 import (  # noqa: PLC0415
                        AgentCoreHardeningError,
                        AgentCoreHardeningPreconditionV1,
                    )

                    try:
                        AgentCoreHardeningPreconditionV1.from_bytes(
                            precondition
                        )
                    except AgentCoreHardeningError as error:
                        raise EvidenceStoreV2Error(
                            "AgentCore hardening precondition inventory is invalid"
                        ) from error
            elif precondition is not None:
                raise EvidenceStoreV2Error(
                    "provider receipt owns an unexpected precondition"
                )
            if not attempted and precondition is None:
                raise EvidenceStoreV2Error(
                    "provider receipt authority disappeared during audit"
                )
            if attempted:
                claimed.add(backend._attempt_name)
            if precondition is not None:
                claimed.add(backend._precondition_name)
            if receipt is not None:
                claimed.add(backend._receipt_name)
            backends.append(backend)
        if receipt_names != claimed:
            raise EvidenceStoreV2Error(
                "provider receipt inventory contains an orphan record"
            )
        return backends

    @staticmethod
    def _expected_receipt_authority(
        *,
        plan: ReleasePlanV2,
        record: RetainedStepEvidenceV2,
        receipt_kind: str,
    ) -> _ReceiptAuthorityV2:
        return _ReceiptAuthorityV2(
            kind=receipt_kind,
            release_plan_sha256=plan.digest(),
            evidence_store_sha256=record.evidence_store_sha256,
            journal_path_sha256=record.journal_path_sha256,
            journal_execution_id=record.journal_execution_id,
            journal_revision=record.journal_revision,
            completed_prefix_sha256=record.completed_prefix_sha256,
            step_id=record.step_id,
            subject=record.subject,
            operation_sha256=record.release_operation_sha256,
        )

    @staticmethod
    def _validate_stack_drift_receipt(
        *,
        plan: ReleasePlanV2,
        record: RetainedStepEvidenceV2,
        predecessor: RetainedStepEvidenceV2,
        payload: bytes,
    ) -> None:
        from release_tools.contracts import (  # noqa: PLC0415
            StackDriftDispatchReceiptV1,
        )

        try:
            receipt = StackDriftDispatchReceiptV1.from_bytes(payload)
            observer = record.observer_evidence_mapping()
            projection = observer.get("projection")
            predecessor_projection = (
                predecessor.observer_evidence_mapping().get("projection")
            )
        except (ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "retained stack drift receipt is invalid"
            ) from error
        if (
            receipt.release_plan_sha256 != plan.digest()
            or receipt.evidence_store_sha256 != record.evidence_store_sha256
            or receipt.journal_path_sha256 != record.journal_path_sha256
            or receipt.journal_execution_id != record.journal_execution_id
            or receipt.journal_revision != record.journal_revision
            or receipt.completed_prefix_sha256
            != record.completed_prefix_sha256
            or receipt.step_id != record.step_id
            or receipt.subject != record.subject
            or receipt.release_operation_sha256
            != record.release_operation_sha256
            or receipt.predecessor_evidence_sha256 != predecessor.digest()
            or receipt.predecessor_observer_evidence_sha256
            != predecessor.observer_evidence_sha256
            or not isinstance(predecessor_projection, Mapping)
            or receipt.stack_id
            != (
                predecessor_projection.get("stackId")
                or predecessor_projection.get("agentCoreStackId")
            )
            or not isinstance(projection, Mapping)
            or observer.get("service") != "cloudformation"
            or observer.get("operation")
            != "describe_stack_drift_detection_status"
            or projection.get("dispatchReceiptSha256") != receipt.digest()
            or projection.get("driftDetectionId")
            != receipt.drift_detection_id
            or projection.get("stackId") != receipt.stack_id
            or projection.get("predecessorEvidenceSha256")
            != predecessor.digest()
            or projection.get("predecessorObserverEvidenceSha256")
            != predecessor.observer_evidence_sha256
        ):
            raise EvidenceStoreV2Error(
                "stack drift receipt or observation crosses retained authority"
            )

    @staticmethod
    def _validate_agentcore_hardening_receipt(
        *,
        plan: ReleasePlanV2,
        record: RetainedStepEvidenceV2,
        payload: bytes,
        precondition_payload: bytes,
    ) -> None:
        from release_tools.agentcore_hardening_v2 import (  # noqa: PLC0415
            AgentCoreHardeningDispatchReceiptV1,
            AgentCoreHardeningError,
            AgentCoreHardeningPreconditionV1,
        )

        try:
            receipt = AgentCoreHardeningDispatchReceiptV1.from_bytes(payload)
            precondition = AgentCoreHardeningPreconditionV1.from_bytes(
                precondition_payload
            )
            precondition_mapping = parse_canonical_object(
                precondition_payload
            )
            observer = record.observer_evidence_mapping()
            projection = observer.get("projection")
        except (AgentCoreHardeningError, ContractError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "retained AgentCore hardening receipt is invalid"
            ) from error
        if (
            receipt.release_plan_sha256 != plan.digest()
            or _agentcore_precondition_authority(precondition_payload)
            != ReleaseEvidenceStoreV2._expected_receipt_authority(
                plan=plan,
                record=record,
                receipt_kind="agentcore-hardening",
            )
            or precondition_mapping.get("account") != plan.account
            or precondition_mapping.get("region") != plan.region
            or precondition_mapping.get("resolvedRequestSha256")
            != receipt.resolved_request_sha256
            or precondition.digest() != receipt.precondition_sha256
            or (
                precondition.mode == "NOOP"
                and receipt.mode != "NOOP"
            )
            or (
                precondition.mode == "UPDATE"
                and receipt.mode != "UPDATED"
            )
            or receipt.transaction_id != plan.transaction_id
            or receipt.source_commit != plan.source_commit
            or receipt.source_tree != plan.source_tree
            or receipt.account != plan.account
            or receipt.region != plan.region
            or receipt.evidence_store_sha256 != record.evidence_store_sha256
            or receipt.journal_path_sha256 != record.journal_path_sha256
            or receipt.journal_execution_id != record.journal_execution_id
            or receipt.journal_revision != record.journal_revision
            or receipt.completed_prefix_sha256
            != record.completed_prefix_sha256
            or receipt.step_id != record.step_id
            or receipt.subject != record.subject
            or receipt.operation_sha256 != record.release_operation_sha256
            or not isinstance(projection, Mapping)
            or observer.get("service") != "bedrock-agentcore-control"
            or observer.get("operation") != "get_agent_runtime"
            or projection.get("hardeningReceiptSha256") != receipt.digest()
            or projection.get("preconditionSha256")
            != receipt.precondition_sha256
            or projection.get("runtimeId") != receipt.runtime_id
            or projection.get("runtimeVersion")
            != receipt.resulting_runtime_version
            or projection.get("runtimeArn") != receipt.resulting_runtime_arn
        ):
            raise EvidenceStoreV2Error(
                "AgentCore hardening receipt or observation crosses authority"
            )

    def _audit_receipt_inventory(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        records_by_digest: Mapping[str, list[RetainedStepEvidenceV2]],
        journal_path_sha256: str,
        journal_execution_id: str,
    ) -> None:
        all_backends = self._all_receipt_backends(plan_sha256=plan.digest())
        relevant_backends = [
            backend
            for backend in all_backends
            if backend.authority.journal_path_sha256 == journal_path_sha256
            and backend.authority.journal_execution_id == journal_execution_id
        ]
        current_backends = {
            (backend.authority.kind, backend.authority.journal_revision): backend
            for backend in relevant_backends
        }
        if len(current_backends) != len(relevant_backends):
            raise EvidenceStoreV2Error(
                "provider receipt inventory duplicates a journal revision"
            )
        expected_keys: set[tuple[str, int]] = set()
        ordered_records: list[RetainedStepEvidenceV2] = []
        for ordinal, completed in enumerate(transaction.completed_steps):
            candidates = records_by_digest.get(completed.evidence_sha256, [])
            if len(candidates) != 1:
                raise EvidenceStoreV2Error(
                    "completed prefix retained record is missing or ambiguous"
                )
            record = candidates[0]
            ordered_records.append(record)
            step = plan.steps[ordinal]
            if step.kind not in {"STACK_DRIFT_CHECK", "AGENTCORE_HARDEN"}:
                continue
            raw_projection = record.observer_evidence_mapping().get(
                "projection"
            )
            # Journal unit tests deliberately exercise the private retention
            # token with a marked fixture projection.  Such records cannot be
            # produced by ReleaseOutcomeComposerV2 and therefore own no live
            # provider receipt.  Every production-shaped observation below is
            # receipt-mandatory.
            if isinstance(raw_projection, Mapping) and "fixtureMarker" in (
                raw_projection
            ):
                continue
            receipt_kind = (
                "stack-drift"
                if step.kind == "STACK_DRIFT_CHECK"
                else "agentcore-hardening"
            )
            key = (receipt_kind, record.journal_revision)
            backend = current_backends.get(key)
            if backend is None:
                raise EvidenceStoreV2Error(
                    "completed provider effect lacks its exact receipt"
                )
            expected = self._expected_receipt_authority(
                plan=plan, record=record, receipt_kind=receipt_kind
            )
            if backend.authority != expected:
                raise EvidenceStoreV2Error(
                    "provider receipt attempt differs from completed evidence"
                )
            attempted, payload = backend.load()
            precondition_payload = backend.load_precondition()
            if not attempted or payload is None:
                raise EvidenceStoreV2Error(
                    "completed provider effect receipt is incomplete"
                )
            if step.kind == "STACK_DRIFT_CHECK":
                if ordinal < 1:
                    raise EvidenceStoreV2Error(
                        "stack drift receipt lacks an immediate predecessor"
                    )
                self._validate_stack_drift_receipt(
                    plan=plan,
                    record=record,
                    predecessor=ordered_records[ordinal - 1],
                    payload=payload,
                )
            else:
                if precondition_payload is None:
                    raise EvidenceStoreV2Error(
                        "completed AgentCore effect lacks its precondition"
                    )
                self._validate_agentcore_hardening_receipt(
                    plan=plan,
                    record=record,
                    payload=payload,
                    precondition_payload=precondition_payload,
                )
            expected_keys.add(key)

        count = transaction.completed_step_count
        if (
            transaction.state == "UNCERTAIN"
            and count < len(plan.steps)
            and plan.steps[count].kind
            in {"STACK_DRIFT_CHECK", "AGENTCORE_HARDEN"}
        ):
            step = plan.steps[count]
            receipt_kind = (
                "stack-drift"
                if step.kind == "STACK_DRIFT_CHECK"
                else "agentcore-hardening"
            )
            key = (receipt_kind, transaction.revision)
            backend = current_backends.get(key)
            if backend is not None:
                prefix = _completed_prefix_sha256(
                    [item.to_mapping() for item in transaction.completed_steps]
                )
                expected = _ReceiptAuthorityV2(
                    kind=receipt_kind,
                    release_plan_sha256=plan.digest(),
                    evidence_store_sha256=self.identity_sha256,
                    journal_path_sha256=journal_path_sha256,
                    journal_execution_id=journal_execution_id,
                    journal_revision=transaction.revision,
                    completed_prefix_sha256=prefix,
                    step_id=step.step_id,
                    subject=step.subject,
                    operation_sha256=transaction.uncertain_operation_sha256,
                )
                if backend.authority != expected:
                    raise EvidenceStoreV2Error(
                        "active provider receipt attempt crosses journal intent"
                    )
                expected_keys.add(key)
        if set(current_backends) != expected_keys:
            raise EvidenceStoreV2Error(
                "provider receipt inventory contains an unexpected operation"
            )

    def _all_transitions(
        self, *, plan_sha256: str
    ) -> list[ReleaseJournalTransitionV2]:
        directory_fd = self._plan_directory(plan_sha256)
        transitions: list[ReleaseJournalTransitionV2] = []
        for name in sorted(os.listdir(directory_fd)):
            if not name.startswith("transition-"):
                continue
            try:
                transition = ReleaseJournalTransitionV2.from_bytes(
                    self._read_secure(
                        directory_fd=directory_fd,
                        name=name,
                        label="journal transition record",
                    )
                )
            except (ContractError, OSError, TypeError, ValueError) as error:
                raise EvidenceStoreV2Error(
                    "journal transition inventory is invalid"
                ) from error
            expected_name = self._transition_name(
                execution_id=transition.journal_execution_id,
                from_revision=transition.prior_transaction.revision,
            )
            if name != expected_name:
                raise EvidenceStoreV2Error(
                    "journal transition filename differs"
                )
            transitions.append(transition)
        return transitions

    def _all_transition_commits(
        self, *, plan_sha256: str
    ) -> list[ReleaseJournalTransitionCommitV2]:
        directory_fd = self._plan_directory(plan_sha256)
        commits: list[ReleaseJournalTransitionCommitV2] = []
        for name in sorted(os.listdir(directory_fd)):
            if not name.startswith("commit-"):
                continue
            try:
                commit = ReleaseJournalTransitionCommitV2.from_bytes(
                    self._read_secure(
                        directory_fd=directory_fd,
                        name=name,
                        label="journal transition commit",
                    )
                )
            except (ContractError, OSError, TypeError, ValueError) as error:
                raise EvidenceStoreV2Error(
                    "journal transition commit inventory is invalid"
                ) from error
            expected_name = self._commit_name(
                execution_id=commit.journal_execution_id,
                from_revision=commit.from_revision,
            )
            if name != expected_name:
                raise EvidenceStoreV2Error(
                    "journal transition commit filename differs"
                )
            commits.append(commit)
        return commits

    def _audit_evidence_prefix(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> None:
        """Rebuild every journal-derived field from exact retained records."""

        try:
            canonical_plan = _canonical_release_plan_v2(plan)
            current = StagingTransactionV2.from_bytes(
                transaction.to_bytes(), plan=canonical_plan
            )
            self._require_binding(
                journal_path=journal_path,
                plan=canonical_plan,
                journal_execution_id=journal_execution_id,
            )
            path_sha256 = _journal_path_sha256(journal_path)
            inventory = self._all_records(plan_sha256=canonical_plan.digest())
            by_digest: dict[str, list[RetainedStepEvidenceV2]] = {}
            for record in inventory:
                by_digest.setdefault(record.digest(), []).append(record)
            self._audit_receipt_inventory(
                plan=canonical_plan,
                transaction=current,
                records_by_digest=by_digest,
                journal_path_sha256=path_sha256,
                journal_execution_id=journal_execution_id,
            )
            derived = {
                "foundationInputsSha256": "",
                "agentCoreStackId": "",
                "runtimeImageDigest": "",
                "runtimeId": "",
                "runtimeVersion": "",
                "runtimeArn": "",
                "runtimeEndpointId": "",
                "runtimeContextSha256": "",
                "routerTargetStackId": "",
                "routerChangeSetId": "",
                "cronTargetStackId": "",
                "cronChangeSetId": "",
                "routerCronChangesetsSha256": "",
                "routerCronApplicationSha256": "",
                "schedulerTargetStackId": "",
                "schedulerChangeSetId": "",
                "schedulerChangesetSha256": "",
                "schedulerApplicationSha256": "",
                "webTargetStackId": "",
                "webChangeSetId": "",
                "webChangesetSha256": "",
                "webApplicationSha256": "",
                "verificationSha256": "",
                "rollbackBaselineSha256": "",
            }
            prior_revision = -1
            completed_mappings: list[dict[str, str]] = []
            active_phase = ""
            phase_raw_records: list[dict[str, str]] = []
            for ordinal, item in enumerate(current.completed_steps):
                candidates = by_digest.get(item.evidence_sha256, [])
                if len(candidates) != 1:
                    raise EvidenceStoreV2Error(
                        "completed prefix retained record is missing or ambiguous"
                    )
                record = candidates[0]
                step = canonical_plan.steps[ordinal]
                prefix = _completed_prefix_sha256(completed_mappings)
                release_operation = _release_operation_sha256(
                    canonical_plan.digest(), step, prefix
                )
                expected_outcome_operation = _release_outcome_operation_sha256(
                    release_operation_sha256=release_operation,
                    journal_path_sha256=path_sha256,
                    journal_execution_id=journal_execution_id,
                    journal_revision=record.journal_revision,
                )
                if (
                    record.evidence_store_sha256 != self.identity_sha256
                    or record.journal_path_sha256 != path_sha256
                    or record.journal_execution_id != journal_execution_id
                    or record.plan_sha256 != canonical_plan.digest()
                    or record.completed_prefix_sha256 != prefix
                    or record.step_id != step.step_id
                    or record.subject != step.subject
                    or record.release_operation_sha256 != release_operation
                    or record.operation_sha256 != expected_outcome_operation
                    or record.disposition != "PRESENT"
                    or record.step_observation is None
                    or record.journal_revision <= prior_revision
                    or record.journal_revision >= current.revision
                ):
                    raise EvidenceStoreV2Error(
                        "completed prefix record differs from its journal execution"
                    )
                observation = record.step_observation
                observation.validate_plan_step(
                    canonical_plan,
                    completed_step_count=ordinal,
                    prior_agent_core_stack_id=derived["agentCoreStackId"],
                    prior_runtime_id=derived["runtimeId"],
                    prior_runtime_version=derived["runtimeVersion"],
                    prior_runtime_arn=derived["runtimeArn"],
                )
                if step.phase != active_phase:
                    active_phase = step.phase
                    phase_raw_records = []
                phase_raw_records.append(
                    {
                        "stepId": record.step_id,
                        "subject": record.subject,
                        "operationSha256": record.release_operation_sha256,
                        "observerEvidenceSha256": (
                            record.observer_evidence_sha256
                        ),
                    }
                )
                phase_field = _PHASE_EVIDENCE_FIELDS.get(step.phase)
                next_phase = (
                    canonical_plan.steps[ordinal + 1].phase
                    if ordinal + 1 < len(canonical_plan.steps)
                    else None
                )
                expected_phase_sha256 = ""
                if phase_field is not None and next_phase != step.phase:
                    expected_phase_sha256 = hashlib.sha256(
                        canonical_json_bytes(
                            {
                                "schema": (
                                    "personal-operator.phase-evidence.v1"
                                ),
                                "planSha256": canonical_plan.digest(),
                                "phase": step.phase,
                                "records": phase_raw_records,
                            }
                        )
                    ).hexdigest()
                    if (
                        observation.to_mapping()[phase_field]
                        != expected_phase_sha256
                    ):
                        raise EvidenceStoreV2Error(
                            "typed phase evidence differs from raw provider records"
                        )
                raw_provider = record.observer_evidence_mapping()
                raw_projection = raw_provider.get("projection")
                is_fixture = isinstance(raw_projection, Mapping) and (
                    "fixtureMarker" in raw_projection
                )
                if not is_fixture:
                    prior = replace(
                        current,
                        completed_step_count=ordinal,
                        completed_steps=current.completed_steps[:ordinal],
                        foundation_inputs_sha256=derived[
                            "foundationInputsSha256"
                        ],
                        agent_core_stack_id=derived["agentCoreStackId"],
                        runtime_image_digest=derived["runtimeImageDigest"],
                        runtime_id=derived["runtimeId"],
                        runtime_version=derived["runtimeVersion"],
                        runtime_arn=derived["runtimeArn"],
                        runtime_endpoint_id=derived["runtimeEndpointId"],
                        runtime_context_sha256=derived[
                            "runtimeContextSha256"
                        ],
                    )
                    expected_foundation = (
                        self._derive_foundation_runtime_inputs(
                            plan=canonical_plan,
                            transaction=prior,
                            journal_path_sha256=path_sha256,
                            journal_execution_id=journal_execution_id,
                            release_operation_sha256=release_operation,
                            provider_digest=record.observer_evidence_sha256,
                        )
                    )
                    is_release_verification = (
                        raw_provider.get("service")
                        == "local-release-verifier"
                        and raw_provider.get("operation") == "verify_release"
                    )
                    derivation_transaction = prior
                    if is_release_verification:
                        derivation_transaction = (
                            StagingTransactionV2.from_bytes(
                                replace(
                                    prior,
                                    state="WEB_APPLIED",
                                    last_stable_state="WEB_APPLIED",
                                    verification_sha256="",
                                    revision=record.journal_revision,
                                ).to_bytes(),
                                plan=canonical_plan,
                            )
                        )
                        if not isinstance(raw_projection, Mapping):
                            raise EvidenceStoreV2Error(
                                "release verification projection is malformed"
                            )
                        self._validate_release_verification_projection(
                            plan=canonical_plan,
                            transaction=derivation_transaction,
                            journal_path_sha256=path_sha256,
                            journal_execution_id=journal_execution_id,
                            projection=raw_projection,
                        )
                    expected_observation = self._derive_present_observation(
                        plan=canonical_plan,
                        transaction=derivation_transaction,
                        provider_mapping=raw_provider,
                        provider_digest=record.observer_evidence_sha256,
                        phase_evidence_sha256=expected_phase_sha256,
                        foundation_runtime_inputs=expected_foundation,
                        trusted_verification_projection=(
                            is_release_verification
                        ),
                    )
                    if observation != expected_observation:
                        raise EvidenceStoreV2Error(
                            "typed observation differs from raw provider derivation"
                        )
                if observation.foundation_runtime_inputs is not None:
                    derived["foundationInputsSha256"] = (
                        observation.foundation_runtime_inputs.digest()
                    )
                    derived["agentCoreStackId"] = (
                        observation.foundation_runtime_inputs.agent_core_stack_id
                    )
                observed_values = {
                    "agentCoreStackId": observation.agent_core_stack_id,
                    "runtimeImageDigest": observation.runtime_image_digest,
                    "runtimeId": observation.runtime_id,
                    "runtimeVersion": observation.runtime_version,
                    "runtimeArn": observation.runtime_arn,
                    "runtimeEndpointId": observation.runtime_endpoint_id,
                    "runtimeContextSha256": observation.runtime_context_sha256,
                    "routerTargetStackId": observation.router_target_stack_id,
                    "routerChangeSetId": observation.router_change_set_id,
                    "cronTargetStackId": observation.cron_target_stack_id,
                    "cronChangeSetId": observation.cron_change_set_id,
                    "routerCronChangesetsSha256": observation.router_cron_changesets_sha256,
                    "routerCronApplicationSha256": observation.router_cron_application_sha256,
                    "schedulerTargetStackId": observation.scheduler_target_stack_id,
                    "schedulerChangeSetId": observation.scheduler_change_set_id,
                    "schedulerChangesetSha256": observation.scheduler_changeset_sha256,
                    "schedulerApplicationSha256": observation.scheduler_application_sha256,
                    "webTargetStackId": observation.web_target_stack_id,
                    "webChangeSetId": observation.web_change_set_id,
                    "webChangesetSha256": observation.web_changeset_sha256,
                    "webApplicationSha256": observation.web_application_sha256,
                    "verificationSha256": observation.verification_sha256,
                }
                for name, value in observed_values.items():
                    if value:
                        derived[name] = value
                if step.kind == "BASELINE_OBSERVE":
                    derived["rollbackBaselineSha256"] = record.digest()
                completed_mappings.append(
                    {"stepId": step.step_id, "evidenceSha256": record.digest()}
                )
                prior_revision = record.journal_revision

            actual = current.to_mapping()
            for field, expected in derived.items():
                if actual[field] != expected:
                    raise EvidenceStoreV2Error(
                        f"journal-derived {field} differs from retained prefix"
                    )

            if current.failed_retained_evidence_sha256:
                candidates = by_digest.get(
                    current.failed_retained_evidence_sha256, []
                )
                if len(candidates) != 1:
                    raise EvidenceStoreV2Error(
                        "terminal failure retained record is missing"
                    )
                record = candidates[0]
                failure = record.failure_observation
                step = canonical_plan.steps[current.completed_step_count]
                prefix = _completed_prefix_sha256(completed_mappings)
                release_operation = _release_operation_sha256(
                    canonical_plan.digest(), step, prefix
                )
                expected_failure = self._derive_failure_observation(
                    plan=canonical_plan,
                    transaction=current,
                    release_operation_sha256=release_operation,
                    provider_mapping=record.observer_evidence_mapping(),
                    provider_digest=record.observer_evidence_sha256,
                )
                if (
                    record.disposition != "FAILED_RETAINED"
                    or failure is None
                    or record.evidence_store_sha256 != self.identity_sha256
                    or record.journal_path_sha256 != path_sha256
                    or record.journal_execution_id != journal_execution_id
                    or record.completed_prefix_sha256 != prefix
                    or record.release_operation_sha256 != release_operation
                    or current.failure_observation_sha256 != failure.digest()
                    or current.failed_step_id != failure.step_id
                    or current.failed_subject != failure.subject
                    or current.failed_operation_sha256
                    != failure.operation_sha256
                    or current.failure_reason != failure.failure_reason
                    or failure != expected_failure
                ):
                    raise EvidenceStoreV2Error(
                        "terminal journal fields differ from retained failure"
                    )
        except EvidenceStoreV2Error:
            raise
        except (AttributeError, ContractError, OSError, TypeError, ValueError) as error:
            raise EvidenceStoreV2Error(
                "completed prefix retained evidence is missing or invalid"
            ) from error

    def _relevant_transitions(
        self,
        *,
        plan: ReleasePlanV2,
        journal_path_sha256: str,
        journal_execution_id: str,
    ) -> list[ReleaseJournalTransitionV2]:
        transitions = [
            transition
            for transition in self._all_transitions(
                plan_sha256=plan.digest()
            )
            if transition.journal_execution_id == journal_execution_id
        ]
        for transition in transitions:
            if (
                transition.plan_sha256 != plan.digest()
                or transition.evidence_store_sha256 != self.identity_sha256
                or transition.journal_path_sha256 != journal_path_sha256
            ):
                raise EvidenceStoreV2Error(
                    "journal transition crosses its exact binding"
                )
        return sorted(
            transitions,
            key=lambda item: item.prior_transaction.revision,
        )

    def _relevant_commits(
        self,
        *,
        plan: ReleasePlanV2,
        journal_path_sha256: str,
        journal_execution_id: str,
    ) -> list[ReleaseJournalTransitionCommitV2]:
        commits = [
            commit
            for commit in self._all_transition_commits(
                plan_sha256=plan.digest()
            )
            if commit.journal_execution_id == journal_execution_id
        ]
        for commit in commits:
            if (
                commit.plan_sha256 != plan.digest()
                or commit.evidence_store_sha256 != self.identity_sha256
                or commit.journal_path_sha256 != journal_path_sha256
            ):
                raise EvidenceStoreV2Error(
                    "journal transition commit crosses its exact binding"
                )
        return sorted(commits, key=lambda item: item.from_revision)

    def _audit_revision_chain(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> None:
        canonical_plan = _canonical_release_plan_v2(plan)
        current = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=canonical_plan
        )
        binding = self._binding(journal_path=journal_path)
        path_sha256 = _journal_path_sha256(journal_path)
        if binding["journalExecutionId"] != journal_execution_id:
            raise EvidenceStoreV2Error(
                "journal revision chain execution differs"
            )
        initial_sha256 = binding["initialJournalSha256"]
        transitions = self._relevant_transitions(
            plan=canonical_plan,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        commits = self._relevant_commits(
            plan=canonical_plan,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        commits_by_revision: dict[int, ReleaseJournalTransitionCommitV2] = {}
        for commit in commits:
            if commit.from_revision in commits_by_revision:
                raise EvidenceStoreV2Error(
                    "journal revision has duplicate CAS commits"
                )
            commits_by_revision[commit.from_revision] = commit

        expected_revision = 0
        expected_sha256 = initial_sha256
        outcome_records = self._journal_records(
            plan_sha256=canonical_plan.digest(),
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        for index, transition in enumerate(transitions):
            prior = transition.prior_transaction
            next_transaction = transition.next_transaction
            if (
                prior.revision != expected_revision
                or transition.prior_journal_sha256 != expected_sha256
            ):
                raise EvidenceStoreV2Error(
                    "journal transition chain skips or rewrites a revision"
                )
            kind, evidence_sha256 = self._classify_transition(
                plan=canonical_plan,
                prior=prior,
                next_transaction=next_transaction,
                journal_path_sha256=path_sha256,
                journal_execution_id=journal_execution_id,
                outcome_records=outcome_records,
            )
            if (
                transition.transition_kind != kind
                or transition.evidence_sha256 != evidence_sha256
            ):
                raise EvidenceStoreV2Error(
                    "journal transition classification differs"
                )
            commit = commits_by_revision.get(prior.revision)
            if commit is None:
                if index != len(transitions) - 1:
                    raise EvidenceStoreV2Error(
                        "uncommitted journal transition is not the chain tip"
                    )
            elif (
                commit.to_revision != next_transaction.revision
                or commit.transition_sha256 != transition.digest()
                or commit.next_journal_sha256
                != transition.next_journal_sha256
            ):
                raise EvidenceStoreV2Error(
                    "journal CAS commit differs from its transition"
                )
            expected_revision = next_transaction.revision
            expected_sha256 = transition.next_journal_sha256

        transition_revisions = {
            item.prior_transaction.revision for item in transitions
        }
        if set(commits_by_revision) - transition_revisions:
            raise EvidenceStoreV2Error(
                "journal CAS commit has no exact transition"
            )
        current_sha256 = self._journal_sha256(current)
        if not transitions:
            if current.revision != 0 or current_sha256 != initial_sha256:
                raise EvidenceStoreV2Error(
                    "journal revision differs from its genesis binding"
                )
            return
        last = transitions[-1]
        last_committed = last.prior_transaction.revision in commits_by_revision
        if current_sha256 == last.next_journal_sha256:
            if current.revision != last.next_transaction.revision:
                raise EvidenceStoreV2Error(
                    "journal revision differs from its transition tip"
                )
            return
        if (
            not last_committed
            and current_sha256 == last.prior_journal_sha256
            and current.revision == last.prior_transaction.revision
        ):
            return
        raise EvidenceStoreV2Error(
            "journal revision is skipped or unexplained by its transition chain"
        )

    def _write_transition_commit(
        self, transition: ReleaseJournalTransitionV2
    ) -> ReleaseJournalTransitionCommitV2:
        commit = ReleaseJournalTransitionCommitV2.from_mapping(
            {
                "schema": ReleaseJournalTransitionCommitV2.SCHEMA,
                "planSha256": transition.plan_sha256,
                "evidenceStoreSha256": transition.evidence_store_sha256,
                "journalPathSha256": transition.journal_path_sha256,
                "journalExecutionId": transition.journal_execution_id,
                "fromRevision": transition.prior_transaction.revision,
                "toRevision": transition.next_transaction.revision,
                "transitionSha256": transition.digest(),
                "nextJournalSha256": transition.next_journal_sha256,
            }
        )
        directory_fd = self._plan_directory(transition.plan_sha256)
        self._append_secure(
            directory_fd=directory_fd,
            name=self._commit_name(
                execution_id=transition.journal_execution_id,
                from_revision=transition.prior_transaction.revision,
            ),
            payload=commit.to_bytes(),
            label="journal transition commit",
            reuse_identical=True,
        )
        return commit

    def prepare_transition(
        self,
        *,
        plan: ReleasePlanV2,
        prior: StagingTransactionV2,
        next_transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
        _token: object | None = None,
    ) -> ReleaseJournalTransitionV2:
        """Persist one exact authorized before/after CAS transition."""

        if _token is not _JOURNAL_TRANSITION_TOKEN:
            raise EvidenceStoreV2Error(
                "journal transitions are transaction-journal-owned"
            )
        canonical_plan = _canonical_release_plan_v2(plan)
        prior = StagingTransactionV2.from_bytes(
            prior.to_bytes(), plan=canonical_plan
        )
        next_transaction = StagingTransactionV2.from_bytes(
            next_transaction.to_bytes(), plan=canonical_plan
        )
        self._audit_evidence_prefix(
            plan=canonical_plan,
            transaction=next_transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        path_sha256 = _journal_path_sha256(journal_path)
        transitions = self._relevant_transitions(
            plan=canonical_plan,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        commits = self._relevant_commits(
            plan=canonical_plan,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        committed_revisions = {item.from_revision for item in commits}
        if transitions:
            tip = transitions[-1]
            if (
                tip.prior_transaction.revision not in committed_revisions
                and self._journal_sha256(prior) == tip.next_journal_sha256
            ):
                self._write_transition_commit(tip)
        kind, evidence_sha256 = self._classify_transition(
            plan=canonical_plan,
            prior=prior,
            next_transaction=next_transaction,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
            outcome_records=self._journal_records(
                plan_sha256=canonical_plan.digest(),
                journal_path_sha256=path_sha256,
                journal_execution_id=journal_execution_id,
            ),
        )
        transition = ReleaseJournalTransitionV2.from_mapping(
            {
                "schema": ReleaseJournalTransitionV2.SCHEMA,
                "planSha256": canonical_plan.digest(),
                "evidenceStoreSha256": self.identity_sha256,
                "journalPathSha256": path_sha256,
                "journalExecutionId": journal_execution_id,
                "transitionKind": kind,
                "evidenceSha256": evidence_sha256,
                "priorJournalSha256": self._journal_sha256(prior),
                "nextJournalSha256": self._journal_sha256(next_transaction),
                "priorTransaction": prior.to_mapping(),
                "nextTransaction": next_transaction.to_mapping(),
            }
        )
        directory_fd = self._plan_directory(canonical_plan.digest())
        self._append_secure(
            directory_fd=directory_fd,
            name=self._transition_name(
                execution_id=journal_execution_id,
                from_revision=prior.revision,
            ),
            payload=transition.to_bytes(),
            label="journal transition record",
            reuse_identical=True,
        )
        return transition

    def commit_transition(
        self,
        transition: ReleaseJournalTransitionV2,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _JOURNAL_TRANSITION_TOKEN:
            raise EvidenceStoreV2Error(
                "journal transition commits are transaction-journal-owned"
            )
        canonical = ReleaseJournalTransitionV2.from_bytes(
            transition.to_bytes()
        )
        self._write_transition_commit(canonical)

    def pending_recovery_transition(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
        _token: object | None = None,
    ) -> ReleaseJournalTransitionV2 | None:
        """Return one fully audited uncommitted tip for journal recovery."""

        if _token is not _JOURNAL_TRANSITION_TOKEN:
            raise EvidenceStoreV2Error(
                "journal recovery is transaction-journal-owned"
            )
        canonical_plan = _canonical_release_plan_v2(plan)
        current = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=canonical_plan
        )
        self.audit_prefix(
            plan=canonical_plan,
            transaction=current,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        path_sha256 = _journal_path_sha256(journal_path)
        transitions = self._relevant_transitions(
            plan=canonical_plan,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        if not transitions:
            return None
        commits = self._relevant_commits(
            plan=canonical_plan,
            journal_path_sha256=path_sha256,
            journal_execution_id=journal_execution_id,
        )
        tip = transitions[-1]
        if any(
            commit.from_revision == tip.prior_transaction.revision
            for commit in commits
        ):
            return None
        current_payload = current.to_bytes()
        if current_payload not in {
            tip.prior_transaction.to_bytes(),
            tip.next_transaction.to_bytes(),
        }:
            raise EvidenceStoreV2Error(
                "journal recovery state differs from its uncommitted tip"
            )

        # Audit the exact durable target before allowing either the journal CAS
        # or its commit acknowledgement.  Outcome transitions additionally
        # revalidate the retained record against the pre-transition journal;
        # recovery never asks a provider to recreate consumed evidence.
        self.audit_prefix(
            plan=canonical_plan,
            transaction=tip.next_transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        if tip.transition_kind.startswith("OUTCOME_"):
            record = self._outcome_for_transition(
                plan=canonical_plan,
                transaction=tip.prior_transaction,
                journal_path_sha256=path_sha256,
                journal_execution_id=journal_execution_id,
            )
            try:
                record.validate_transaction(
                    canonical_plan,
                    tip.prior_transaction,
                    evidence_store_sha256=self.identity_sha256,
                    journal_path_sha256=path_sha256,
                    journal_execution_id=journal_execution_id,
                )
            except ContractError as error:
                raise EvidenceStoreV2Error(
                    "journal recovery retained outcome is invalid"
                ) from error
            if record.digest() != tip.evidence_sha256:
                raise EvidenceStoreV2Error(
                    "journal recovery retained outcome differs"
                )
        return tip

    def audit_prefix(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> None:
        """Audit retained evidence and the exact persisted revision chain."""

        self._audit_evidence_prefix(
            plan=plan,
            transaction=transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )
        self._audit_revision_chain(
            plan=plan,
            transaction=transaction,
            journal_path=journal_path,
            journal_execution_id=journal_execution_id,
        )

    def retained_prefix_for_execution(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
    ) -> tuple[RetainedStepEvidenceV2, ...]:
        """Return the exact audited prefix for the current durable journal."""

        try:
            canonical_plan = _canonical_release_plan_v2(plan)
            current = StagingTransactionV2.from_bytes(
                transaction.to_bytes(), plan=canonical_plan
            )
            expected_journal = current.to_bytes()
            if read_regular_bytes(Path(journal_path)) != expected_journal:
                raise EvidenceStoreV2Error(
                    "requested transaction is not the current journal"
                )
            self._require_binding(
                journal_path=Path(journal_path),
                plan=canonical_plan,
                journal_execution_id=journal_execution_id,
            )
            self.audit_prefix(
                plan=canonical_plan,
                transaction=current,
                journal_path=Path(journal_path),
                journal_execution_id=journal_execution_id,
            )
            by_digest: dict[str, list[RetainedStepEvidenceV2]] = {}
            for record in self._all_records(
                plan_sha256=canonical_plan.digest()
            ):
                by_digest.setdefault(record.digest(), []).append(record)
            retained: list[RetainedStepEvidenceV2] = []
            for ordinal, completed in enumerate(current.completed_steps):
                candidates = by_digest.get(completed.evidence_sha256, [])
                if len(candidates) != 1:
                    raise EvidenceStoreV2Error(
                        "completed prefix retained record is missing or ambiguous"
                    )
                record = RetainedStepEvidenceV2.from_bytes(
                    candidates[0].to_bytes()
                )
                if (
                    record.digest() != completed.evidence_sha256
                    or record.step_id != completed.step_id
                    or record.step_id != canonical_plan.steps[ordinal].step_id
                ):
                    raise EvidenceStoreV2Error(
                        "completed prefix retained record differs"
                    )
                retained.append(record)
            if read_regular_bytes(Path(journal_path)) != expected_journal:
                raise EvidenceStoreV2Error(
                    "requested transaction is not the current journal"
                )
            return tuple(retained)
        except EvidenceStoreV2Error:
            raise
        except (
            AttributeError,
            ContractError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise EvidenceStoreV2Error(
                "current journal retained prefix is unavailable"
            ) from error


__all__ = [
    "EvidenceStoreV2Error",
    "ReleaseEvidenceStoreV2",
    "ReleaseOutcomeComposerV2",
    "VerifiedStepOutcomeV2",
]
