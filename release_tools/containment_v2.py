"""Fail-closed containment and retained-evidence purge contracts for release v2.

This module is deliberately separate from release mutation authority.  It has
no SDK, subprocess, credential, network, or production-provider adapter.  It
models an immutable destructive plan, a durable one-shot action journal, and a
pure fake provider used to exercise crash/reconciliation semantics.

An action is armed by durably appending an immutable ``UNCERTAIN`` record
before a private single-use authority is returned.  A restarted process can
inspect that hash-linked record but can never recover the authority.  Only an
exact token-gated observer record with two distinct, ordered retained sweep
proofs can advance the cursor; PRESENT, mixed, or unknown observations leave
the same revision UNCERTAIN.  There is intentionally no rollback transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, ClassVar, Mapping, Protocol, Sequence
import uuid

from release_tools.contracts import (
    MAX_CONTRACT_BYTES,
    ContractError,
    canonical_json_bytes,
    parse_canonical_object,
)


class ContainmentError(RuntimeError):
    """A destructive plan, authority, journal, or observation is invalid."""


_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_BUCKET = re.compile(
    r"(?=.{3,63}\Z)(?![0-9]+(?:\.[0-9]+){3}\Z)"
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
)
_S3_VERSION = re.compile(r"s3://([^/?#]+)/([^?#]+)\?versionId=([^&#]+)")
_S3_UPLOAD = re.compile(r"s3://([^/?#]+)/([^?#]+)\?uploadId=([^&#]+)")
_ECR_IMAGE = re.compile(
    r"arn:aws:ecr:eu-west-1:([0-9]{12}):repository/"
    r"([A-Za-z0-9._/-]+)@sha256:[0-9a-f]{64}"
)
_ECR_REPOSITORY = re.compile(
    r"arn:aws:ecr:eu-west-1:([0-9]{12}):repository/([A-Za-z0-9._/-]+)"
)
_KMS_KEY = re.compile(
    r"arn:aws:kms:eu-west-1:([0-9]{12}):key/"
    r"[0-9a-fA-F-]{8,128}"
)
_AUTHORITY_TOKEN = object()
_RETAINED_EVIDENCE_TOKEN = object()
_PLAN_PARSE_TOKEN = object()
_CONTAINED_JOURNAL_TOKEN = object()
_OBSERVER_TOKEN = object()


CONTAINMENT_RESOURCE_KINDS = (
    "CF_STACK_PERSONAL_OPERATOR_WEB",
    "CF_STACK_PERSONAL_OPERATOR_SCHEDULER",
    "CF_STACK_OPENCLAW_ROUTER",
    "CF_STACK_OPENCLAW_CRON",
    "AGENTCORE_ENDPOINT_RESOURCE_POLICY",
    "AGENTCORE_ENDPOINT",
    "AGENTCORE_RUNTIME_RESOURCE_POLICY",
    "AGENTCORE_RUNTIME",
    "CF_STACK_OPENCLAW_OBSERVABILITY",
    "CF_STACK_OPENCLAW_AGENTCORE",
    "CF_STACK_PERSONAL_OPERATOR_CAPABILITIES",
    "CF_STACK_OPENCLAW_GUARDRAILS",
    "CF_STACK_OPENCLAW_VPC",
    "CF_STACK_OPENCLAW_SECURITY",
    "CF_STACK_CDK_TOOLKIT",
)

_CONTAINMENT_OPERATIONS = (
    ("CLOUDFORMATION", "DELETE_STACK", "web"),
    ("CLOUDFORMATION", "DELETE_STACK", "scheduler"),
    ("CLOUDFORMATION", "DELETE_STACK", "router"),
    ("CLOUDFORMATION", "DELETE_STACK", "cron"),
    ("AGENTCORE_POLICY", "DELETE_RESOURCE_POLICY", "endpoint-policy"),
    ("AGENTCORE_CONTROL", "DELETE_ENDPOINT", "endpoint"),
    ("AGENTCORE_POLICY", "DELETE_RESOURCE_POLICY", "runtime-policy"),
    ("AGENTCORE_CONTROL", "DELETE_RUNTIME", "runtime"),
    ("CLOUDFORMATION", "DELETE_STACK", "observability"),
    ("CLOUDFORMATION", "DELETE_STACK", "agentcore"),
    ("CLOUDFORMATION", "DELETE_STACK", "capabilities"),
    ("CLOUDFORMATION", "DELETE_STACK", "guardrails"),
    ("CLOUDFORMATION", "DELETE_STACK", "vpc"),
    ("CLOUDFORMATION", "DELETE_STACK", "security"),
    ("CLOUDFORMATION", "DELETE_STACK", "cdktoolkit"),
)

PURGE_TARGET_KINDS = (
    "S3_MULTIPART_UPLOAD",
    "S3_OBJECT_VERSION",
    "S3_BUCKET",
    "ECR_IMAGE_REFERENCE",
    "ECR_SIGNING_CONFIGURATION",
    "SIGNER_SIGNING_PROFILE",
    "ECR_REPOSITORY",
    "DYNAMODB_TABLE",
    "CLOUDWATCH_LOG_GROUP",
    "KMS_KEY",
)
_PURGE_RANK = {kind: ordinal for ordinal, kind in enumerate(PURGE_TARGET_KINDS)}
_PURGE_OPERATION = {
    "S3_MULTIPART_UPLOAD": ("S3", "ABORT_MULTIPART_UPLOAD"),
    "S3_OBJECT_VERSION": ("S3", "DELETE_OBJECT_VERSION"),
    "S3_BUCKET": ("S3", "DELETE_BUCKET"),
    "ECR_IMAGE_REFERENCE": ("ECR", "BATCH_DELETE_IMAGE"),
    "ECR_SIGNING_CONFIGURATION": ("ECR", "DELETE_SIGNING_CONFIGURATION"),
    "SIGNER_SIGNING_PROFILE": ("SIGNER", "CANCEL_SIGNING_PROFILE"),
    "ECR_REPOSITORY": ("ECR", "DELETE_REPOSITORY"),
    "DYNAMODB_TABLE": ("DYNAMODB", "DELETE_TABLE"),
    "CLOUDWATCH_LOG_GROUP": ("CLOUDWATCH_LOGS", "DELETE_LOG_GROUP"),
    "KMS_KEY": ("KMS", "SCHEDULE_KEY_DELETION"),
}

_SWEEP_VALUES = frozenset(
    {"ABSENT", "PRESENT", "SCHEDULED", "CANCELED", "UNKNOWN"}
)
_NONTERMINAL_STATES = frozenset({"READY", "UNCERTAIN"})
_TERMINAL_STATES = frozenset(
    {"CONTAINED", "PURGED", "PURGED_WITH_SCHEDULED_KEYS"}
)


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContainmentError(f"{label} is invalid")
    return value


def _text(value: object, *, label: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ContainmentError(f"{label} is invalid")
    return value


def _exact_identity(value: object, *, label: str) -> str:
    try:
        identity = _text(value, label=label)
    except ContainmentError as error:
        raise ContainmentError(f"{label} must be one exact resource") from error
    if (
        "*" in identity
        or "?" in identity
        or identity.endswith("/")
        or identity.lower().endswith("prefix:")
        or identity in {".", ".."}
    ):
        raise ContainmentError(f"{label} must be one exact resource")
    return identity


def _exact_fields(raw: Mapping[str, Any], fields: frozenset[str], *, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ContainmentError(f"{label} fields are invalid")
    return dict(raw)


def _parse(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        return parse_canonical_object(payload)
    except (ContractError, TypeError, ValueError) as error:
        raise ContainmentError(f"{label} is not canonical") from error


@dataclass(frozen=True, slots=True)
class ReleaseClosureBindingV1:
    """Identity of the exact verified release and its retained evidence."""

    SCHEMA: ClassVar[str] = "personal-operator.release-closure-binding.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "account",
            "region",
            "releasePlanSha256",
            "releaseTransactionId",
            "releaseTransactionSha256",
            "releaseJournalPathSha256",
            "releaseJournalExecutionId",
            "evidenceStoreSha256",
            "releaseEvidenceSha256",
        }
    )

    account: str
    region: str
    release_plan_sha256: str
    release_transaction_id: str
    release_transaction_sha256: str
    release_journal_path_sha256: str
    release_journal_execution_id: str
    evidence_store_sha256: str
    release_evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.account, str) or _ACCOUNT.fullmatch(self.account) is None:
            raise ContainmentError("release account is invalid")
        if self.account == "000000000000":
            raise ContainmentError("release account is invalid")
        if self.region != "eu-west-1":
            raise ContainmentError("release region must be exactly eu-west-1")
        _sha256(self.release_plan_sha256, label="release plan digest")
        _text(self.release_transaction_id, label="release transaction identity")
        _sha256(self.release_transaction_sha256, label="release transaction digest")
        _sha256(self.release_journal_path_sha256, label="release journal path digest")
        _sha256(
            self.release_journal_execution_id,
            label="release journal execution identity",
        )
        _sha256(self.evidence_store_sha256, label="evidence store digest")
        _sha256(self.release_evidence_sha256, label="release evidence digest")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReleaseClosureBindingV1":
        value = _exact_fields(raw, cls.FIELDS, label="release closure binding")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("release closure binding schema is invalid")
        return cls(
            account=value["account"],
            region=value["region"],
            release_plan_sha256=value["releasePlanSha256"],
            release_transaction_id=value["releaseTransactionId"],
            release_transaction_sha256=value["releaseTransactionSha256"],
            release_journal_path_sha256=value["releaseJournalPathSha256"],
            release_journal_execution_id=value["releaseJournalExecutionId"],
            evidence_store_sha256=value["evidenceStoreSha256"],
            release_evidence_sha256=value["releaseEvidenceSha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "account": self.account,
            "region": self.region,
            "releasePlanSha256": self.release_plan_sha256,
            "releaseTransactionId": self.release_transaction_id,
            "releaseTransactionSha256": self.release_transaction_sha256,
            "releaseJournalPathSha256": self.release_journal_path_sha256,
            "releaseJournalExecutionId": self.release_journal_execution_id,
            "evidenceStoreSha256": self.evidence_store_sha256,
            "releaseEvidenceSha256": self.release_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class OwnedResourceIdentityV1:
    SCHEMA: ClassVar[str] = "personal-operator.owned-resource-identity.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "resourceKind",
            "resourceIdentity",
            "ownershipProofSha256",
            "observationEvidenceSha256",
        }
    )

    resource_kind: str
    resource_identity: str
    ownership_proof_sha256: str
    observation_evidence_sha256: str

    def __post_init__(self) -> None:
        if self.resource_kind not in CONTAINMENT_RESOURCE_KINDS:
            raise ContainmentError("owned resource kind is invalid")
        _exact_identity(self.resource_identity, label="owned resource identity")
        _sha256(self.ownership_proof_sha256, label="ownership proof digest")
        _sha256(
            self.observation_evidence_sha256,
            label="ownership observation evidence digest",
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OwnedResourceIdentityV1":
        value = _exact_fields(raw, cls.FIELDS, label="owned resource")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("owned resource schema is invalid")
        return cls(
            value["resourceKind"],
            value["resourceIdentity"],
            value["ownershipProofSha256"],
            value["observationEvidenceSha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "resourceKind": self.resource_kind,
            "resourceIdentity": self.resource_identity,
            "ownershipProofSha256": self.ownership_proof_sha256,
            "observationEvidenceSha256": self.observation_evidence_sha256,
        }


def _purge_identity(kind: str, value: object) -> str:
    identity = _text(value, label="purge target identity")
    if "*" in identity or "\x00" in identity:
        raise ContainmentError("purge target must be one exact resource")
    if kind == "S3_BUCKET":
        if _BUCKET.fullmatch(identity) is None:
            raise ContainmentError("purge target must be one exact S3 bucket")
    elif kind == "S3_OBJECT_VERSION":
        match = _S3_VERSION.fullmatch(identity)
        if match is None or _BUCKET.fullmatch(match.group(1)) is None:
            raise ContainmentError("purge target must be one exact S3 object version")
    elif kind == "S3_MULTIPART_UPLOAD":
        match = _S3_UPLOAD.fullmatch(identity)
        if match is None or _BUCKET.fullmatch(match.group(1)) is None:
            raise ContainmentError("purge target must be one exact S3 multipart upload")
    elif kind == "ECR_IMAGE_REFERENCE":
        if _ECR_IMAGE.fullmatch(identity) is None:
            raise ContainmentError("purge target must be one exact ECR image reference")
    elif kind == "ECR_REPOSITORY":
        if _ECR_REPOSITORY.fullmatch(identity) is None:
            raise ContainmentError("purge target must be one exact ECR repository")
    elif kind == "KMS_KEY":
        if _KMS_KEY.fullmatch(identity) is None:
            raise ContainmentError("purge target must be one exact KMS key ARN")
    elif not identity.startswith("arn:aws:") or "?" in identity or identity.endswith("/"):
        raise ContainmentError("purge target must be one exact ARN")
    return identity


@dataclass(frozen=True, slots=True)
class PurgeTargetV1:
    SCHEMA: ClassVar[str] = "personal-operator.purge-target.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "targetKind",
            "resourceIdentity",
            "ownershipProofSha256",
            "releaseEvidenceSha256",
            "releaseEvidenceEntrySha256",
        }
    )

    target_kind: str
    resource_identity: str
    ownership_proof_sha256: str
    release_evidence_sha256: str
    release_evidence_entry_sha256: str

    def __post_init__(self) -> None:
        if self.target_kind not in PURGE_TARGET_KINDS:
            raise ContainmentError("purge target kind is invalid")
        _purge_identity(self.target_kind, self.resource_identity)
        _sha256(self.ownership_proof_sha256, label="purge ownership proof digest")
        _sha256(
            self.release_evidence_sha256,
            label="purge release evidence root digest",
        )
        _sha256(
            self.release_evidence_entry_sha256,
            label="purge release evidence entry digest",
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PurgeTargetV1":
        value = _exact_fields(raw, cls.FIELDS, label="purge target")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("purge target schema is invalid")
        return cls(
            value["targetKind"],
            value["resourceIdentity"],
            value["ownershipProofSha256"],
            value["releaseEvidenceSha256"],
            value["releaseEvidenceEntrySha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "targetKind": self.target_kind,
            "resourceIdentity": self.resource_identity,
            "ownershipProofSha256": self.ownership_proof_sha256,
            "releaseEvidenceSha256": self.release_evidence_sha256,
            "releaseEvidenceEntrySha256": self.release_evidence_entry_sha256,
        }


def _validate_exact_release_identity(
    identity: str,
    binding: ReleaseClosureBindingV1,
    *,
    label: str,
    allow_non_arn: bool = False,
) -> None:
    """Bind every regional ARN to the exact preclosed release identity."""

    if not identity.startswith("arn:aws:"):
        if allow_non_arn:
            return
        raise ContainmentError(f"{label} must be an exact release ARN")
    parts = identity.split(":", 5)
    if len(parts) != 6:
        raise ContainmentError(f"{label} ARN is not exact")
    if parts[3] != binding.region or parts[4] != binding.account:
        raise ContainmentError(f"{label} account or region differs from the release")


class RetainedReleaseEvidenceV1:
    """Non-serializable capability over one canonical retained evidence inventory.

    Plans consume this capability rather than accepting caller-supplied resource
    strings.  The fake boundary below exists only for deterministic contract
    tests; a production adapter must mint the same capability only after its
    retained evidence store has authenticated the preclosed release record.
    """

    __slots__ = (
        "_binding",
        "_owned_resources",
        "_purge_targets",
        "_canonical_bytes",
        "_canonical_sha256",
    )

    def __init__(
        self,
        binding: ReleaseClosureBindingV1,
        owned_resources: Sequence[OwnedResourceIdentityV1],
        purge_targets: Sequence[PurgeTargetV1],
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _RETAINED_EVIDENCE_TOKEN:
            raise ContainmentError("retained release evidence is not constructible")
        canonical_binding = ReleaseClosureBindingV1.from_mapping(binding.to_mapping())
        resources = tuple(
            OwnedResourceIdentityV1.from_mapping(resource.to_mapping())
            for resource in owned_resources
        )
        targets = tuple(
            PurgeTargetV1.from_mapping(target.to_mapping()) for target in purge_targets
        )
        if tuple(resource.resource_kind for resource in resources) != CONTAINMENT_RESOURCE_KINDS:
            raise ContainmentError("retained evidence has no exact containment inventory")
        if len({resource.resource_identity for resource in resources}) != len(resources):
            raise ContainmentError("retained evidence containment identity is duplicated")
        target_order = tuple(
            (_PURGE_RANK[target.target_kind], target.resource_identity)
            for target in targets
        )
        if len({(target.target_kind, target.resource_identity) for target in targets}) != len(targets):
            raise ContainmentError("retained evidence purge target is duplicated")
        if not targets or target_order != tuple(sorted(target_order)):
            raise ContainmentError("retained evidence purge targets are not in safe exact order")
        for resource in resources:
            _validate_exact_release_identity(
                resource.resource_identity,
                canonical_binding,
                label="retained containment resource",
            )
        for target in targets:
            _validate_target_binding(target, canonical_binding)
            _validate_exact_release_identity(
                target.resource_identity,
                canonical_binding,
                label="retained purge target",
                allow_non_arn=target.target_kind.startswith("S3_"),
            )
        payload = canonical_json_bytes(
            {
                "schema": "personal-operator.retained-release-evidence-inventory.v1",
                "binding": canonical_binding.to_mapping(),
                "containmentResources": [item.to_mapping() for item in resources],
                "purgeTargets": [item.to_mapping() for item in targets],
            }
        )
        self._binding = canonical_binding
        self._owned_resources = resources
        self._purge_targets = targets
        self._canonical_bytes = payload
        self._canonical_sha256 = hashlib.sha256(payload).hexdigest()

    @property
    def binding(self) -> ReleaseClosureBindingV1:
        return self._binding

    @property
    def owned_resources(self) -> tuple[OwnedResourceIdentityV1, ...]:
        return self._owned_resources

    @property
    def purge_targets(self) -> tuple[PurgeTargetV1, ...]:
        return self._purge_targets

    @property
    def canonical_sha256(self) -> str:
        return self._canonical_sha256


class FakeRetainedReleaseEvidenceBoundaryV1:
    """Pure test boundary that canonicalizes synthetic retained evidence."""

    @staticmethod
    def retain(
        *,
        binding: ReleaseClosureBindingV1,
        owned_resources: Sequence[OwnedResourceIdentityV1],
        purge_targets: Sequence[PurgeTargetV1],
    ) -> RetainedReleaseEvidenceV1:
        return RetainedReleaseEvidenceV1(
            binding,
            owned_resources,
            purge_targets,
            _token=_RETAINED_EVIDENCE_TOKEN,
        )


@dataclass(frozen=True, slots=True)
class DestructiveActionV1:
    SCHEMA: ClassVar[str] = "personal-operator.destructive-action.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "ordinal",
            "actionId",
            "provider",
            "operation",
            "resourceKind",
            "resourceIdentity",
            "ownershipProofSha256",
            "sourceEvidenceSha256",
        }
    )

    ordinal: int
    action_id: str
    provider: str
    operation: str
    resource_kind: str
    resource_identity: str
    ownership_proof_sha256: str
    source_evidence_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ContainmentError("destructive action ordinal is invalid")
        _text(self.action_id, label="destructive action ID")
        _text(self.provider, label="destructive action provider")
        _text(self.operation, label="destructive action operation")
        _text(self.resource_kind, label="destructive action resource kind")
        _text(self.resource_identity, label="destructive action resource identity")
        _sha256(self.ownership_proof_sha256, label="action ownership proof digest")
        _sha256(self.source_evidence_sha256, label="action source evidence digest")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DestructiveActionV1":
        value = _exact_fields(raw, cls.FIELDS, label="destructive action")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("destructive action schema is invalid")
        return cls(
            value["ordinal"],
            value["actionId"],
            value["provider"],
            value["operation"],
            value["resourceKind"],
            value["resourceIdentity"],
            value["ownershipProofSha256"],
            value["sourceEvidenceSha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "ordinal": self.ordinal,
            "actionId": self.action_id,
            "provider": self.provider,
            "operation": self.operation,
            "resourceKind": self.resource_kind,
            "resourceIdentity": self.resource_identity,
            "ownershipProofSha256": self.ownership_proof_sha256,
            "sourceEvidenceSha256": self.source_evidence_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _containment_actions(resources: Sequence[OwnedResourceIdentityV1]) -> tuple[DestructiveActionV1, ...]:
    return tuple(
        DestructiveActionV1(
            ordinal=ordinal,
            action_id=f"contain-{ordinal:02d}-{slug}",
            provider=provider,
            operation=operation,
            resource_kind=resource.resource_kind,
            resource_identity=resource.resource_identity,
            ownership_proof_sha256=resource.ownership_proof_sha256,
            source_evidence_sha256=resource.observation_evidence_sha256,
        )
        for ordinal, (resource, (provider, operation, slug)) in enumerate(
            zip(resources, _CONTAINMENT_OPERATIONS, strict=True)
        )
    )


def _purge_actions(targets: Sequence[PurgeTargetV1]) -> tuple[DestructiveActionV1, ...]:
    return tuple(
        DestructiveActionV1(
            ordinal=ordinal,
            action_id=f"purge-{ordinal:04d}-{target.target_kind.lower().replace('_', '-')}",
            provider=_PURGE_OPERATION[target.target_kind][0],
            operation=_PURGE_OPERATION[target.target_kind][1],
            resource_kind=target.target_kind,
            resource_identity=target.resource_identity,
            ownership_proof_sha256=target.ownership_proof_sha256,
            source_evidence_sha256=target.release_evidence_entry_sha256,
        )
        for ordinal, target in enumerate(targets)
    )


@dataclass(frozen=True, slots=True, init=False)
class ContainmentPlanV1:
    SCHEMA: ClassVar[str] = "personal-operator.containment-plan.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "operationId",
            "binding",
            "retainedEvidenceSha256",
            "ownedResources",
            "actions",
        }
    )
    kind: ClassVar[str] = "CONTAINMENT"

    operation_id: str
    binding: ReleaseClosureBindingV1
    retained_evidence_sha256: str
    owned_resources: tuple[OwnedResourceIdentityV1, ...]
    actions: tuple[DestructiveActionV1, ...]
    _authorized: bool = field(compare=False, repr=False)

    def __init__(
        self,
        operation_id: str,
        binding: ReleaseClosureBindingV1,
        retained_evidence_sha256: str,
        owned_resources: tuple[OwnedResourceIdentityV1, ...],
        actions: tuple[DestructiveActionV1, ...],
        *,
        _token: object | None = None,
    ) -> None:
        if _token not in {_RETAINED_EVIDENCE_TOKEN, _PLAN_PARSE_TOKEN}:
            raise ContainmentError(
                "containment plan requires retained release evidence capability"
            )
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "retained_evidence_sha256", retained_evidence_sha256)
        object.__setattr__(self, "owned_resources", owned_resources)
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "_authorized", _token is _RETAINED_EVIDENCE_TOKEN)
        self.__post_init__()

    def __post_init__(self) -> None:
        _sha256(self.operation_id, label="containment operation identity")
        _sha256(
            self.retained_evidence_sha256,
            label="retained release evidence digest",
        )
        canonical_binding = ReleaseClosureBindingV1.from_mapping(self.binding.to_mapping())
        if canonical_binding != self.binding:
            raise ContainmentError("containment release binding is not canonical")
        if len(self.owned_resources) != len(CONTAINMENT_RESOURCE_KINDS):
            raise ContainmentError("containment requires the exact inventory")
        if tuple(resource.resource_kind for resource in self.owned_resources) != CONTAINMENT_RESOURCE_KINDS:
            raise ContainmentError("containment resources are not in the exact order")
        if len({resource.resource_identity for resource in self.owned_resources}) != len(self.owned_resources):
            raise ContainmentError("containment resource identity is duplicated")
        expected = _containment_actions(self.owned_resources)
        if self.actions != expected:
            raise ContainmentError("containment derived actions differ")

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        retained_evidence: RetainedReleaseEvidenceV1,
    ) -> "ContainmentPlanV1":
        if not isinstance(retained_evidence, RetainedReleaseEvidenceV1):
            raise ContainmentError(
                "containment plan requires retained release evidence capability"
            )
        resources = retained_evidence.owned_resources
        return cls(
            operation_id,
            retained_evidence.binding,
            retained_evidence.canonical_sha256,
            resources,
            _containment_actions(resources),
            _token=_RETAINED_EVIDENCE_TOKEN,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ContainmentPlanV1":
        value = _exact_fields(raw, cls.FIELDS, label="containment plan")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("containment plan schema is invalid")
        if not isinstance(value["ownedResources"], list) or not isinstance(value["actions"], list):
            raise ContainmentError("containment plan inventories are invalid")
        return cls(
            value["operationId"],
            ReleaseClosureBindingV1.from_mapping(value["binding"]),
            value["retainedEvidenceSha256"],
            tuple(OwnedResourceIdentityV1.from_mapping(item) for item in value["ownedResources"]),
            tuple(DestructiveActionV1.from_mapping(item) for item in value["actions"]),
            _token=_PLAN_PARSE_TOKEN,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ContainmentPlanV1":
        return cls.from_mapping(_parse(payload, label="containment plan"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "operationId": self.operation_id,
            "binding": self.binding.to_mapping(),
            "retainedEvidenceSha256": self.retained_evidence_sha256,
            "ownedResources": [resource.to_mapping() for resource in self.owned_resources],
            "actions": [action.to_mapping() for action in self.actions],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


class ExactContainedJournalAuthorityV1:
    """One-shot proof that one exact containment journal is terminal."""

    __slots__ = (
        "_binding",
        "_retained_evidence_sha256",
        "_containment_plan_sha256",
        "_containment_journal_sha256",
        "_consumed",
    )

    def __init__(
        self,
        *,
        binding: ReleaseClosureBindingV1,
        retained_evidence_sha256: str,
        containment_plan_sha256: str,
        containment_journal_sha256: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _CONTAINED_JOURNAL_TOKEN:
            raise ContainmentError(
                "exact contained journal authority is not constructible"
            )
        self._binding = ReleaseClosureBindingV1.from_mapping(binding.to_mapping())
        self._retained_evidence_sha256 = _sha256(
            retained_evidence_sha256,
            label="contained retained evidence digest",
        )
        self._containment_plan_sha256 = _sha256(
            containment_plan_sha256,
            label="contained plan digest",
        )
        self._containment_journal_sha256 = _sha256(
            containment_journal_sha256,
            label="contained journal digest",
        )
        self._consumed = False

    def consume(
        self,
        retained_evidence: RetainedReleaseEvidenceV1,
    ) -> tuple[ReleaseClosureBindingV1, str, str, str]:
        if self._consumed:
            raise ContainmentError("exact contained journal authority is already consumed")
        if not isinstance(retained_evidence, RetainedReleaseEvidenceV1):
            raise ContainmentError("purge requires retained release evidence capability")
        if (
            retained_evidence.binding != self._binding
            or retained_evidence.canonical_sha256 != self._retained_evidence_sha256
        ):
            raise ContainmentError("contained journal and retained evidence differ")
        self._consumed = True
        return (
            self._binding,
            self._retained_evidence_sha256,
            self._containment_plan_sha256,
            self._containment_journal_sha256,
        )


@dataclass(frozen=True, slots=True, init=False)
class PurgePlanV1:
    SCHEMA: ClassVar[str] = "personal-operator.purge-plan.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "operationId",
            "binding",
            "retainedEvidenceSha256",
            "containmentPlanSha256",
            "containmentJournalSha256",
            "targets",
            "actions",
        }
    )
    kind: ClassVar[str] = "PURGE"

    operation_id: str
    binding: ReleaseClosureBindingV1
    retained_evidence_sha256: str
    containment_plan_sha256: str
    containment_journal_sha256: str
    targets: tuple[PurgeTargetV1, ...]
    actions: tuple[DestructiveActionV1, ...]
    _authorized: bool = field(compare=False, repr=False)

    def __init__(
        self,
        operation_id: str,
        binding: ReleaseClosureBindingV1,
        retained_evidence_sha256: str,
        containment_plan_sha256: str,
        containment_journal_sha256: str,
        targets: tuple[PurgeTargetV1, ...],
        actions: tuple[DestructiveActionV1, ...],
        *,
        _token: object | None = None,
    ) -> None:
        if _token not in {_CONTAINED_JOURNAL_TOKEN, _PLAN_PARSE_TOKEN}:
            raise ContainmentError(
                "purge plan requires exact terminal contained journal capability"
            )
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "retained_evidence_sha256", retained_evidence_sha256)
        object.__setattr__(self, "containment_plan_sha256", containment_plan_sha256)
        object.__setattr__(self, "containment_journal_sha256", containment_journal_sha256)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "_authorized", _token is _CONTAINED_JOURNAL_TOKEN)
        self.__post_init__()

    def __post_init__(self) -> None:
        _sha256(self.operation_id, label="purge operation identity")
        ReleaseClosureBindingV1.from_mapping(self.binding.to_mapping())
        _sha256(
            self.retained_evidence_sha256,
            label="retained release evidence digest",
        )
        _sha256(self.containment_plan_sha256, label="containment plan digest")
        _sha256(self.containment_journal_sha256, label="contained journal digest")
        if not self.targets:
            raise ContainmentError("purge requires at least one exact retained target")
        identities = tuple((target.target_kind, target.resource_identity) for target in self.targets)
        if len(set(identities)) != len(identities):
            raise ContainmentError("purge target is duplicated")
        order = tuple((_PURGE_RANK[target.target_kind], target.resource_identity) for target in self.targets)
        if order != tuple(sorted(order)):
            raise ContainmentError("purge targets are not in safe exact order")
        for target in self.targets:
            _validate_target_binding(target, self.binding)
        if self.actions != _purge_actions(self.targets):
            raise ContainmentError("purge derived actions differ")

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        retained_evidence: RetainedReleaseEvidenceV1,
        contained_journal: ExactContainedJournalAuthorityV1,
    ) -> "PurgePlanV1":
        if not isinstance(contained_journal, ExactContainedJournalAuthorityV1):
            raise ContainmentError(
                "purge requires exact terminal contained journal capability"
            )
        (
            binding,
            retained_evidence_sha256,
            containment_plan_sha256,
            containment_journal_sha256,
        ) = contained_journal.consume(retained_evidence)
        exact_targets = retained_evidence.purge_targets
        return cls(
            operation_id,
            binding,
            retained_evidence_sha256,
            containment_plan_sha256,
            containment_journal_sha256,
            exact_targets,
            _purge_actions(exact_targets),
            _token=_CONTAINED_JOURNAL_TOKEN,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PurgePlanV1":
        value = _exact_fields(raw, cls.FIELDS, label="purge plan")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("purge plan schema is invalid")
        if not isinstance(value["targets"], list) or not isinstance(value["actions"], list):
            raise ContainmentError("purge plan inventories are invalid")
        return cls(
            value["operationId"],
            ReleaseClosureBindingV1.from_mapping(value["binding"]),
            value["retainedEvidenceSha256"],
            value["containmentPlanSha256"],
            value["containmentJournalSha256"],
            tuple(PurgeTargetV1.from_mapping(item) for item in value["targets"]),
            tuple(DestructiveActionV1.from_mapping(item) for item in value["actions"]),
            _token=_PLAN_PARSE_TOKEN,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "PurgePlanV1":
        return cls.from_mapping(_parse(payload, label="purge plan"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "operationId": self.operation_id,
            "binding": self.binding.to_mapping(),
            "retainedEvidenceSha256": self.retained_evidence_sha256,
            "containmentPlanSha256": self.containment_plan_sha256,
            "containmentJournalSha256": self.containment_journal_sha256,
            "targets": [target.to_mapping() for target in self.targets],
            "actions": [action.to_mapping() for action in self.actions],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _validate_target_binding(target: PurgeTargetV1, binding: ReleaseClosureBindingV1) -> None:
    if target.release_evidence_sha256 != binding.release_evidence_sha256:
        raise ContainmentError("purge target release evidence root differs")
    identity = target.resource_identity
    if identity.startswith("arn:aws:"):
        parts = identity.split(":", 5)
        if len(parts) != 6:
            raise ContainmentError("purge target ARN is not exact")
        region = parts[3]
        account = parts[4]
        if region and region != binding.region:
            raise ContainmentError("purge target region differs from the release")
        if account and account != binding.account:
            raise ContainmentError("purge target account differs from the release")


class _DestructivePlan(Protocol):
    kind: ClassVar[str]
    binding: ReleaseClosureBindingV1
    actions: tuple[DestructiveActionV1, ...]

    def to_bytes(self) -> bytes: ...
    def digest(self) -> str: ...


def _canonical_plan(plan: _DestructivePlan) -> ContainmentPlanV1 | PurgePlanV1:
    if isinstance(plan, ContainmentPlanV1):
        parsed = ContainmentPlanV1.from_bytes(plan.to_bytes())
        if parsed != plan:
            raise ContainmentError("containment plan is not canonical")
        return plan
    if isinstance(plan, PurgePlanV1):
        parsed = PurgePlanV1.from_bytes(plan.to_bytes())
        if parsed != plan:
            raise ContainmentError("purge plan is not canonical")
        return plan
    raise ContainmentError("destructive plan type is invalid")


@dataclass(frozen=True, slots=True)
class DestructiveAttemptV1:
    SCHEMA: ClassVar[str] = "personal-operator.destructive-attempt.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "planSha256",
            "journalPathSha256",
            "journalExecutionId",
            "journalRevision",
            "actionOrdinal",
            "actionSha256",
            "resourceKind",
            "resourceIdentity",
            "ownershipProofSha256",
        }
    )

    plan_sha256: str
    journal_path_sha256: str
    journal_execution_id: str
    journal_revision: int
    action_ordinal: int
    action_sha256: str
    resource_kind: str
    resource_identity: str
    ownership_proof_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.plan_sha256, label="attempt plan digest")
        _sha256(self.journal_path_sha256, label="attempt journal path digest")
        _sha256(self.journal_execution_id, label="attempt journal execution identity")
        if isinstance(self.journal_revision, bool) or not isinstance(self.journal_revision, int) or self.journal_revision < 1:
            raise ContainmentError("attempt journal revision is invalid")
        if isinstance(self.action_ordinal, bool) or not isinstance(self.action_ordinal, int) or self.action_ordinal < 0:
            raise ContainmentError("attempt action ordinal is invalid")
        _sha256(self.action_sha256, label="attempt action digest")
        _text(self.resource_kind, label="attempt resource kind")
        _text(self.resource_identity, label="attempt resource identity")
        _sha256(self.ownership_proof_sha256, label="attempt ownership proof digest")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DestructiveAttemptV1":
        value = _exact_fields(raw, cls.FIELDS, label="destructive attempt")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("destructive attempt schema is invalid")
        return cls(
            value["planSha256"],
            value["journalPathSha256"],
            value["journalExecutionId"],
            value["journalRevision"],
            value["actionOrdinal"],
            value["actionSha256"],
            value["resourceKind"],
            value["resourceIdentity"],
            value["ownershipProofSha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
            "journalRevision": self.journal_revision,
            "actionOrdinal": self.action_ordinal,
            "actionSha256": self.action_sha256,
            "resourceKind": self.resource_kind,
            "resourceIdentity": self.resource_identity,
            "ownershipProofSha256": self.ownership_proof_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


class FreshContainmentAuthorityV1:
    """Non-serializable, single-use authority minted after journal durability."""

    __slots__ = ("_attempt", "_consumed")

    def __init__(self, attempt: DestructiveAttemptV1, *, _token: object | None = None) -> None:
        if _token is not _AUTHORITY_TOKEN:
            raise ContainmentError("fresh containment authority is not constructible")
        self._attempt = DestructiveAttemptV1.from_mapping(attempt.to_mapping())
        self._consumed = False

    def consume(self, action: DestructiveActionV1) -> DestructiveAttemptV1:
        if self._consumed:
            raise ContainmentError("fresh containment authority is already consumed")
        canonical = DestructiveActionV1.from_mapping(action.to_mapping())
        if (
            canonical.ordinal != self._attempt.action_ordinal
            or canonical.digest() != self._attempt.action_sha256
            or canonical.resource_kind != self._attempt.resource_kind
            or canonical.resource_identity != self._attempt.resource_identity
            or canonical.ownership_proof_sha256 != self._attempt.ownership_proof_sha256
        ):
            raise ContainmentError("fresh containment authority binding differs")
        self._consumed = True
        return self._attempt


@dataclass(frozen=True, slots=True, init=False)
class DestructiveObservationV1:
    SCHEMA: ClassVar[str] = "personal-operator.destructive-observation.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "planSha256",
            "journalPathSha256",
            "journalExecutionId",
            "attemptSha256",
            "actionSha256",
            "resourceKind",
            "resourceIdentity",
            "ownershipProofSha256",
            "sweepOne",
            "sweepOneSequence",
            "sweepOneEvidenceSha256",
            "sweepTwo",
            "sweepTwoSequence",
            "sweepTwoEvidenceSha256",
            "observerEvidenceSha256",
        }
    )

    plan_sha256: str
    journal_path_sha256: str
    journal_execution_id: str
    attempt_sha256: str
    action_sha256: str
    resource_kind: str
    resource_identity: str
    ownership_proof_sha256: str
    sweep_one: str
    sweep_one_sequence: int
    sweep_one_evidence_sha256: str
    sweep_two: str
    sweep_two_sequence: int
    sweep_two_evidence_sha256: str
    observer_evidence_sha256: str
    _reconcilable: bool = field(compare=False, repr=False)

    def __init__(
        self,
        plan_sha256: str,
        journal_path_sha256: str,
        journal_execution_id: str,
        attempt_sha256: str,
        action_sha256: str,
        resource_kind: str,
        resource_identity: str,
        ownership_proof_sha256: str,
        sweep_one: str,
        sweep_one_sequence: int,
        sweep_one_evidence_sha256: str,
        sweep_two: str,
        sweep_two_sequence: int,
        sweep_two_evidence_sha256: str,
        observer_evidence_sha256: str,
        *,
        _token: object | None = None,
        _reconcilable: bool = False,
    ) -> None:
        if _token is not _OBSERVER_TOKEN:
            raise ContainmentError(
                "destructive observation requires a closed observer token"
            )
        values = {
            "plan_sha256": plan_sha256,
            "journal_path_sha256": journal_path_sha256,
            "journal_execution_id": journal_execution_id,
            "attempt_sha256": attempt_sha256,
            "action_sha256": action_sha256,
            "resource_kind": resource_kind,
            "resource_identity": resource_identity,
            "ownership_proof_sha256": ownership_proof_sha256,
            "sweep_one": sweep_one,
            "sweep_one_sequence": sweep_one_sequence,
            "sweep_one_evidence_sha256": sweep_one_evidence_sha256,
            "sweep_two": sweep_two,
            "sweep_two_sequence": sweep_two_sequence,
            "sweep_two_evidence_sha256": sweep_two_evidence_sha256,
            "observer_evidence_sha256": observer_evidence_sha256,
        }
        for field, value in values.items():
            object.__setattr__(self, field, value)
        object.__setattr__(self, "_reconcilable", _reconcilable)
        self.__post_init__()

    def __post_init__(self) -> None:
        _sha256(self.plan_sha256, label="observation plan digest")
        _sha256(self.journal_path_sha256, label="observation journal path digest")
        _sha256(self.journal_execution_id, label="observation journal execution identity")
        _sha256(self.attempt_sha256, label="observation attempt digest")
        _sha256(self.action_sha256, label="observation action digest")
        _text(self.resource_kind, label="observation resource kind")
        _text(self.resource_identity, label="observation resource identity")
        _sha256(self.ownership_proof_sha256, label="observation ownership proof digest")
        if self.sweep_one not in _SWEEP_VALUES or self.sweep_two not in _SWEEP_VALUES:
            raise ContainmentError("observation sweep value is invalid")
        if (
            isinstance(self.sweep_one_sequence, bool)
            or not isinstance(self.sweep_one_sequence, int)
            or self.sweep_one_sequence < 1
            or self.sweep_two_sequence != self.sweep_one_sequence + 1
        ):
            raise ContainmentError("observation sweeps are not distinct and ordered")
        _sha256(
            self.sweep_one_evidence_sha256,
            label="first sweep retained evidence digest",
        )
        _sha256(
            self.sweep_two_evidence_sha256,
            label="second sweep retained evidence digest",
        )
        if self.sweep_one_evidence_sha256 == self.sweep_two_evidence_sha256:
            raise ContainmentError("observation sweep evidence is not distinct")
        expected_sweep_one = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.destructive-sweep-evidence.v1",
                    "ordinal": 1,
                    "sequence": self.sweep_one_sequence,
                    "attemptSha256": self.attempt_sha256,
                    "actionSha256": self.action_sha256,
                    "value": self.sweep_one,
                    "previousSweepEvidenceSha256": "0" * 64,
                }
            )
        ).hexdigest()
        expected_sweep_two = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.destructive-sweep-evidence.v1",
                    "ordinal": 2,
                    "sequence": self.sweep_two_sequence,
                    "attemptSha256": self.attempt_sha256,
                    "actionSha256": self.action_sha256,
                    "value": self.sweep_two,
                    "previousSweepEvidenceSha256": self.sweep_one_evidence_sha256,
                }
            )
        ).hexdigest()
        if (
            self.sweep_one_evidence_sha256 != expected_sweep_one
            or self.sweep_two_evidence_sha256 != expected_sweep_two
        ):
            raise ContainmentError("retained sweep evidence binding differs")
        _sha256(self.observer_evidence_sha256, label="observer evidence digest")
        expected_observer_evidence = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.destructive-observer-evidence.v1",
                    "planSha256": self.plan_sha256,
                    "attemptSha256": self.attempt_sha256,
                    "actionSha256": self.action_sha256,
                    "sweepOne": self.sweep_one,
                    "sweepOneSequence": self.sweep_one_sequence,
                    "sweepOneEvidenceSha256": self.sweep_one_evidence_sha256,
                    "sweepTwo": self.sweep_two,
                    "sweepTwoSequence": self.sweep_two_sequence,
                    "sweepTwoEvidenceSha256": self.sweep_two_evidence_sha256,
                }
            )
        ).hexdigest()
        if self.observer_evidence_sha256 != expected_observer_evidence:
            raise ContainmentError("closed observer evidence binding differs")

    @property
    def disposition(self) -> str:
        if self.sweep_one == self.sweep_two and self.sweep_one in {
            "ABSENT",
            "PRESENT",
            "SCHEDULED",
            "CANCELED",
        }:
            return self.sweep_one
        return "AMBIGUOUS"

    @classmethod
    def _from_mapping(cls, raw: Mapping[str, Any]) -> "DestructiveObservationV1":
        value = _exact_fields(raw, cls.FIELDS, label="destructive observation")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("destructive observation schema is invalid")
        return cls(
            value["planSha256"],
            value["journalPathSha256"],
            value["journalExecutionId"],
            value["attemptSha256"],
            value["actionSha256"],
            value["resourceKind"],
            value["resourceIdentity"],
            value["ownershipProofSha256"],
            value["sweepOne"],
            value["sweepOneSequence"],
            value["sweepOneEvidenceSha256"],
            value["sweepTwo"],
            value["sweepTwoSequence"],
            value["sweepTwoEvidenceSha256"],
            value["observerEvidenceSha256"],
            _token=_OBSERVER_TOKEN,
            _reconcilable=False,
        )

    @classmethod
    def _from_bytes(cls, payload: bytes) -> "DestructiveObservationV1":
        return cls._from_mapping(_parse(payload, label="destructive observation"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
            "attemptSha256": self.attempt_sha256,
            "actionSha256": self.action_sha256,
            "resourceKind": self.resource_kind,
            "resourceIdentity": self.resource_identity,
            "ownershipProofSha256": self.ownership_proof_sha256,
            "sweepOne": self.sweep_one,
            "sweepOneSequence": self.sweep_one_sequence,
            "sweepOneEvidenceSha256": self.sweep_one_evidence_sha256,
            "sweepTwo": self.sweep_two,
            "sweepTwoSequence": self.sweep_two_sequence,
            "sweepTwoEvidenceSha256": self.sweep_two_evidence_sha256,
            "observerEvidenceSha256": self.observer_evidence_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class _JournalRecordV1:
    SCHEMA: ClassVar[str] = "personal-operator.destructive-journal.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "planSchema",
            "planSha256",
            "account",
            "region",
            "releasePlanSha256",
            "releaseTransactionId",
            "releaseTransactionSha256",
            "releaseJournalPathSha256",
            "releaseJournalExecutionId",
            "evidenceStoreSha256",
            "releaseEvidenceSha256",
            "journalPathSha256",
            "journalExecutionId",
            "state",
            "cursor",
            "completedActionSha256",
            "completedAttempts",
            "completedObservations",
            "currentAttempt",
            "scheduledKeyCount",
            "revision",
            "previousRecordSha256",
        }
    )

    plan_schema: str
    plan_sha256: str
    binding: ReleaseClosureBindingV1
    journal_path_sha256: str
    journal_execution_id: str
    state: str
    cursor: int
    completed_action_sha256: tuple[str, ...]
    completed_attempts: tuple[DestructiveAttemptV1, ...]
    completed_observations: tuple[DestructiveObservationV1, ...]
    current_attempt: DestructiveAttemptV1 | None
    scheduled_key_count: int
    revision: int
    previous_record_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "_JournalRecordV1":
        value = _exact_fields(raw, cls.FIELDS, label="destructive journal")
        if value["schema"] != cls.SCHEMA:
            raise ContainmentError("destructive journal schema is invalid")
        completed = value["completedActionSha256"]
        raw_completed_attempts = value["completedAttempts"]
        raw_completed_observations = value["completedObservations"]
        if (
            not isinstance(completed, list)
            or not isinstance(raw_completed_attempts, list)
            or not isinstance(raw_completed_observations, list)
        ):
            raise ContainmentError("destructive journal completed prefix is invalid")
        raw_attempt = value["currentAttempt"]
        if raw_attempt == {}:
            attempt = None
        elif isinstance(raw_attempt, Mapping):
            attempt = DestructiveAttemptV1.from_mapping(raw_attempt)
        else:
            raise ContainmentError("destructive journal attempt is invalid")
        binding = ReleaseClosureBindingV1(
            account=value["account"],
            region=value["region"],
            release_plan_sha256=value["releasePlanSha256"],
            release_transaction_id=value["releaseTransactionId"],
            release_transaction_sha256=value["releaseTransactionSha256"],
            release_journal_path_sha256=value["releaseJournalPathSha256"],
            release_journal_execution_id=value["releaseJournalExecutionId"],
            evidence_store_sha256=value["evidenceStoreSha256"],
            release_evidence_sha256=value["releaseEvidenceSha256"],
        )
        return cls(
            plan_schema=value["planSchema"],
            plan_sha256=value["planSha256"],
            binding=binding,
            journal_path_sha256=value["journalPathSha256"],
            journal_execution_id=value["journalExecutionId"],
            state=value["state"],
            cursor=value["cursor"],
            completed_action_sha256=tuple(completed),
            completed_attempts=tuple(
                DestructiveAttemptV1.from_mapping(item)
                for item in raw_completed_attempts
            ),
            completed_observations=tuple(
                DestructiveObservationV1._from_mapping(item)
                for item in raw_completed_observations
            ),
            current_attempt=attempt,
            scheduled_key_count=value["scheduledKeyCount"],
            revision=value["revision"],
            previous_record_sha256=value["previousRecordSha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        binding = self.binding
        return {
            "schema": self.SCHEMA,
            "planSchema": self.plan_schema,
            "planSha256": self.plan_sha256,
            "account": binding.account,
            "region": binding.region,
            "releasePlanSha256": binding.release_plan_sha256,
            "releaseTransactionId": binding.release_transaction_id,
            "releaseTransactionSha256": binding.release_transaction_sha256,
            "releaseJournalPathSha256": binding.release_journal_path_sha256,
            "releaseJournalExecutionId": binding.release_journal_execution_id,
            "evidenceStoreSha256": binding.evidence_store_sha256,
            "releaseEvidenceSha256": binding.release_evidence_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
            "state": self.state,
            "cursor": self.cursor,
            "completedActionSha256": list(self.completed_action_sha256),
            "completedAttempts": [
                attempt.to_mapping() for attempt in self.completed_attempts
            ],
            "completedObservations": [
                observation.to_mapping()
                for observation in self.completed_observations
            ],
            "currentAttempt": self.current_attempt.to_mapping() if self.current_attempt else {},
            "scheduledKeyCount": self.scheduled_key_count,
            "revision": self.revision,
            "previousRecordSha256": self.previous_record_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

def _journal_path_sha256(path: Path) -> str:
    absolute = os.path.abspath(os.fspath(Path(path)))
    return hashlib.sha256(os.fsencode(absolute)).hexdigest()


_FileIdentity = tuple[int, int]


def _read_regular_with_identity(path: Path) -> tuple[bytes, _FileIdentity]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ContainmentError("destructive journal is not a regular file") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ContainmentError("destructive journal is not one regular file")
        if stat.S_IMODE(status.st_mode) & 0o077:
            raise ContainmentError("destructive journal permissions are not owner-only")
        chunks: list[bytes] = []
        remaining = MAX_CONTRACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_CONTRACT_BYTES:
            raise ContainmentError("destructive journal exceeds its byte limit")
        return payload, (status.st_dev, status.st_ino)
    finally:
        os.close(descriptor)


def _read_regular(path: Path) -> bytes:
    return _read_regular_with_identity(path)[0]


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short destructive journal write")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise ContainmentError("destructive journal already exists") from error
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _record_path(path: Path, revision: int) -> Path:
    if revision == 0:
        return Path(path)
    return Path(path).parent / f".{Path(path).name}.containment-r{revision:020d}"


def _revision_numbers(path: Path, *, maximum: int) -> tuple[int, ...]:
    prefix = f".{Path(path).name}.containment-r"
    revisions: list[int] = []
    try:
        entries = tuple(Path(path).parent.iterdir())
    except OSError as error:
        raise ContainmentError("destructive journal directory is unreadable") from error
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix) :]
        if len(suffix) != 20 or not suffix.isdigit():
            raise ContainmentError("destructive journal revision name is invalid")
        revision = int(suffix)
        if revision < 1 or revision > maximum:
            raise ContainmentError("destructive journal revision is out of range")
        revisions.append(revision)
    ordered = tuple(sorted(revisions))
    if ordered != tuple(range(1, len(ordered) + 1)):
        raise ContainmentError("destructive journal append-only chain has a gap")
    return ordered


def _read_chain(
    path: Path,
    *,
    maximum_revision: int,
) -> tuple[tuple[bytes, _FileIdentity], ...]:
    revisions = _revision_numbers(path, maximum=maximum_revision)
    return tuple(
        _read_regular_with_identity(_record_path(path, revision))
        for revision in range(len(revisions) + 1)
    )


def _open_lock(path: Path) -> int:
    target = Path(path)
    lock_path = target.parent / f".{target.name}.containment.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ContainmentError("destructive journal lock is unsafe") from error
    try:
        lock_status = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_status.st_mode)
            or lock_status.st_nlink != 1
            or lock_status.st_uid != os.getuid()
            or stat.S_IMODE(lock_status.st_mode) & 0o077
        ):
            raise ContainmentError("destructive journal lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        return lock_descriptor
    except BaseException:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
        raise


def _close_lock(lock_descriptor: int) -> None:
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
    finally:
        os.close(lock_descriptor)


def _append_record(
    path: Path,
    *,
    revision: int,
    expected: bytes,
    expected_identity: _FileIdentity,
    payload: bytes,
    maximum_revision: int,
) -> _FileIdentity:
    """Append one immutable revision without ever replacing a pathname."""

    target = Path(path)
    lock_descriptor = _open_lock(target)
    try:
        chain = _read_chain(target, maximum_revision=maximum_revision)
        if len(chain) - 1 != revision:
            raise ContainmentError("destructive journal changed concurrently")
        current_payload, current_identity = chain[-1]
        if current_payload != expected or current_identity != expected_identity:
            raise ContainmentError("destructive journal identity changed concurrently")
        next_path = _record_path(target, revision + 1)
        _write_new(next_path, payload)
        # A replacement racing after the identity check is never overwritten:
        # the append targets a new O_EXCL pathname.  Detect it before success.
        prior_payload, prior_identity = _read_regular_with_identity(
            _record_path(target, revision)
        )
        if prior_payload != expected or prior_identity != expected_identity:
            raise ContainmentError("destructive journal identity was substituted")
        next_payload, next_identity = _read_regular_with_identity(next_path)
        if next_payload != payload:
            raise ContainmentError("destructive journal append differs")
        return next_identity
    finally:
        _close_lock(lock_descriptor)


def _record_from_bytes(payload: bytes) -> _JournalRecordV1:
    return _JournalRecordV1.from_mapping(_parse(payload, label="destructive journal"))


def _validate_record(
    record: _JournalRecordV1,
    *,
    plan: ContainmentPlanV1 | PurgePlanV1,
    path_digest: str,
) -> _JournalRecordV1:
    if record.plan_schema != plan.SCHEMA or record.plan_sha256 != plan.digest():
        raise ContainmentError("destructive journal plan differs")
    expected = plan.binding
    if record.binding != expected:
        raise ContainmentError("destructive journal release binding differs")
    if record.journal_path_sha256 != path_digest:
        raise ContainmentError("destructive journal path binding differs")
    _sha256(record.journal_execution_id, label="destructive journal execution identity")
    if record.state not in _NONTERMINAL_STATES | _TERMINAL_STATES:
        raise ContainmentError("destructive journal state is invalid")
    if isinstance(record.cursor, bool) or not isinstance(record.cursor, int) or not (0 <= record.cursor <= len(plan.actions)):
        raise ContainmentError("destructive journal cursor is invalid")
    expected_prefix = tuple(action.digest() for action in plan.actions[: record.cursor])
    if record.completed_action_sha256 != expected_prefix:
        raise ContainmentError("destructive journal completed prefix differs")
    if (
        len(record.completed_attempts) != record.cursor
        or len(record.completed_observations) != record.cursor
    ):
        raise ContainmentError("destructive journal completed history differs")
    expected_revision = record.cursor * 2 + int(record.state == "UNCERTAIN")
    if (
        isinstance(record.revision, bool)
        or not isinstance(record.revision, int)
        or record.revision != expected_revision
    ):
        raise ContainmentError("destructive journal revision is invalid")
    _sha256(
        record.previous_record_sha256,
        label="destructive journal previous record digest",
    )
    if record.revision == 0 and record.previous_record_sha256 != "0" * 64:
        raise ContainmentError("initial destructive journal has a predecessor")
    if record.revision > 0 and record.previous_record_sha256 == "0" * 64:
        raise ContainmentError("destructive journal predecessor is missing")
    if isinstance(record.scheduled_key_count, bool) or not isinstance(record.scheduled_key_count, int) or record.scheduled_key_count < 0:
        raise ContainmentError("destructive journal scheduled key count is invalid")
    scheduled_from_history = 0
    for ordinal, (attempt, observation) in enumerate(
        zip(
            record.completed_attempts,
            record.completed_observations,
            strict=True,
        )
    ):
        action = plan.actions[ordinal]
        if (
            attempt.plan_sha256 != plan.digest()
            or attempt.journal_path_sha256 != path_digest
            or attempt.journal_execution_id != record.journal_execution_id
            or attempt.journal_revision != ordinal * 2 + 1
            or attempt.action_ordinal != ordinal
            or attempt.action_sha256 != action.digest()
            or attempt.resource_kind != action.resource_kind
            or attempt.resource_identity != action.resource_identity
            or attempt.ownership_proof_sha256 != action.ownership_proof_sha256
        ):
            raise ContainmentError("destructive journal completed attempt differs")
        if (
            observation.plan_sha256 != plan.digest()
            or observation.journal_path_sha256 != path_digest
            or observation.journal_execution_id != record.journal_execution_id
            or observation.attempt_sha256 != attempt.digest()
            or observation.action_sha256 != action.digest()
            or observation.resource_kind != action.resource_kind
            or observation.resource_identity != action.resource_identity
            or observation.ownership_proof_sha256
            != action.ownership_proof_sha256
        ):
            raise ContainmentError("destructive journal completed observation differs")
        if observation.disposition == "SCHEDULED":
            if action.resource_kind != "KMS_KEY":
                raise ContainmentError(
                    "destructive journal completed observation differs"
                )
            scheduled_from_history += 1
        elif observation.disposition == "CANCELED":
            if action.resource_kind != "SIGNER_SIGNING_PROFILE":
                raise ContainmentError(
                    "destructive journal completed observation differs"
                )
        elif observation.disposition != "ABSENT":
            raise ContainmentError(
                "destructive journal completed observation is not terminal"
            )
    if record.scheduled_key_count != scheduled_from_history:
        raise ContainmentError("destructive journal scheduled key count differs")
    if record.state == "UNCERTAIN":
        attempt = record.current_attempt
        if attempt is None or record.cursor >= len(plan.actions):
            raise ContainmentError("UNCERTAIN journal lacks one current attempt")
        action = plan.actions[record.cursor]
        if (
            attempt.plan_sha256 != plan.digest()
            or attempt.journal_path_sha256 != path_digest
            or attempt.journal_execution_id != record.journal_execution_id
            or attempt.journal_revision != record.revision
            or attempt.action_ordinal != record.cursor
            or attempt.action_sha256 != action.digest()
            or attempt.resource_kind != action.resource_kind
            or attempt.resource_identity != action.resource_identity
            or attempt.ownership_proof_sha256 != action.ownership_proof_sha256
        ):
            raise ContainmentError("UNCERTAIN journal attempt binding differs")
    elif record.current_attempt is not None:
        raise ContainmentError("stable destructive journal retains an attempt")
    if record.state == "READY" and record.cursor >= len(plan.actions):
        raise ContainmentError("completed destructive journal is not terminal")
    if record.state in _TERMINAL_STATES:
        if record.cursor != len(plan.actions):
            raise ContainmentError("terminal destructive journal is incomplete")
        expected_terminal = _terminal_state(plan, record.scheduled_key_count)
        if record.state != expected_terminal:
            raise ContainmentError("destructive journal terminal state differs")
    elif record.cursor == len(plan.actions):
        raise ContainmentError("complete destructive journal is not terminal")
    return record


def _terminal_state(plan: ContainmentPlanV1 | PurgePlanV1, scheduled: int) -> str:
    if isinstance(plan, ContainmentPlanV1):
        if scheduled:
            raise ContainmentError("containment cannot schedule key deletion")
        return "CONTAINED"
    return "PURGED_WITH_SCHEDULED_KEYS" if scheduled else "PURGED"


def _validated_chain(
    path: Path,
    *,
    plan: ContainmentPlanV1 | PurgePlanV1,
) -> tuple[_JournalRecordV1, bytes, _FileIdentity]:
    path_digest = _journal_path_sha256(path)
    chain = _read_chain(path, maximum_revision=len(plan.actions) * 2)
    previous_payload: bytes | None = None
    final_record: _JournalRecordV1 | None = None
    for revision, (payload, _identity) in enumerate(chain):
        record = _validate_record(
            _record_from_bytes(payload),
            plan=plan,
            path_digest=path_digest,
        )
        if record.revision != revision:
            raise ContainmentError("destructive journal append-only revision differs")
        expected_previous = (
            "0" * 64
            if previous_payload is None
            else hashlib.sha256(previous_payload).hexdigest()
        )
        if record.previous_record_sha256 != expected_previous:
            raise ContainmentError("destructive journal append-only chain differs")
        previous_payload = payload
        final_record = record
    if final_record is None:
        raise ContainmentError("destructive journal append-only chain is empty")
    final_payload, final_identity = chain[-1]
    return final_record, final_payload, final_identity


class ContainmentJournalV1:
    """Durable forward-only journal shared by containment and later purge."""

    def __init__(
        self,
        path: Path,
        *,
        plan: ContainmentPlanV1 | PurgePlanV1,
        record: _JournalRecordV1,
        payload: bytes,
        identity: _FileIdentity,
    ) -> None:
        self.path = Path(path)
        self.plan = _canonical_plan(plan)
        self._record = _validate_record(
            record,
            plan=self.plan,
            path_digest=_journal_path_sha256(self.path),
        )
        self._payload = payload
        self._identity = identity
        if self._record.to_bytes() != payload:
            raise ContainmentError("destructive journal payload differs")

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        plan: ContainmentPlanV1 | PurgePlanV1,
    ) -> "ContainmentJournalV1":
        canonical = _canonical_plan(plan)
        if not canonical._authorized:
            raise ContainmentError(
                "destructive journal creation requires a retained plan capability"
            )
        path_digest = _journal_path_sha256(Path(path))
        record = _JournalRecordV1(
            plan_schema=canonical.SCHEMA,
            plan_sha256=canonical.digest(),
            binding=canonical.binding,
            journal_path_sha256=path_digest,
            journal_execution_id=secrets.token_hex(32),
            state="READY",
            cursor=0,
            completed_action_sha256=(),
            completed_attempts=(),
            completed_observations=(),
            current_attempt=None,
            scheduled_key_count=0,
            revision=0,
            previous_record_sha256="0" * 64,
        )
        payload = record.to_bytes()
        _write_new(Path(path), payload)
        actual_payload, identity = _read_regular_with_identity(Path(path))
        if actual_payload != payload:
            raise ContainmentError("destructive journal initial append differs")
        return cls(
            Path(path),
            plan=canonical,
            record=record,
            payload=payload,
            identity=identity,
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        plan: ContainmentPlanV1 | PurgePlanV1,
    ) -> "ContainmentJournalV1":
        canonical = _canonical_plan(plan)
        record, payload, identity = _validated_chain(
            Path(path),
            plan=canonical,
        )
        return cls(
            Path(path),
            plan=canonical,
            record=record,
            payload=payload,
            identity=identity,
        )

    @property
    def state(self) -> str:
        return self._record.state

    @property
    def cursor(self) -> int:
        return self._record.cursor

    @property
    def revision(self) -> int:
        return self._record.revision

    @property
    def journal_path_sha256(self) -> str:
        return self._record.journal_path_sha256

    @property
    def journal_execution_id(self) -> str:
        return self._record.journal_execution_id

    @property
    def current_attempt(self) -> DestructiveAttemptV1 | None:
        return self._record.current_attempt

    @property
    def scheduled_key_count(self) -> int:
        return self._record.scheduled_key_count

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def to_bytes(self) -> bytes:
        return self._payload

    def authorize_purge(
        self,
        retained_evidence: RetainedReleaseEvidenceV1,
    ) -> ExactContainedJournalAuthorityV1:
        """Mint a one-shot purge prerequisite from one exact terminal record."""

        self._audit()
        if not isinstance(self.plan, ContainmentPlanV1) or self.state != "CONTAINED":
            raise ContainmentError("purge requires an exact terminal CONTAINED journal")
        if not isinstance(retained_evidence, RetainedReleaseEvidenceV1):
            raise ContainmentError("purge requires retained release evidence capability")
        if (
            retained_evidence.binding != self.plan.binding
            or retained_evidence.canonical_sha256
            != self.plan.retained_evidence_sha256
        ):
            raise ContainmentError("contained journal and retained evidence differ")
        return ExactContainedJournalAuthorityV1(
            binding=self.plan.binding,
            retained_evidence_sha256=self.plan.retained_evidence_sha256,
            containment_plan_sha256=self.plan.digest(),
            containment_journal_sha256=hashlib.sha256(self._payload).hexdigest(),
            _token=_CONTAINED_JOURNAL_TOKEN,
        )

    def _audit(self) -> None:
        record, payload, identity = _validated_chain(self.path, plan=self.plan)
        if payload != self._payload or identity != self._identity:
            raise ContainmentError("destructive journal changed concurrently")
        if record.to_mapping() != self._record.to_mapping():
            raise ContainmentError("destructive journal in-memory state differs")

    def _persist(self, record: _JournalRecordV1) -> None:
        validated = _validate_record(
            record,
            plan=self.plan,
            path_digest=self.journal_path_sha256,
        )
        payload = validated.to_bytes()
        identity = _append_record(
            self.path,
            revision=self.revision,
            expected=self._payload,
            expected_identity=self._identity,
            payload=payload,
            maximum_revision=len(self.plan.actions) * 2,
        )
        self._record = validated
        self._payload = payload
        self._identity = identity
        self._audit()

    def arm_next(self) -> FreshContainmentAuthorityV1:
        self._audit()
        if not self.plan._authorized:
            raise ContainmentError(
                "destructive action requires a retained plan capability"
            )
        if self.terminal:
            raise ContainmentError("destructive journal is terminal")
        if self.state == "UNCERTAIN":
            raise ContainmentError("destructive journal already has a durable attempt")
        action = self.plan.actions[self.cursor]
        revision = self.revision + 1
        attempt = DestructiveAttemptV1(
            plan_sha256=self.plan.digest(),
            journal_path_sha256=self.journal_path_sha256,
            journal_execution_id=self.journal_execution_id,
            journal_revision=revision,
            action_ordinal=self.cursor,
            action_sha256=action.digest(),
            resource_kind=action.resource_kind,
            resource_identity=action.resource_identity,
            ownership_proof_sha256=action.ownership_proof_sha256,
        )
        record = _JournalRecordV1(
            plan_schema=self._record.plan_schema,
            plan_sha256=self._record.plan_sha256,
            binding=self._record.binding,
            journal_path_sha256=self.journal_path_sha256,
            journal_execution_id=self.journal_execution_id,
            state="UNCERTAIN",
            cursor=self.cursor,
            completed_action_sha256=self._record.completed_action_sha256,
            completed_attempts=self._record.completed_attempts,
            completed_observations=self._record.completed_observations,
            current_attempt=attempt,
            scheduled_key_count=self.scheduled_key_count,
            revision=revision,
            previous_record_sha256=hashlib.sha256(self._payload).hexdigest(),
        )
        self._persist(record)
        return FreshContainmentAuthorityV1(attempt, _token=_AUTHORITY_TOKEN)

    def reconcile(self, observation: DestructiveObservationV1) -> str:
        self._audit()
        if self.state != "UNCERTAIN" or self.current_attempt is None:
            raise ContainmentError("only an UNCERTAIN destructive action can reconcile")
        if not isinstance(observation, DestructiveObservationV1):
            raise ContainmentError("reconcile requires a closed observer token")
        if not observation._reconcilable:
            raise ContainmentError("reconcile requires a fresh closed observer token")
        canonical = DestructiveObservationV1._from_bytes(observation.to_bytes())
        if canonical.to_mapping() != observation.to_mapping():
            raise ContainmentError("destructive observation is not canonical")
        attempt = self.current_attempt
        action = self.plan.actions[self.cursor]
        if canonical.plan_sha256 != self.plan.digest():
            raise ContainmentError("destructive observation plan differs")
        if (
            canonical.journal_path_sha256 != self.journal_path_sha256
            or canonical.journal_execution_id != self.journal_execution_id
        ):
            raise ContainmentError("destructive observation journal differs")
        if canonical.attempt_sha256 != attempt.digest():
            raise ContainmentError("destructive observation attempt differs")
        if canonical.action_sha256 != action.digest():
            raise ContainmentError("destructive observation action differs")
        if (
            canonical.resource_kind != action.resource_kind
            or canonical.resource_identity != action.resource_identity
        ):
            raise ContainmentError("destructive observation resource differs")
        if canonical.ownership_proof_sha256 != action.ownership_proof_sha256:
            raise ContainmentError("destructive observation ownership proof differs")
        disposition = canonical.disposition
        complete = disposition == "ABSENT" or (
            action.resource_kind == "KMS_KEY" and disposition == "SCHEDULED"
        ) or (
            action.resource_kind == "SIGNER_SIGNING_PROFILE"
            and disposition == "CANCELED"
        )
        if disposition == "SCHEDULED" and action.resource_kind != "KMS_KEY":
            raise ContainmentError("only an exact KMS key can be scheduled")
        if (
            disposition == "CANCELED"
            and action.resource_kind != "SIGNER_SIGNING_PROFILE"
        ):
            raise ContainmentError("only an exact signing profile can be canceled")
        if not complete:
            return self.state
        scheduled = self.scheduled_key_count + int(disposition == "SCHEDULED")
        cursor = self.cursor + 1
        state = (
            _terminal_state(self.plan, scheduled)
            if cursor == len(self.plan.actions)
            else "READY"
        )
        record = _JournalRecordV1(
            plan_schema=self._record.plan_schema,
            plan_sha256=self._record.plan_sha256,
            binding=self._record.binding,
            journal_path_sha256=self.journal_path_sha256,
            journal_execution_id=self.journal_execution_id,
            state=state,
            cursor=cursor,
            completed_action_sha256=(
                *self._record.completed_action_sha256,
                action.digest(),
            ),
            completed_attempts=(
                *self._record.completed_attempts,
                attempt,
            ),
            completed_observations=(
                *self._record.completed_observations,
                canonical,
            ),
            current_attempt=None,
            scheduled_key_count=scheduled,
            revision=self.revision + 1,
            previous_record_sha256=hashlib.sha256(self._payload).hexdigest(),
        )
        self._persist(record)
        return state


class FakeContainmentProviderV1:
    """Pure deterministic simulator; it is not a production adapter."""

    def __init__(self, plan: ContainmentPlanV1 | PurgePlanV1) -> None:
        self._plan = _canonical_plan(plan)
        if not self._plan._authorized:
            raise ContainmentError("fake provider requires a retained plan capability")
        self._states = {action.digest(): "PRESENT" for action in self._plan.actions}
        self._counts = {action.digest(): 0 for action in self._plan.actions}
        self._sweeps: dict[str, tuple[str, str]] = {}
        self._observation_sequence = 0

    @classmethod
    def from_plan(
        cls, plan: ContainmentPlanV1 | PurgePlanV1
    ) -> "FakeContainmentProviderV1":
        return cls(plan)

    def _action(self, action: DestructiveActionV1) -> DestructiveActionV1:
        canonical = DestructiveActionV1.from_mapping(action.to_mapping())
        if canonical.ordinal >= len(self._plan.actions) or self._plan.actions[canonical.ordinal] != canonical:
            raise ContainmentError("fake provider action differs from its plan")
        return canonical

    def dispatch(
        self,
        authority: FreshContainmentAuthorityV1,
        action: DestructiveActionV1,
        *,
        crash_before_effect: bool = False,
        crash_after_effect: bool = False,
    ) -> DestructiveAttemptV1:
        if not isinstance(authority, FreshContainmentAuthorityV1):
            raise ContainmentError("fake provider requires fresh containment authority")
        canonical = self._action(action)
        attempt = authority.consume(canonical)
        digest = canonical.digest()
        self._counts[digest] += 1
        if crash_before_effect:
            raise RuntimeError("simulated crash before destructive effect")
        if canonical.resource_kind == "KMS_KEY":
            self._states[digest] = "SCHEDULED"
        elif canonical.resource_kind == "SIGNER_SIGNING_PROFILE":
            self._states[digest] = "CANCELED"
        else:
            self._states[digest] = "ABSENT"
        if crash_after_effect:
            raise RuntimeError("simulated crash after destructive effect")
        return attempt

    def dispatch_count(self, action: DestructiveActionV1) -> int:
        return self._counts[self._action(action).digest()]

    def set_sweeps(
        self, action: DestructiveActionV1, sweep_one: str, sweep_two: str
    ) -> None:
        canonical = self._action(action)
        if sweep_one not in _SWEEP_VALUES or sweep_two not in _SWEEP_VALUES:
            raise ContainmentError("fake observer sweep is invalid")
        self._sweeps[canonical.digest()] = (sweep_one, sweep_two)

    def observe_current(
        self, journal: ContainmentJournalV1
    ) -> DestructiveObservationV1:
        if not isinstance(journal, ContainmentJournalV1):
            raise ContainmentError("fake observer journal is invalid")
        if journal.plan.digest() != self._plan.digest():
            raise ContainmentError("fake observer plan differs")
        attempt = journal.current_attempt
        if journal.state != "UNCERTAIN" or attempt is None:
            raise ContainmentError("fake observer requires one UNCERTAIN action")
        action = self._plan.actions[journal.cursor]
        digest = action.digest()
        sweeps = self._sweeps.get(digest, (self._states[digest], self._states[digest]))
        sweep_one_sequence = self._observation_sequence + 1
        sweep_two_sequence = sweep_one_sequence + 1
        self._observation_sequence = sweep_two_sequence
        sweep_one_evidence = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.destructive-sweep-evidence.v1",
                    "ordinal": 1,
                    "sequence": sweep_one_sequence,
                    "attemptSha256": attempt.digest(),
                    "actionSha256": digest,
                    "value": sweeps[0],
                    "previousSweepEvidenceSha256": "0" * 64,
                }
            )
        ).hexdigest()
        sweep_two_evidence = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.destructive-sweep-evidence.v1",
                    "ordinal": 2,
                    "sequence": sweep_two_sequence,
                    "attemptSha256": attempt.digest(),
                    "actionSha256": digest,
                    "value": sweeps[1],
                    "previousSweepEvidenceSha256": sweep_one_evidence,
                }
            )
        ).hexdigest()
        evidence = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.destructive-observer-evidence.v1",
                    "planSha256": self._plan.digest(),
                    "attemptSha256": attempt.digest(),
                    "actionSha256": digest,
                    "sweepOne": sweeps[0],
                    "sweepOneSequence": sweep_one_sequence,
                    "sweepOneEvidenceSha256": sweep_one_evidence,
                    "sweepTwo": sweeps[1],
                    "sweepTwoSequence": sweep_two_sequence,
                    "sweepTwoEvidenceSha256": sweep_two_evidence,
                }
            )
        ).hexdigest()
        return DestructiveObservationV1(
            plan_sha256=self._plan.digest(),
            journal_path_sha256=journal.journal_path_sha256,
            journal_execution_id=journal.journal_execution_id,
            attempt_sha256=attempt.digest(),
            action_sha256=digest,
            resource_kind=action.resource_kind,
            resource_identity=action.resource_identity,
            ownership_proof_sha256=action.ownership_proof_sha256,
            sweep_one=sweeps[0],
            sweep_one_sequence=sweep_one_sequence,
            sweep_one_evidence_sha256=sweep_one_evidence,
            sweep_two=sweeps[1],
            sweep_two_sequence=sweep_two_sequence,
            sweep_two_evidence_sha256=sweep_two_evidence,
            observer_evidence_sha256=evidence,
            _token=_OBSERVER_TOKEN,
            _reconcilable=True,
        )


__all__ = [
    "CONTAINMENT_RESOURCE_KINDS",
    "PURGE_TARGET_KINDS",
    "ContainmentError",
    "ContainmentJournalV1",
    "ContainmentPlanV1",
    "DestructiveActionV1",
    "DestructiveAttemptV1",
    "DestructiveObservationV1",
    "ExactContainedJournalAuthorityV1",
    "FakeContainmentProviderV1",
    "FakeRetainedReleaseEvidenceBoundaryV1",
    "FreshContainmentAuthorityV1",
    "OwnedResourceIdentityV1",
    "PurgePlanV1",
    "PurgeTargetV1",
    "ReleaseClosureBindingV1",
    "RetainedReleaseEvidenceV1",
]
