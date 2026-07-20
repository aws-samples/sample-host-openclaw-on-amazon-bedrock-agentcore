"""Hostile tests for the release-v2 AgentCore MMDSv2 hardening boundary."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from typing import Iterator

import pytest

from release_tools.aws_authority_v2 import AttestedAwsClientV2, _CLIENT_TOKEN
from release_tools.agentcore_hardening_v2 import (
    AgentCoreHardeningAuthorityV1,
    AgentCoreHardeningDispatchAmbiguous,
    AgentCoreHardeningDispatchReceiptV1,
    AgentCoreHardeningDispatcherV1,
    AgentCoreHardeningError,
    AgentCoreHardeningInspectorV1,
    AgentCoreHardeningObservationAmbiguous,
    AgentCoreHardeningObserverV1,
    AgentCoreHardeningOperationV1,
    AgentCoreHardeningPreconditionV1,
    AgentCoreHardeningReceiptSinkV1,
    VerifiedAgentCoreHardeningPreflightV1,
    VerifiedAgentCoreHardeningReceiptV1,
    _new_agentcore_hardening_receipt_sink,
    _verified_retained_receipt,
    validate_agentcore_hardening_authority,
    validate_agentcore_hardening_preflight,
)
from release_tools.contracts import (
    ReleasePlanV2,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.evidence_store_v2 import _journal_path_sha256
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)
from release_tools.production_observer_v2 import CanonicalReadObservationV2
from release_tools.test_contracts import _release_plan_v2
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _resolved_mutation_request,
)
from release_tools.transaction import ObservationDisposition


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
RUNTIME_ID = "Runtime-ABCDEFGHIJ"
RUNTIME_VERSION = "7"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agent/"
    f"12345678-1234-1234-1234-123456789abc:{RUNTIME_VERSION}"
)


def _operation(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": AgentCoreHardeningOperationV1.SCHEMA,
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "runtimeName": "personal_operator_bridge",
        "metadataConfiguration": {"requireMMDSV2": True},
    }
    value.update(overrides)
    return value


def _plan_for_operation(payload: bytes) -> ReleasePlanV2:
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    step = next(item for item in steps if item["kind"] == "AGENTCORE_HARDEN")
    artifact = next(
        item for item in artifacts if item["path"] == step["requestArtifact"]
    )
    digest = hashlib.sha256(payload).hexdigest()
    step["requestSha256"] = digest
    step["expectedRequestSha256"] = digest
    artifact["sha256"] = digest
    artifact["size"] = len(payload)
    return ReleasePlanV2.from_mapping(value)


def test_static_operation_is_exact_and_uniquely_planned() -> None:
    payload = canonical_json_bytes(_operation())
    operation = AgentCoreHardeningOperationV1.from_bytes(payload)
    preflight = validate_agentcore_hardening_preflight(
        operation,
        release_plan=_plan_for_operation(payload),
    )

    assert operation.to_bytes() == payload
    assert isinstance(preflight, VerifiedAgentCoreHardeningPreflightV1)
    with pytest.raises(AgentCoreHardeningError, match="not directly constructible"):
        AgentCoreHardeningOperationV1(
            source_commit=COMMIT,
            source_tree=TREE,
            account=ACCOUNT,
            region=REGION,
            runtime_name="personal_operator_bridge",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account", "999999999999"),
        ("region", "us-east-1"),
        ("sourceCommit", "c" * 40),
        ("sourceTree", "d" * 40),
        ("runtimeName", "caller_selected"),
        ("metadataConfiguration", {"requireMMDSV2": False}),
        ("metadataConfiguration", {"requireMMDSV2": True, "extra": True}),
    ],
)
def test_static_operation_cannot_cross_or_weaken_plan_identity(
    field: str,
    value: object,
) -> None:
    canonical_payload = canonical_json_bytes(_operation())
    plan = _plan_for_operation(canonical_payload)
    with pytest.raises(AgentCoreHardeningError):
        candidate = AgentCoreHardeningOperationV1.from_mapping(
            _operation(**{field: value})
        )
        validate_agentcore_hardening_preflight(candidate, release_plan=plan)


class MemoryReceiptBackend:
    def __init__(
        self,
        *,
        evidence_store_sha256: str = "1" * 64,
        journal_path_sha256: str = "2" * 64,
        journal_execution_id: str = "3" * 64,
    ) -> None:
        self.attempted = False
        self.payload: bytes | None = None
        self.precondition_payload: bytes | None = None
        self.retain_error: BaseException | None = None
        self.retain_replacement: bytes | None = None
        self.precondition_retain_error: BaseException | None = None
        self.binding_value = {
            "evidenceStoreSha256": evidence_store_sha256,
            "journalPathSha256": journal_path_sha256,
            "journalExecutionId": journal_execution_id,
        }

    def binding(self) -> dict[str, str]:
        return dict(self.binding_value)

    def load(self) -> tuple[bool, bytes | None]:
        return self.attempted, self.payload

    def load_precondition(self) -> bytes | None:
        return self.precondition_payload

    def retain_precondition(self, payload: bytes) -> None:
        if self.precondition_retain_error is not None:
            raise self.precondition_retain_error
        if (
            self.precondition_payload is not None
            and self.precondition_payload != payload
        ):
            raise RuntimeError("alternate precondition")
        self.precondition_payload = payload

    def begin_attempt(self) -> bool:
        if self.precondition_payload is None:
            raise RuntimeError("attempt preceded retained precondition")
        if self.attempted:
            return False
        self.attempted = True
        return True

    def retain(self, payload: bytes) -> None:
        if self.retain_error is not None:
            raise self.retain_error
        self.payload = self.retain_replacement or payload


class FakeAgentCore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.get_responses: list[object] = []
        self.update_responses: list[object] = []
        self.meta = SimpleNamespace(
            region_name=REGION,
            service_model=SimpleNamespace(
                service_name="bedrock-agentcore-control"
            ),
            config=SimpleNamespace(
                region_name=REGION,
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    @staticmethod
    def _next(queue: list[object]) -> object:
        if not queue:
            raise AssertionError("unexpected AgentCore call")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def get_agent_runtime(self, **kwargs: object) -> object:
        self.calls.append(("get_agent_runtime", dict(kwargs)))
        return self._next(self.get_responses)

    def update_agent_runtime(self, **kwargs: object) -> object:
        self.calls.append(("update_agent_runtime", dict(kwargs)))
        return self._next(self.update_responses)

    def close(self) -> None:
        return None


@contextmanager
def _attested_agentcore_client(
    client: object,
    *,
    capability: str,
) -> Iterator[AttestedAwsClientV2]:
    authority = AttestedAwsClientV2(
        client,
        service="bedrock-agentcore-control",
        account=ACCOUNT,
        region=REGION,
        capability=capability,
        _token=_CLIENT_TOKEN,
    )
    try:
        yield authority
    finally:
        authority.close()


@pytest.fixture(scope="module")
def hardening_prefix(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[
    VerifiedAgentCoreHardeningPreflightV1,
    ResolvedMutationRequestV2,
    ReleasePlanV2,
    StagingTransactionV2,
    str,
    str,
    str,
]:
    tmp_path = tmp_path_factory.mktemp("agentcore-hardening-prefix")
    payload = canonical_json_bytes(_operation())
    plan = _plan_for_operation(payload)
    preflight = validate_agentcore_hardening_preflight(
        AgentCoreHardeningOperationV1.from_bytes(payload),
        release_plan=plan,
    )
    journal = _create_v2(tmp_path / "journal", plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "runtime:AGENTCORE_HARDEN")
    journal.begin_step()
    transaction = journal.current
    resolved = _resolved_mutation_request(
        journal,
        request_artifact_size=len(payload),
    )
    return (
        preflight,
        resolved,
        plan,
        transaction,
        journal.evidence_store.identity_sha256,
        _journal_path_sha256(journal.path),
        journal.journal_execution_id,
    )


def _verified_authority(
    prefix: tuple[
        VerifiedAgentCoreHardeningPreflightV1,
        ResolvedMutationRequestV2,
        ReleasePlanV2,
        StagingTransactionV2,
        str,
        str,
        str,
    ],
    *,
    binding_overrides: dict[str, str] | None = None,
) -> tuple[
    AgentCoreHardeningAuthorityV1,
    MemoryReceiptBackend,
    ResolvedMutationRequestV2,
    ReleasePlanV2,
    StagingTransactionV2,
]:
    (
        preflight,
        resolved,
        plan,
        transaction,
        evidence_store_sha256,
        journal_path_sha256,
        journal_execution_id,
    ) = prefix
    binding = {
        "evidenceStoreSha256": evidence_store_sha256,
        "journalPathSha256": journal_path_sha256,
        "journalExecutionId": journal_execution_id,
    }
    binding.update(binding_overrides or {})
    backend = MemoryReceiptBackend(
        evidence_store_sha256=binding["evidenceStoreSha256"],
        journal_path_sha256=binding["journalPathSha256"],
        journal_execution_id=binding["journalExecutionId"],
    )
    sink = _new_agentcore_hardening_receipt_sink(
        backend,
        release_plan=plan,
        transaction=transaction,
        evidence_store_sha256=binding["evidenceStoreSha256"],
        journal_path_sha256=binding["journalPathSha256"],
        journal_execution_id=binding["journalExecutionId"],
    )
    authority = validate_agentcore_hardening_authority(
        resolved,
        preflight,
        transaction,
        sink,
    )
    return authority, backend, resolved, plan, transaction


def _runtime(
    resolved: ResolvedMutationRequestV2,
    *,
    version: str | None = None,
    metadata: object = None,
    service_s3_endpoint: object = None,
    status: str = "READY",
) -> dict[str, object]:
    selected_version = version or resolved.runtime_version
    foundation = resolved.foundation_runtime_inputs
    assert foundation is not None
    environment = {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
        "CAPABILITY_GATEWAY_FUNCTION_ARN": (
            foundation.capability_gateway_function_arn
        ),
        "DISABLE_ADOT_OBSERVABILITY": "true",
        "S3_USER_FILES_BUCKET": foundation.user_files_bucket_name,
        "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
            foundation.workspace_broker_function_name
        ),
        "WORKSPACE_SYNC_INTERVAL_MS": "300000",
    }
    if foundation.guardrail_id:
        environment.update(
            {
                "BEDROCK_GUARDRAIL_ID": foundation.guardrail_id,
                "BEDROCK_GUARDRAIL_VERSION": foundation.guardrail_version,
            }
        )
    vpc: dict[str, object] = {
        "securityGroups": list(foundation.runtime_security_group_ids),
        "subnets": list(foundation.private_subnet_ids),
    }
    if service_s3_endpoint is not None:
        vpc["requireServiceS3Endpoint"] = service_s3_endpoint
    value: dict[str, object] = {
        "agentRuntimeId": resolved.runtime_id,
        "agentRuntimeName": "personal_operator_bridge",
        "agentRuntimeVersion": selected_version,
        "agentRuntimeArn": resolved.runtime_arn.rsplit(":", 1)[0]
        + ":"
        + selected_version,
        "status": status,
        "roleArn": (
            f"arn:aws:iam::{ACCOUNT}:role/"
            f"openclaw-agentcore-execution-role-{REGION}"
        ),
        "description": (
            f"Personal Operator immutable bridge runtime at commit {COMMIT}"
        ),
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": resolved.runtime_image_digest}
        },
        "authorizerConfiguration": {},
        "requestHeaderConfiguration": {},
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": vpc,
        },
        "environmentVariables": environment,
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 1800,
            "maxLifetime": 28800,
        },
    }
    value["agentRuntimeArtifact"] = {
        "containerConfiguration": {
            "containerUri": (
                f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
                f"personal-operator/bridge@{resolved.runtime_image_digest}"
            )
        }
    }
    if metadata is not None:
        value["metadataConfiguration"] = metadata
    return value


def _inspect(
    authority: AgentCoreHardeningAuthorityV1,
    responses: list[object],
) -> tuple[AgentCoreHardeningPreconditionV1, FakeAgentCore]:
    fake = FakeAgentCore()
    fake.get_responses.extend(responses)
    with _attested_agentcore_client(fake, capability="observer") as client:
        precondition = AgentCoreHardeningInspectorV1(client).inspect(authority)
    return precondition, fake


def test_precondition_derives_exact_noop_from_two_stable_prefix_bound_reads(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    live = _runtime(
        resolved,
        metadata={"requireMMDSV2": True},
        service_s3_endpoint=False,
    )

    precondition, fake = _inspect(authority, [deepcopy(live), deepcopy(live)])

    assert precondition.mode == "NOOP"
    assert fake.calls == [
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": RUNTIME_VERSION,
            },
        ),
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": RUNTIME_VERSION,
            },
        ),
    ]


def test_precondition_is_canonical_durable_and_exact_authority_bound(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    live = _runtime(
        resolved,
        metadata={"requireMMDSV2": False},
        service_s3_endpoint=True,
    )
    original, _ = _inspect(authority, [deepcopy(live), deepcopy(live)])

    payload = original.to_bytes()
    recovered = AgentCoreHardeningPreconditionV1.from_bytes(payload)

    assert recovered.to_bytes() == payload
    assert recovered.digest() == hashlib.sha256(payload).hexdigest()
    assert recovered.mode == "UPDATE"
    assert recovered._binding(authority).projection_bytes == (
        original._binding(authority).projection_bytes
    )

    crossed, _, _, _, _ = _verified_authority(
        hardening_prefix,
        binding_overrides={"journalExecutionId": "f" * 64},
    )
    with pytest.raises(AgentCoreHardeningError, match="precondition|authority"):
        recovered._binding(crossed)


@pytest.mark.parametrize(
    ("metadata", "s3", "mode"),
    [
        (None, None, "UPDATE"),
        ({}, None, "UPDATE"),
        ({"requireMMDSV2": False}, None, "UPDATE"),
        ({"requireMMDSV2": None}, None, "UPDATE"),
        ({"requireMMDSV2": True}, True, "UPDATE"),
        ({"requireMMDSV2": True}, None, "NOOP"),
        ({"requireMMDSV2": True}, False, "NOOP"),
    ],
)
def test_precondition_closes_mmdsv2_and_service_s3_tri_state(
    hardening_prefix: tuple,
    metadata: object,
    s3: object,
    mode: str,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    live = _runtime(resolved, metadata=metadata, service_s3_endpoint=s3)

    precondition, _ = _inspect(authority, [deepcopy(live), deepcopy(live)])

    assert precondition.mode == mode


def test_precondition_rejects_unstable_or_unreviewed_configuration(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    first = _runtime(resolved, metadata={"requireMMDSV2": False})
    changed = deepcopy(first)
    environment = changed["environmentVariables"]
    assert isinstance(environment, dict)
    environment["WORKSPACE_SYNC_INTERVAL_MS"] = "300001"

    with pytest.raises(AgentCoreHardeningError, match="changed"):
        _inspect(authority, [first, changed])

    malformed = _runtime(resolved, metadata={"requireMMDSV2": False})
    network = malformed["networkConfiguration"]
    assert isinstance(network, dict)
    vpc = network["networkModeConfig"]
    assert isinstance(vpc, dict)
    vpc["callerSelected"] = True
    with pytest.raises(AgentCoreHardeningError, match="configuration"):
        _inspect(authority, [malformed, deepcopy(malformed)])


def test_authority_rejects_prefix_or_runtime_substitution(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, plan, transaction = _verified_authority(
        hardening_prefix
    )
    assert isinstance(authority, AgentCoreHardeningAuthorityV1)
    payload = canonical_json_bytes(_operation())
    preflight = validate_agentcore_hardening_preflight(
        AgentCoreHardeningOperationV1.from_bytes(payload),
        release_plan=plan,
    )
    (
        _,
        _,
        _,
        _,
        evidence_store_sha256,
        journal_path_sha256,
        journal_execution_id,
    ) = hardening_prefix
    backend = MemoryReceiptBackend(
        evidence_store_sha256=evidence_store_sha256,
        journal_path_sha256=journal_path_sha256,
        journal_execution_id=journal_execution_id,
    )
    sink = _new_agentcore_hardening_receipt_sink(
        backend,
        release_plan=plan,
        transaction=transaction,
        evidence_store_sha256=evidence_store_sha256,
        journal_path_sha256=journal_path_sha256,
        journal_execution_id=journal_execution_id,
    )
    crossed = replace(
        resolved,
        runtime_id="Runtime-KLMNOPQRST",
    )

    with pytest.raises(AgentCoreHardeningError):
        validate_agentcore_hardening_authority(
            crossed,
            preflight,
            transaction,
            sink,
        )


@pytest.mark.parametrize(
    ("field", "crossed"),
    [
        ("evidenceStoreSha256", "d" * 64),
        ("journalPathSha256", "e" * 64),
        ("journalExecutionId", "f" * 64),
    ],
)
def test_sink_mint_rejects_crossed_backend_storage_binding(
    hardening_prefix: tuple,
    field: str,
    crossed: str,
) -> None:
    (
        _,
        _,
        plan,
        transaction,
        evidence_store_sha256,
        journal_path_sha256,
        journal_execution_id,
    ) = hardening_prefix
    expected = {
        "evidenceStoreSha256": evidence_store_sha256,
        "journalPathSha256": journal_path_sha256,
        "journalExecutionId": journal_execution_id,
    }
    backend = MemoryReceiptBackend(
        evidence_store_sha256=evidence_store_sha256,
        journal_path_sha256=journal_path_sha256,
        journal_execution_id=journal_execution_id,
    )
    supplied = dict(expected)
    supplied[field] = crossed

    with pytest.raises(AgentCoreHardeningError, match="binding"):
        _new_agentcore_hardening_receipt_sink(
            backend,
            release_plan=plan,
            transaction=transaction,
            evidence_store_sha256=supplied["evidenceStoreSha256"],
            journal_path_sha256=supplied["journalPathSha256"],
            journal_execution_id=supplied["journalExecutionId"],
        )


def test_sink_rechecks_backend_binding_before_every_authority_use(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    live = _runtime(resolved, metadata={"requireMMDSV2": True})
    backend.binding_value["journalPathSha256"] = "e" * 64

    with pytest.raises(AgentCoreHardeningError, match="binding"):
        _inspect(authority, [deepcopy(live), deepcopy(live)])


def test_storage_binding_is_part_of_the_unforgeable_authority_digest(
    hardening_prefix: tuple,
) -> None:
    original, _, _, _, _ = _verified_authority(hardening_prefix)
    restarted, _, _, _, _ = _verified_authority(
        hardening_prefix,
        binding_overrides={"journalExecutionId": "f" * 64},
    )

    assert original.digest() != restarted.digest()


def test_internal_authority_types_are_not_caller_constructible() -> None:
    with pytest.raises(AgentCoreHardeningError, match="not constructible"):
        AgentCoreHardeningReceiptSinkV1(backend=MemoryReceiptBackend())


def _precondition_for(
    authority: AgentCoreHardeningAuthorityV1,
    resolved: ResolvedMutationRequestV2,
    *,
    metadata: object,
    service_s3_endpoint: object = None,
) -> AgentCoreHardeningPreconditionV1:
    live = _runtime(
        resolved,
        metadata=metadata,
        service_s3_endpoint=service_s3_endpoint,
    )
    precondition, _ = _inspect(authority, [deepcopy(live), deepcopy(live)])
    return precondition


def _fresh_dispatch_authority(
    authority: AgentCoreHardeningAuthorityV1,
    *,
    provider: str = "AGENTCORE",
    operation_sha256: str | None = None,
    resolved_request_sha256: str | None = None,
) -> FreshDispatchAuthorityV1:
    operation, plan, resolved, transaction, _sink, storage = (
        authority._binding()
    )
    request = resolved.mutation_request
    attempt = ReleaseDispatchAttemptV1.from_mapping(
        {
            "schema": ReleaseDispatchAttemptV1.SCHEMA,
            "releasePlanSha256": plan.digest(),
            "evidenceStoreSha256": storage.evidence_store_sha256,
            "journalPathSha256": storage.journal_path_sha256,
            "journalExecutionId": storage.journal_execution_id,
            "journalRevision": transaction.revision,
            "completedPrefixSha256": request.completed_prefix_sha256,
            "stepId": request.step_id,
            "subject": operation.subject,
            "operationSha256": operation_sha256 or request.operation_sha256,
            "resolvedRequestSha256": (
                resolved_request_sha256 or resolved.digest()
            ),
            "provider": provider,
        }
    )
    return _mint_fresh_dispatch_authority(attempt)


@pytest.mark.parametrize(
    "mode",
    (
        "missing",
        "duck",
        "crossed-provider",
        "crossed-operation",
        "crossed-resolved",
        "consumed",
    ),
)
def test_dispatch_requires_exact_fresh_attempt_before_any_provider_effect(
    hardening_prefix: tuple,
    mode: str,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(
        hardening_prefix
    )
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    fresh: object | None
    if mode == "missing":
        fresh = None
    elif mode == "duck":
        fresh = SimpleNamespace(consume=lambda **_kwargs: None)
    elif mode == "crossed-provider":
        fresh = _fresh_dispatch_authority(authority, provider="S3")
    elif mode == "crossed-operation":
        fresh = _fresh_dispatch_authority(
            authority, operation_sha256="sha256:" + "0" * 64
        )
    elif mode == "crossed-resolved":
        fresh = _fresh_dispatch_authority(
            authority, resolved_request_sha256="0" * 64
        )
    else:
        fresh = _fresh_dispatch_authority(authority)
        fresh.consume(
            provider="AGENTCORE",
            operation_sha256=resolved.mutation_request.operation_sha256,
            resolved_request_sha256=resolved.digest(),
        )
    fake = FakeAgentCore()
    fake.update_responses.append(_update_ack())
    with _attested_agentcore_client(fake, capability="mutation") as client:
        with pytest.raises((AgentCoreHardeningError, DispatchAttemptError)):
            AgentCoreHardeningDispatcherV1(client).dispatch(
                authority,
                precondition,
                fresh,
            )
    assert backend.attempted is False
    assert backend.payload is None
    assert fake.calls == []


def _dispatch(
    authority: AgentCoreHardeningAuthorityV1,
    precondition: AgentCoreHardeningPreconditionV1,
    responses: list[object],
) -> tuple[VerifiedAgentCoreHardeningReceiptV1, FakeAgentCore]:
    fake = FakeAgentCore()
    fake.update_responses.extend(responses)
    with _attested_agentcore_client(fake, capability="mutation") as client:
        attempt = AgentCoreHardeningDispatcherV1(client).dispatch(
            authority,
            precondition,
            _fresh_dispatch_authority(authority),
        )
    assert attempt.provider == "AGENTCORE"
    sink = authority._binding()[4]
    _attempted, payload = sink._load()
    assert payload is not None
    receipt = _verified_retained_receipt(
        payload,
        authority=authority,
        precondition=precondition,
    )
    return receipt, fake


def _update_ack(
    *,
    runtime_id: str = RUNTIME_ID,
    version: str = "8",
    status: str = "UPDATING",
) -> dict[str, str]:
    return {
        "agentRuntimeId": runtime_id,
        "agentRuntimeVersion": version,
        "status": status,
    }


def test_noop_is_receipted_without_call_and_cannot_be_replayed(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": True},
    )

    receipt, fake = _dispatch(authority, precondition, [])
    with pytest.raises(AgentCoreHardeningDispatchAmbiguous, match="replayed"):
        _dispatch(authority, precondition, [])

    assert receipt.receipt.mode == "NOOP"
    assert receipt.receipt.prior_runtime_version == RUNTIME_VERSION
    assert receipt.receipt.resulting_runtime_version == RUNTIME_VERSION
    assert receipt.receipt.update_request_sha256 == ""
    assert receipt.receipt.evidence_store_sha256 == hardening_prefix[4]
    assert receipt.receipt.journal_path_sha256 == hardening_prefix[5]
    assert receipt.receipt.journal_execution_id == hardening_prefix[6]
    assert backend.precondition_payload == precondition.to_bytes()
    assert backend.attempted is True
    assert backend.payload == receipt.receipt.to_bytes()
    assert fake.calls == []


def test_restart_loads_retained_precondition_and_receipt_without_prior_read(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, plan, transaction = _verified_authority(
        hardening_prefix
    )
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    receipt, dispatch_client = _dispatch(
        authority, precondition, [_update_ack(version="8")]
    )
    assert [method for method, _ in dispatch_client.calls] == [
        "update_agent_runtime"
    ]
    assert backend.precondition_payload is not None
    assert backend.payload is not None

    preflight = validate_agentcore_hardening_preflight(
        AgentCoreHardeningOperationV1.from_bytes(
            canonical_json_bytes(_operation())
        ),
        release_plan=plan,
    )
    restarted_sink = _new_agentcore_hardening_receipt_sink(
        backend,
        release_plan=plan,
        transaction=transaction,
        evidence_store_sha256=hardening_prefix[4],
        journal_path_sha256=hardening_prefix[5],
        journal_execution_id=hardening_prefix[6],
    )
    restarted = validate_agentcore_hardening_authority(
        resolved, preflight, transaction, restarted_sink
    )
    recovered_precondition = AgentCoreHardeningPreconditionV1.from_bytes(
        restarted_sink._load_precondition()
    )
    recovered_receipt = _verified_retained_receipt(
        restarted_sink._load()[1],
        authority=restarted,
        precondition=recovered_precondition,
    )
    hardened = _runtime(
        resolved,
        version="8",
        metadata={"requireMMDSV2": True},
        service_s3_endpoint=False,
    )

    observation, observer_client = _observe(
        restarted,
        recovered_receipt,
        [deepcopy(hardened), deepcopy(hardened)],
    )

    assert observation.disposition is ObservationDisposition.PRESENT
    assert observation.projection()["runtimeVersion"] == "8"
    assert observer_client.calls == [
        (
            "get_agent_runtime",
            {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": "8"},
        ),
        (
            "get_agent_runtime",
            {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": "8"},
        ),
    ]
    assert receipt.receipt.to_bytes() == recovered_receipt.receipt.to_bytes()


@pytest.mark.parametrize("damage", ["missing", "torn", "crossed"])
def test_restart_rejects_invalid_retained_precondition_before_provider_read(
    hardening_prefix: tuple,
    damage: str,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    _dispatch(authority, precondition, [_update_ack(version="8")])
    assert backend.precondition_payload is not None
    if damage == "missing":
        backend.precondition_payload = None
    elif damage == "torn":
        backend.precondition_payload = backend.precondition_payload[:-1]
    else:
        raw = parse_canonical_object(backend.precondition_payload)
        raw["resolvedRequestSha256"] = "f" * 64
        backend.precondition_payload = canonical_json_bytes(raw)

    fake = FakeAgentCore()
    with pytest.raises(AgentCoreHardeningError, match="precondition|receipt"):
        retained = backend.load_precondition()
        if retained is None:
            raise AgentCoreHardeningError("retained precondition is missing")
        recovered = AgentCoreHardeningPreconditionV1.from_bytes(retained)
        recovered._binding(authority)
        _verified_retained_receipt(
            backend.payload,
            authority=authority,
            precondition=recovered,
        )
    assert fake.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidenceStoreSha256": "d" * 64},
        {"journalPathSha256": "e" * 64},
        {"journalExecutionId": "f" * 64},
    ],
    ids=("cross-store", "cross-path", "cross-execution"),
)
def test_retained_receipt_cannot_replay_across_store_path_or_execution(
    hardening_prefix: tuple,
    overrides: dict[str, str],
) -> None:
    original, _, original_resolved, _, _ = _verified_authority(
        hardening_prefix
    )
    original_precondition = _precondition_for(
        original,
        original_resolved,
        metadata={"requireMMDSV2": True},
    )
    receipt, _ = _dispatch(original, original_precondition, [])

    crossed, backend, resolved, _, _ = _verified_authority(
        hardening_prefix,
        binding_overrides=overrides,
    )
    backend.attempted = True
    backend.payload = receipt.receipt.to_bytes()
    backend.precondition_payload = original_precondition.to_bytes()
    crossed_precondition = _precondition_for(
        crossed,
        resolved,
        metadata={"requireMMDSV2": True},
    )

    with pytest.raises(
        AgentCoreHardeningError, match="precondition|receipt|binding"
    ):
        _dispatch(crossed, crossed_precondition, [])


def test_retained_receipt_cannot_replay_after_journal_restart(
    hardening_prefix: tuple,
) -> None:
    original, _, original_resolved, _, _ = _verified_authority(
        hardening_prefix
    )
    original_precondition = _precondition_for(
        original,
        original_resolved,
        metadata={"requireMMDSV2": True},
    )
    receipt, _ = _dispatch(original, original_precondition, [])

    restarted, backend, resolved, _, _ = _verified_authority(
        hardening_prefix,
        binding_overrides={"journalExecutionId": "d" * 64},
    )
    backend.attempted = True
    backend.payload = receipt.receipt.to_bytes()
    backend.precondition_payload = original_precondition.to_bytes()
    restarted_precondition = _precondition_for(
        restarted,
        resolved,
        metadata={"requireMMDSV2": True},
    )

    with pytest.raises(
        AgentCoreHardeningError, match="precondition|receipt|binding"
    ):
        _dispatch(restarted, restarted_precondition, [])


@pytest.mark.parametrize(
    ("service_s3_endpoint", "expected_field"),
    [(None, False), (False, False), (True, True)],
)
def test_update_sends_complete_reviewed_config_and_only_transitional_s3_false(
    hardening_prefix: tuple,
    service_s3_endpoint: object,
    expected_field: bool,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
        service_s3_endpoint=service_s3_endpoint,
    )

    verified, fake = _dispatch(authority, precondition, [_update_ack()])

    assert len(fake.calls) == 1
    method, request = fake.calls[0]
    assert method == "update_agent_runtime"
    assert set(request) == {
        "agentRuntimeId",
        "agentRuntimeArtifact",
        "roleArn",
        "networkConfiguration",
        "description",
        "protocolConfiguration",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "environmentVariables",
        "filesystemConfigurations",
        "clientToken",
    }
    assert request["agentRuntimeId"] == RUNTIME_ID
    assert request["metadataConfiguration"] == {"requireMMDSV2": True}
    network = request["networkConfiguration"]
    assert isinstance(network, dict)
    vpc = network["networkModeConfig"]
    assert isinstance(vpc, dict)
    assert ("requireServiceS3Endpoint" in vpc) is expected_field
    if expected_field:
        assert vpc["requireServiceS3Endpoint"] is False
    assert verified.receipt.mode == "UPDATED"
    assert verified.receipt.prior_runtime_version == "7"
    assert verified.receipt.resulting_runtime_version == "8"
    assert verified.receipt.resulting_runtime_arn.endswith(":8")
    assert verified.receipt.update_request_sha256 == hashlib.sha256(
        canonical_json_bytes(request)
    ).hexdigest()
    assert backend.payload == verified.receipt.to_bytes()


@pytest.mark.parametrize(
    "response",
    [
        TimeoutError("lost response"),
        {},
        _update_ack(runtime_id="Runtime-KLMNOPQRST"),
        _update_ack(version="7"),
        _update_ack(version="6"),
        _update_ack(version="latest"),
        _update_ack(status="UNKNOWN"),
    ],
)
def test_update_unknown_effect_is_attempted_once_and_never_retried(
    hardening_prefix: tuple,
    response: object,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    fake = FakeAgentCore()
    fake.update_responses.extend([response, _update_ack()])
    with _attested_agentcore_client(fake, capability="mutation") as client:
        dispatcher = AgentCoreHardeningDispatcherV1(client)
        with pytest.raises(AgentCoreHardeningDispatchAmbiguous):
            dispatcher.dispatch(
                authority,
                precondition,
                _fresh_dispatch_authority(authority),
            )
        with pytest.raises(AgentCoreHardeningDispatchAmbiguous, match="attempted"):
            dispatcher.dispatch(
                authority,
                precondition,
                _fresh_dispatch_authority(authority),
            )

    assert backend.attempted is True
    assert backend.payload is None
    assert len(fake.calls) == 1


def test_crash_after_call_before_receipt_retention_never_retries(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    backend.retain_error = OSError("simulated crash boundary")
    fake = FakeAgentCore()
    fake.update_responses.extend([_update_ack(), _update_ack()])
    with _attested_agentcore_client(fake, capability="mutation") as client:
        dispatcher = AgentCoreHardeningDispatcherV1(client)
        with pytest.raises(AgentCoreHardeningDispatchAmbiguous, match="receipt"):
            dispatcher.dispatch(
                authority,
                precondition,
                _fresh_dispatch_authority(authority),
            )
        with pytest.raises(AgentCoreHardeningDispatchAmbiguous, match="attempted"):
            dispatcher.dispatch(
                authority,
                precondition,
                _fresh_dispatch_authority(authority),
            )

    assert len(fake.calls) == 1


def test_retained_receipt_tamper_or_precondition_substitution_fails_closed(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    original_precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    receipt, _ = _dispatch(authority, original_precondition, [_update_ack()])
    assert backend.payload is not None

    tampered = parse_canonical_object(backend.payload)
    tampered["resultingRuntimeVersion"] = "9"
    backend.payload = canonical_json_bytes(tampered)
    with pytest.raises(AgentCoreHardeningError, match="changed|receipt"):
        _dispatch(authority, original_precondition, [])

    second_authority, second_backend, second_resolved, _, _ = _verified_authority(
        hardening_prefix
    )
    second_backend.attempted = True
    second_backend.payload = receipt.receipt.to_bytes()
    second_backend.precondition_payload = original_precondition.to_bytes()
    crossed_precondition = _precondition_for(
        second_authority,
        second_resolved,
        metadata={"requireMMDSV2": False},
        service_s3_endpoint=True,
    )
    with pytest.raises(AgentCoreHardeningError, match="precondition|receipt"):
        _dispatch(second_authority, crossed_precondition, [])


def _observe(
    authority: AgentCoreHardeningAuthorityV1,
    receipt: VerifiedAgentCoreHardeningReceiptV1,
    responses: list[object],
) -> tuple[CanonicalReadObservationV2, FakeAgentCore]:
    fake = FakeAgentCore()
    fake.get_responses.extend(responses)
    with _attested_agentcore_client(fake, capability="observer") as client:
        observation = AgentCoreHardeningObserverV1(client).observe(
            authority,
            receipt,
        )
    return observation, fake


def test_observer_reads_only_exact_receipted_version_and_returns_canonical_present(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    receipt, _ = _dispatch(authority, precondition, [_update_ack(version="8")])
    hardened = _runtime(
        resolved,
        version="8",
        metadata={"requireMMDSV2": True},
        service_s3_endpoint=False,
    )

    observation, fake = _observe(
        authority,
        receipt,
        [deepcopy(hardened), deepcopy(hardened)],
    )

    assert isinstance(observation, CanonicalReadObservationV2)
    assert observation.disposition is ObservationDisposition.PRESENT
    assert observation.provider_status == "READY"
    projection = observation.projection()
    assert projection["runtimeId"] == RUNTIME_ID
    assert projection["runtimeVersion"] == "8"
    assert projection["runtimeArn"].endswith(":8")
    assert projection["requiresMMDSV2"] is True
    assert projection["requiresServiceS3Endpoint"] is False
    assert projection["hardeningReceiptSha256"] == receipt.receipt.digest()
    assert fake.calls == [
        (
            "get_agent_runtime",
            {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": "8"},
        ),
        (
            "get_agent_runtime",
            {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": "8"},
        ),
    ]


def test_observer_never_guesses_latest_and_rejects_unstable_or_downgraded_state(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    receipt, _ = _dispatch(authority, precondition, [_update_ack(version="8")])
    exact = _runtime(
        resolved,
        version="8",
        metadata={"requireMMDSV2": True},
    )
    raced = _runtime(
        resolved,
        version="9",
        metadata={"requireMMDSV2": True},
    )
    with pytest.raises(AgentCoreHardeningError, match="identity"):
        _observe(authority, receipt, [raced, deepcopy(raced)])

    downgraded = deepcopy(exact)
    downgraded["metadataConfiguration"] = {"requireMMDSV2": False}
    with pytest.raises(AgentCoreHardeningError, match="MMDSv2"):
        _observe(authority, receipt, [downgraded, deepcopy(downgraded)])

    changed = deepcopy(exact)
    changed["description"] = "changed"
    with pytest.raises(AgentCoreHardeningError, match="description"):
        _observe(authority, receipt, [exact, changed])


@pytest.mark.parametrize(
    ("status", "disposition"),
    [
        ("UPDATING", ObservationDisposition.PENDING),
        ("UPDATE_FAILED", ObservationDisposition.FAILED_RETAINED),
    ],
)
def test_observer_reports_only_stable_exact_async_or_failed_version(
    hardening_prefix: tuple,
    status: str,
    disposition: ObservationDisposition,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    receipt, _ = _dispatch(authority, precondition, [_update_ack(version="8")])
    runtime = _runtime(
        resolved,
        version="8",
        metadata={"requireMMDSV2": False},
        status=status,
    )

    observation, _ = _observe(
        authority,
        receipt,
        [deepcopy(runtime), deepcopy(runtime)],
    )

    assert observation.disposition is disposition
    assert observation.provider_status == status
    assert observation.projection()["runtimeVersion"] == "8"


def test_all_provider_paths_reject_raw_and_crossed_attested_clients(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    live = _runtime(resolved, metadata={"requireMMDSV2": False})
    raw = FakeAgentCore()
    raw.get_responses.extend([deepcopy(live), deepcopy(live)])
    with pytest.raises(AgentCoreHardeningError, match="attested"):
        AgentCoreHardeningInspectorV1(raw).inspect(authority)
    assert raw.calls == []

    crossed_raw = FakeAgentCore()
    crossed = AttestedAwsClientV2(
        crossed_raw,
        service="bedrock-agentcore-control",
        account="999999999999",
        region=REGION,
        capability="observer",
        _token=_CLIENT_TOKEN,
    )
    try:
        with pytest.raises(AgentCoreHardeningError, match="authority"):
            AgentCoreHardeningInspectorV1(crossed).inspect(authority)
    finally:
        crossed.close()
    assert crossed_raw.calls == []


def test_receipt_and_observation_capabilities_are_not_caller_constructible() -> None:
    with pytest.raises(AgentCoreHardeningError, match="not constructible"):
        AgentCoreHardeningDispatchReceiptV1()
    with pytest.raises(AgentCoreHardeningError, match="not constructible"):
        VerifiedAgentCoreHardeningReceiptV1()


class _ForgedAttestedAgentCoreClient(AttestedAwsClientV2):
    """Empty-init subclass that would bypass a nominal isinstance boundary."""

    __slots__ = ("calls", "responses")

    def __init__(self, responses: list[object]) -> None:
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "responses", list(responses))

    def require_scope(self, **kwargs: object) -> None:
        self.calls.append(("require_scope", dict(kwargs)))

    def invoke(self, operation_name: str, **kwargs: object) -> object:
        self.calls.append((operation_name, dict(kwargs)))
        if not self.responses:
            raise AssertionError("forged client was unexpectedly invoked")
        return self.responses.pop(0)


@pytest.mark.parametrize("boundary", ["inspect", "dispatch", "observe"])
def test_provider_paths_reject_attested_client_subclasses_before_use(
    hardening_prefix: tuple,
    boundary: str,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    live = _runtime(resolved, metadata={"requireMMDSV2": False})
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )

    if boundary == "inspect":
        forged = _ForgedAttestedAgentCoreClient(
            [deepcopy(live), deepcopy(live)]
        )
        action = lambda: AgentCoreHardeningInspectorV1(forged).inspect(
            authority
        )
    elif boundary == "dispatch":
        forged = _ForgedAttestedAgentCoreClient([_update_ack()])
        action = lambda: AgentCoreHardeningDispatcherV1(forged).dispatch(
            authority,
            precondition,
            _fresh_dispatch_authority(authority),
        )
    else:
        noop = _precondition_for(
            authority,
            resolved,
            metadata={"requireMMDSV2": True},
        )
        receipt, _ = _dispatch(authority, noop, [])
        hardened = _runtime(
            resolved,
            metadata={"requireMMDSV2": True},
        )
        forged = _ForgedAttestedAgentCoreClient(
            [deepcopy(hardened), deepcopy(hardened)]
        )
        action = lambda: AgentCoreHardeningObserverV1(forged).observe(
            authority,
            receipt,
        )

    before = (backend.attempted, backend.precondition_payload, backend.payload)
    with pytest.raises(AgentCoreHardeningError, match="attested"):
        action()
    assert forged.calls == []
    assert (backend.attempted, backend.precondition_payload, backend.payload) == before


@pytest.mark.parametrize("capability", ["preflight", "receipt-sink"])
def test_authority_mint_rejects_opaque_capability_subclasses(
    hardening_prefix: tuple,
    capability: str,
) -> None:
    authority, _, resolved, _, transaction = _verified_authority(
        hardening_prefix
    )
    real_preflight = hardening_prefix[0]
    real_sink = authority._binding()[4]

    class ForgedPreflight(VerifiedAgentCoreHardeningPreflightV1):
        __slots__ = ("delegate",)

        def __init__(self) -> None:
            self.delegate = real_preflight

        def _canonical(self):
            return self.delegate._canonical()

    class ForgedSink(AgentCoreHardeningReceiptSinkV1):
        __slots__ = ("delegate",)

        def __init__(self) -> None:
            self.delegate = real_sink

        def _authority(self):
            return self.delegate._authority()

        def _load(self):
            return self.delegate._load()

    preflight: object = real_preflight
    sink: object = real_sink
    if capability == "preflight":
        preflight = ForgedPreflight()
    else:
        sink = ForgedSink()

    with pytest.raises(AgentCoreHardeningError, match="verified|retained"):
        validate_agentcore_hardening_authority(
            resolved,
            preflight,
            transaction,
            sink,
        )


@pytest.mark.parametrize(
    "boundary", ["precondition-binding", "inspect", "dispatch", "observe"]
)
def test_agentcore_boundaries_reject_authority_subclasses_before_effect(
    hardening_prefix: tuple,
    boundary: str,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )

    class ForgedAuthority(AgentCoreHardeningAuthorityV1):
        __slots__ = ("delegate",)

        def __init__(self) -> None:
            self.delegate = authority

        def _binding(self):
            return self.delegate._binding()

        def digest(self) -> str:
            return self.delegate.digest()

    forged = ForgedAuthority()
    fake = FakeAgentCore()
    before = (backend.attempted, backend.precondition_payload, backend.payload)

    if boundary == "precondition-binding":
        action = lambda: precondition._binding(forged)
    elif boundary == "inspect":
        live = _runtime(resolved, metadata={"requireMMDSV2": False})
        fake.get_responses.extend([deepcopy(live), deepcopy(live)])
        client_context = _attested_agentcore_client(
            fake, capability="observer"
        )
        with client_context as client:
            with pytest.raises(AgentCoreHardeningError, match="authority"):
                AgentCoreHardeningInspectorV1(client).inspect(forged)
        assert fake.calls == []
        assert (
            backend.attempted,
            backend.precondition_payload,
            backend.payload,
        ) == before
        return
    elif boundary == "dispatch":
        fake.update_responses.append(_update_ack())
        client_context = _attested_agentcore_client(
            fake, capability="mutation"
        )
        with client_context as client:
            with pytest.raises(AgentCoreHardeningError, match="authority"):
                AgentCoreHardeningDispatcherV1(client).dispatch(
                    forged,
                    precondition,
                    _fresh_dispatch_authority(authority),
                )
        assert fake.calls == []
        assert (
            backend.attempted,
            backend.precondition_payload,
            backend.payload,
        ) == before
        return
    else:
        noop = _precondition_for(
            authority,
            resolved,
            metadata={"requireMMDSV2": True},
        )
        receipt, _ = _dispatch(authority, noop, [])
        hardened = _runtime(
            resolved,
            metadata={"requireMMDSV2": True},
        )
        fake.get_responses.extend([deepcopy(hardened), deepcopy(hardened)])
        client_context = _attested_agentcore_client(
            fake, capability="observer"
        )
        with client_context as client:
            with pytest.raises(AgentCoreHardeningError, match="authority"):
                AgentCoreHardeningObserverV1(client).observe(forged, receipt)
        assert fake.calls == []
        return

    with pytest.raises(AgentCoreHardeningError, match="authority"):
        action()
    assert fake.calls == []
    assert (backend.attempted, backend.precondition_payload, backend.payload) == before


def test_dispatch_rejects_precondition_subclass_before_durable_or_provider_effect(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    real = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )

    class ForgedPrecondition(AgentCoreHardeningPreconditionV1):
        __slots__ = ("delegate",)

        def __init__(self) -> None:
            self.delegate = real

        @property
        def mode(self) -> str:
            return self.delegate.mode

        def _binding(self, supplied_authority):
            return self.delegate._binding(supplied_authority)

        def to_bytes(self) -> bytes:
            return self.delegate.to_bytes()

        def digest(self) -> str:
            return self.delegate.digest()

    fake = FakeAgentCore()
    fake.update_responses.append(_update_ack())
    before = (backend.attempted, backend.precondition_payload, backend.payload)
    with _attested_agentcore_client(fake, capability="mutation") as client:
        with pytest.raises(AgentCoreHardeningError, match="authority"):
            AgentCoreHardeningDispatcherV1(client).dispatch(
                authority,
                ForgedPrecondition(),
                _fresh_dispatch_authority(authority),
            )
    assert fake.calls == []
    assert (backend.attempted, backend.precondition_payload, backend.payload) == before


def test_observer_rejects_verified_receipt_subclass_before_provider_read(
    hardening_prefix: tuple,
) -> None:
    authority, _, resolved, _, _ = _verified_authority(hardening_prefix)
    precondition = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": True},
    )
    receipt, _ = _dispatch(authority, precondition, [])

    class ForgedVerifiedReceipt(VerifiedAgentCoreHardeningReceiptV1):
        __slots__ = ("delegate",)

        def __init__(self) -> None:
            self.delegate = receipt

        def _binding(self, supplied_authority):
            return self.delegate._binding(supplied_authority)

    fake = FakeAgentCore()
    hardened = _runtime(
        resolved,
        metadata={"requireMMDSV2": True},
    )
    fake.get_responses.extend([deepcopy(hardened), deepcopy(hardened)])
    with _attested_agentcore_client(fake, capability="observer") as client:
        with pytest.raises(AgentCoreHardeningError, match="authority"):
            AgentCoreHardeningObserverV1(client).observe(
                authority,
                ForgedVerifiedReceipt(),
            )
    assert fake.calls == []


def test_deserialized_precondition_cannot_authorize_standalone_dispatch(
    hardening_prefix: tuple,
) -> None:
    authority, backend, resolved, _, _ = _verified_authority(hardening_prefix)
    inspected = _precondition_for(
        authority,
        resolved,
        metadata={"requireMMDSV2": False},
    )
    deserialized = AgentCoreHardeningPreconditionV1.from_bytes(
        inspected.to_bytes()
    )
    fake = FakeAgentCore()
    fake.update_responses.append(_update_ack())

    with _attested_agentcore_client(fake, capability="mutation") as client:
        with pytest.raises(AgentCoreHardeningError, match="inspected"):
            AgentCoreHardeningDispatcherV1(client).dispatch(
                authority,
                deserialized,
                _fresh_dispatch_authority(authority),
            )

    assert backend.precondition_payload is None
    assert backend.attempted is False
    assert backend.payload is None
    assert fake.calls == []
