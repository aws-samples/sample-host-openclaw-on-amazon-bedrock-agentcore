"""Plan-bound, no-clobber local persistence for RuntimeContext V3.

This module has no AWS client surface.  Live runtime and endpoint facts enter
only through the exact retained endpoint observation in a completed v2 plan
prefix; callers cannot pass those facts to the file writer individually.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Any, ClassVar, Iterator, Mapping, Sequence
import uuid

from release_tools.contracts import (
    ContractError,
    MAX_CONTRACT_BYTES,
    ReleasePlanV2,
    ResolvedMutationRequestV2,
    RetainedStepEvidenceV2,
    RuntimeConfigurationV1,
    RuntimeContextV3,
    StagingTransactionV2,
    _canonical_release_plan_v2,
    _completed_prefix_sha256,
    _release_operation_sha256,
    _release_outcome_operation_sha256,
    canonical_json_bytes,
    expected_execution_role_arn,
    parse_canonical_object,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)
from release_tools.release_plan_v2 import (
    PreclosedStaticRequestV2,
    ReleasePlanAssemblyError,
)
from release_tools.transaction import ObservationDisposition


_SHA_64 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{9,47}")
_RUNTIME_VERSION = re.compile(r"[1-9][0-9]{0,4}")
_ENDPOINT_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):agentEndpoint/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}"
)
_WORKLOAD_IDENTITY_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):"
    r"workload-identity-directory/default/workload-identity/"
    r"[A-Za-z0-9_.-]{3,255}"
)
_DIRECTORY_MODE = 0o700
_TEMPORARY_MODE = 0o600
_CONTEXT_MODE = 0o400
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_ENDPOINT_PROJECTION_FIELDS = {
    "agentCoreStackId",
    "cloudFormationTemplateSha256",
    "cloudFormationRequestSha256",
    "runtimeId",
    "runtimeVersion",
    "runtimeArn",
    "runtimeConfiguration",
    "runtimeConfigurationSha256",
    "guardrailId",
    "guardrailVersion",
    "requiresMMDSV2",
    "requiresServiceS3Endpoint",
    "endpointId",
    "endpointName",
    "endpointArn",
    "workloadIdentityArn",
}


class RuntimeContextV2Error(RuntimeError):
    """The context request or its retained authority is invalid."""


class RuntimeContextV2Ambiguous(RuntimeContextV2Error):
    """The local filesystem cannot prove one stable exact result."""


@dataclass(frozen=True, slots=True)
class RuntimeContextWriteRequestV2:
    """Exact acyclic static artifact accepted by the release assembler."""

    SCHEMA: ClassVar[str] = PreclosedStaticRequestV2.SCHEMA

    kind: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    subject: str

    def __post_init__(self) -> None:
        try:
            canonical = PreclosedStaticRequestV2(
                self.kind,
                self.source_commit,
                self.source_tree,
                self.account,
                self.region,
                self.subject,
            )
        except ReleasePlanAssemblyError as error:
            raise RuntimeContextV2Error(
                "runtime context static request is invalid"
            ) from error
        if canonical.kind != "RUNTIME_CONTEXT_WRITE":
            raise RuntimeContextV2Error(
                "runtime context static request kind is not exact"
            )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RuntimeContextWriteRequestV2":
        try:
            canonical = PreclosedStaticRequestV2.from_bytes(payload)
        except (ReleasePlanAssemblyError, TypeError, ValueError) as error:
            raise RuntimeContextV2Error(
                "runtime context static request is not canonical"
            ) from error
        if canonical.kind != "RUNTIME_CONTEXT_WRITE":
            raise RuntimeContextV2Error(
                "runtime context static request kind is not exact"
            )
        return cls(
            canonical.kind,
            canonical.source_commit,
            canonical.source_tree,
            canonical.account,
            canonical.region,
            canonical.subject,
        )

    @classmethod
    def from_plan(cls, plan: ReleasePlanV2) -> "RuntimeContextWriteRequestV2":
        try:
            canonical = _canonical_release_plan_v2(plan)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise RuntimeContextV2Error(
                "runtime context request requires a canonical release plan"
            ) from error
        context_steps = [
            step
            for step in canonical.steps
            if step.phase == "context" and step.kind == "RUNTIME_CONTEXT_WRITE"
        ]
        if len(context_steps) != 1:
            raise RuntimeContextV2Error(
                "runtime context plan step is not singular"
            )
        request = cls(
            "RUNTIME_CONTEXT_WRITE",
            canonical.source_commit,
            canonical.source_tree,
            canonical.account,
            canonical.region,
            context_steps[0].subject,
        )
        request.validate_plan(canonical)
        return request

    def validate_plan(self, plan: ReleasePlanV2) -> ReleasePlanV2:
        try:
            canonical = _canonical_release_plan_v2(plan)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise RuntimeContextV2Error(
                "runtime context request requires a canonical release plan"
            ) from error
        context_steps = [
            step
            for step in canonical.steps
            if step.phase == "context" and step.kind == "RUNTIME_CONTEXT_WRITE"
        ]
        if len(context_steps) != 1:
            raise RuntimeContextV2Error(
                "runtime context plan step is not singular"
            )
        step = context_steps[0]
        if (
            self.kind != "RUNTIME_CONTEXT_WRITE"
            or (
                self.source_commit,
                self.source_tree,
                self.account,
                self.region,
            )
            != (
                canonical.source_commit,
                canonical.source_tree,
                canonical.account,
                canonical.region,
            )
        ):
            raise RuntimeContextV2Error(
                "runtime context request differs from its plan identity"
            )
        if self.subject != step.subject:
            raise RuntimeContextV2Error(
                "runtime context request differs from its plan subject"
            )
        artifacts = [
            artifact
            for artifact in canonical.artifacts
            if artifact.path == step.request_artifact
        ]
        if len(artifacts) != 1:
            raise RuntimeContextV2Error(
                "runtime context request artifact is not singular"
            )
        payload = self.to_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if step.request_sha256 != digest or artifacts[0].sha256 != digest:
            raise RuntimeContextV2Error(
                "runtime context request artifact digest differs"
            )
        if artifacts[0].size != len(payload):
            raise RuntimeContextV2Error(
                "runtime context request artifact size differs"
            )
        return canonical

    def to_mapping(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "subject": self.subject,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class TrustedRuntimeContextInputsV2:
    """Private capability derived from the exact retained endpoint record."""

    request_sha256: str
    plan_sha256: str
    completed_prefix_sha256: str
    transaction_id: str
    step_id: str
    operation_sha256: str
    request_artifact: str
    request_artifact_size: int
    source_commit: str
    source_tree: str
    account: str
    region: str
    runtime_image_digest: str
    runtime_image_uri: str
    runtime_endpoint_name: str
    context_relative_path: str
    endpoint_evidence_sha256: str
    foundation_inputs_sha256: str
    agent_core_stack_id: str
    runtime_id: str
    runtime_version: str
    runtime_arn: str
    runtime_endpoint_id: str
    runtime_endpoint_arn: str
    workload_identity_arn: str
    runtime_configuration: RuntimeConfigurationV1
    runtime_configuration_sha256: str

    def __init__(
        self,
        *,
        _token: object | None = None,
        **values: object,
    ) -> None:
        if _token is not _TRUSTED_INPUT_TOKEN:
            raise RuntimeContextV2Error(
                "trusted runtime context inputs are not constructible"
            )
        expected = {
            "request_sha256",
            "plan_sha256",
            "completed_prefix_sha256",
            "transaction_id",
            "step_id",
            "operation_sha256",
            "request_artifact",
            "request_artifact_size",
            "source_commit",
            "source_tree",
            "account",
            "region",
            "runtime_image_digest",
            "runtime_image_uri",
            "runtime_endpoint_name",
            "context_relative_path",
            "endpoint_evidence_sha256",
            "foundation_inputs_sha256",
            "agent_core_stack_id",
            "runtime_id",
            "runtime_version",
            "runtime_arn",
            "runtime_endpoint_id",
            "runtime_endpoint_arn",
            "workload_identity_arn",
            "runtime_configuration",
            "runtime_configuration_sha256",
        }
        if set(values) != expected:
            raise RuntimeContextV2Error(
                "trusted runtime context inputs are incomplete"
            )
        for field in expected:
            object.__setattr__(self, field, values[field])

    def runtime_context(
        self, request: RuntimeContextWriteRequestV2
    ) -> RuntimeContextV3:
        if not isinstance(request, RuntimeContextWriteRequestV2):
            raise RuntimeContextV2Error(
                "runtime context writer requires its typed request"
            )
        expected_subject = (
            f"release:{self.account}:{self.region}:{self.source_commit}:"
            f"artifact:{self.context_relative_path}"
        )
        if request.digest() != self.request_sha256 or (
            request.kind,
            request.source_commit,
            request.source_tree,
            request.account,
            request.region,
        ) != (
            "RUNTIME_CONTEXT_WRITE",
            self.source_commit,
            self.source_tree,
            self.account,
            self.region,
        ) or request.subject != expected_subject:
            raise RuntimeContextV2Error(
                "trusted runtime context inputs cross their static request"
            )
        try:
            return RuntimeContextV3.from_mapping(
                {
                    "schema": RuntimeContextV3.SCHEMA,
                    "sourceCommit": self.source_commit,
                    "account": self.account,
                    "region": self.region,
                    "runtimeId": self.runtime_id,
                    "runtimeEndpointId": self.runtime_endpoint_id,
                    "runtimeEndpointName": self.runtime_endpoint_name,
                    "runtimeArn": self.runtime_arn,
                    "runtimeVersion": self.runtime_version,
                    "runtimeImageUri": self.runtime_image_uri,
                    "executionRoleArn": expected_execution_role_arn(
                        self.account, self.region
                    ),
                    "runtimeConfiguration": (
                        self.runtime_configuration.to_mapping()
                    ),
                    "runtimeConfigurationSha256": (
                        self.runtime_configuration_sha256
                    ),
                }
            )
        except (ContractError, TypeError, ValueError) as error:
            raise RuntimeContextV2Error(
                "retained runtime facts do not form RuntimeContext V3"
            ) from error


_TRUSTED_INPUT_TOKEN = object()


def _canonical_transaction(
    transaction: StagingTransactionV2, plan: ReleasePlanV2
) -> StagingTransactionV2:
    try:
        return StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
    except (AttributeError, ContractError, TypeError, ValueError) as error:
        raise RuntimeContextV2Error(
            "runtime context transaction differs from the release plan"
        ) from error


def _canonical_retained_prefix(
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    retained_prefix: Sequence[RetainedStepEvidenceV2],
) -> tuple[RetainedStepEvidenceV2, ...]:
    if (
        not isinstance(retained_prefix, Sequence)
        or isinstance(retained_prefix, (str, bytes, bytearray))
        or len(retained_prefix) != transaction.completed_step_count
    ):
        raise RuntimeContextV2Error(
            "runtime context requires the exact completed prefix"
        )
    canonical: list[RetainedStepEvidenceV2] = []
    binding: tuple[str, str, str] | None = None
    prior_revision = -1
    completed_mappings = [
        item.to_mapping() for item in transaction.completed_steps
    ]
    for ordinal, candidate in enumerate(retained_prefix):
        try:
            record = RetainedStepEvidenceV2.from_bytes(candidate.to_bytes())
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise RuntimeContextV2Error(
                "runtime context retained prefix is not canonical"
            ) from error
        completed = transaction.completed_steps[ordinal]
        step = plan.steps[ordinal]
        prefix_sha256 = _completed_prefix_sha256(
            completed_mappings[:ordinal]
        )
        release_operation = _release_operation_sha256(
            plan.digest(), step, prefix_sha256
        )
        outcome_operation = _release_outcome_operation_sha256(
            release_operation_sha256=release_operation,
            journal_path_sha256=record.journal_path_sha256,
            journal_execution_id=record.journal_execution_id,
            journal_revision=record.journal_revision,
        )
        current_binding = (
            record.evidence_store_sha256,
            record.journal_path_sha256,
            record.journal_execution_id,
        )
        if binding is None:
            binding = current_binding
        if (
            record.digest() != completed.evidence_sha256
            or record.step_id != completed.step_id
            or record.step_id != step.step_id
            or record.subject != step.subject
            or record.plan_sha256 != plan.digest()
            or record.completed_prefix_sha256 != prefix_sha256
            or record.disposition != "PRESENT"
            or record.step_observation is None
            or record.release_operation_sha256 != release_operation
            or record.operation_sha256 != outcome_operation
            or current_binding != binding
            or record.journal_revision <= prior_revision
        ):
            raise RuntimeContextV2Error(
                "runtime context retained records are not the ordered completed prefix"
            )
        prior_revision = record.journal_revision
        canonical.append(record)
    if canonical and canonical[-1].journal_revision + 1 != transaction.revision:
        raise RuntimeContextV2Error(
            "runtime context retained records do not close the journal revision"
        )
    return tuple(canonical)


def derive_trusted_runtime_context_inputs(
    *,
    request: RuntimeContextWriteRequestV2,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    retained_prefix: Sequence[RetainedStepEvidenceV2],
) -> TrustedRuntimeContextInputsV2:
    """Derive live facts from exact retained foundation and endpoint records."""

    if not isinstance(request, RuntimeContextWriteRequestV2):
        raise RuntimeContextV2Error(
            "runtime context derivation requires its typed request"
        )
    canonical_plan = request.validate_plan(plan)
    canonical_transaction = _canonical_transaction(transaction, canonical_plan)
    count = canonical_transaction.completed_step_count
    if count >= len(canonical_plan.steps):
        raise RuntimeContextV2Error("runtime context transaction has no next step")
    next_step = canonical_plan.steps[count]
    if (
        canonical_transaction.state != "ENDPOINT_READY"
        or canonical_transaction.last_stable_state != "ENDPOINT_READY"
        or (next_step.phase, next_step.kind, next_step.subject)
        != ("context", "RUNTIME_CONTEXT_WRITE", request.subject)
    ):
        raise RuntimeContextV2Error(
            "runtime context requires the exact completed endpoint boundary"
        )
    records = _canonical_retained_prefix(
        plan=canonical_plan,
        transaction=canonical_transaction,
        retained_prefix=retained_prefix,
    )
    foundation_steps = [
        step for step in canonical_plan.steps if step.phase == "foundation"
    ]
    if not foundation_steps:
        raise RuntimeContextV2Error(
            "runtime context plan has no foundation record owner"
        )
    foundation_step = foundation_steps[-1]
    expected_foundation_subject = (
        f"cfn:{canonical_plan.account}:{canonical_plan.region}:stack:"
        f"OpenClawObservability:release:{canonical_plan.source_commit}:drift"
    )
    if (
        foundation_step.ordinal,
        foundation_step.kind,
        foundation_step.subject,
    ) != (
        max(step.ordinal for step in foundation_steps),
        "STACK_DRIFT_CHECK",
        expected_foundation_subject,
    ):
        raise RuntimeContextV2Error(
            "runtime context plan has the wrong final foundation record owner"
        )
    foundation_candidates = [
        (ordinal, record.step_observation.foundation_runtime_inputs)
        for ordinal, record in enumerate(records)
        if record.step_observation is not None
        and record.step_observation.foundation_runtime_inputs is not None
    ]
    if (
        len(foundation_candidates) != 1
        or foundation_candidates[0][0] != foundation_step.ordinal
    ):
        raise RuntimeContextV2Error(
            "runtime context retained foundation record is not singular at its exact owner"
        )
    foundation = foundation_candidates[0][1]
    if foundation is None:  # pragma: no cover - narrowed by the predicate above
        raise RuntimeContextV2Error(
            "runtime context retained foundation record is missing typed inputs"
        )
    try:
        foundation.validate_plan_identity(canonical_plan)
    except (ContractError, TypeError, ValueError) as error:
        raise RuntimeContextV2Error(
            "runtime context retained foundation record crosses the release plan"
        ) from error
    if foundation.digest() != canonical_transaction.foundation_inputs_sha256:
        raise RuntimeContextV2Error(
            "runtime context retained foundation inputs digest differs from the journal"
        )
    if foundation.agent_core_stack_id != canonical_transaction.agent_core_stack_id:
        raise RuntimeContextV2Error(
            "runtime context retained foundation AgentCore stack differs from the journal"
        )
    endpoint_candidates = [
        record
        for ordinal, record in enumerate(records)
        if canonical_plan.steps[ordinal].phase == "endpoint"
        and canonical_plan.steps[ordinal].kind == "STACK_UPDATE"
    ]
    if len(endpoint_candidates) != 1:
        raise RuntimeContextV2Error(
            "runtime context retained endpoint observation is not singular"
        )
    endpoint_record = endpoint_candidates[0]
    observer = endpoint_record.observer_evidence_mapping()
    projection = observer.get("projection")
    if (
        observer.get("service") != "bedrock-agentcore-control"
        or observer.get("operation") != "get_agent_runtime_endpoint"
        or observer.get("providerStatus") != "READY"
        or observer.get("disposition") != "PRESENT"
        or not isinstance(projection, Mapping)
        or set(projection) != _ENDPOINT_PROJECTION_FIELDS
    ):
        raise RuntimeContextV2Error(
            "runtime context endpoint observation has the wrong exact projection"
        )
    runtime_id = projection["runtimeId"]
    runtime_version = projection["runtimeVersion"]
    runtime_arn = projection["runtimeArn"]
    endpoint_id = projection["endpointId"]
    endpoint_name = projection["endpointName"]
    endpoint_arn = projection["endpointArn"]
    workload_identity_arn = projection["workloadIdentityArn"]
    endpoint_arn_match = (
        _ENDPOINT_ARN.fullmatch(endpoint_arn)
        if isinstance(endpoint_arn, str)
        else None
    )
    workload_identity_match = (
        _WORKLOAD_IDENTITY_ARN.fullmatch(workload_identity_arn)
        if isinstance(workload_identity_arn, str)
        else None
    )
    if (
        not isinstance(runtime_id, str)
        or _RUNTIME_ID.fullmatch(runtime_id) is None
        or not isinstance(runtime_version, str)
        or _RUNTIME_VERSION.fullmatch(runtime_version) is None
        or not isinstance(runtime_arn, str)
        or not isinstance(endpoint_id, str)
        or _RUNTIME_ID.fullmatch(endpoint_id) is None
        or endpoint_name != canonical_plan.runtime_endpoint_name
        or endpoint_arn_match is None
        or endpoint_arn_match.group(1) != canonical_plan.account
        or workload_identity_match is None
        or workload_identity_match.group(1) != canonical_plan.account
        or projection["agentCoreStackId"]
        != canonical_transaction.agent_core_stack_id
        or (runtime_id, runtime_version, runtime_arn, endpoint_id)
        != (
            canonical_transaction.runtime_id,
            canonical_transaction.runtime_version,
            canonical_transaction.runtime_arn,
            canonical_transaction.runtime_endpoint_id,
        )
    ):
        raise RuntimeContextV2Error(
            "runtime context endpoint identity crosses the retained journal"
        )
    if (
        not isinstance(projection["cloudFormationTemplateSha256"], str)
        or _SHA_64.fullmatch(projection["cloudFormationTemplateSha256"])
        is None
        or not isinstance(projection["cloudFormationRequestSha256"], str)
        or _SHA_64.fullmatch(projection["cloudFormationRequestSha256"])
        is None
        or projection["requiresMMDSV2"] is not True
        or not isinstance(projection["requiresServiceS3Endpoint"], bool)
        or not isinstance(projection["guardrailId"], str)
        or not isinstance(projection["guardrailVersion"], str)
    ):
        raise RuntimeContextV2Error(
            "runtime context endpoint security projection is invalid"
        )
    raw_configuration = projection["runtimeConfiguration"]
    if not isinstance(raw_configuration, Mapping):
        raise RuntimeContextV2Error(
            "runtime context retained configuration is malformed"
        )
    try:
        configuration = RuntimeConfigurationV1.from_mapping(
            raw_configuration,
            runtime_image_uri=canonical_plan.runtime_image_uri,
            account=canonical_plan.account,
            region=canonical_plan.region,
        )
    except (ContractError, TypeError, ValueError) as error:
        raise RuntimeContextV2Error(
            "runtime context retained configuration differs from the plan"
        ) from error
    role_arn = expected_execution_role_arn(
        canonical_plan.account, canonical_plan.region
    )
    configuration_sha256 = configuration.digest_for_role(role_arn)
    if projection["runtimeConfigurationSha256"] != configuration_sha256:
        raise RuntimeContextV2Error(
            "runtime context configuration digest differs from exact bytes"
        )
    environment = dict(configuration.environment_variables)
    if (
        environment.get("BEDROCK_GUARDRAIL_ID", "")
        != projection["guardrailId"]
        or environment.get("BEDROCK_GUARDRAIL_VERSION", "")
        != projection["guardrailVersion"]
    ):
        raise RuntimeContextV2Error(
            "runtime context guardrail projection differs from configuration"
        )
    if (
        foundation.guardrail_id,
        foundation.guardrail_version,
    ) != (
        projection["guardrailId"],
        projection["guardrailVersion"],
    ) or (
        foundation.guardrail_id,
        foundation.guardrail_version,
    ) != (
        environment.get("BEDROCK_GUARDRAIL_ID", ""),
        environment.get("BEDROCK_GUARDRAIL_VERSION", ""),
    ):
        raise RuntimeContextV2Error(
            "runtime context retained foundation guardrail differs from the endpoint"
        )
    completed_prefix_sha256 = _completed_prefix_sha256(
        [item.to_mapping() for item in canonical_transaction.completed_steps]
    )
    trusted = TrustedRuntimeContextInputsV2(
        _token=_TRUSTED_INPUT_TOKEN,
        request_sha256=request.digest(),
        plan_sha256=canonical_plan.digest(),
        completed_prefix_sha256=completed_prefix_sha256,
        transaction_id=canonical_plan.transaction_id,
        step_id=next_step.step_id,
        operation_sha256=_release_operation_sha256(
            canonical_plan.digest(), next_step, completed_prefix_sha256
        ),
        request_artifact=next_step.request_artifact,
        request_artifact_size=len(request.to_bytes()),
        source_commit=request.source_commit,
        source_tree=request.source_tree,
        account=request.account,
        region=request.region,
        runtime_image_digest=canonical_plan.runtime_image_digest,
        runtime_image_uri=canonical_plan.runtime_image_uri,
        runtime_endpoint_name=canonical_plan.runtime_endpoint_name,
        context_relative_path=canonical_plan.context_relative_path,
        endpoint_evidence_sha256=endpoint_record.digest(),
        foundation_inputs_sha256=foundation.digest(),
        agent_core_stack_id=foundation.agent_core_stack_id,
        runtime_id=runtime_id,
        runtime_version=runtime_version,
        runtime_arn=runtime_arn,
        runtime_endpoint_id=endpoint_id,
        runtime_endpoint_arn=endpoint_arn,
        workload_identity_arn=workload_identity_arn,
        runtime_configuration=configuration,
        runtime_configuration_sha256=configuration_sha256,
    )
    trusted.runtime_context(request)
    return trusted


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    device: int
    inode: int
    mode: int
    uid: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _snapshot(details: os.stat_result) -> _FileSnapshot:
    return _FileSnapshot(
        device=details.st_dev,
        inode=details.st_ino,
        mode=details.st_mode,
        uid=details.st_uid,
        link_count=details.st_nlink,
        size=details.st_size,
        mtime_ns=details.st_mtime_ns,
        ctime_ns=details.st_ctime_ns,
    )


def _validate_candidate_stat(
    details: os.stat_result, *, expected_uid: int
) -> None:
    if stat.S_ISLNK(details.st_mode):
        raise RuntimeContextV2Ambiguous("runtime context target is a symlink")
    if stat.S_ISFIFO(details.st_mode):
        raise RuntimeContextV2Ambiguous("runtime context target is a fifo")
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeContextV2Ambiguous(
            "runtime context target is nonregular"
        )
    if details.st_uid != expected_uid:
        raise RuntimeContextV2Ambiguous(
            "runtime context target has the wrong owner"
        )
    if stat.S_IMODE(details.st_mode) != _CONTEXT_MODE:
        raise RuntimeContextV2Ambiguous(
            "runtime context target has the wrong mode"
        )
    if details.st_nlink != 1:
        raise RuntimeContextV2Ambiguous(
            "runtime context target is a hardlink"
        )
    if details.st_size > MAX_CONTRACT_BYTES:
        raise RuntimeContextV2Ambiguous(
            "runtime context target is oversize"
        )


def _read_candidate_once(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
) -> tuple[_FileSnapshot, bytes]:
    try:
        path_details = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RuntimeContextV2Ambiguous(
            "runtime context target cannot be inspected"
        ) from error
    _validate_candidate_stat(path_details, expected_uid=expected_uid)
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        raise RuntimeContextV2Ambiguous(
            "runtime context target changed before open"
        ) from error
    try:
        before = os.fstat(descriptor)
        _validate_candidate_stat(before, expected_uid=expected_uid)
        if (before.st_dev, before.st_ino) != (
            path_details.st_dev,
            path_details.st_ino,
        ):
            raise RuntimeContextV2Ambiguous(
                "runtime context target changed before read"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONTRACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        _validate_candidate_stat(after, expected_uid=expected_uid)
        if _snapshot(before) != _snapshot(after):
            raise RuntimeContextV2Ambiguous(
                "runtime context target is unstable during read"
            )
    finally:
        os.close(descriptor)
    try:
        RuntimeContextV3.from_bytes(payload)
    except (ContractError, TypeError, ValueError) as error:
        raise RuntimeContextV2Ambiguous(
            "runtime context target is noncanonical"
        ) from error
    return _snapshot(after), payload


def _read_stable_candidate(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
) -> tuple[_FileSnapshot, bytes] | None:
    try:
        first = _read_candidate_once(
            directory_fd, name, expected_uid=expected_uid
        )
    except FileNotFoundError:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RuntimeContextV2Ambiguous(
                "runtime context absence is ambiguous"
            ) from error
        raise RuntimeContextV2Ambiguous(
            "runtime context target appeared during observation"
        )
    try:
        second = _read_candidate_once(
            directory_fd, name, expected_uid=expected_uid
        )
    except FileNotFoundError as error:
        raise RuntimeContextV2Ambiguous(
            "runtime context target disappeared during observation"
        ) from error
    if first != second:
        raise RuntimeContextV2Ambiguous(
            "runtime context target is unstable across two reads"
        )
    return first


def _read_after_publish_collision(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
) -> tuple[_FileSnapshot, bytes] | None:
    """Allow only the bounded transient nlink=2 window of link publication."""

    for _attempt in range(128):
        try:
            return _read_stable_candidate(
                directory_fd, name, expected_uid=expected_uid
            )
        except RuntimeContextV2Ambiguous as error:
            if "hardlink" not in str(error):
                raise
            time.sleep(0.001)
    raise RuntimeContextV2Ambiguous(
        "runtime context concurrent publication retained a hardlink"
    )


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    device: int
    inode: int
    uid: int
    mode: int


def _directory_snapshot(
    details: os.stat_result, *, label: str, expected_uid: int
) -> _DirectorySnapshot:
    if not stat.S_ISDIR(details.st_mode):
        raise RuntimeContextV2Ambiguous(f"{label} is not a directory")
    if details.st_uid != expected_uid:
        raise RuntimeContextV2Ambiguous(f"{label} has the wrong owner")
    if stat.S_IMODE(details.st_mode) != _DIRECTORY_MODE:
        raise RuntimeContextV2Ambiguous(f"{label} is not owner-only")
    return _DirectorySnapshot(
        details.st_dev,
        details.st_ino,
        details.st_uid,
        details.st_mode,
    )


@dataclass(slots=True)
class _PinnedDirectoryChain:
    root: Path
    names: tuple[str, ...]
    descriptors: list[int]
    snapshots: tuple[_DirectorySnapshot, ...]
    expected_uid: int

    @property
    def parent_fd(self) -> int:
        return self.descriptors[-1]

    def verify(self) -> None:
        opened: list[int] = []
        try:
            root_fd = os.open(self.root, _DIRECTORY_FLAGS)
            opened.append(root_fd)
            root_snapshot = _directory_snapshot(
                os.fstat(root_fd),
                label="runtime context root",
                expected_uid=self.expected_uid,
            )
            if root_snapshot != self.snapshots[0]:
                raise RuntimeContextV2Ambiguous(
                    "runtime context directory chain was replaced"
                )
            current = root_fd
            for index, name in enumerate(self.names, start=1):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=current)
                opened.append(child)
                observed = _directory_snapshot(
                    os.fstat(child),
                    label="runtime context parent",
                    expected_uid=self.expected_uid,
                )
                if observed != self.snapshots[index]:
                    raise RuntimeContextV2Ambiguous(
                        "runtime context directory chain was replaced"
                    )
                current = child
        except RuntimeContextV2Ambiguous:
            raise
        except OSError as error:
            raise RuntimeContextV2Ambiguous(
                "runtime context directory chain was replaced"
            ) from error
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)


@contextmanager
def _pin_directory_chain(
    root: Path,
    parent_parts: tuple[str, ...],
    *,
    create: bool,
) -> Iterator[_PinnedDirectoryChain | None]:
    expected_uid = os.geteuid()
    descriptors: list[int] = []
    snapshots: list[_DirectorySnapshot] = []
    try:
        try:
            root_fd = os.open(root, _DIRECTORY_FLAGS)
        except OSError as error:
            raise RuntimeContextV2Ambiguous(
                "runtime context root cannot be pinned"
            ) from error
        descriptors.append(root_fd)
        snapshots.append(
            _directory_snapshot(
                os.fstat(root_fd),
                label="runtime context root",
                expected_uid=expected_uid,
            )
        )
        current = root_fd
        names: list[str] = []
        for part in parent_parts:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    chain = _PinnedDirectoryChain(
                        root,
                        tuple(names),
                        descriptors,
                        tuple(snapshots),
                        expected_uid,
                    )
                    chain.verify()
                    try:
                        os.stat(part, dir_fd=current, follow_symlinks=False)
                    except FileNotFoundError:
                        yield None
                        return
                    except OSError as error:
                        raise RuntimeContextV2Ambiguous(
                            "runtime context parent absence is ambiguous"
                        ) from error
                    raise RuntimeContextV2Ambiguous(
                        "runtime context parent appeared during observation"
                    )
                try:
                    os.mkdir(part, _DIRECTORY_MODE, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise RuntimeContextV2Ambiguous(
                        "runtime context parent cannot be created"
                    ) from error
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                except OSError as error:
                    raise RuntimeContextV2Ambiguous(
                        "runtime context parent cannot be pinned"
                    ) from error
            except OSError as error:
                raise RuntimeContextV2Ambiguous(
                    "runtime context parent cannot be pinned"
                ) from error
            descriptors.append(child)
            names.append(part)
            snapshots.append(
                _directory_snapshot(
                    os.fstat(child),
                    label="runtime context parent",
                    expected_uid=expected_uid,
                )
            )
            current = child
        chain = _PinnedDirectoryChain(
            root,
            tuple(names),
            descriptors,
            tuple(snapshots),
            expected_uid,
        )
        chain.verify()
        yield chain
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise RuntimeContextV2Ambiguous(
                "runtime context temporary write made no progress"
            )
        offset += written


def _fault_hook(_stage: str) -> None:
    """Test seam for deterministic crash-boundary coverage."""


@dataclass(frozen=True, slots=True, init=False)
class RuntimeContextLocalObservationV2:
    """Canonical local read suitable for later evidence-store retention."""

    SCHEMA: ClassVar[str] = "personal-operator.canonical-read-observation.v2"
    service: str
    operation: str
    subject: str
    disposition: ObservationDisposition
    provider_status: str
    projection_bytes: bytes

    def __init__(
        self,
        *,
        subject: str,
        disposition: ObservationDisposition,
        provider_status: str,
        projection: Mapping[str, Any],
        _token: object | None = None,
    ) -> None:
        if _token is not _LOCAL_OBSERVATION_TOKEN:
            raise RuntimeContextV2Error(
                "runtime context local observation is not constructible"
            )
        try:
            projection_bytes = canonical_json_bytes(dict(projection))
            parse_canonical_object(projection_bytes)
        except (ContractError, TypeError, ValueError) as error:
            raise RuntimeContextV2Error(
                "runtime context local projection is not canonical"
            ) from error
        object.__setattr__(self, "service", "local-filesystem")
        object.__setattr__(self, "operation", "read_runtime_context")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "provider_status", provider_status)
        object.__setattr__(self, "projection_bytes", projection_bytes)

    def projection(self) -> dict[str, Any]:
        return parse_canonical_object(self.projection_bytes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "service": self.service,
            "operation": self.operation,
            "subject": self.subject,
            "disposition": self.disposition.value,
            "providerStatus": self.provider_status,
            "projection": self.projection(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


_LOCAL_OBSERVATION_TOKEN = object()


def _local_observation(
    *,
    request: RuntimeContextWriteRequestV2,
    trusted_inputs: TrustedRuntimeContextInputsV2,
    disposition: ObservationDisposition,
    provider_status: str,
    expected_sha256: str,
    observed_sha256: str,
    size: int,
) -> RuntimeContextLocalObservationV2:
    return RuntimeContextLocalObservationV2(
        subject=request.subject,
        disposition=disposition,
        provider_status=provider_status,
        projection={
            "planSha256": trusted_inputs.plan_sha256,
            "completedPrefixSha256": trusted_inputs.completed_prefix_sha256,
            "sourceCommit": request.source_commit,
            "sourceTree": request.source_tree,
            "contextRelativePath": trusted_inputs.context_relative_path,
            "runtimeImageDigest": trusted_inputs.runtime_image_digest,
            "runtimeEndpointName": trusted_inputs.runtime_endpoint_name,
            "runtimeEndpointArn": trusted_inputs.runtime_endpoint_arn,
            "workloadIdentityArn": trusted_inputs.workload_identity_arn,
            "endpointEvidenceSha256": (
                trusted_inputs.endpoint_evidence_sha256
            ),
            "expectedRuntimeContextSha256": expected_sha256,
            "observedRuntimeContextSha256": observed_sha256,
            "size": size,
        },
        _token=_LOCAL_OBSERVATION_TOKEN,
    )


def _bind_resolved_context_write(
    *,
    request: RuntimeContextWriteRequestV2,
    trusted_inputs: TrustedRuntimeContextInputsV2,
    resolved_request: ResolvedMutationRequestV2,
) -> ResolvedMutationRequestV2:
    if type(resolved_request) is not ResolvedMutationRequestV2:
        raise RuntimeContextV2Error(
            "runtime context write requires an exact resolved mutation request"
        )
    try:
        resolved = ResolvedMutationRequestV2.from_bytes(
            resolved_request.to_bytes()
        )
    except (AttributeError, ContractError, TypeError, ValueError) as error:
        raise RuntimeContextV2Error(
            "runtime context resolved mutation request is not canonical"
        ) from error
    mutation = resolved.mutation_request
    foundation = resolved.foundation_runtime_inputs
    if (
        mutation.transaction_id != trusted_inputs.transaction_id
        or mutation.plan_sha256 != trusted_inputs.plan_sha256
        or mutation.completed_prefix_sha256
        != trusted_inputs.completed_prefix_sha256
        or mutation.step_id != trusted_inputs.step_id
        or mutation.operation_sha256 != trusted_inputs.operation_sha256
        or mutation.request_artifact != trusted_inputs.request_artifact
        or mutation.kind != "RUNTIME_CONTEXT_WRITE"
        or mutation.subject != request.subject
        or mutation.request_sha256 != request.digest()
        or resolved.source_commit != trusted_inputs.source_commit
        or resolved.source_tree != trusted_inputs.source_tree
        or resolved.account != trusted_inputs.account
        or resolved.region != trusted_inputs.region
        or resolved.step_phase != "context"
        or resolved.request_artifact_size
        != trusted_inputs.request_artifact_size
        or any(
            (
                resolved.expected_template_sha256,
                resolved.expected_template_parameter_sha256,
                resolved.expected_observed_request_sha256,
                resolved.expected_content_sha256,
                resolved.runtime_context_sha256,
                resolved.router_target_stack_id,
                resolved.router_change_set_id,
                resolved.cron_target_stack_id,
                resolved.cron_change_set_id,
                resolved.router_cron_changesets_sha256,
                resolved.router_cron_application_sha256,
                resolved.scheduler_target_stack_id,
                resolved.scheduler_change_set_id,
                resolved.scheduler_changeset_sha256,
                resolved.scheduler_application_sha256,
                resolved.web_target_stack_id,
                resolved.web_change_set_id,
                resolved.web_changeset_sha256,
                resolved.web_application_sha256,
                resolved.predecessor_stack_id,
                resolved.predecessor_evidence_sha256,
                resolved.predecessor_observer_evidence_sha256,
            )
        )
        or foundation is None
        or foundation.digest() != trusted_inputs.foundation_inputs_sha256
        or (
            foundation.source_commit,
            foundation.source_tree,
            foundation.account,
            foundation.region,
            foundation.release_plan_sha256,
        )
        != (
            trusted_inputs.source_commit,
            trusted_inputs.source_tree,
            trusted_inputs.account,
            trusted_inputs.region,
            trusted_inputs.plan_sha256,
        )
        or foundation.agent_core_stack_id != trusted_inputs.agent_core_stack_id
        or resolved.agent_core_stack_id != trusted_inputs.agent_core_stack_id
        or resolved.runtime_image_digest != trusted_inputs.runtime_image_digest
        or resolved.runtime_id != trusted_inputs.runtime_id
        or resolved.runtime_version != trusted_inputs.runtime_version
        or resolved.runtime_arn != trusted_inputs.runtime_arn
        or resolved.runtime_endpoint_id != trusted_inputs.runtime_endpoint_id
    ):
        raise RuntimeContextV2Error(
            "runtime context resolved mutation differs from retained inputs"
        )
    return resolved


def _consume_context_dispatch(
    fresh_authority: FreshDispatchAuthorityV1,
    *,
    resolved_request: ResolvedMutationRequestV2,
) -> ReleaseDispatchAttemptV1:
    if type(fresh_authority) is not FreshDispatchAuthorityV1:
        raise RuntimeContextV2Error(
            "runtime context write requires exact fresh dispatch authority"
        )
    try:
        return fresh_authority.consume(
            provider="LOCAL_FILESYSTEM",
            operation_sha256=(
                resolved_request.mutation_request.operation_sha256
            ),
            resolved_request_sha256=resolved_request.digest(),
        )
    except DispatchAttemptError as error:
        raise RuntimeContextV2Error(
            "runtime context fresh dispatch authority differs from its operation"
        ) from error


class RuntimeContextFileV2:
    """Secure no-clobber writer and two-read observer for one context file."""

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        if not candidate.is_absolute():
            candidate = candidate.absolute()
        self._root = candidate

    @staticmethod
    def _expected(
        request: RuntimeContextWriteRequestV2,
        trusted_inputs: TrustedRuntimeContextInputsV2,
    ) -> tuple[RuntimeContextV3, bytes, str]:
        if not isinstance(trusted_inputs, TrustedRuntimeContextInputsV2):
            raise RuntimeContextV2Error(
                "runtime context writer requires retained-derived inputs"
            )
        context = trusted_inputs.runtime_context(request)
        payload = context.to_bytes()
        if not payload or len(payload) > MAX_CONTRACT_BYTES:
            raise RuntimeContextV2Error(
                "runtime context canonical payload exceeds its bound"
            )
        try:
            reparsed = RuntimeContextV3.from_bytes(payload)
        except ContractError as error:
            raise RuntimeContextV2Error(
                "runtime context canonical payload is invalid"
            ) from error
        if reparsed != context:
            raise RuntimeContextV2Error(
                "runtime context canonical payload changed during validation"
            )
        return context, payload, hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _path_parts(
        trusted_inputs: TrustedRuntimeContextInputsV2,
    ) -> tuple[tuple[str, ...], str]:
        parts = PurePosixPath(trusted_inputs.context_relative_path).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise RuntimeContextV2Error(
                "runtime context request path is not canonical"
            )
        return tuple(parts[:-1]), parts[-1]

    def observe(
        self,
        *,
        request: RuntimeContextWriteRequestV2,
        trusted_inputs: TrustedRuntimeContextInputsV2,
    ) -> RuntimeContextLocalObservationV2:
        _context, expected, expected_sha256 = self._expected(
            request, trusted_inputs
        )
        parents, name = self._path_parts(trusted_inputs)
        with _pin_directory_chain(
            self._root, parents, create=False
        ) as chain:
            if chain is None:
                return _local_observation(
                    request=request,
                    trusted_inputs=trusted_inputs,
                    disposition=ObservationDisposition.ABSENT,
                    provider_status="NOT_FOUND",
                    expected_sha256=expected_sha256,
                    observed_sha256="",
                    size=0,
                )
            chain.verify()
            observed = _read_stable_candidate(
                chain.parent_fd,
                name,
                expected_uid=chain.expected_uid,
            )
            chain.verify()
            if observed is None:
                return _local_observation(
                    request=request,
                    trusted_inputs=trusted_inputs,
                    disposition=ObservationDisposition.ABSENT,
                    provider_status="NOT_FOUND",
                    expected_sha256=expected_sha256,
                    observed_sha256="",
                    size=0,
                )
            _snapshot_value, payload = observed
            observed_sha256 = hashlib.sha256(payload).hexdigest()
            if payload != expected:
                return _local_observation(
                    request=request,
                    trusted_inputs=trusted_inputs,
                    disposition=ObservationDisposition.FAILED_RETAINED,
                    provider_status="EXISTING_CONTENT_CONFLICT",
                    expected_sha256=expected_sha256,
                    observed_sha256=observed_sha256,
                    size=len(payload),
                )
            return _local_observation(
                request=request,
                trusted_inputs=trusted_inputs,
                disposition=ObservationDisposition.PRESENT,
                provider_status="PRESENT",
                expected_sha256=expected_sha256,
                observed_sha256=observed_sha256,
                size=len(payload),
            )

    def write(
        self,
        *,
        request: RuntimeContextWriteRequestV2,
        trusted_inputs: TrustedRuntimeContextInputsV2,
        resolved_request: ResolvedMutationRequestV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        _context, payload, _expected_sha256 = self._expected(
            request, trusted_inputs
        )
        resolved = _bind_resolved_context_write(
            request=request,
            trusted_inputs=trusted_inputs,
            resolved_request=resolved_request,
        )
        parents, name = self._path_parts(trusted_inputs)
        preexisting: tuple[_FileSnapshot, bytes] | None = None
        with _pin_directory_chain(
            self._root, parents, create=False
        ) as existing_chain:
            if existing_chain is not None:
                existing_chain.verify()
                preexisting = _read_after_publish_collision(
                    existing_chain.parent_fd,
                    name,
                    expected_uid=existing_chain.expected_uid,
                )
                existing_chain.verify()
        consumed_attempt = _consume_context_dispatch(
            fresh_authority,
            resolved_request=resolved,
        )
        if preexisting is not None:
            return consumed_attempt
        with _pin_directory_chain(self._root, parents, create=True) as chain:
            if chain is None:  # pragma: no cover - create=True cannot yield None
                raise RuntimeContextV2Ambiguous(
                    "runtime context parent creation was inconclusive"
                )
            chain.verify()
            # Another exact writer may be inside the bounded hard-link
            # publication window (temporary name plus final name).  Wait only
            # for that specific transient; every other ambiguity still fails
            # immediately and a persistent hardlink remains rejected.
            existing = _read_after_publish_collision(
                chain.parent_fd,
                name,
                expected_uid=chain.expected_uid,
            )
            if existing is not None:
                chain.verify()
                return consumed_attempt
            temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
            temporary_exists = False
            published = False
            try:
                try:
                    descriptor = os.open(
                        temporary_name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        _TEMPORARY_MODE,
                        dir_fd=chain.parent_fd,
                    )
                except OSError as error:
                    raise RuntimeContextV2Ambiguous(
                        "runtime context temporary file cannot be created"
                    ) from error
                temporary_exists = True
                try:
                    before = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != chain.expected_uid
                        or stat.S_IMODE(before.st_mode) != _TEMPORARY_MODE
                        or before.st_nlink != 1
                    ):
                        raise RuntimeContextV2Ambiguous(
                            "runtime context temporary file is unsafe"
                        )
                    _write_all(descriptor, payload)
                    _fault_hook("after_write")
                    os.fsync(descriptor)
                    _fault_hook("after_file_fsync")
                    os.fchmod(descriptor, _CONTEXT_MODE)
                    os.fsync(descriptor)
                    _fault_hook("after_chmod")
                finally:
                    os.close(descriptor)
                _fault_hook("before_publish")
                chain.verify()
                try:
                    os.link(
                        temporary_name,
                        name,
                        src_dir_fd=chain.parent_fd,
                        dst_dir_fd=chain.parent_fd,
                        follow_symlinks=False,
                    )
                    published = True
                except FileExistsError:
                    observed = _read_after_publish_collision(
                        chain.parent_fd,
                        name,
                        expected_uid=chain.expected_uid,
                    )
                    if observed is None:
                        raise RuntimeContextV2Ambiguous(
                            "runtime context concurrent publication is unstable"
                        )
                    chain.verify()
                    return consumed_attempt
                except OSError as error:
                    raise RuntimeContextV2Ambiguous(
                        "runtime context cannot be published without replacement"
                    ) from error
                os.unlink(temporary_name, dir_fd=chain.parent_fd)
                temporary_exists = False
                _fault_hook("after_publish")
                os.fsync(chain.parent_fd)
                _fault_hook("after_directory_fsync")
                chain.verify()
                observed = _read_stable_candidate(
                    chain.parent_fd,
                    name,
                    expected_uid=chain.expected_uid,
                )
                if observed is None or observed[1] != payload:
                    raise RuntimeContextV2Ambiguous(
                        "runtime context publication cannot be re-observed"
                    )
                return consumed_attempt
            finally:
                if temporary_exists:
                    try:
                        os.unlink(temporary_name, dir_fd=chain.parent_fd)
                        os.fsync(chain.parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        if not published:
                            raise RuntimeContextV2Ambiguous(
                                "runtime context temporary cleanup is ambiguous"
                            )


__all__ = [
    "RuntimeContextFileV2",
    "RuntimeContextLocalObservationV2",
    "RuntimeContextV2Ambiguous",
    "RuntimeContextV2Error",
    "RuntimeContextWriteRequestV2",
    "TrustedRuntimeContextInputsV2",
    "derive_trusted_runtime_context_inputs",
]
